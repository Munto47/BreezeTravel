from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Protocol
from uuid import uuid4

from app.trip_understanding.models import (
    ActivityCardView,
    ActivityRole,
    AssumptionChipView,
    CompiledActivity,
    InferenceProposal,
    MapReadinessView,
    PipelineOutput,
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
    async def resolve(self, *, city: str, atomic_place_name: str) -> ResolvedPlace | None: ...


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
    def project(self, destination_name: str, activities: list[ResolvedActivity]) -> UserFacingTripResult:
        day_views: list[TripDayView] = []
        for day_index in range(1, 4):
            cards = []
            for item in sorted(
                (
                    activity
                    for activity in activities
                    if activity.compiled.mention.role == ActivityRole.PLANNED
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
        return UserFacingTripResult(
            status="READY",
            assumptions=[
                AssumptionChipView(
                    key="destination",
                    label="目的地",
                    value=destination_name,
                    editable=True,
                ),
                AssumptionChipView(
                    key="calendar",
                    label="日期",
                    value="未填写，按 Day 1～Day 3 展示",
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
    ) -> None:
        self.inference_provider = inference_provider
        self.place_resolver = place_resolver
        self.compiler = compiler or EvidenceCompiler()
        self.projector = projector or PublicResultProjector()

    async def run(self, source_text: str) -> PipelineOutput:
        proposal = await self.inference_provider.propose(source_text)
        compiled, claims, compiler_receipt = self.compiler.compile(source_text, proposal)
        resolved: list[ResolvedActivity] = []
        for item in compiled:
            if not item.eligible_for_place_search:
                resolved.append(
                    ResolvedActivity(
                        compiled=item,
                        resolution_status=ResolutionStatus.NOT_ELIGIBLE,
                    )
                )
                continue
            place = await self.place_resolver.resolve(
                city=proposal.destination_name,
                atomic_place_name=item.mention.atomic_place_name or "",
            )
            resolved.append(
                ResolvedActivity(
                    compiled=item,
                    resolution_status=(
                        ResolutionStatus.AUTO_MATCHED if place else ResolutionStatus.NEEDS_CONFIRMATION
                    ),
                    place=place,
                )
            )
        public_result = self.projector.project(proposal.destination_name, resolved)
        internal_content = {
            "source_hash": proposal.source_hash,
            "destination": proposal.destination_name,
            "proposal": proposal.model_dump(mode="json"),
            "compiler_receipt": compiler_receipt,
            "activities": [item.model_dump(mode="json") for item in resolved],
        }
        return PipelineOutput(
            source_hash=proposal.source_hash,
            destination={"name": proposal.destination_name, "status": "EXPLICIT"},
            assumptions=[
                {"key": "calendar", "value": "DAY_INDEX_ONLY", "source": "SOFT_ASSUMPTION"},
                {"key": "party_size", "value": 2, "source": "SOFT_ASSUMPTION"},
            ],
            proposal=proposal,
            inference_binding=proposal.binding,
            compiler_receipt=compiler_receipt,
            activities=resolved,
            claims=claims,
            public_result=public_result,
            content_hash=canonical_sha256(internal_content),
        )
