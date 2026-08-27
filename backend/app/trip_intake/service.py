from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
from uuid import uuid4

from pydantic import ValidationError

from app.itineraries.errors import ResourceNotFound
from app.itineraries.hash_service import sha256_canonical
from app.trip_intake.extraction import DeterministicTripIntakeExtractor, TripIntakeExtractor
from app.trip_intake.models import (
    ConfirmedField,
    DateRangeExpression,
    EvidenceSpan,
    IntakeReadiness,
    IntakeSource,
    IntakeSourceType,
    IntakeStatus,
    LocationEntityType,
    LocationExtraction,
    LocationMention,
    LocationRole,
    LocationStatus,
    PartialDate,
    PartySizeExtraction,
    PreferenceStatus,
    QuantifiedValue,
    QuantityDerivation,
    QuantityQuantifier,
    TemporalExtraction,
    TripIntakeExtraction,
    TripIntakeRevision,
    with_intake_content_hash,
)
from app.trip_intake.repository import TripIntakeRepository


class TripIntakeApplicationService:
    def __init__(
        self,
        repository: TripIntakeRepository,
        extractor: TripIntakeExtractor | None = None,
    ):
        self.repository = repository
        self.extractor = extractor or DeterministicTripIntakeExtractor()

    async def create(
        self,
        *,
        room_id: str,
        source_type: IntakeSourceType,
        source_texts: list[str],
        actor_user_id: str,
        source_metadata: list[dict[str, object]] | None = None,
    ) -> TripIntakeRevision:
        if not source_texts or any(not text.strip() for text in source_texts):
            raise ValueError("at least one non-blank intake source is required")
        if len(source_texts) > 6:
            raise ValueError("at most six intake sources are supported")
        if source_metadata is not None and len(source_metadata) != len(source_texts):
            raise ValueError("source metadata must align with source texts")
        intake_id = str(uuid4())
        sources = [
            IntakeSource(
                source_id=f"{intake_id}:source:{index + 1}",
                source_type=source_type,
                text=text,
                text_sha256=sha256(text.encode("utf-8")).hexdigest(),
                metadata=(source_metadata[index] if source_metadata is not None else {}),
            )
            for index, text in enumerate(source_texts)
        ]
        outcome = await self.extractor.extract(sources)
        raw_text = "\n\n".join(source_texts)
        candidate = TripIntakeRevision(
            intake_id=intake_id,
            room_id=room_id,
            revision=1,
            content_hash="0" * 64,
            source_type=source_type,
            raw_text=raw_text,
            raw_text_sha256=sha256(raw_text.encode("utf-8")).hexdigest(),
            sources=sources,
            parser_binding=outcome.parser_binding,
            extraction=outcome.extraction,
            status=outcome.status,
            created_by=actor_user_id,
        )
        return await self.repository.save_initial(with_intake_content_hash(candidate))

    async def patch(
        self,
        *,
        intake_id: str,
        revision: int,
        extraction: TripIntakeExtraction,
        actor_user_id: str,
        idempotency_key: str,
    ) -> tuple[TripIntakeRevision, bool]:
        base = await self.repository.get_revision(intake_id, revision)
        if base is None:
            raise ResourceNotFound("trip intake revision does not exist")
        now = datetime.now(timezone.utc)
        candidate = base.model_copy(
            update={
                "revision": revision + 1,
                "parent_revision": revision,
                "extraction": extraction.model_copy(
                    update={"readiness": IntakeReadiness.NEEDS_CONFIRMATION}
                ),
                "confirmed_fields": set(),
                "status": IntakeStatus.NEEDS_CONFIRMATION,
                "content_hash": "0" * 64,
                "created_by": actor_user_id,
                "created_at": now,
                "confirmed_by": None,
                "confirmed_at": None,
            }
        )
        candidate = TripIntakeRevision.model_validate(candidate.model_dump())
        candidate = with_intake_content_hash(candidate)
        request_hash = sha256_canonical(
            {
                "operation": "PATCH",
                "intake_id": intake_id,
                "revision": revision,
                "actor_user_id": actor_user_id,
                "extraction": extraction.model_dump(mode="json"),
            }
        )
        return await self.repository.save_command_revision(
            candidate,
            expected_revision=revision,
            operation="PATCH",
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

    async def patch_confirmed_values(
        self,
        *,
        intake_id: str,
        revision: int,
        city: str,
        start_date: date,
        end_date: date,
        party_size: int,
        actor_user_id: str,
        idempotency_key: str,
    ) -> tuple[TripIntakeRevision, bool]:
        if not city.strip() or party_size <= 0 or end_date < start_date:
            raise ValueError("city, ordered dates, and positive party size are required")
        base = await self.repository.get_revision(intake_id, revision)
        if base is None:
            raise ResourceNotFound("trip intake revision does not exist")
        correction_text = (
            f"确认目的城市：{city.strip()}；确认日期：{start_date.isoformat()}至{end_date.isoformat()}；"
            f"确认人数：{party_size}人"
        )
        source_id = f"{intake_id}:correction:{revision + 1}"
        source = IntakeSource(
            source_id=source_id,
            source_type=IntakeSourceType.MANUAL_TEXT,
            text=correction_text,
            text_sha256=sha256(correction_text.encode("utf-8")).hexdigest(),
            metadata={"role": "USER_CONFIRMATION"},
        )
        if len(base.sources) >= 6:
            raise ValueError("intake already contains six sources; create a new intake to correct it")

        def span(quote: str) -> EvidenceSpan:
            start = correction_text.index(quote)
            return EvidenceSpan(
                source_id=source_id,
                start=start,
                end=start + len(quote),
                quote=quote,
            )

        city_value = city.strip()
        city_mention = LocationMention(
            mention_id=f"confirmed-city-{revision + 1}",
            raw_text=city_value,
            normalized_name=(city_value if city_value.endswith("市") else f"{city_value}市"),
            country_code="CN",
            entity_type=LocationEntityType.CITY,
            role=LocationRole.PRIMARY_DESTINATION,
            confidence=1,
            evidence=[span(city_value)],
        )
        prior_non_primary = [
            item
            for item in base.extraction.locations.mentions
            if item.role != LocationRole.PRIMARY_DESTINATION
        ]
        date_quote = f"{start_date.isoformat()}至{end_date.isoformat()}"
        count_quote = f"{party_size}人"
        extraction = base.extraction.model_copy(
            update={
                "locations": LocationExtraction(
                    mentions=[*prior_non_primary, city_mention],
                    primary_mention_id=city_mention.mention_id,
                    status=LocationStatus.EXACT,
                ),
                "party_size": PartySizeExtraction(
                    total=QuantifiedValue(
                        min=party_size,
                        max=party_size,
                        quantifier=QuantityQuantifier.EXACT,
                        derivation=QuantityDerivation.EXPLICIT_COUNT,
                        evidence=[span(count_quote)],
                    ),
                    composition=base.extraction.party_size.composition,
                ),
                "temporal": TemporalExtraction(
                    days=base.extraction.temporal.days,
                    nights=base.extraction.temporal.nights,
                    date_range=DateRangeExpression(
                        raw_text=date_quote,
                        start=PartialDate(
                            year=start_date.year,
                            month=start_date.month,
                            day=start_date.day,
                        ),
                        end=PartialDate(
                            year=end_date.year,
                            month=end_date.month,
                            day=end_date.day,
                        ),
                        evidence=[span(date_quote)],
                    ),
                    arrival=base.extraction.temporal.arrival,
                    departure=base.extraction.temporal.departure,
                ),
                "issues": [
                    issue
                    for issue in base.extraction.issues
                    if issue.field_path
                    not in {"locations.primary_city", "party_size.total", "temporal.date_range"}
                ],
                "readiness": IntakeReadiness.NEEDS_CONFIRMATION,
            }
        )
        raw_text = f"{base.raw_text}\n\n{correction_text}"
        now = datetime.now(timezone.utc)
        candidate = base.model_copy(
            update={
                "revision": revision + 1,
                "parent_revision": revision,
                "raw_text": raw_text,
                "raw_text_sha256": sha256(raw_text.encode("utf-8")).hexdigest(),
                "sources": [*base.sources, source],
                "extraction": extraction,
                "confirmed_fields": set(),
                "status": IntakeStatus.NEEDS_CONFIRMATION,
                "content_hash": "0" * 64,
                "created_by": actor_user_id,
                "created_at": now,
                "confirmed_by": None,
                "confirmed_at": None,
            }
        )
        candidate = TripIntakeRevision.model_validate(candidate.model_dump())
        candidate = with_intake_content_hash(candidate)
        request_hash = sha256_canonical(
            {
                "operation": "PATCH_CONFIRMED_VALUES",
                "intake_id": intake_id,
                "revision": revision,
                "actor_user_id": actor_user_id,
                "city": city_value,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "party_size": party_size,
            }
        )
        return await self.repository.save_command_revision(
            candidate,
            expected_revision=revision,
            operation="PATCH",
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

    async def confirm(
        self,
        *,
        intake_id: str,
        revision: int,
        actor_user_id: str,
        idempotency_key: str,
    ) -> tuple[TripIntakeRevision, bool]:
        base = await self.repository.get_revision(intake_id, revision)
        if base is None:
            raise ResourceNotFound("trip intake revision does not exist")
        if base.status == IntakeStatus.READY:
            raise ValueError("confirmed intake revisions are immutable")
        try:
            ready_extraction = TripIntakeExtraction.model_validate(
                base.extraction.model_copy(update={"readiness": IntakeReadiness.READY}).model_dump()
            )
        except ValidationError as exc:
            raise ValueError(
                "行程草稿尚未满足确认条件，请先补全并保存目的城市、完整日期和正整数人数"
            ) from exc
        now = datetime.now(timezone.utc)
        confirmed_fields = {
            ConfirmedField.PRIMARY_CITY,
            ConfirmedField.DATE_RANGE,
            ConfirmedField.PARTY_SIZE,
        }
        if base.extraction.preferences.status != PreferenceStatus.UNSPECIFIED:
            confirmed_fields.add(ConfirmedField.PREFERENCES)
        candidate = base.model_copy(
            update={
                "revision": revision + 1,
                "parent_revision": revision,
                "extraction": ready_extraction,
                "confirmed_fields": confirmed_fields,
                "status": IntakeStatus.READY,
                "content_hash": "0" * 64,
                "created_by": actor_user_id,
                "created_at": now,
                "confirmed_by": actor_user_id,
                "confirmed_at": now,
            }
        )
        candidate = TripIntakeRevision.model_validate(candidate.model_dump())
        candidate = with_intake_content_hash(candidate)
        request_hash = sha256_canonical(
            {
                "operation": "CONFIRM",
                "intake_id": intake_id,
                "revision": revision,
                "actor_user_id": actor_user_id,
                "content_hash": base.content_hash,
            }
        )
        return await self.repository.save_command_revision(
            candidate,
            expected_revision=revision,
            operation="CONFIRM",
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
