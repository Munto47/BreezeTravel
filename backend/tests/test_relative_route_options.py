"""User-route comparisons with the real AMap adapter and fixed HTTP transport.

These exercise transport/selection and source protection, not live AMap accuracy.
No network, database, model, or production resource is used.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.trip_understanding.amap_route import AmapRouteProvider
from app.trip_understanding.map_render import ROUTE_CONFIG_SHA256, InternalRouteModeFact, MapStop
from app.trip_understanding.map_render import RouteGeometryPoint
from app.trip_understanding.route_connection import connection_evidence
from app.trip_understanding.relative_route_options import (
    DirectedRouteFacts,
    RelativeRouteVisit,
    RouteEndpoint,
    SourceOrderConstraints,
    build_relative_route_options,
)


def visits(count=4, day=1):
    return tuple(RelativeRouteVisit(
        visit_id=f"visit-{i}", source_kind="SOURCE_BOUND",
        stop=MapStop(
            activity_token=f"current-token-{i}", day_index=day, day_label=f"Day {day}",
            sequence_index=i, name=f"固定景点{i}", category="景点",
            canonical_place_id=f"poi-{i}", resolution_status="AUTO_MATCHED", city="北京",
            longitude=116.30 + i / 100, latitude=39.9,
        ),
    ) for i in range(count))


def known(items, *, hard=(), gaps=()):
    # Explicitly verified absence of hard restrictions is different from UNKNOWN.
    return SourceOrderConstraints(frozenset(v.visit_id for v in items), hard, gaps)


def mode_fact(mode, minutes=25, distance=1000, *, expires=None, observed=None, origin=None, destination=None):
    now = datetime.now(UTC)
    return InternalRouteModeFact(
        mode=mode, status="AVAILABLE" if minutes else "UNAVAILABLE",
        duration_minutes=minutes, distance_meters=distance if minutes else None,
        response_hash="a" * 64, request_hash="b" * 64,
        # Synthetic comparisons explicitly bind their returned endpoints. Old
        # unbound data is tested separately and cannot claim a complete route.
        provider_binding={"execution_mode": "controlled_test", **({"route_connection": connection_evidence(origin, destination,
            [RouteGeometryPoint(longitude=s.longitude, latitude=s.latitude) for s in (origin,destination)])} if origin and destination and all(s.longitude is not None and s.latitude is not None for s in (origin,destination)) else {})}, external_call_count=0,
        observed_at=observed or now - timedelta(minutes=1),
        expires_at=expires or now + timedelta(minutes=10),
    )


def current_edges(items, *, walking=25, transit=40):
    return tuple(DirectedRouteFacts(
        RouteEndpoint.from_stop(a.stop), RouteEndpoint.from_stop(b.stop),
        mode_fact("walking", walking, origin=a.stop, destination=b.stop), mode_fact("transit", transit, origin=a.stop, destination=b.stop), ROUTE_CONFIG_SHA256,
    ) for a, b in zip(items, items[1:]) if a.stop.day_index == b.stop.day_index)


class FixedAmap:
    def __init__(self, *, minutes=None, distances=None, failure=None, delay=0):
        self.calls = []
        self.minutes = minutes or {}
        self.distances = distances or {}
        self.failure = failure
        self.delay = delay
        self.cancelled = False

    async def respond(self, request):
        mode = "walking" if request.url.path.endswith("walking") else "transit"
        origin = round((float(request.url.params["origin"].split(",")[0]) - 116.30) * 100)
        destination = round((float(request.url.params["destination"].split(",")[0]) - 116.30) * 100)
        key = (origin, destination, mode)
        self.calls.append(key)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.failure == key:
            return httpx.Response(503, json={"status": "0"})
        # A direction has its own answer. The reverse edge is never symmetric.
        minutes = self.minutes.get(key, 10 if mode == "walking" else 20)
        distance = self.distances.get(key, 800 if mode == "walking" else 1400)
        path = {"duration": str(minutes * 60), "distance": str(distance)}
        polyline = request.url.params["origin"] + ";" + request.url.params["destination"]
        if mode == "walking":
            assert request.url.params["origin_id"] == f"poi-{origin}"
            assert request.url.params["destination_id"] == f"poi-{destination}"
            payload = {"status": "1", "route": {"paths": [{**path, "steps": [{"polyline": polyline}]}]}}
        else:
            assert request.url.params["city"] == request.url.params["cityd"] == "北京"
            payload = {"status": "1", "route": {"transits": [{**path, "segments": [{"walking": {"steps": [{"polyline": polyline}]}}]}]}}
        return httpx.Response(200, json=payload)


async def compare(items, *, source=None, edges=None, fixture=None, **kwargs):
    fixture = fixture or FixedAmap()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.respond)) as client:
        provider = AmapRouteProvider(api_key="fixed-test-not-a-real-key", client=client)
        output = await build_relative_route_options(
            items, day_index=1, constraints=source if source is not None else known(items),
            current_edges=edges if edges is not None else current_edges(items),
            provider=provider, **kwargs,
        )
    return output, fixture.calls


@pytest.mark.asyncio
async def test_four_free_visits_compare_all_three_directed_edges_through_real_adapter():
    items = visits()
    fixture = FixedAmap(minutes={(2, 1, "walking"): 45, (2, 1, "transit"): 14})
    before = tuple(v.stop.model_dump() for v in items)
    edges = current_edges(items)
    output, calls = await compare(items, fixture=fixture, edges=edges)
    assert output.no_options_reason is None
    assert output.eligible_swap_count == output.compared_swap_count == 1
    option, = output.options
    assert option.before_visit_order == ("visit-0", "visit-1", "visit-2", "visit-3")
    assert option.after_visit_order == ("visit-0", "visit-2", "visit-1", "visit-3")
    assert (option.moved_visit_id, option.target_position) == ("visit-1", 2)
    assert [(e.from_visit_id, e.to_visit_id) for e in option.changed_edges_before] == [
        ("visit-0", "visit-1"), ("visit-1", "visit-2"), ("visit-2", "visit-3"),
    ]
    assert [(e.from_visit_id, e.to_visit_id) for e in option.changed_edges_after] == [
        ("visit-0", "visit-2"), ("visit-2", "visit-1"), ("visit-1", "visit-3"),
    ]
    assert [e.selected_mode for e in option.changed_edges_after] == ["walking", "transit", "walking"]
    assert (option.duration_minutes_before, option.duration_minutes_after, option.minutes_saved) == (75, 34, 41)
    assert (option.distance_meters_before, option.distance_meters_after) == (3000, 3000)
    assert calls == [(a, b, mode) for a, b in [(0, 2), (2, 1), (1, 3)] for mode in ["walking", "transit"]]
    assert output.route_calls == 6
    assert option.expires_at == min(e.expires_at for e in edges)
    assert tuple(v.stop.model_dump() for v in items) == before


@pytest.mark.asyncio
async def test_at_most_three_preselected_swaps_share_new_edges_without_querying_unchanged_days():
    day1 = visits(7)
    day2 = tuple(replace(v, visit_id=f"second-{i}", stop=v.stop.model_copy(update={
        "canonical_place_id": f"other-{i}", "activity_token": f"other-token-{i}",
    })) for i, v in enumerate(visits(2, day=2)))
    output, calls = await compare(day1 + day2)
    assert output.eligible_swap_count == 4
    assert output.compared_swap_count == len(output.options) == 3
    assert [o.moved_visit_id for o in output.options] == ["visit-1", "visit-2", "visit-3"]
    assert len(calls) == len(set(calls)) == output.route_calls == 14
    assert all(a < 6 and b < 6 for a, b, _ in calls)
    assert not any((a, b) in [(i, i + 1) for i in range(6)] for a, b, _ in calls)


@pytest.mark.asyncio
async def test_explicit_hard_order_blocks_swap_but_free_source_order_does_not():
    items = visits()
    blocked, calls = await compare(items, source=known(items, hard=(("visit-1", "visit-2"),)))
    assert blocked.no_options_reason == "NO_SAFE_CANDIDATE"
    assert blocked.options == () and calls == []
    free, _ = await compare(items, source=known(items, hard=(("visit-0", "visit-3"),)))
    assert len(free.options) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("protection", [
    "source_unknown", "unverified", "meal", "named_meal_area", "lodging", "pickup",
    "branch", "revisit", "pending", "placeholder", "missing_coordinate", "missing_city", "cross_city",
])
async def test_source_and_business_boundaries_never_become_free_after_filtering(protection):
    items = list(visits())
    index = 1
    item = items[index]
    source = known(items)
    if protection == "source_unknown":
        item = replace(item, source_kind="UNKNOWN")
    elif protection == "unverified":
        source = replace(source, verified_visit_ids=frozenset({"visit-0", "visit-2", "visit-3"}))
    elif protection in {"meal", "lodging", "pending", "placeholder", "missing_coordinate", "missing_city", "cross_city", "pickup"}:
        updates = {
            "meal": {"category": "餐饮"}, "lodging": {"category": "住宿"},
            "pickup": {"lodging_event": "LUGGAGE_PICKUP"},
            "pending": {"resolution_status": "NEEDS_CONFIRMATION"},
            "placeholder": {"source_place_is_placeholder": True},
            "missing_coordinate": {"longitude": None}, "missing_city": {"city": None},
            "cross_city": {"city": "上海"},
        }[protection]
        item = replace(item, stop=item.stop.model_copy(update=updates))
    elif protection == "named_meal_area":
        item = replace(item, meal_role="DINNER")
    elif protection == "branch":
        item = replace(item, in_choice_branch=True)
    elif protection == "revisit":
        item = replace(item, is_revisit=True)
    items[index] = item
    output, calls = await compare(items, source=source)
    assert output.no_options_reason == "NO_SAFE_CANDIDATE"
    assert output.options == () and calls == []


@pytest.mark.asyncio
async def test_pending_gap_missing_from_snapshot_is_rejected_not_renumbered():
    items = visits(5)
    output, calls = await compare((items[0], *items[2:]))
    assert output.no_options_reason == "INCOMPLETE_VISIT_SNAPSHOT"
    assert calls == []


@pytest.mark.asyncio
async def test_anonymous_meal_anchor_adjacency_and_cross_day_revisit_are_protected():
    items = visits()
    meal, calls = await compare(items, source=known(items, gaps=(("visit-1", "visit-2"),)))
    assert meal.options == () and calls == []
    repeat = replace(items[1], visit_id="next-day-return", stop=items[1].stop.model_copy(update={
        "day_index": 2, "day_label": "Day 2", "sequence_index": 0, "activity_token": "return-token",
    }))
    revisits, calls = await compare((*items, repeat))
    assert revisits.options == () and calls == []


@pytest.mark.asyncio
async def test_meal_neighbours_are_preserved_while_an_independent_later_window_remains_usable():
    items = list(visits(7))
    items[1] = replace(items[1], meal_role="LUNCH")
    output, _ = await compare(items)
    assert [o.moved_visit_id for o in output.options] == ["visit-3", "visit-4"]
    assert all(o.after_visit_order[:3] == ("visit-0", "visit-1", "visit-2") for o in output.options)


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["missing", "expired", "future", "wrong_mode", "wrong_policy", "wrong_identity", "wrong_coordinates", "conflicting"])
async def test_old_route_must_be_complete_current_and_bound_before_any_network(problem):
    items = visits()
    edges = list(current_edges(items))
    edge = edges[1]
    if problem == "missing":
        edges.pop(1)
    elif problem == "expired":
        edges[1] = replace(edge, walking=mode_fact("walking", expires=datetime.now(UTC) - timedelta(seconds=1)))
    elif problem == "future":
        edges[1] = replace(edge, walking=mode_fact("walking", observed=datetime.now(UTC) + timedelta(minutes=1)))
    elif problem == "wrong_mode":
        edges[1] = replace(edge, walking=mode_fact("transit"))
    elif problem == "wrong_policy":
        edges[1] = replace(edge, route_config_hash="old-policy")
    elif problem == "wrong_identity":
        edges[1] = replace(edge, origin=replace(edge.origin, canonical_place_id="other-branch"))
    elif problem == "wrong_coordinates":
        edges[1] = replace(edge, origin=replace(edge.origin, longitude=120))
    elif problem == "conflicting":
        edges.append(replace(edge, walking=mode_fact("walking", minutes=1)))
    output, calls = await compare(items, edges=edges)
    assert output.no_options_reason == "CURRENT_ROUTES_NEED_UPDATE"
    assert output.options == () and calls == []


@pytest.mark.asyncio
async def test_no_improvement_returns_no_adoptable_option_with_no_replenishment():
    items = visits(7)
    fixture = FixedAmap(minutes={(a, b, mode): 29 for a in range(7) for b in range(7) for mode in ["walking", "transit"]})
    output, calls = await compare(items, fixture=fixture)
    assert output.no_options_reason == "NO_IMPROVEMENT"
    assert output.options == () and output.compared_swap_count == 3
    assert len(calls) == 14
    assert not any(6 in (a, b) for a, b, _ in calls)  # The fourth safe swap is not tried.


@pytest.mark.asyncio
async def test_failed_direction_does_not_get_reverse_fallback_or_retry_or_fourth_swap():
    items = visits(7)
    fixture = FixedAmap(failure=(2, 1, "walking"))
    output, calls = await compare(items, fixture=fixture)
    # Existing product policy permits the independently verified transit route
    # when walking is unavailable; neither reverse reuse nor retry is needed.
    assert [o.moved_visit_id for o in output.options] == ["visit-1", "visit-2", "visit-3"]
    edge = output.options[0].changed_edges_after[1]
    assert edge.selected_mode == "transit" and edge.duration_minutes == 20
    assert edge.facts.walking.status == "UNAVAILABLE"
    assert edge.facts.walking.duration_minutes is None
    assert calls.count((2, 1, "walking")) == 1
    assert calls.count((2, 1, "transit")) == 1
    assert not any(6 in (a, b) for a, b, _ in calls)
    assert len(calls) <= 18


@pytest.mark.asyncio
async def test_budget_reserves_complete_comparison_and_shared_edges_reduce_actual_cost():
    blocked, calls = await compare(visits(), max_http_calls=5)
    assert blocked.no_options_reason == "REQUEST_LIMIT_REACHED"
    assert calls == []
    limited, calls = await compare(visits(7), max_http_calls=10)
    assert len(limited.options) == 2
    assert len(calls) == limited.route_calls == 10
    assert limited.issues[-1].code == "REQUEST_LIMIT_REACHED"


@pytest.mark.asyncio
async def test_overall_deadline_cancels_real_adapter_request_without_retry():
    fixture = FixedAmap(delay=.1)
    output, calls = await compare(visits(), fixture=fixture, deadline_seconds=.02)
    assert output.no_options_reason == "DEADLINE_EXCEEDED"
    assert output.options == () and output.route_calls == len(calls) == 1
    assert fixture.cancelled


@pytest.mark.asyncio
async def test_old_facts_expiring_during_new_route_queries_produce_no_saving():
    items = visits()
    expires = datetime.now(UTC) + timedelta(milliseconds=35)
    edges = [replace(e, walking=mode_fact("walking", expires=expires, origin=e.origin, destination=e.destination)) for e in current_edges(items)]
    output, _ = await compare(items, edges=edges, fixture=FixedAmap(delay=.015))
    assert output.options == ()
    assert output.no_options_reason == "COMPARISON_ROUTES_UNAVAILABLE"


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_converted_to_success_or_unavailable():
    fixture = FixedAmap(delay=.2)
    task = asyncio.create_task(compare(visits(), fixture=fixture))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fixture.cancelled and len(fixture.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["walking", "transit"])
async def test_missing_new_mode_preserves_unknown_values_and_uses_existing_selection_policy(mode):
    # The first version incorrectly required both modes. The accepted policy is
    # short walking first, otherwise independently available transit, never zero.
    fixture = FixedAmap(minutes={(2, 1, mode): 0})
    output, calls = await compare(visits(), fixture=fixture)
    option, = output.options
    edge = option.changed_edges_after[1]
    missing = edge.facts.walking if mode == "walking" else edge.facts.transit
    assert missing.status == "UNAVAILABLE"
    assert missing.duration_minutes is None and missing.distance_meters is None
    assert edge.selected_mode == ("transit" if mode == "walking" else "walking")
    assert option.duration_minutes_after == (40 if mode == "walking" else 30)
    assert calls.count((2, 1, mode)) == 1
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_saved_new_direction_facts_can_be_reused_but_expired_facts_never_hide_in_a_complete_preview():
    items = visits()
    initial, _ = await compare(items)
    option, = initial.options
    edges = (*current_edges(items), *(e.facts for e in option.changed_edges_after))
    reused, calls = await compare(items, edges=edges)
    assert len(reused.options) == 1 and reused.route_calls == 0 and calls == []
    old_new_edge = edges[-1]
    expired = replace(old_new_edge, walking=mode_fact("walking", expires=datetime.now(UTC) - timedelta(seconds=1)))
    unavailable, calls = await compare(items, edges=(*edges[:-1], expired))
    assert unavailable.options == () and calls == []
    assert unavailable.no_options_reason == "COMPARISON_ROUTES_UNAVAILABLE"


@pytest.mark.asyncio
async def test_detached_snapshot_keeps_names_and_identities_if_caller_mutates_while_query_is_in_flight():
    items = visits()
    fixture = FixedAmap(delay=.005)
    task = asyncio.create_task(compare(items, fixture=fixture))
    await asyncio.sleep(.002)
    items[1].stop.name = "调用方稍后换了别的景点"
    items[1].stop.canonical_place_id = "another-poi"
    output, calls = await task
    assert len(output.options) == 1 and len(calls) == 6
    option, = output.options
    assert option.changed_edges_after[1].to_name == "固定景点1"
    assert option.changed_edges_after[1].facts.destination.canonical_place_id == "poi-1"
    # Publication must still recheck the version at the caller; this module only
    # promises that its internally consistent old snapshot was not mutated.


@pytest.mark.asyncio
async def test_unknown_constraint_endpoint_and_already_broken_source_order_do_not_get_guessed():
    items = visits()
    for hard in [(('outside-snapshot', 'visit-1'),), (('visit-2', 'visit-1'),)]:
        output, calls = await compare(items, source=known(items, hard=hard))
        assert output.options == () and calls == []
        assert output.no_options_reason == "SOURCE_CONSTRAINTS_UNAVAILABLE"


@pytest.mark.asyncio
async def test_short_valid_walking_with_unavailable_transit_is_a_complete_walking_comparison():
    items = visits()
    edges = [replace(e, transit=mode_fact("transit", minutes=None)) for e in current_edges(items)]
    fixture = FixedAmap(minutes={(2, 1, "walking"): 15}, failure=(2, 1, "transit"))
    output, calls = await compare(items, edges=edges, fixture=fixture)
    option, = output.options
    assert option.duration_minutes_after == 35 and option.minutes_saved == 40
    edge = option.changed_edges_after[1]
    assert edge.selected_mode == "walking" and edge.duration_minutes == 15
    assert edge.facts.transit.status == "UNAVAILABLE"
    assert edge.facts.transit.duration_minutes is None and edge.facts.transit.distance_meters is None
    assert len(calls) == output.route_calls == 6


@pytest.mark.asyncio
async def test_long_walking_cannot_replace_unavailable_required_transit_with_fictitious_saving():
    fixture = FixedAmap(minutes={(2, 1, "walking"): 35}, failure=(2, 1, "transit"))
    output, calls = await compare(visits(), fixture=fixture)
    assert output.options == () and output.no_options_reason is not None
    assert calls.count((2, 1, "walking")) == calls.count((2, 1, "transit")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", ["walking", "transit"])
async def test_expired_actual_selected_mode_is_never_switched_to_the_other_mode(selected):
    items = visits()
    edges = list(current_edges(items, walking=15 if selected == "walking" else 35, transit=10))
    expired_fact = getattr(edges[1], selected).model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    edges[1] = replace(edges[1], **{selected: expired_fact})
    output, calls = await compare(items, edges=edges)
    assert output.options == () and calls == []
    assert output.no_options_reason == "CURRENT_ROUTES_NEED_UPDATE"


@pytest.mark.asyncio
async def test_unselected_mode_expiry_does_not_shorten_valid_walking_comparison():
    items = visits()
    edges = [replace(e, transit=mode_fact("transit", expires=datetime.now(UTC) - timedelta(seconds=1)))
             for e in current_edges(items)]
    output, _ = await compare(items, edges=edges)
    option, = output.options
    assert option.expires_at == min(e.walking.expires_at for e in edges)
    assert all(e.selected_mode == "walking" for e in option.changed_edges_before)
