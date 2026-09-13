"""Bounded, read-only comparisons of adjacent visits within one day.

Call with a complete immutable snapshot (including pending visits), after releasing
database locks. The caller must recheck its version and these source constraints
before storing/using a preview. This module neither loads source text nor infers
freedom to reorder from the absence of an explicit constraint.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Literal, Sequence

from app.trip_understanding.errors import RouteProviderUnavailableError
from app.trip_understanding.map_render import (
    ROUTE_CONFIG_SHA256,
    InternalRouteModeFact,
    MapStop,
    RouteProvider,
    _unavailable_fact,
    choose_route_mode,
)


@dataclass(frozen=True)
class RelativeRouteVisit:
    # Stable *visit* identity, not a name/POI ID: different visits never collapse.
    visit_id: str
    stop: MapStop
    source_kind: Literal["SOURCE_BOUND", "USER_ADDED", "UNKNOWN"]
    meal_role: str | None = None
    in_choice_branch: bool = False
    is_revisit: bool = False


@dataclass(frozen=True)
class SourceOrderConstraints:
    # VERIFIED means the caller has checked both presence AND absence of hard
    # order requirements for this visit. An empty set therefore allows no swaps.
    verified_visit_ids: frozenset[str]
    hard_precedence: tuple[tuple[str, str], ...]
    # Preserve adjacency around anonymous meals or other source-bound gaps.
    protected_adjacencies: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RouteEndpoint:
    canonical_place_id: str | None
    city: str | None
    longitude: float | None
    latitude: float | None

    @classmethod
    def from_stop(cls, stop: MapStop) -> RouteEndpoint:
        return cls(stop.canonical_place_id, stop.city, stop.longitude, stop.latitude)


@dataclass(frozen=True)
class DirectedRouteFacts:
    origin: RouteEndpoint
    destination: RouteEndpoint
    walking: InternalRouteModeFact
    transit: InternalRouteModeFact
    route_config_hash: str

    @property
    def key(self) -> tuple[RouteEndpoint, RouteEndpoint]:
        return self.origin, self.destination

    def selected_mode(self, now: datetime) -> Literal["walking", "transit"] | None:
        if self.route_config_hash != ROUTE_CONFIG_SHA256:
            return None
        if self.walking.mode != "walking" or self.transit.mode != "transit":
            return None
        # Select by the existing policy BEFORE freshness checks. An expired
        # selected walk must require an update, not silently switch to transit.
        # A failed/unselected transit must not invalidate a real short walk.
        mode = choose_route_mode(self.walking, self.transit)
        if mode is None:
            return None
        fact = self.walking if mode == "walking" else self.transit
        if (
            fact.observed_at.utcoffset() is None
            or fact.expires_at.utcoffset() is None
            or not fact.observed_at <= now < fact.expires_at
        ):
            return None
        return mode

    @property
    def expires_at(self) -> datetime:
        mode = choose_route_mode(self.walking, self.transit)
        if mode is not None:
            return (self.walking if mode == "walking" else self.transit).expires_at
        return min(self.walking.expires_at, self.transit.expires_at)


@dataclass(frozen=True)
class ComparedRouteEdge:
    from_visit_id: str
    to_visit_id: str
    from_name: str
    to_name: str
    selected_mode: Literal["walking", "transit"]
    duration_minutes: int
    distance_meters: int
    facts: DirectedRouteFacts


@dataclass(frozen=True)
class RelativeRouteOption:
    day_index: int
    moved_visit_id: str
    target_position: int
    before_visit_order: tuple[str, ...]
    after_visit_order: tuple[str, ...]
    changed_edges_before: tuple[ComparedRouteEdge, ...]
    changed_edges_after: tuple[ComparedRouteEdge, ...]
    duration_minutes_before: int
    duration_minutes_after: int
    minutes_saved: int
    distance_meters_before: int
    distance_meters_after: int
    expires_at: datetime


@dataclass(frozen=True)
class RouteOptionIssue:
    visit_ids: tuple[str, ...]
    code: str


@dataclass(frozen=True)
class RelativeRouteOptions:
    options: tuple[RelativeRouteOption, ...]
    eligible_swap_count: int
    compared_swap_count: int
    route_calls: int
    issues: tuple[RouteOptionIssue, ...]
    no_options_reason: str | None


def _pairs(order: Sequence[RelativeRouteVisit]) -> list[tuple[RelativeRouteVisit, RelativeRouteVisit]]:
    return list(zip(order, order[1:]))


def _ids(pair: tuple[RelativeRouteVisit, RelativeRouteVisit]) -> tuple[str, str]:
    return pair[0].visit_id, pair[1].visit_id


def _key(pair: tuple[RelativeRouteVisit, RelativeRouteVisit]) -> tuple[RouteEndpoint, RouteEndpoint]:
    return RouteEndpoint.from_stop(pair[0].stop), RouteEndpoint.from_stop(pair[1].stop)


def _protected(visit: RelativeRouteVisit, repeated: set[str], constraints: SourceOrderConstraints) -> bool:
    stop = visit.stop
    return bool(
        visit.source_kind not in {"SOURCE_BOUND", "USER_ADDED"}
        or visit.visit_id not in constraints.verified_visit_ids
        or visit.meal_role
        or visit.in_choice_branch
        or visit.is_revisit
        or stop.canonical_place_id in repeated
        or stop.category in {"餐饮", "住宿"}
        or stop.lodging_event
        or stop.lodging_scope
        or stop.lodging_role_uncertain
        or stop.lodging_excluded_nights
        or stop.is_stay_anchor
        or stop.source_place_is_placeholder
        or stop.resolution_status != "AUTO_MATCHED"
        or not stop.activity_token
        or not stop.canonical_place_id
        or not stop.city or not stop.city.strip()
        or stop.longitude is None or stop.latitude is None
    )


def _constraints_hold(
    visits: Sequence[RelativeRouteVisit], constraints: SourceOrderConstraints,
) -> bool:
    positions = {v.visit_id: (v.stop.day_index, i) for i, v in enumerate(visits)}
    for before, after in constraints.hard_precedence:
        if before not in positions or after not in positions or positions[before] >= positions[after]:
            return False
    adjacency = {_ids(p) for p in _pairs(visits) if p[0].stop.day_index == p[1].stop.day_index}
    return all(pair in adjacency for pair in constraints.protected_adjacencies)


def _comparison(
    pair: tuple[RelativeRouteVisit, RelativeRouteVisit], facts: DirectedRouteFacts, now: datetime,
) -> ComparedRouteEdge | None:
    mode = facts.selected_mode(now)
    if mode is None:
        return None
    selected = facts.walking if mode == "walking" else facts.transit
    if selected.duration_minutes is None or selected.distance_meters is None:
        return None
    return ComparedRouteEdge(
        *_ids(pair), pair[0].stop.name, pair[1].stop.name, mode,
        selected.duration_minutes, selected.distance_meters, facts,
    )


async def build_relative_route_options(
    visits: Sequence[RelativeRouteVisit],
    *,
    day_index: int,
    constraints: SourceOrderConstraints,
    current_edges: Sequence[DirectedRouteFacts],
    provider: RouteProvider,
    deadline_seconds: float = 20.0,
    max_http_calls: int = 18,
) -> RelativeRouteOptions:
    """Compare at most three preselected interior swaps, without replenishment.

    AmapRouteProvider performs one HTTP attempt per route() with no retry. This
    routine limits those calls, shares exact directed facts, and has one overall
    deadline. Provider failures never trigger fallback estimates or more swaps.
    No database/session/lock object is accepted here.
    """
    if not 1 <= day_index <= 14 or not 0 < deadline_seconds <= 60:
        raise ValueError("Invalid day or relative-route deadline")
    if type(max_http_calls) is not int or not 0 <= max_http_calls <= 18:
        raise ValueError("Relative-route HTTP limit must be between 0 and 18")
    if len({v.visit_id for v in visits}) != len(visits) or any(not v.visit_id for v in visits):
        raise ValueError("Relative-route visits require unique visit identities")
    # Detach from mutable MapStops/facts before the first await.
    visits = tuple(RelativeRouteVisit(
        v.visit_id, v.stop.model_copy(deep=True), v.source_kind, v.meal_role,
        v.in_choice_branch, v.is_revisit,
    ) for v in visits)
    constraints = SourceOrderConstraints(
        frozenset(constraints.verified_visit_ids), tuple(constraints.hard_precedence),
        tuple(constraints.protected_adjacencies),
    )
    ordered = sorted(visits, key=lambda v: (v.stop.day_index, v.stop.sequence_index))
    if list(visits) != ordered or any(
        [v.stop.sequence_index for v in visits if v.stop.day_index == day] != list(range(sum(
            v.stop.day_index == day for v in visits
        ))) for day in {v.stop.day_index for v in visits}
    ):
        return RelativeRouteOptions((), 0, 0, 0, (), "INCOMPLETE_VISIT_SNAPSHOT")
    if not _constraints_hold(visits, constraints):
        return RelativeRouteOptions((), 0, 0, 0, (), "SOURCE_CONSTRAINTS_UNAVAILABLE")

    day_visits = [v for v in visits if v.stop.day_index == day_index]
    repeated = {place for place, count in Counter(
        v.stop.canonical_place_id for v in visits if v.stop.canonical_place_id
    ).items() if count > 1}
    candidates = []
    for i in range(1, len(day_visits) - 2):
        # Four endpoints cover every changed edge. Protecting a meal/stay anchor
        # here also preserves its immediate neighbours, not merely its index.
        window = day_visits[i - 1:i + 3]
        if any(_protected(v, repeated, constraints) for v in window):
            continue
        if len({v.stop.city for v in window}) != 1:
            continue
        after = day_visits.copy()
        after[i], after[i + 1] = after[i + 1], after[i]
        replaced = iter(after)
        global_after = [next(replaced) if v.stop.day_index == day_index else v for v in visits]
        if _constraints_hold(global_after, constraints):
            candidates.append((i, after))
    if not candidates:
        return RelativeRouteOptions((), 0, 0, 0, (), "NO_SAFE_CANDIDATE")

    cache: dict[tuple[RouteEndpoint, RouteEndpoint], DirectedRouteFacts | None] = {}
    ambiguous_keys = set()
    for edge in current_edges:
        edge = DirectedRouteFacts(
            edge.origin, edge.destination, edge.walking.model_copy(deep=True),
            edge.transit.model_copy(deep=True), edge.route_config_hash,
        )
        if edge.key in cache and cache[edge.key] != edge:
            ambiguous_keys.add(edge.key)
        cache[edge.key] = edge
    for key in ambiguous_keys:
        cache[key] = None

    deadline = monotonic() + deadline_seconds
    route_calls = 0
    compared = 0
    options: list[RelativeRouteOption] = []
    issues: list[RouteOptionIssue] = []
    original_pairs = _pairs(day_visits)
    original_ids = {_ids(p) for p in original_pairs}
    for i, after in candidates[:3]:
        pair_ids = (day_visits[i].visit_id, day_visits[i + 1].visit_id)
        if monotonic() >= deadline:
            issues.append(RouteOptionIssue(pair_ids, "DEADLINE_EXCEEDED"))
            break
        after_pairs = _pairs(after)
        after_ids = {_ids(p) for p in after_pairs}
        before_changed = [p for p in original_pairs if _ids(p) not in after_ids]
        after_changed = [p for p in after_pairs if _ids(p) not in original_ids]
        now = datetime.now(UTC)
        if any(cache.get(_key(p)) is None or cache[_key(p)].selected_mode(now) is None for p in before_changed):
            issues.append(RouteOptionIssue(pair_ids, "CURRENT_ROUTES_NEED_UPDATE"))
            continue
        if any(_key(p) in cache and (cache[_key(p)] is None or cache[_key(p)].selected_mode(now) is None) for p in after_changed):
            issues.append(RouteOptionIssue(pair_ids, "COMPARISON_ROUTES_UNAVAILABLE"))
            continue
        needed = list(dict.fromkeys(_key(p) for p in after_changed if _key(p) not in cache))
        # Reserve the whole comparison, rather than consuming a partial budget
        # that cannot produce a usable answer. Shared successful edges cost zero.
        if route_calls + len(needed) * 2 > max_http_calls:
            issues.append(RouteOptionIssue(pair_ids, "REQUEST_LIMIT_REACHED"))
            break
        failed = None
        for pair in after_changed:
            key = _key(pair)
            if key in cache:
                continue
            mode_facts = []
            for mode in ("walking", "transit"):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    failed = "DEADLINE_EXCEEDED"
                    break
                route_calls += 1
                try:
                    fact = await asyncio.wait_for(provider.route(
                        pair[0].stop, pair[1].stop, mode, observed_at=datetime.now(UTC),
                    ), timeout=remaining)
                except TimeoutError:
                    failed = "DEADLINE_EXCEEDED"
                    break
                except RouteProviderUnavailableError as exc:
                    # Same typed unavailable fact as MapRenderer: keep unknown
                    # minutes/distance null, with no retry or reverse fallback.
                    # The independent other mode may still satisfy the policy.
                    fact = _unavailable_fact(
                        pair[0].stop, pair[1].stop, mode, reason=exc.category,
                        observed_at=datetime.now(UTC), provider_binding=exc.provider_binding,
                        external_call_count=exc.external_call_count,
                    )
                mode_facts.append(fact)
            if failed:
                cache[key] = None
                break
            cache[key] = DirectedRouteFacts(*key, *mode_facts, ROUTE_CONFIG_SHA256)
            if cache[key].selected_mode(datetime.now(UTC)) is None:
                failed = "NEW_ROUTE_UNAVAILABLE"
                break
        if failed:
            issues.append(RouteOptionIssue(pair_ids, failed))
            if failed == "DEADLINE_EXCEEDED":
                break
            continue
        # Recheck all old/new facts after network awaits; they can expire in flight.
        now = datetime.now(UTC)
        before_edges = [_comparison(p, cache[_key(p)], now) if cache.get(_key(p)) else None for p in before_changed]
        after_edges = [_comparison(p, cache[_key(p)], now) if cache.get(_key(p)) else None for p in after_changed]
        if any(e is None for e in before_edges + after_edges):
            issues.append(RouteOptionIssue(pair_ids, "COMPARISON_ROUTES_UNAVAILABLE"))
            continue
        compared += 1
        old_minutes = sum(e.duration_minutes for e in before_edges)
        new_minutes = sum(e.duration_minutes for e in after_edges)
        if new_minutes >= old_minutes:
            continue
        options.append(RelativeRouteOption(
            day_index, day_visits[i].visit_id, i + 1,
            tuple(v.visit_id for v in day_visits), tuple(v.visit_id for v in after),
            tuple(before_edges), tuple(after_edges), old_minutes, new_minutes,
            old_minutes - new_minutes, sum(e.distance_meters for e in before_edges),
            sum(e.distance_meters for e in after_edges),
            min(e.facts.expires_at for e in before_edges + after_edges),
        ))
    # An earlier option must still be fresh after later candidates have finished.
    now = datetime.now(UTC)
    fresh_options = tuple(o for o in options if now < o.expires_at)
    if len(fresh_options) < len(options):
        issues.append(RouteOptionIssue((), "COMPARISON_EXPIRED"))
    reason = None if fresh_options else issues[0].code if issues else "NO_IMPROVEMENT"
    return RelativeRouteOptions(fresh_options, len(candidates), compared, route_calls, tuple(issues), reason)
