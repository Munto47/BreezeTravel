from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.audit.models import AuditFinding, AuditSeverity, AuditStatus, EvidenceFreshness
from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.errors import CommandTargetChangedError, ResourceNotReadyError, RevisionConflictError
from app.trip_understanding.g03 import public_checks
from app.trip_understanding.map_render import MapRenderer
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.models import ActivityTimesApplyCommand, UndoCommand
from app.trip_understanding.schedule_checks import ScheduleFeasibilityRule
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_experience_twelve_tasks import TwentyMinuteRoutes
from tests.test_experience_v3_journey import create, repository_for, refresh


def context_for(windows, routes, *, locked=(), suggested=()):
    stops = [SimpleNamespace(stop_id=f"stop-{index}", raw_name=f"活动{index + 1}",
        start_time=start, end_time=end, visit_duration_minutes=duration,
        locked=index in locked, fixed_commitment=False)
        for index, (start, end, duration) in enumerate(windows)]
    facts = [SimpleNamespace(fact_id=f"route-{index}", fact_type="ROUTE_MODE_SET",
        subject_id=f"stop-{index}->stop-{index+1}", value={"selected_duration_minutes": duration},
        freshness_status=EvidenceFreshness.FRESH, valid_until=None) for index, duration in enumerate(routes) if duration is not None]
    return SimpleNamespace(revision=SimpleNamespace(workspace_id="test", revision=1,
        change_summary={"timing_sources": {f"stop-{index}": "SUGGESTED" for index in suggested}},
        days=[SimpleNamespace(day_index=0, stops=stops)]),
        evidence_snapshot=SimpleNamespace(snapshot_id="route-evidence", facts=facts))


def first_conflict(context):
    return next(finding for finding in ScheduleFeasibilityRule().evaluate(context)
        if finding.reason_code == "SCHEDULE_CONFLICT" and finding.affected_stop_ids[0] == "stop-0")


def test_delay_propagates_each_leg_and_absorbs_existing_gaps():
    context = context_for([("10:00", "12:00", 120), ("11:00", "12:00", 60),
        ("12:30", "13:30", 60)], [20, 20])
    finding = first_conflict(context)
    assert finding.repairable
    assert [(change["stop_id"], change["start_time"], change["end_time"]) for change in finding.input_values["shift_changes"]] == [
        ("stop-1", "12:20", "13:20"), ("stop-2", "13:40", "14:40")]
    assert finding.evidence_fact_ids == ["route-0", "route-1"]


def test_absorbing_gap_stops_propagation_before_locked_or_unknown_later_stops():
    context = context_for([("10:00", "12:00", 120), ("11:00", "12:00", 60),
        ("14:00", "15:00", 60), (None, None, None)], [20, 20, None], locked=(2,))
    finding = first_conflict(context)
    assert finding.repairable
    assert [change["stop_id"] for change in finding.input_values["shift_changes"]] == ["stop-1"]


@pytest.mark.parametrize("case", ["missing_route", "stale_route", "locked", "suggested", "unknown_duration", "midnight"])
def test_uncertain_or_protected_downstream_boundary_has_no_automatic_partial_plan(case):
    windows = [("10:00", "12:00", 120), ("11:00", "12:00", 60), ("12:30", "13:30", 60)]
    if case == "unknown_duration":
        windows[1] = ("11:00", None, None)
    if case == "midnight":
        windows = [("22:00", "23:00", 60), ("22:30", "23:50", 80), ("23:55", None, None)]
    context = context_for(windows, [20, None if case == "missing_route" else 20],
        locked=(2,) if case == "locked" else (), suggested=(2,) if case == "suggested" else ())
    if case == "stale_route":
        context.evidence_snapshot.facts[1].freshness_status = EvidenceFreshness.STALE
    finding = first_conflict(context)
    assert not finding.repairable
    assert finding.input_values["shift_changes"] == []


def test_route_staleness_downgrades_only_route_dependent_checks():
    context = context_for([("10:00", "12:00", 120), ("11:00", "12:00", 60)], [20])
    route = first_conflict(context)
    place = AuditFinding(finding_id="place", rule_id="g03.place_readiness", rule_version="1.0",
        status=AuditStatus.UNKNOWN, severity=AuditSeverity.HIGH,
        reason_code="PLACE_CONFIRMATION_REQUIRED", message="地点待确认")
    report = SimpleNamespace(findings=[route, place])
    checks = public_checks(report, context.evidence_snapshot, routes_current=False,
        check_tokens={route.finding_id: "route-check-token-00000001", "place": "place-check-token-00000001"})
    route_view = next(item for item in checks.items if item.depends_on_routes)
    place_view = next(item for item in checks.items if not item.depends_on_routes)
    assert route_view.basis_status == "NEEDS_RECHECK" and route_view.label == "需要确认" and not route_view.can_preview
    assert place_view.basis_status == "CURRENT" and place_view.title == "确认地点"
    assert checks.remaining_must_adjust == 0


def test_historical_uniform_shift_report_remains_readable_but_requires_recheck():
    context = context_for([("10:00", "12:00", 120), ("11:00", "12:00", 60)], [20])
    old = first_conflict(context).model_copy(update={"rule_version": "1.0.0",
        "input_values": {"shift_minutes": 80, "shift_stop_ids": ["stop-1"]}})
    view = public_checks(SimpleNamespace(findings=[old]), context.evidence_snapshot,
        check_tokens={old.finding_id: "historical-check-00000001"})
    assert view.items[0].basis_status == "NEEDS_RECHECK" and not view.items[0].can_preview
    assert old.rule_version == "1.0.0" and "shift_changes" not in old.input_values


async def scheduled_trip(repo, now, *, short_route_ttl=False):
    created = await create(repo, "scheduled-three-stops", now)
    output = await build_demo_pipeline().run(DEMO_SOURCE_TEXT)
    templates = [activity for activity in output.activities
        if activity.compiled.mention.day_index == 1 and activity.compiled.mention.role.value == "PLANNED"]
    cards, activities = [], []
    for index, (start, end, duration) in enumerate([("10:00", "12:00", 120), ("11:00", "12:00", 60), ("12:30", "13:30", 60)]):
        card = output.public_result.days[0].activities[index % 2].model_copy(deep=True)
        card.activity_token = uuid4().hex
        card.start_time, card.end_time, card.visit_duration_minutes = start, end, duration
        card.locked, card.fixed_commitment, card.timing_source = False, False, "USER"
        cards.append(card)
        activity = templates[index % 2].model_copy(deep=True)
        activity.compiled.activity_id = str(uuid4())
        activity.compiled.public_activity_token = card.activity_token
        activity.compiled.mention.sequence_index = index
        activities.append(activity)
    output.public_result.days = [output.public_result.days[0].model_copy(update={"activities": cards})]
    output.activities, output.claims = activities, []
    job = await repo.claim_next(worker_id="schedule", now=now, lease_seconds=60)
    await repo.complete_job(job, output, now=now)
    resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a"*64, now=now)

    class Routes(TwentyMinuteRoutes):
        async def route(self, *args, **kwargs):
            result = await super().route(*args, **kwargs)
            return result.model_copy(update={"expires_at": now + timedelta(seconds=30)}) if short_route_ttl else result

    routes = Routes()
    await MapRenderWorker(repo, renderer=MapRenderer(routes)).run_once("schedule-map", now=now)
    stored = await repo.get_result(resource)
    service = TripUnderstandingApplicationService(repo)
    await service.materialize_trip(resource, expected_etag=stored.opaque_etag, idempotency_key="schedule-check", now=now)
    check = next(item for item in (await repo.get_trip_checks(resource)).items
        if item.title == "这段时间来不及" and cards[0].activity_token in item.affected_activity_tokens)
    return resource, stored, service, check, routes


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_preview_is_read_only_and_adopts_one_atomic_variable_shift_then_undo(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource, before, service, check, routes = await scheduled_trip(repo, now)
        assert check.depends_on_routes and check.basis_status == "CURRENT"
        preview = await service.preview_trip_change(resource, check_token=check.check_token, idempotency_key="preview", now=now)
        assert [(change.after.start_time, change.after.end_time) for change in preview.preview.changes] == [("12:20", "13:20"), ("13:40", "14:40")]
        _, after_preview = await refresh(repo, resource, now)
        assert after_preview.opaque_etag == before.opaque_etag and after_preview.result == before.result
        route_calls = routes.calls
        revision_before = repo.resources[resource.public_resource_id]["current_revision"] if kind == "memory" else await repo._pool.fetchval(
            "SELECT current_revision FROM trip_understandings WHERE understanding_id=$1", resource.understanding_id)
        adopted = await service.adopt_trip_change(resource, change_token=preview.preview.change_token,
            expected_etag=before.opaque_etag, idempotency_key="adopt", now=now)
        resource, changed = await refresh(repo, resource, now)
        revision_after = repo.resources[resource.public_resource_id]["current_revision"] if kind == "memory" else await repo._pool.fetchval(
            "SELECT current_revision FROM trip_understandings WHERE understanding_id=$1", resource.understanding_id)
        assert revision_after == revision_before + 1
        assert [(card.start_time, card.end_time) for card in changed.result.days[0].activities] == [
            ("10:00", "12:00"), ("12:20", "13:20"), ("13:40", "14:40")]
        assert changed.result.map.status == "NEEDS_UPDATE" and routes.calls == route_calls
        assert all(item.basis_status == "NEEDS_RECHECK" and item.label != "必须调整" and not item.can_preview
            for item in adopted.adopted.checks.items if item.depends_on_routes)
        assert (await service.adopt_trip_change(resource, change_token=preview.preview.change_token,
            expected_etag=before.opaque_etag, idempotency_key="adopt", now=now)).replayed
        with pytest.raises((RevisionConflictError, ResourceNotReadyError)):
            await service.adopt_trip_change(resource, change_token=preview.preview.change_token,
                expected_etag=before.opaque_etag, idempotency_key="stale", now=now)
        await service.apply_command(resource, UndoCommand(command_type="UNDO"),
            expected_etag=changed.opaque_etag, idempotency_key="undo-group", now=now)
        _, undone = await refresh(repo, resource, now)
        assert [(card.start_time, card.end_time) for card in undone.result.days[0].activities] == [
            ("10:00", "12:00"), ("11:00", "12:00"), ("12:30", "13:30")]
        assert undone.opaque_etag not in {before.opaque_etag, changed.opaque_etag}
        assert undone.result.map.status == "NEEDS_UPDATE" and routes.calls == route_calls


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_route_expiry_rejects_preview_adoption_even_before_15min_preview_deadline(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource, before, service, check, _routes = await scheduled_trip(repo, now, short_route_ttl=True)
        preview = await service.preview_trip_change(resource, check_token=check.check_token, idempotency_key="preview", now=now)
        for action in ("preview", "adopt"):
            with pytest.raises(ResourceNotReadyError):
                if action == "preview":
                    await service.preview_trip_change(resource, check_token=check.check_token,
                        idempotency_key="expired-route-preview", now=now + timedelta(seconds=31))
                else:
                    await service.adopt_trip_change(resource, change_token=preview.preview.change_token,
                        expected_etag=before.opaque_etag, idempotency_key="expired-route-adopt", now=now + timedelta(seconds=31))
        _, unchanged = await refresh(repo, resource, now)
        assert unchanged.opaque_etag == before.opaque_etag


@pytest.mark.asyncio
async def test_group_timing_command_rejects_all_changes_when_one_target_is_locked():
    output = await build_demo_pipeline().run(DEMO_SOURCE_TEXT)
    current = output.public_result
    for card in current.days[0].activities:
        card.start_time, card.end_time = "10:00", "11:00"
    current.days[0].activities[1].locked = True
    before = current.model_copy(deep=True)
    command = ActivityTimesApplyCommand(command_type="ACTIVITY_TIMES_APPLY", changes=[
        {"activity_token": card.activity_token, "start_time": "11:00", "end_time": "12:00"}
        for card in current.days[0].activities])
    with pytest.raises(CommandTargetChangedError):
        apply_public_command(current, command)
    assert current == before
