from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Protocol

from app.schemas.place import Place
from app.templates.models import (
    CandidateGate,
    CandidateSuggestion,
    CandidateTier,
    EvidenceFreshness,
    HotelAreaScore,
    HotelSuggestion,
    RouteEstimate,
)


class RouteEstimator(Protocol):
    async def route(self, origin_place_id: str, destination_place_id: str) -> RouteEstimate: ...


class StaticRouteEstimator:
    """Deterministic test/cache adapter. Missing edges stay explicitly unknown."""

    def __init__(self, edges: Mapping[tuple[str, str], RouteEstimate]):
        self._edges = dict(edges)

    async def route(self, origin_place_id: str, destination_place_id: str) -> RouteEstimate:
        if origin_place_id == destination_place_id:
            return RouteEstimate(minutes=0, source="same_place", freshness=EvidenceFreshness.FRESH)
        return self._edges.get((origin_place_id, destination_place_id), RouteEstimate())


class AmapRouteEstimator:
    """Provider adapter used by the HTTP boundary; no client supplied durations."""

    def __init__(self, places: list[Place], city: str, mode: str = "transit"):
        self.places = {place.place_id: place for place in places}
        self.city = city
        self.mode = mode

    async def route(self, origin_place_id: str, destination_place_id: str) -> RouteEstimate:
        if origin_place_id == destination_place_id:
            return RouteEstimate(minutes=0, source="same_place", freshness=EvidenceFreshness.FRESH)
        origin, destination = self.places.get(origin_place_id), self.places.get(destination_place_id)
        if origin is None or destination is None:
            return RouteEstimate(source="route_context_missing", freshness=EvidenceFreshness.UNAVAILABLE, failure_reason="place_coordinates_missing")
        # Existing provider wrapper centralizes mock/offline failure semantics.
        from app.constraints.geo_routes import fetch_amap_route
        import aiohttp

        async with aiohttp.ClientSession() as session:
            result = await fetch_amap_route(session, origin.coords, destination.coords, self.mode, self.city)
        return RouteEstimate(
            minutes=result.duration_minutes,
            source=result.source,
            freshness=EvidenceFreshness.FRESH if result.status == "ok" else EvidenceFreshness.UNAVAILABLE,
            observed_at=result.observed_at or datetime.now(timezone.utc),
            failure_reason=result.failure_reason,
        )


def _worst_freshness(estimates: list[RouteEstimate]) -> EvidenceFreshness:
    ordered = (
        EvidenceFreshness.CONFLICTING,
        EvidenceFreshness.UNAVAILABLE,
        EvidenceFreshness.STALE,
        EvidenceFreshness.FRESH,
    )
    return next((value for value in ordered if any(item.freshness is value for item in estimates)), EvidenceFreshness.UNAVAILABLE)


def _hard_gate(gate: CandidateGate) -> tuple[bool, list[str]]:
    reasons = list(gate.reason_codes)
    passed = gate.hard_constraint_passed
    for value, code in (
        (gate.opening_time_fit, "OPENING_TIME_CONFLICT"),
        (gate.reservation_fit, "RESERVATION_CONFLICT"),
        (gate.member_suitability, "MEMBER_HARD_CONSTRAINT_CONFLICT"),
    ):
        if value is False:
            passed = False
            if code not in reasons:
                reasons.append(code)
    return passed, reasons


class CandidateSuggestionService:
    """Ranks by evidence-backed insertion cost; it never drops remote candidates."""

    def __init__(self, route_estimator: RouteEstimator):
        self.route_estimator = route_estimator

    async def suggest(
        self,
        *,
        candidate: Place,
        previous: Place | None,
        next_stop: Place | None,
        gate: CandidateGate | None = None,
    ) -> CandidateSuggestion:
        gate_passed, reasons = _hard_gate(gate or CandidateGate())
        if not gate_passed:
            return CandidateSuggestion(
                candidate=candidate,
                tier=CandidateTier.NOT_FEASIBLE,
                hard_gate_passed=False,
                explanation_codes=[*reasons, "CURRENTLY_NOT_FEASIBLE"],
                explanation="当前不可安排：营业、预约或成员硬约束与该时间槽冲突。",
            )

        # At a day boundary no subtraction can be proven without a counterpart.
        if previous is None and next_stop is None:
            return CandidateSuggestion(
                candidate=candidate,
                tier=CandidateTier.ACCEPTABLE,
                hard_gate_passed=True,
                explanation_codes=["ROUTE_CONTEXT_MISSING"],
                explanation="可作为当天起点；尚无相邻地点，暂不能计算额外通勤。",
            )

        estimates: list[RouteEstimate] = []
        if previous is not None:
            estimates.append(await self.route_estimator.route(previous.place_id, candidate.place_id))
        if next_stop is not None:
            estimates.append(await self.route_estimator.route(candidate.place_id, next_stop.place_id))
        baseline = None
        if previous is not None and next_stop is not None:
            baseline_estimate = await self.route_estimator.route(previous.place_id, next_stop.place_id)
            estimates.append(baseline_estimate)
            baseline = baseline_estimate.minutes

        insertion = None if any(item.minutes is None for item in estimates[:2]) else sum(item.minutes or 0 for item in estimates[:2])
        if previous is None:
            insertion = estimates[0].minutes if estimates else None
        if next_stop is None:
            insertion = estimates[0].minutes if estimates else None
        delta = insertion - baseline if insertion is not None and baseline is not None else None
        freshness = _worst_freshness(estimates)
        if delta is None:
            tier = CandidateTier.ACCEPTABLE
            codes = ["ROUTE_COST_UNKNOWN", "ROUTE_EVIDENCE_REQUIRED"]
            description = "路线证据暂不可用，保留为可选项，加入前需要重新核验通勤。"
        elif delta <= 15:
            tier = CandidateTier.ON_THE_WAY
            codes = ["ON_THE_WAY", "DELTA_ROUTE_LE_15"]
            description = f"顺路：插入后额外通勤约 {delta} 分钟。"
        elif delta <= 30:
            tier = CandidateTier.ACCEPTABLE
            codes = ["ACCEPTABLE_DETOUR", "DELTA_ROUTE_16_TO_30"]
            description = f"可接受：插入后额外通勤约 {delta} 分钟。"
        else:
            tier = CandidateTier.ANOTHER_DAY
            codes = ["SUGGEST_ANOTHER_DAY", "DELTA_ROUTE_GT_30"]
            description = f"建议另一天：插入后额外通勤约 {delta} 分钟。"
        if freshness is not EvidenceFreshness.FRESH:
            codes.append(f"ROUTE_EVIDENCE_{freshness.value}")
        return CandidateSuggestion(
            candidate=candidate,
            tier=tier,
            insertion_route_minutes=insertion,
            current_route_minutes=baseline,
            delta_route_minutes=delta,
            route_evidence=estimates,
            evidence_freshness=freshness,
            hard_gate_passed=True,
            explanation_codes=codes,
            explanation=description,
        )


class HotelScoringService:
    """Scores an area against every day boundary, not only the first attraction."""

    def __init__(self, route_estimator: RouteEstimator):
        self.route_estimator = route_estimator

    async def score_area(self, area_id: str, days: list[list[Place]]) -> HotelAreaScore:
        estimates: list[RouteEstimate] = []
        missing_days: list[int] = []
        for index, stops in enumerate(days):
            if not stops:
                missing_days.append(index)
                continue
            estimates.append(await self.route_estimator.route(area_id, stops[0].place_id))
            estimates.append(await self.route_estimator.route(stops[-1].place_id, area_id))
        complete = not missing_days and bool(days) and all(item.minutes is not None for item in estimates)
        return HotelAreaScore(
            area_id=area_id,
            score_minutes=sum(item.minutes or 0 for item in estimates) if complete else None,
            all_days_covered=complete,
            evidence_freshness=_worst_freshness(estimates),
            explanation_codes=(
                ["HOTEL_ALL_DAY_BOUNDARIES_SCORED"]
                if complete else ["HOTEL_ALL_DAY_BOUNDARIES_INCOMPLETE", *(f"DAY_{day}_MISSING_STOPS" for day in missing_days)]
            ),
        )

    async def suggest_hotel(self, hotel: Place, days: list[list[Place]], gate: CandidateGate | None = None) -> HotelSuggestion:
        passed, _ = _hard_gate(gate or CandidateGate())
        return HotelSuggestion(
            hotel=hotel,
            area_score=await self.score_area(hotel.place_id, days),
            hotel_evidence_freshness=EvidenceFreshness.UNAVAILABLE,
            hard_gate_passed=passed,
        )
