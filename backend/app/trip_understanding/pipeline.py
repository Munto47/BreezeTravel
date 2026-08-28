from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from typing import Protocol
from uuid import uuid4

from app.trip_understanding.errors import (
    InferenceProviderUnavailableError,
    PlaceProviderUnavailableError,
)
from app.trip_understanding.models import (
    ActivityCardView,
    ActivityRole,
    AssumptionChipView,
    CompiledActivity,
    DestinationBasis,
    InferenceProposal,
    MapReadinessView,
    PipelineOutput,
    PlaceResolutionOutcome,
    ResolutionStatus,
    ResolvedActivity,
    ResolvedPlace,
    SourceClaimRecord,
    StaySuggestionView,
    TripDayView,
    UserFacingTripResult,
)


URL_RE = re.compile(r"https?://", re.IGNORECASE)
SENTENCE_MARKERS = set("。！？；\n")


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class StructuredInferenceProvider(Protocol):
    async def propose(self, source_text: str) -> InferenceProposal: ...


class PlaceResolver(Protocol):
    async def resolve(
        self,
        *,
        city: str,
        atomic_place_name: str,
        category_hint: str | None = None,
    ) -> PlaceResolutionOutcome | ResolvedPlace | None: ...


class ResilientStructuredInferenceProvider:
    """Use one explicit local fallback only for a typed provider outage.

    Programming/schema errors still fail the job. The fallback binding records
    the primary attempt and never changes to the frozen DeepSeek baseline.
    """

    def __init__(
        self,
        primary: StructuredInferenceProvider,
        fallback: StructuredInferenceProvider,
    ) -> None:
        self.primary = primary
        self.fallback = fallback

    async def propose(self, source_text: str) -> InferenceProposal:
        try:
            return await self.primary.propose(source_text)
        except InferenceProviderUnavailableError as exc:
            proposal = await self.fallback.propose(source_text)
            fallback_binding = dict(proposal.binding)
            fallback_binding.update(
                {
                    "fallback_used": True,
                    "fallback_reason": exc.category,
                    "primary_provider_binding": exc.provider_binding,
                    "primary_external_call_count": exc.external_call_count,
                    "fallback_policy": "LOCAL_DETERMINISTIC_PARTIAL_RESULT",
                }
            )
            return proposal.model_copy(update={"binding": fallback_binding})


def is_atomic_planned_place(mention) -> bool:
    if mention.role != ActivityRole.PLANNED or mention.day_index is None:
        return False
    candidate = (mention.atomic_place_name or "").strip()
    if not candidate or len(candidate) > 40 or URL_RE.search(candidate):
        return False
    if any(marker in candidate for marker in SENTENCE_MARKERS):
        return False
    if any(word in candidate for word in ("预约", "说明", "网址", "链接")):
        return False
    return True


def is_user_facing_planned_place(mention) -> bool:
    if mention.role != ActivityRole.PLANNED or mention.day_index is None:
        return False
    candidate = (mention.atomic_place_name or mention.raw_text or "").strip()
    if not candidate or len(candidate) > 40 or URL_RE.search(candidate):
        return False
    if any(marker in candidate for marker in SENTENCE_MARKERS):
        return False
    return not any(word in candidate for word in ("预约", "说明", "网址", "链接"))


class EvidenceCompiler:
    def compile(
        self,
        source_text: str,
        proposal: InferenceProposal,
    ) -> tuple[list[CompiledActivity], list[SourceClaimRecord], dict[str, object]]:
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        if proposal.source_hash != source_hash:
            raise ValueError("proposal source binding mismatch")
        compiled: list[CompiledActivity] = []
        claims: list[SourceClaimRecord] = []
        mention_ids: set[str] = set()
        valid_spans = 0
        for mention in proposal.mentions:
            if mention.mention_id in mention_ids:
                raise ValueError("duplicate proposal mention ID")
            mention_ids.add(mention.mention_id)
            if source_text[mention.span_start : mention.span_end] != mention.raw_text:
                raise ValueError("proposal evidence span does not match source text")
            valid_spans += 1
            activity_id = str(uuid4())
            compiled_activity = CompiledActivity(
                activity_id=activity_id,
                public_activity_token=secrets.token_urlsafe(24),
                mention=mention,
                eligible_for_place_search=is_atomic_planned_place(mention),
            )
            compiled.append(compiled_activity)
            claim_type = "EXCLUSION" if mention.role == ActivityRole.EXCLUDED else "PLACE_MENTION"
            claims.append(
                SourceClaimRecord(
                    claim_id=str(uuid4()),
                    activity_id=activity_id,
                    claim_type=claim_type,
                    span_start=mention.span_start,
                    span_end=mention.span_end,
                    quote=mention.raw_text,
                )
            )
        return compiled, claims, {
            "compiler": "trip-understanding-evidence-compiler-v1",
            "unicode_basis": "CODE_POINT_HALF_OPEN",
            "mention_count": len(compiled),
            "valid_span_count": valid_spans,
            "eligible_place_count": sum(item.eligible_for_place_search for item in compiled),
        }


class PublicResultProjector:
    def project(
        self,
        destination_name: str,
        destination_basis: DestinationBasis,
        activities: list[ResolvedActivity],
    ) -> UserFacingTripResult:
        planned = [
            activity
            for activity in activities
            if is_user_facing_planned_place(activity.compiled.mention)
        ]
        day_count = max(
            (activity.compiled.mention.day_index or 1 for activity in planned),
            default=1,
        )
        day_views: list[TripDayView] = []
        for day_index in range(1, day_count + 1):
            cards = []
            for item in sorted(
                (
                    activity
                    for activity in activities
                    if is_user_facing_planned_place(activity.compiled.mention)
                    and activity.compiled.mention.day_index == day_index
                ),
                key=lambda activity: activity.compiled.mention.sequence_index,
            ):
                mention = item.compiled.mention
                place = item.place
                cards.append(
                    ActivityCardView(
                        activity_token=item.compiled.public_activity_token,
                        name=place.name if place else (mention.atomic_place_name or "地点待确认"),
                        category=place.category if place else (mention.category_hint or "地点"),
                        area_or_address=place.area_or_address if place else "地点待确认",
                        time_hint=mention.time_hint,
                        status="READY" if place else "NEEDS_CONFIRMATION",
                        available_actions=["VIEW_DETAILS", "REPLACE", "DELETE", "MOVE"],
                    )
                )
            day_views.append(TripDayView(label=f"Day {day_index}", activities=cards))
        resolved_count = sum(item.place is not None for item in planned)
        if planned and resolved_count == len(planned):
            result_status = "READY"
        elif resolved_count:
            result_status = "PARTIAL_RESULT"
        else:
            result_status = "BASIC_ONLY"
        return UserFacingTripResult(
            status=result_status,
            assumptions=[
                AssumptionChipView(
                    key="destination",
                    label="目的地",
                    value=(
                        destination_name
                        if destination_basis == DestinationBasis.EXPLICIT
                        else f"暂按 {destination_name}"
                    ),
                    editable=True,
                ),
                AssumptionChipView(
                    key="calendar",
                    label="日期",
                    value=(
                        f"未填写，按 Day 1～Day {day_count} 展示"
                        if day_count > 1
                        else "未填写，按 Day 1 展示"
                    ),
                    editable=True,
                ),
                AssumptionChipView(
                    key="party_size",
                    label="同行人数",
                    value="暂按 2 人",
                    editable=True,
                ),
            ],
            days=day_views,
            map=MapReadinessView(
                status="UNAVAILABLE",
                message="路线地图暂不可用，不影响查看和编辑卡片",
            ),
            stay=StaySuggestionView(
                status="UNAVAILABLE",
                message="住宿待选择",
            ),
            available_actions=["EDIT_ASSUMPTIONS", "EDIT_CARDS"],
        )


class TripUnderstandingPipeline:
    def __init__(
        self,
        inference_provider: StructuredInferenceProvider,
        place_resolver: PlaceResolver,
        compiler: EvidenceCompiler | None = None,
        projector: PublicResultProjector | None = None,
        max_executable_activities: int = 80,
        max_place_concurrency: int = 4,
    ) -> None:
        if max_place_concurrency < 1 or max_place_concurrency > 8:
            raise ValueError("place concurrency must be between 1 and 8")
        self.inference_provider = inference_provider
        self.place_resolver = place_resolver
        self.compiler = compiler or EvidenceCompiler()
        self.projector = projector or PublicResultProjector()
        self.max_executable_activities = max_executable_activities
        self.max_place_concurrency = max_place_concurrency

    async def _resolve_place(
        self,
        item: CompiledActivity,
        *,
        city: str,
        semaphore: asyncio.Semaphore,
    ) -> tuple[PlaceResolutionOutcome, bool]:
        try:
            async with semaphore:
                raw_outcome = await self.place_resolver.resolve(
                    city=city,
                    atomic_place_name=item.mention.atomic_place_name or "",
                    category_hint=item.mention.category_hint,
                )
        except PlaceProviderUnavailableError as exc:
            return (
                PlaceResolutionOutcome(
                    receipt={
                        "status": "UNAVAILABLE",
                        "failure_category": exc.category,
                        "provider_binding": exc.provider_binding,
                        "external_calls": exc.external_call_count,
                    }
                ),
                True,
            )
        if isinstance(raw_outcome, PlaceResolutionOutcome):
            return raw_outcome, False
        if isinstance(raw_outcome, ResolvedPlace):
            return (
                PlaceResolutionOutcome(
                    place=raw_outcome,
                    receipt={
                        "status": "AUTO_MATCHED",
                        **raw_outcome.provider_binding,
                    },
                ),
                False,
            )
        return (
            PlaceResolutionOutcome(
                receipt={
                    "status": "NO_UNIQUE_MATCH",
                    "external_calls": 0,
                }
            ),
            False,
        )

    async def run(self, source_text: str) -> PipelineOutput:
        proposal = await self.inference_provider.propose(source_text)
        compiled, claims, compiler_receipt = self.compiler.compile(source_text, proposal)
        resolved: list[ResolvedActivity] = []
        attempted_count = 0
        unavailable_count = 0
        budget_limited_count = 0
        deduplicated_count = 0
        semaphore = asyncio.Semaphore(self.max_place_concurrency)
        tasks_by_key: dict[
            tuple[str, str, str],
            asyncio.Task[tuple[PlaceResolutionOutcome, bool]],
        ] = {}
        resolution_slots: list[
            tuple[str, asyncio.Task[tuple[PlaceResolutionOutcome, bool]] | None, bool, str]
        ] = []
        for item in compiled:
            if not item.eligible_for_place_search:
                resolution_slots.append(("NOT_ELIGIBLE", None, False, ""))
                continue
            if attempted_count >= self.max_executable_activities:
                budget_limited_count += 1
                resolution_slots.append(("BUDGET_LIMITED", None, False, ""))
                continue
            attempted_count += 1
            resolution_key = (
                proposal.destination_name.strip().casefold(),
                (item.mention.atomic_place_name or "").strip().casefold(),
                (item.mention.category_hint or "").strip().casefold(),
            )
            task = tasks_by_key.get(resolution_key)
            is_owner = task is None
            if task is None:
                task = asyncio.create_task(
                    self._resolve_place(
                        item,
                        city=proposal.destination_name,
                        semaphore=semaphore,
                    )
                )
                tasks_by_key[resolution_key] = task
            else:
                deduplicated_count += 1
            resolution_slots.append(
                (
                    "RESOLVE",
                    task,
                    is_owner,
                    canonical_sha256(resolution_key),
                )
            )

        if tasks_by_key:
            await asyncio.gather(*tasks_by_key.values())

        for item, (slot_type, task, is_owner, resolution_key_sha256) in zip(
            compiled,
            resolution_slots,
            strict=True,
        ):
            if slot_type == "NOT_ELIGIBLE":
                resolved.append(
                    ResolvedActivity(
                        compiled=item,
                        resolution_status=ResolutionStatus.NOT_ELIGIBLE,
                        resolver_receipt={
                            "status": "NOT_ELIGIBLE",
                            "external_calls": 0,
                        },
                    )
                )
                continue
            if slot_type == "BUDGET_LIMITED":
                resolved.append(
                    ResolvedActivity(
                        compiled=item,
                        resolution_status=ResolutionStatus.UNRESOLVED,
                        resolver_receipt={
                            "status": "BUDGET_LIMITED",
                            "budget": "max_executable_activities",
                            "limit": self.max_executable_activities,
                            "external_calls": 0,
                        },
                    )
                )
                continue
            assert task is not None
            outcome, provider_unavailable = task.result()
            unavailable_count += int(provider_unavailable)
            receipt = dict(outcome.receipt)
            if not is_owner:
                receipt.update(
                    {
                        "external_calls": 0,
                        "deduplicated": True,
                        "resolution_key_sha256": resolution_key_sha256,
                    }
                )
            place = outcome.place
            resolved.append(
                ResolvedActivity(
                    compiled=item,
                    resolution_status=(
                        ResolutionStatus.AUTO_MATCHED if place else ResolutionStatus.NEEDS_CONFIRMATION
                    ),
                    place=place,
                    resolver_receipt=receipt,
                )
            )
        public_result = self.projector.project(
            proposal.destination_name,
            proposal.destination_basis,
            resolved,
        )
        fallback_used = proposal.binding.get("fallback_used") is True
        if budget_limited_count:
            public_result = public_result.model_copy(update={"status": "LIMITED"})
        elif (fallback_used or unavailable_count) and public_result.status != "PARTIAL_RESULT":
            public_result = public_result.model_copy(update={"status": "PARTIAL_RESULT"})
        resolution_receipt = {
            "policy": "atomic-planned-place-resolution-v1",
            "eligible_count": sum(item.eligible_for_place_search for item in compiled),
            "attempted_count": attempted_count,
            "auto_matched_count": sum(item.place is not None for item in resolved),
            "needs_confirmation_count": sum(
                item.resolution_status == ResolutionStatus.NEEDS_CONFIRMATION for item in resolved
            ),
            "provider_unavailable_count": unavailable_count,
            "unique_resolution_count": len(tasks_by_key),
            "deduplicated_resolution_count": deduplicated_count,
            "place_external_call_count": sum(
                int(item.resolver_receipt.get("external_calls", 0))
                for item in resolved
                if isinstance(item.resolver_receipt.get("external_calls"), int)
            ),
            "max_place_concurrency": self.max_place_concurrency,
            "budget_limited_count": budget_limited_count,
            "max_executable_activities": self.max_executable_activities,
            "inference_fallback_used": fallback_used,
            "provider_failures_exposed_publicly": 0,
        }
        internal_content = {
            "source_hash": proposal.source_hash,
            "destination": proposal.destination_name,
            "proposal": proposal.model_dump(mode="json"),
            "compiler_receipt": compiler_receipt,
            "resolution_receipt": resolution_receipt,
            "activities": [item.model_dump(mode="json") for item in resolved],
        }
        return PipelineOutput(
            source_hash=proposal.source_hash,
            destination={
                "name": proposal.destination_name,
                "status": proposal.destination_basis.value,
            },
            assumptions=[
                {"key": "calendar", "value": "DAY_INDEX_ONLY", "source": "SOFT_ASSUMPTION"},
                {"key": "party_size", "value": 2, "source": "SOFT_ASSUMPTION"},
            ],
            proposal=proposal,
            inference_binding=proposal.binding,
            compiler_receipt=compiler_receipt,
            resolution_receipt=resolution_receipt,
            activities=resolved,
            claims=claims,
            public_result=public_result,
            content_hash=canonical_sha256(internal_content),
        )
