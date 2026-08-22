from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.importing.entity_resolver import EntityResolver
from app.importing.errors import DraftAmbiguousError, InvalidImportStateError
from app.importing.models import (
    ImportApplyResult,
    ImportSourceType,
    ImportStatus,
    ItineraryImport,
    ResolvedPlaceReceipt,
    ResolvedStop,
    resolution_set_is_ready,
    selected_place_candidate,
)
from app.importing.parser import ItineraryTextParser
from app.importing.repositories import ImportRepository
from app.itineraries.errors import InvalidEditCommandError, ResourceNotFound
from app.itineraries.hash_service import sha256_canonical, with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
)
from app.itineraries.repositories import ItineraryRepository
from app.operations.models import CreationCommandResponse, CreationOperation
from app.operations.repositories import CreationCommandRepository


def _clock(raw: str, *, afternoon_hint: bool = False) -> str | None:
    match = re.search(r"(\d{1,2})(?::(\d{2})|点(半|\d{1,2}分)?)?", raw)
    if not match:
        return None
    hour = int(match.group(1))
    minute_token = match.group(2) or match.group(3)
    minute = 30 if minute_token == "半" else int(str(minute_token or "0").removesuffix("分"))
    if afternoon_hint and hour < 12:
        hour += 12
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_time_range(raw: str | None) -> tuple[str | None, str | None, int | None]:
    if not raw:
        return None, None, None
    parts = re.split(r"[-—~～至]", raw, maxsplit=1)
    start_hint = any(term in parts[0] for term in ("下午", "晚上"))
    start = _clock(parts[0], afternoon_hint=start_hint)
    end = None
    if len(parts) == 2:
        end_hint = any(term in parts[1] for term in ("下午", "晚上")) or start_hint
        end = _clock(parts[1], afternoon_hint=end_hint)
    duration = None
    if start and end:
        start_time, end_time = time.fromisoformat(start), time.fromisoformat(end)
        start_minutes = start_time.hour * 60 + start_time.minute
        end_minutes = end_time.hour * 60 + end_time.minute
        if end_minutes < start_minutes:
            end_minutes += 24 * 60
        duration = end_minutes - start_minutes
    return start, end, duration


def _normalized_city(value: str) -> str:
    normalized = re.sub(r"\s+", "", value).casefold()
    for suffix in ("特别行政区", "自治州", "地区", "盟", "市"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def _place_record(candidate: Any, receipt: ResolvedPlaceReceipt) -> dict[str, Any]:
    """Create the authoritative room-place projection from one provider receipt."""

    return {
        "place_id": receipt.canonical_place_id,
        "provider_place_id": receipt.provider_place_id,
        "name": receipt.name,
        "city": receipt.city,
        "district": receipt.district,
        "address": receipt.address or "",
        "category": receipt.category or candidate.category or "unknown",
        "coords": {"lng": receipt.longitude, "lat": receipt.latitude},
        "provider": receipt.provider,
        "source": "amap_poi" if receipt.provider == "amap" else receipt.provider,
        "execution_mode": receipt.execution_mode.value,
        "retrieval_provider": receipt.provider,
        "retrieval_request_hash": receipt.request_hash,
        "retrieval_response_hash": receipt.response_hash,
        "retrieval_observed_at": receipt.observed_at.isoformat(),
        "source_url": receipt.source_url,
        "opening_hours": candidate.opening_hours,
        "phone": candidate.phone,
        "amap_rating": candidate.amap_rating,
        "amap_price": candidate.amap_price,
        "resolved_place_receipt": receipt.model_dump(mode="json"),
    }


class ImportApplicationService:
    def __init__(
        self,
        *,
        import_repository: ImportRepository,
        itinerary_repository: ItineraryRepository,
        entity_resolver: EntityResolver,
        parser: ItineraryTextParser | None = None,
    ):
        self.import_repository = import_repository
        self.itinerary_repository = itinerary_repository
        self.entity_resolver = entity_resolver
        self.parser = parser or ItineraryTextParser()

    async def create_import(
        self,
        *,
        workspace_id: str,
        source_type: ImportSourceType,
        raw_text: str,
        actor_user_id: str,
    ) -> ItineraryImport:
        workspace = await self.itinerary_repository.get_workspace(workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        import_id = str(uuid4())
        draft = self.parser.parse(raw_text, import_id=import_id)
        status = ImportStatus.FAILED if not draft.raw_stops else ImportStatus.PARSED
        itinerary_import = ItineraryImport(
            import_id=import_id,
            workspace_id=workspace_id,
            source_type=source_type,
            raw_text=raw_text,
            parse_version=self.parser.version,
            status=status,
            raw_stops=draft.raw_stops,
            member_summary=draft.member_summary,
            parse_errors=draft.errors,
            created_by=actor_user_id,
        )
        await self.import_repository.create_import(itinerary_import)
        if not draft.raw_stops:
            return itinerary_import
        resolutions = await self.entity_resolver.resolve_all(draft.raw_stops, city=workspace.city)
        return await self.import_repository.save_resolutions(import_id, resolutions)

    async def create_import_idempotent(
        self,
        *,
        workspace_id: str,
        source_type: ImportSourceType,
        raw_text: str,
        actor_user_id: str,
        idempotency_key: str,
        command_repository: CreationCommandRepository,
    ) -> tuple[ItineraryImport, bool]:
        workspace = await self.itinerary_repository.get_workspace(workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        basis = {"current_import_id": workspace.current_import_id}
        request_hash = sha256_canonical(
            {
                "schema_version": 1,
                "operation": CreationOperation.CREATE_IMPORT.value,
                "workspace_id": workspace_id,
                "target_id": workspace_id,
                "actor_user_id": actor_user_id,
                "body": {"source_type": source_type.value, "raw_text": raw_text},
            }
        )
        claim = await command_repository.claim(
            workspace_id=workspace_id,
            operation=CreationOperation.CREATE_IMPORT,
            target_id=workspace_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            basis=basis,
        )
        if claim.replay is not None:
            return ItineraryImport.model_validate(claim.replay.body), True
        try:
            import_id = str(uuid4())
            draft = self.parser.parse(raw_text, import_id=import_id)
            resolutions = (
                await self.entity_resolver.resolve_all(draft.raw_stops, city=workspace.city) if draft.raw_stops else []
            )
            ready = resolution_set_is_ready(draft.raw_stops, resolutions)
            status = (
                ImportStatus.FAILED
                if not draft.raw_stops
                else ImportStatus.READY
                if ready
                else ImportStatus.NEEDS_RESOLUTION
            )
            now = datetime.now(timezone.utc)
            itinerary_import = ItineraryImport(
                import_id=import_id,
                workspace_id=workspace_id,
                source_type=source_type,
                raw_text=raw_text,
                parse_version=self.parser.version,
                status=status,
                raw_stops=draft.raw_stops,
                resolutions=resolutions,
                member_summary=draft.member_summary,
                parse_errors=draft.errors,
                state_version=2 if draft.raw_stops else 1,
                created_by=actor_user_id,
                created_at=now,
                updated_at=now,
            )

            async def finalize(conn, stored_basis):
                stored = await self.import_repository.create_import_bundle(
                    itinerary_import,
                    basis=stored_basis,
                    conn=conn,
                )
                return CreationCommandResponse(
                    status_code=201,
                    body=stored.model_dump(mode="json"),
                    headers={
                        "ETag": f'"{stored.state_version}"',
                        "Cache-Control": "no-store",
                    },
                )

            response = await command_repository.finalize(claim, finalize)
            return ItineraryImport.model_validate(response.body), response.idempotent_replay
        except Exception:
            await command_repository.abandon(claim)
            raise

    async def confirm_resolution(
        self,
        *,
        import_id: str,
        raw_stop_id: str,
        place_id: str,
        actor_user_id: str,
        expected_state_version: int | None = None,
    ) -> ItineraryImport:
        return await self.import_repository.confirm_resolution(
            import_id,
            raw_stop_id,
            place_id,
            actor_user_id,
            expected_state_version,
        )

    async def confirm_resolutions(
        self,
        *,
        import_id: str,
        confirmations: dict[str, str],
        actor_user_id: str,
        expected_state_version: int | None = None,
    ) -> ItineraryImport:
        return await self.import_repository.confirm_resolutions(
            import_id,
            confirmations,
            actor_user_id,
            expected_state_version,
        )

    async def retry_resolution(
        self,
        *,
        import_id: str,
        raw_stop_id: str,
        query: str,
        expected_state_version: int | None = None,
    ) -> ItineraryImport:
        itinerary_import = await self.import_repository.get_import(import_id)
        if itinerary_import is None:
            raise ResourceNotFound("import does not exist")
        if itinerary_import.status in {ImportStatus.APPLIED, ImportStatus.FAILED}:
            raise InvalidImportStateError(
                "terminal imports cannot change entity candidates",
                context={"import_status": itinerary_import.status.value},
            )
        raw_stop = next((item for item in itinerary_import.raw_stops if item.raw_stop_id == raw_stop_id), None)
        if raw_stop is None:
            raise ResourceNotFound("raw stop does not belong to import")
        normalized_query = query.strip()
        if not normalized_query or len(normalized_query) > 160:
            raise InvalidEditCommandError("candidate search query must contain 1 to 160 characters")
        workspace = await self.itinerary_repository.get_workspace(itinerary_import.workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        retried = await self.entity_resolver.resolve(
            raw_stop.model_copy(update={"raw_name": normalized_query}),
            city=workspace.city,
        )
        return await self.import_repository.save_resolution(
            import_id,
            retried,
            expected_state_version=(
                expected_state_version if expected_state_version is not None else itinerary_import.state_version
            ),
        )

    async def apply_import(
        self,
        import_id: str,
        *,
        actor_user_id: str,
        expected_state_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> ImportApplyResult:
        itinerary_import = await self.import_repository.get_import(import_id)
        if itinerary_import is None:
            raise ResourceNotFound("import does not exist")
        if itinerary_import.status == ImportStatus.FAILED:
            raise InvalidImportStateError(
                "import is not in an applicable draft state",
                context={"import_status": itinerary_import.status.value},
            )
        if itinerary_import.status != ImportStatus.READY and itinerary_import.status != ImportStatus.APPLIED:
            raise DraftAmbiguousError("all ambiguous or missing places must be resolved before apply")
        workspace = await self.itinerary_repository.get_workspace(itinerary_import.workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        resolution_by_id: dict[str, ResolvedStop] = {
            resolution.raw_stop_id: resolution for resolution in itinerary_import.resolutions
        }
        day_count = (workspace.trip_date_range.end - workspace.trip_date_range.start).days + 1
        stops_by_day: dict[int, list[ItineraryStop]] = {index: [] for index in range(day_count)}
        map_stop_projections: dict[str, dict[str, Any]] = {}
        place_records: dict[str, dict[str, Any]] = {}
        place_records_by_stop: dict[str, dict[str, Any]] = {}
        resolved_place_receipts: list[ResolvedPlaceReceipt] = []
        receipts_by_stop: dict[str, ResolvedPlaceReceipt] = {}
        for raw_stop in itinerary_import.raw_stops:
            day_index = raw_stop.day_index if raw_stop.day_index is not None else 0
            if day_index not in stops_by_day:
                raise InvalidEditCommandError(
                    "parsed stop falls outside workspace date range",
                    context={"raw_stop_id": raw_stop.raw_stop_id, "day_index": day_index},
                )
            resolution = resolution_by_id.get(raw_stop.raw_stop_id)
            if resolution is None or not resolution.canonical_place_id:
                raise DraftAmbiguousError("all ambiguous or missing places must be resolved before apply")
            selected = selected_place_candidate(resolution)
            receipt = selected.resolved_place_receipt if selected is not None else None
            if receipt is None:
                raise InvalidEditCommandError(
                    "resolved place lacks authoritative coordinates or provider receipt",
                    context={
                        "reason": "RESOLVED_PLACE_RECEIPT_INCOMPLETE",
                        "raw_stop_id": raw_stop.raw_stop_id,
                        "place_id": resolution.canonical_place_id,
                    },
                )
            if receipt.canonical_place_id != resolution.canonical_place_id:
                raise InvalidEditCommandError(
                    "resolved place receipt does not match the selected canonical place",
                    context={
                        "reason": "RESOLVED_PLACE_RECEIPT_ID_MISMATCH",
                        "raw_stop_id": raw_stop.raw_stop_id,
                    },
                )
            if _normalized_city(receipt.city) != _normalized_city(workspace.city):
                raise InvalidEditCommandError(
                    "resolved place belongs to a different city",
                    context={
                        "reason": "RESOLVED_PLACE_CITY_MISMATCH",
                        "raw_stop_id": raw_stop.raw_stop_id,
                        "workspace_city": workspace.city,
                        "resolved_city": receipt.city,
                    },
                )
            resolved_place_receipts.append(receipt)
            receipts_by_stop[raw_stop.raw_stop_id] = receipt
            materialized_place = _place_record(selected, receipt)
            place_records[receipt.canonical_place_id] = materialized_place
            place_records_by_stop[raw_stop.raw_stop_id] = materialized_place
            map_stop_projections[raw_stop.raw_stop_id] = {
                "place_id": receipt.canonical_place_id,
                "coords": {"lng": receipt.longitude, "lat": receipt.latitude},
                "coordinate_role": "CANONICAL_PROVIDER_POI",
                "provenance": f"{receipt.provider}:{receipt.execution_mode.value}",
                "receipt_hash": sha256_canonical(receipt.model_dump(mode="json")),
                "canonical_name": receipt.name,
            }
            start_time, end_time, duration = parse_time_range(raw_stop.raw_time)
            stops_by_day[day_index].append(
                ItineraryStop(
                    stop_id=raw_stop.raw_stop_id,
                    place_id=resolution.canonical_place_id,
                    day_index=day_index,
                    order_index=len(stops_by_day[day_index]),
                    start_time=start_time,
                    end_time=end_time,
                    visit_duration_minutes=duration,
                    raw_name=raw_stop.raw_name,
                    source_raw_stop_id=raw_stop.raw_stop_id,
                    resolution_status=resolution.resolution_status,
                    commitment_kind=raw_stop.commitment_kind,
                    fixed_commitment=raw_stop.fixed_commitment,
                    locked=raw_stop.fixed_commitment,
                    category=selected.category if selected and selected.category else "attraction",
                )
            )
        days = [
            ItineraryDay(
                day_index=index,
                date=workspace.trip_date_range.start + timedelta(days=index),
                stops=stops_by_day[index],
            )
            for index in range(day_count)
        ]
        revision = with_content_hash(
            ItineraryRevisionContent(
                itinerary_id=str(uuid4()),
                workspace_id=workspace.workspace_id,
                revision=1,
                source_type=RevisionSource.IMPORT,
                city=workspace.city,
                date_range=workspace.trip_date_range,
                days=days,
                locked_commitments=[stop.stop_id for day in days for stop in day.stops if stop.fixed_commitment],
                change_summary={
                    "import_id": import_id,
                    "operation": "APPLY_IMPORT",
                    "map_stop_projections": map_stop_projections,
                    "resolved_place_receipts": [
                        receipt.model_dump(mode="json")
                        for receipt in resolved_place_receipts
                    ],
                    "materialization_hash": sha256_canonical({
                        "map_stop_projections": map_stop_projections,
                        "place_records": place_records,
                        "place_records_by_stop": place_records_by_stop,
                        "receipts_by_stop": {
                            stop_id: receipt.model_dump(mode="json")
                            for stop_id, receipt in receipts_by_stop.items()
                        },
                    }),
                },
                created_by=actor_user_id,
                created_at=datetime.now(timezone.utc),
            )
        )
        return await self.import_repository.apply_ready_import(
            import_id,
            revision,
            actor_user_id=actor_user_id,
            expected_state_version=(
                expected_state_version if expected_state_version is not None else itinerary_import.state_version
            ),
            idempotency_key=idempotency_key or f"legacy-apply:{import_id}",
            place_records=place_records,
            place_records_by_stop=place_records_by_stop,
            resolved_place_receipts=resolved_place_receipts,
            resolved_place_receipts_by_stop=receipts_by_stop,
        )
