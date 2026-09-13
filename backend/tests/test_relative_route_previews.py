"""A fixed real-adapter comparison can only authorize that current day move."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.relative_route_previews import issue_route_preview, verify_route_preview
from tests.test_relative_route_options import compare, visits


async def preview_fixture():
    items = tuple(replace(v, stop=v.stop.model_copy(update={
        "activity_token": f"public-visit-token-{i:08d}"})) for i, v in enumerate(visits()))
    comparison, _ = await compare(items)
    now = datetime.now(UTC)
    public = issue_route_preview(comparison.options[0], items, public_resource_id="my-trip",
        expected_etag="current-etag", now=now)
    return public, items, now, comparison.options[0]


@pytest.mark.asyncio
async def test_public_comparison_contains_measured_changed_edges_without_private_binding():
    public, items, now, option = await preview_fixture()
    assert public.minutes_saved == 45
    assert public.duration_minutes_before == 75
    assert public.duration_minutes_after == 30
    assert len(public.routes_before) == len(public.routes_after) == 3
    assert public.comparison_scope == "CHANGED_EDGES_ONLY"
    visible = public.model_dump_json(exclude={"change_token"})
    for forbidden in ("visit-0", "public-visit-token", "poi-0", "provider_binding", "response_hash", "source_quote"):
        assert forbidden not in visible
    verified = verify_route_preview(public.change_token, public_resource_id="my-trip",
        expected_etag="current-etag", now=now)
    assert verified.command.activity_token == items[1].stop.activity_token
    assert verified.command.target_position == 2
    assert verified.before_tokens == tuple(v.stop.activity_token for v in items)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["resource", "version", "ciphertext", "expired", "prefix", "oversize"])
async def test_preview_cannot_authorize_different_or_expired_operation(change):
    public, _, now, option = await preview_fixture()
    token = public.change_token
    if change == "ciphertext":
        index = len(token) // 2
        token = token[:index] + ("A" if token[index] != "A" else "B") + token[index + 1:]
    if change == "prefix":
        token = "poi_" + token[4:]
    if change == "oversize":
        token += "a" * 24000
    with pytest.raises(CommandTargetChangedError):
        verify_route_preview(token, public_resource_id="another-trip" if change == "resource" else "my-trip",
            expected_etag="new-version" if change == "version" else "current-etag",
            now=option.expires_at if change == "expired" else now)


@pytest.mark.asyncio
async def test_issuance_rejects_option_whose_command_does_not_match_compared_order():
    public, items, now, option = await preview_fixture()
    with pytest.raises(CommandTargetChangedError):
        issue_route_preview(replace(option, target_position=3), items,
            public_resource_id="my-trip", expected_etag="current-etag", now=now)
    with pytest.raises(CommandTargetChangedError):
        issue_route_preview(replace(option, expires_at=now), items,
            public_resource_id="my-trip", expected_etag="current-etag", now=now)


@pytest.mark.asyncio
async def test_adoption_uses_complete_current_order_and_only_moves_compared_visits():
    public, items, now, _ = await preview_fixture()
    result = (await build_demo_pipeline().run(DEMO_SOURCE_TEXT)).public_result
    prototype = result.days[0].activities[0]
    result.days[0].activities = [prototype.model_copy(update={
        "activity_token": v.stop.activity_token, "name": v.stop.name}) for v in items]
    other_days = [day.model_dump() for day in result.days[1:]]
    verified = verify_route_preview(public.change_token, public_resource_id="my-trip",
        expected_etag="current-etag", now=now, current_result=result)
    changed = apply_public_command(result, verified.command)
    assert [c.name for c in changed.result.days[0].activities] == public.after
    assert [day.label for day in result.days[1:]] == [day.label for day in changed.result.days[1:]]
    assert [[c.name for c in day.activities] for day in changed.result.days[1:]] == [
        [c["name"] for c in day["activities"]] for day in other_days]
    assert changed.changed_days == [result.days[0].label]
    assert changed.result.map.status == "NEEDS_UPDATE"
    result.days[0].activities.reverse()
    with pytest.raises(CommandTargetChangedError):
        verify_route_preview(public.change_token, public_resource_id="my-trip",
            expected_etag="current-etag", now=now, current_result=result)


@pytest.mark.asyncio
async def test_shortest_fact_expiry_not_generic_ten_minutes_controls_adoption():
    _, items, now, option = await preview_fixture()
    expiry = now + timedelta(seconds=2)
    public = issue_route_preview(replace(option, expires_at=expiry), items,
        public_resource_id="my-trip", expected_etag="current-etag", now=now)
    verify_route_preview(public.change_token, public_resource_id="my-trip", expected_etag="current-etag",
        now=expiry - timedelta(microseconds=1))
    with pytest.raises(CommandTargetChangedError):
        verify_route_preview(public.change_token, public_resource_id="my-trip", expected_etag="current-etag", now=expiry)
