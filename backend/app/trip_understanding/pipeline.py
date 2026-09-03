from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from collections.abc import Sequence
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
ATOMIC_PLACE_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff·（）()—_-]+")
GENERIC_ACTIVITY_RE = re.compile(
    r"(?:"
    r"(?:吃|用|享用)?(?:早|午|晚)(?:饭|餐)|"
    r"(?:自由|自行)活动|"
    r"(?:在)?(?:酒店|宾馆|民宿|住处)?休息|"
    r"(?:随便)?看看?风景|"
    r"用餐|就餐|吃饭"
    r")"
)
DINING_CONTEXT_RE = re.compile(
    r"(?:早餐|早饭|午餐|午饭|晚餐|晚饭|用餐|就餐|吃饭|吃)"
    r"\s*(?:安排|选择|打算|准备)?\s*(?:去|到|在)?\s*$"
)
FORBIDDEN_PLACE_MARKERS = (
    "预约",
    "说明",
    "网址",
    "链接",
    "电话",
    "导航",
    "路线",
    "流程",
    "确认",
    "分钟",
)
DEEP_CITIES = ("北京", "上海", "杭州")
# This is deliberately a bounded lexical guard, not a complete statement of
# product coverage.  Live inference can preserve any domestic destination;
# source recovery only overrides it when the source contains an unambiguous,
# exact city token.  That prevents phrases such as ``一家三口`` and place names
# such as ``广州北京路`` from being promoted to a destination.
DOMESTIC_CITY_NAMES = (
    "北京",
    "上海",
    "杭州",
    "成都",
    "南京",
    "广州",
    "深圳",
    "苏州",
    "武汉",
    "西安",
    "重庆",
    "青岛",
    "厦门",
    "长沙",
    "天津",
    "昆明",
    "大理",
    "三亚",
    "哈尔滨",
    "沈阳",
    "郑州",
    "济南",
    "福州",
    "合肥",
    "南昌",
    "南宁",
    "贵阳",
    "兰州",
    "太原",
    "石家庄",
    "乌鲁木齐",
    "拉萨",
    "海口",
    "银川",
    "西宁",
    "呼和浩特",
    "长春",
)
_DOMESTIC_CITY_PATTERN = "(?:" + "|".join(
    re.escape(city) for city in sorted(DOMESTIC_CITY_NAMES, key=len, reverse=True)
) + ")"
MULTI_CITY_HEADER_RE = re.compile(
    rf"^\s*(?P<cities>{_DOMESTIC_CITY_PATTERN}(?:\s*[、，,和与/]\s*{_DOMESTIC_CITY_PATTERN})+?)"
    r"\s*(?:两地|三地|多地)?(?:游|行程|攻略|旅行)"
)
BASIC_CITY_HEADER_RE = re.compile(
    rf"^\s*(?P<city>(?:{_DOMESTIC_CITY_PATTERN})(?:市)?|[\u4e00-\u9fff]{{2,6}}市)"
    r"\s*[一二两三四五六七八九十0-9]+"
    r"(?:日|天)(?:游|行程|攻略|旅行)"
)
DESTINATION_CONTEXT_RE = re.compile(
    r"(?:围绕|一段|整理|关于)\s*"
    r"(?P<cities>[\u4e00-\u9fff]{2,20}(?:\s*[、，,和与/]\s*[\u4e00-\u9fff]{2,20}){0,2})"
    r"\s*的"
)
CITY_SEPARATOR_RE = re.compile(r"\s*[、，,和与/]\s*")
GENERIC_PLACE_NAMES = frozenset({"酒店", "宾馆", "民宿", "住处", "住宿"})


def _ordered_deep_cities(value: str) -> tuple[str, ...]:
    positions = [
        (position, city)
        for city in DEEP_CITIES
        if (position := value.find(city)) >= 0
    ]
    return tuple(city for _position, city in sorted(positions))


def source_destination_cities(source_text: str) -> tuple[str, ...]:
    """Recover only source-explicit destination cities from itinerary framing.

    This intentionally does not treat every occurrence of a city token as the
    trip destination: names such as ``北京路步行街`` may be places in another
    city.  The accepted forms are itinerary headers and explicit framing such
    as ``围绕北京的`` or ``一段北京、上海的``.
    """

    multi_city = MULTI_CITY_HEADER_RE.search(source_text)
    if multi_city:
        return tuple(
            city.strip().removesuffix("市")
            for city in CITY_SEPARATOR_RE.split(multi_city.group("cities"))
        )
    contextual = DESTINATION_CONTEXT_RE.search(source_text)
    if contextual:
        cities = tuple(
            city.strip().removesuffix("市")
            for city in CITY_SEPARATOR_RE.split(contextual.group("cities"))
            if city.strip()
        )
        if cities and all(city in DOMESTIC_CITY_NAMES for city in cities):
            return cities
    basic_city = BASIC_CITY_HEADER_RE.search(source_text)
    if basic_city:
        return (basic_city.group("city").removesuffix("市"),)
    return ()


def normalized_destination_name(source_text: str, destination_name: str) -> str:
    source_cities = source_destination_cities(source_text)
    return "、".join(source_cities) if source_cities else destination_name


def resolution_cities(source_text: str, destination_name: str) -> tuple[str, ...]:
    """Return conservative city-limited search lanes for one itinerary.

    A multi-city destination is searched once per explicitly named deep city.
    If a model translated or softened the destination, source-verbatim city
    tokens recover the safe search boundary. Unknown cities remain a single
    basic-only lane and are rejected by the live resolver without a call.
    """

    header_cities = source_destination_cities(source_text)
    if header_cities:
        return header_cities
    destination_cities = _ordered_deep_cities(destination_name)
    if destination_cities:
        return destination_cities
    if re.search(r"[\u4e00-\u9fff]", destination_name):
        return (destination_name,)
    source_cities = _ordered_deep_cities(source_text)
    return source_cities or (destination_name,)


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

    async def aclose(self) -> None:
        await _close_async_resources(self.primary, self.fallback)


async def _close_async_resources(*resources: object) -> None:
    """Best-effort close every distinct async resource, then surface an error."""

    first_error: Exception | None = None
    seen: set[int] = set()
    for resource in resources:
        identity = id(resource)
        if identity in seen:
            continue
        seen.add(identity)
        close = getattr(resource, "aclose", None)
        if close is None:
            continue
        try:
            await close()
        except Exception as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def is_atomic_planned_place(mention) -> bool:
    if mention.role != ActivityRole.PLANNED or mention.day_index is None:
        return False
    candidate = (mention.atomic_place_name or "").strip()
    if not candidate or len(candidate) > 40 or URL_RE.search(candidate):
        return False
    if candidate in GENERIC_PLACE_NAMES:
        return False
    raw_candidate = re.sub(r"\r?\n[ \t]*", "", (mention.raw_text or "").strip())
    if candidate != raw_candidate:
        return False
    if any(marker in candidate for marker in SENTENCE_MARKERS):
        return False
    if any(word in candidate for word in FORBIDDEN_PLACE_MARKERS):
        return False
    if GENERIC_ACTIVITY_RE.fullmatch(candidate):
        return False
    if ATOMIC_PLACE_RE.fullmatch(candidate) is None:
        return False
    return re.search(r"[A-Za-z\u4e00-\u9fff]", candidate) is not None


def _apply_contextual_category_hints(
    source_text: str,
    proposal: InferenceProposal,
) -> InferenceProposal:
    """Recover a narrow category only when source wording is explicit.

    The hint constrains Provider selection; it never invents a place.  Name-
    based or model-supplied categories continue to win, while an explicit meal
    cue immediately before an otherwise ambiguous place name prevents a hotel
    or attraction with the same short name from being auto-selected.
    """

    mentions = []
    changed = False
    for mention in proposal.mentions:
        category_hint = mention.category_hint
        if category_hint is None and mention.role == ActivityRole.PLANNED:
            local_before = source_text[max(0, mention.span_start - 18) : mention.span_start]
            if DINING_CONTEXT_RE.search(local_before):
                category_hint = "餐饮"
        if category_hint != mention.category_hint:
            changed = True
            mention = mention.model_copy(update={"category_hint": category_hint})
        mentions.append(mention)
    if not changed:
        return proposal
    return proposal.model_copy(update={"mentions": mentions})


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
            if is_atomic_planned_place(activity.compiled.mention)
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
                    if is_atomic_planned_place(activity.compiled.mention)
                    and activity.compiled.mention.day_index == day_index
                ),
                key=lambda activity: activity.compiled.mention.sequence_index,
            ):
                mention = item.compiled.mention
                place = item.place
                source_confirmation_required = (
                    item.resolver_receipt.get("status")
                    == "SOURCE_CONFIRMATION_REQUIRED"
                )
                cards.append(
                    ActivityCardView(
                        activity_token=item.compiled.public_activity_token,
                        name=(
                            place.name
                            if place
                            else (
                                "地点待确认"
                                if source_confirmation_required
                                else (mention.atomic_place_name or "地点待确认")
                            )
                        ),
                        category=(
                            place.category
                            if place
                            else (
                                "地点"
                                if source_confirmation_required
                                else (mention.category_hint or "地点")
                            )
                        ),
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

    async def aclose(self) -> None:
        await _close_async_resources(self.inference_provider, self.place_resolver)

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
            outcome = raw_outcome
        elif isinstance(raw_outcome, ResolvedPlace):
            outcome = PlaceResolutionOutcome(
                place=raw_outcome,
                receipt={
                    "status": "AUTO_MATCHED",
                    **raw_outcome.provider_binding,
                },
            )
        else:
            outcome = PlaceResolutionOutcome(
                receipt={
                    "status": "NO_UNIQUE_MATCH",
                    "external_calls": 0,
                }
            )
        reported_cities = [outcome.receipt.get("city")]
        if outcome.place is not None:
            reported_cities.append(outcome.place.provider_binding.get("city"))
        mismatched_city = next(
            (
                reported
                for reported in reported_cities
                if isinstance(reported, str)
                and reported.strip().removesuffix("市")
                != city.strip().removesuffix("市")
            ),
            None,
        )
        if outcome.place is not None and mismatched_city is not None:
            return (
                PlaceResolutionOutcome(
                    receipt={
                        **outcome.receipt,
                        "status": "NO_UNIQUE_MATCH",
                        "failure_category": "CROSS_CITY_PROVIDER_RESULT",
                        "requested_city": city,
                        "reported_city": mismatched_city,
                    }
                ),
                False,
            )
        return outcome, False

    async def _resolve_place_across_cities(
        self,
        item: CompiledActivity,
        *,
        cities: tuple[str, ...],
        semaphore: asyncio.Semaphore,
    ) -> tuple[PlaceResolutionOutcome, bool]:
        results = await asyncio.gather(
            *(
                self._resolve_place(item, city=city, semaphore=semaphore)
                for city in cities
            )
        )
        if len(results) == 1:
            return results[0]

        external_calls = sum(
            int(outcome.receipt.get("external_calls", 0))
            for outcome, _unavailable in results
            if isinstance(outcome.receipt.get("external_calls", 0), int)
        )
        unavailable = any(value for _outcome, value in results)
        ambiguous = any(
            outcome.receipt.get("status") == "AMBIGUOUS"
            or (
                outcome.receipt.get("status") == "NO_UNIQUE_MATCH"
                and isinstance(
                    outcome.receipt.get("category_compatible_candidate_count"),
                    int,
                )
                and int(outcome.receipt["category_compatible_candidate_count"]) > 1
            )
            for outcome, _unavailable in results
        )
        matches = [
            (city, outcome)
            for city, (outcome, _unavailable) in zip(cities, results, strict=True)
            if outcome.place is not None
        ]
        receipt_hashes = [
            canonical_sha256(
                {
                    "city": city,
                    "receipt": outcome.receipt,
                }
            )
            for city, (outcome, _unavailable) in zip(cities, results, strict=True)
        ]
        successful_place_candidates = [
            {
                "city": city,
                "place": outcome.place.model_dump(mode="json"),
                "receipt": outcome.receipt,
            }
            for city, (outcome, _unavailable) in zip(cities, results, strict=True)
            if outcome.place is not None
        ]
        if not unavailable and not ambiguous and len(matches) == 1:
            selected_city, selected = matches[0]
            return (
                PlaceResolutionOutcome(
                    place=selected.place,
                    receipt={
                        **selected.receipt,
                        "multi_city_resolution": True,
                        "queried_cities": list(cities),
                        "selected_city": selected_city,
                        "city_receipt_sha256": receipt_hashes,
                        "external_calls": external_calls,
                    },
                ),
                False,
            )

        return (
            PlaceResolutionOutcome(
                receipt={
                    "provider": "MULTI_CITY_CONSERVATIVE_RESOLUTION",
                    "status": (
                        "UNAVAILABLE"
                        if unavailable
                        else "NO_UNIQUE_MATCH"
                    ),
                    "multi_city_resolution": True,
                    "queried_cities": list(cities),
                    "city_receipt_sha256": receipt_hashes,
                    "successful_place_candidates": successful_place_candidates,
                    "external_calls": external_calls,
                }
            ),
            unavailable,
        )

    async def run(
        self,
        source_text: str,
        *,
        requires_confirmation_spans: Sequence[tuple[int, int]] = (),
        partial_source: bool = False,
    ) -> PipelineOutput:
        confirmation_spans = tuple(requires_confirmation_spans)
        if any(
            start < 0 or end <= start or end > len(source_text)
            for start, end in confirmation_spans
        ):
            raise ValueError("confirmation spans must be valid source code-point ranges")
        proposal = _apply_contextual_category_hints(
            source_text,
            await self.inference_provider.propose(source_text),
        )
        destination_name = normalized_destination_name(
            source_text,
            proposal.destination_name,
        )
        if destination_name != proposal.destination_name:
            binding = dict(proposal.binding)
            binding["destination_source_recovery_count"] = int(
                binding.get("destination_source_recovery_count", 0)
            ) + 1
            proposal = proposal.model_copy(
                update={"destination_name": destination_name, "binding": binding}
            )
        search_cities = resolution_cities(source_text, proposal.destination_name)
        compiled, claims, compiler_receipt = self.compiler.compile(source_text, proposal)
        confirmation_activity_ids: set[str] = set()
        guarded_compiled: list[CompiledActivity] = []
        for item in compiled:
            mention = item.mention
            intersects_confirmation = any(
                mention.span_start < end and start < mention.span_end
                for start, end in confirmation_spans
            )
            if item.eligible_for_place_search and intersects_confirmation:
                confirmation_activity_ids.add(item.activity_id)
                item = item.model_copy(update={"eligible_for_place_search": False})
            guarded_compiled.append(item)
        compiled = guarded_compiled
        resolved: list[ResolvedActivity] = []
        attempted_count = 0
        unavailable_count = 0
        budget_limited_count = 0
        deduplicated_count = 0
        semaphore = asyncio.Semaphore(self.max_place_concurrency)
        tasks_by_key: dict[
            tuple[tuple[str, ...], str, str],
            asyncio.Task[tuple[PlaceResolutionOutcome, bool]],
        ] = {}
        resolution_slots: list[
            tuple[str, asyncio.Task[tuple[PlaceResolutionOutcome, bool]] | None, bool, str]
        ] = []
        for item in compiled:
            if item.activity_id in confirmation_activity_ids:
                resolution_slots.append(("CONFIRMATION_REQUIRED", None, False, ""))
                continue
            if not item.eligible_for_place_search:
                resolution_slots.append(("NOT_ELIGIBLE", None, False, ""))
                continue
            if attempted_count >= self.max_executable_activities:
                budget_limited_count += 1
                resolution_slots.append(("BUDGET_LIMITED", None, False, ""))
                continue
            attempted_count += 1
            resolution_key = (
                tuple(city.strip().casefold() for city in search_cities),
                (item.mention.atomic_place_name or "").strip().casefold(),
                (item.mention.category_hint or "").strip().casefold(),
            )
            task = tasks_by_key.get(resolution_key)
            is_owner = task is None
            if task is None:
                task = asyncio.create_task(
                    self._resolve_place_across_cities(
                        item,
                        cities=search_cities,
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
            if slot_type == "CONFIRMATION_REQUIRED":
                resolved.append(
                    ResolvedActivity(
                        compiled=item,
                        resolution_status=ResolutionStatus.NEEDS_CONFIRMATION,
                        resolver_receipt={
                            "status": "SOURCE_CONFIRMATION_REQUIRED",
                            "external_calls": 0,
                        },
                    )
                )
                continue
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
        if partial_source:
            public_result = public_result.model_copy(update={"status": "PARTIAL_RESULT"})
        elif budget_limited_count:
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
            "source_confirmation_required_count": len(confirmation_activity_ids),
            "partial_source": partial_source,
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
