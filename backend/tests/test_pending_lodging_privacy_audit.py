"""Independent source-erasure and candidate-scope checks for hotel recovery.

Synthetic input and controlled inference only; no external model or map calls.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.trip_understanding.candidates import CandidatePlace, GCJ02Position, issue_candidate
from app.trip_understanding.dining import verify_command_candidate
from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.lodging_recovery import recovery_binding
from app.trip_understanding.map_render import MapLodgingConstraint, MapRenderPlan, MapStop, PlanRevisionRef
from app.trip_understanding.map_repository import plan_with_source_lodging
from app.trip_understanding.models import ActivityTimeSetCommand, LodgingRecoverCommand, LodgingRecoveryIntent, UndoCommand
from app.trip_understanding.overnight_context import overnight_segments
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_experience_inference import Client, provider
from tests.test_experience_v3_journey import repository_for
from tests.test_semantic_partial_recovery import activity


SOURCE = "北京游。星河酒店，哪晚住尚未确定。\nDay1：月光桥。\nDay2：晨光湖。\nDay3：晚霞公园。"
HOTEL = "星河酒店"
NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def hotel_candidate():
    return CandidatePlace(canonical_place_id="amap:controlled-hotel-audit", city="北京", name=HOTEL,
        category="住宿", area_or_address="北京测试地址", position=GCJ02Position(longitude=116.4, latitude=39.9))


async def ready(repo):
    now = datetime.now(timezone.utc)
    created = await repo.create_full(owner_user_id="experience-owner", source_text=SOURCE,
        idempotency_key="pending-privacy", request_hash=canonical_sha256(SOURCE), now=now, retention_days=30)
    job = await repo.claim_next(worker_id="pending-privacy", now=now, lease_seconds=60)
    draft = {"destination": "北京", "activities": [activity(HOTEL, day=None, category="住宿"),
        activity("月光桥"), activity("晨光湖", 2), activity("晚霞公园", 3)]}
    client = Client(json.dumps(draft), json.dumps({"activities": []}))
    output = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run(SOURCE)
    assert len(client.calls) == 2
    assert len(output.public_result.pending_lodgings) == 1
    assert HOTEL not in output.public_result.model_dump_json()
    assert len(output.public_result.days) == 3
    assert all(card.category != "住宿" for day in output.public_result.days for card in day.activities)
    await repo.complete_job(job, output, now=now)
    resource = await repo.authorize(created.accepted.public_resource_id, capability_hash=None,
        user_id="experience-owner", now=now)
    return resource, job, now


def recover_command(resource, stored, now, intent=None):
    intent = intent or LodgingRecoveryIntent(kind="NIGHTS", overnight_days=[1])
    pending = stored.result.pending_lodgings[0].pending_token
    token = issue_candidate(hotel_candidate(), public_resource_id=resource.public_resource_id,
        activity_token=recovery_binding(pending, intent), expected_etag=stored.opaque_etag, now=now)
    return LodgingRecoverCommand(command_type="LODGING_RECOVER", pending_token=pending,
        candidate_token=token.candidate_token, intent=intent)


async def current_result(repo, resource):
    current = await repo.authorize(resource.public_resource_id, capability_hash=None,
        user_id="experience-owner", now=datetime.now(timezone.utc))
    return await repo.get_result(current)


async def edit_first(repo, resource, now, key="ordinary-edit"):
    stored = await current_result(repo, resource)
    command = ActivityTimeSetCommand(command_type="ACTIVITY_TIME_SET",
        activity_token=stored.result.days[0].activities[0].activity_token, start_time="09:15")
    await TripUnderstandingApplicationService(repo).apply_command(resource, command,
        expected_etag=stored.opaque_etag, idempotency_key=key, now=now)


async def assert_no_pending_names(repo, resource, kind):
    view = await repo.get_supplementary_view(resource, now=datetime.now(timezone.utc), include_pending_lodgings=True)
    assert not view.pending_lodgings
    if kind == "postgres":
        assert await repo._pool.fetchval("""SELECT count(*) FROM trip_understanding_activities
            WHERE understanding_id=$1 AND day_index IS NULL AND canonical_place_id IS NULL
              AND (atomic_place_name=$2 OR mention_text LIKE '%' || $2 || '%')""", resource.understanding_id, HOTEL) == 0
    else:
        assert all(not data.get("pending_lodgings") for (owner, _), data in repo.g03_pipeline_inputs.items()
            if owner == resource.understanding_id)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_deleted_pending_source_cannot_return_through_undo_or_old_candidate(kind):
    async with repository_for(kind) as repo:
        resource, _, now = await ready(repo)
        original = await current_result(repo, resource)
        default_view = await repo.get_supplementary_view(resource, now=now)
        assert not default_view.pending_lodgings
        private = await repo.get_supplementary_view(resource, now=now, include_pending_lodgings=True)
        assert [item.name for item in private.pending_lodgings] == [HOTEL]
        await edit_first(repo, resource, now)
        stored = await current_result(repo, resource)
        assert stored.result.pending_lodgings[0].pending_token != original.result.pending_lodgings[0].pending_token
        command = recover_command(resource, stored, now)
        await repo.delete_source(resource, user_id="experience-owner", idempotency_key="delete-pending-source",
            request_hash=canonical_sha256("delete-pending-source"), now=now)
        service = TripUnderstandingApplicationService(repo)
        with pytest.raises(CommandTargetChangedError):
            await service.apply_command(resource, command, expected_etag=stored.opaque_etag,
                idempotency_key="late-recovery", now=now)
        assert (await current_result(repo, resource)).opaque_etag == stored.opaque_etag
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=stored.opaque_etag,
            idempotency_key="undo-after-delete", now=now)
        after = await current_result(repo, resource)
        assert HOTEL not in after.result.model_dump_json()
        await assert_no_pending_names(repo, resource, kind)
        # A historical anonymous reference is not permission to issue a fresh recovery.
        forged_current = recover_command(resource, after, now)
        with pytest.raises(CommandTargetChangedError):
            await service.apply_command(resource, forged_current, expected_etag=after.opaque_etag,
                idempotency_key="recover-after-undo", now=now)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_confirmed_lodging_facts_survive_erasure_but_undo_never_restores_private_name(kind):
    async with repository_for(kind) as repo:
        resource, _, now = await ready(repo)
        stored = await current_result(repo, resource)
        service = TripUnderstandingApplicationService(repo)
        command = recover_command(resource, stored, now, LodgingRecoveryIntent(kind="WHOLE_TRIP"))
        await service.apply_command(resource, command, expected_etag=stored.opaque_etag,
            idempotency_key="recover-before-delete", now=now)
        recovered = await current_result(repo, resource)
        assert not recovered.result.pending_lodgings
        assert recovered.result.lodging_constraints[0].overnight_days == [1, 2]
        assert all(card.category != "住宿" for day in recovered.result.days for card in day.activities)
        await repo.delete_source(resource, user_id="experience-owner", idempotency_key="delete-recovered-source",
            request_hash=canonical_sha256("delete-recovered-source"), now=now)
        assert (await current_result(repo, resource)).result.lodging_constraints[0].name == HOTEL
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=recovered.opaque_etag,
            idempotency_key="undo-recovery-after-delete", now=now)
        undone = await current_result(repo, resource)
        assert not undone.result.lodging_constraints
        assert HOTEL not in undone.result.model_dump_json()
        await assert_no_pending_names(repo, resource, kind)


@pytest.mark.parametrize("change", ["purpose", "night", "day", "position", "pending", "resource", "etag", "expired"])
def test_recovery_candidate_is_bound_to_every_user_choice(change):
    intent = LodgingRecoveryIntent(kind="VISIT_ONLY", day_index=1, before_activity_token="b" * 24)
    if change in {"purpose", "night"}:
        intent = LodgingRecoveryIntent(kind="NIGHTS", overnight_days=[1])
    issued = issue_candidate(hotel_candidate(), public_resource_id="resource-a", activity_token=recovery_binding("p" * 24, intent),
        expected_etag="etag-a", now=NOW)
    values = intent.model_dump()
    if change == "purpose":
        values = {"kind": "WHOLE_TRIP"}
    elif change == "night":
        values["overnight_days"] = [2]
    elif change == "day":
        values["day_index"] = 2
    elif change == "position":
        values["before_activity_token"] = None
    command = LodgingRecoverCommand(command_type="LODGING_RECOVER", pending_token=("q" if change == "pending" else "p") * 24,
        candidate_token=issued.candidate_token, intent=LodgingRecoveryIntent(**values))
    with pytest.raises(CommandTargetChangedError):
        verify_command_candidate(command, public_resource_id="resource-b" if change == "resource" else "resource-a",
            expected_etag="etag-b" if change == "etag" else "etag-a", now=NOW + timedelta(minutes=11) if change == "expired" else NOW)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_expired_source_is_unavailable_before_cleanup_and_not_copied_by_edit(kind):
    async with repository_for(kind) as repo:
        resource, job, now = await ready(repo)
        stored = await current_result(repo, resource)
        command = recover_command(resource, stored, now)
        if kind == "postgres":
            await repo._pool.execute("UPDATE trip_understanding_sources SET retention_until=$2 WHERE understanding_id=$1",
                resource.understanding_id, now - timedelta(seconds=1))
        else:
            repo.source_expiries[job.job_id] = now - timedelta(seconds=1)
        private = await repo.get_supplementary_view(resource, now=now, include_pending_lodgings=True)
        assert private.status == "UNAVAILABLE" and not private.pending_lodgings
        with pytest.raises(CommandTargetChangedError):
            await TripUnderstandingApplicationService(repo).apply_command(resource, command,
                expected_etag=stored.opaque_etag, idempotency_key="recover-after-expiry", now=now)
        await edit_first(repo, resource, now)
        current = await current_result(repo, resource)
        assert HOTEL not in current.result.model_dump_json()
        if kind == "postgres":
            assert await repo._pool.fetchval("""SELECT count(*) FROM trip_understanding_activities a
                JOIN trip_understandings u ON u.understanding_id=a.understanding_id AND u.current_revision=a.revision
                WHERE a.understanding_id=$1 AND a.day_index IS NULL AND a.atomic_place_name=$2""", resource.understanding_id, HOTEL) == 0
        else:
            revision = repo.resources[resource.public_resource_id]["current_revision"]
            assert not repo.g03_pipeline_inputs[(resource.understanding_id, revision)].get("pending_lodgings")
        await repo.purge_expired_private_data(now=now, limit=100)
        await assert_no_pending_names(repo, resource, kind)


def boundary_plan(cities):
    stops = [MapStop(day_index=index, day_label=f"Day {index}", sequence_index=0, name=f"测试景点{index}",
        category="景点", city=city, canonical_place_id=f"controlled:{index}", resolution_status="AUTO_MATCHED",
        longitude=116.4 if city == "北京" else 121.4, latitude=39.9 if city == "北京" else 31.2)
        for index, city in enumerate(cities, 1)]
    return MapRenderPlan(understanding_id="controlled-boundaries", day_count=len(cities),
        plan_ref=PlanRevisionRef(kind="UNDERSTANDING", aggregate_id="controlled-boundaries", revision=3, stop_set_hash="a" * 64),
        route_config_hash="b" * 64, stops=stops)


def test_cross_city_explicit_nights_are_preserved_without_fabricated_cross_city_hotel_legs():
    plan = boundary_plan(["北京", "上海", "上海"])
    plan.lodging_constraints = [MapLodgingConstraint(name=HOTEL, category="住宿", city="北京", lodging_event="OVERNIGHT",
        canonical_place_id="controlled:hotel", resolution_status="AUTO_MATCHED", longitude=116.4, latitude=39.9, overnight_days=[1, 2])]
    mapped = plan_with_source_lodging(plan)
    assert mapped.lodging_constraints[0].day_index is None
    assert mapped.lodging_constraints[0].overnight_days == [1, 2]
    segments = overnight_segments(mapped)
    assert [segment.overnight_days for segment in segments] == [[1], [2]]
    assert all(segment.preserved_hotels == [HOTEL] and segment.uncertain for segment in segments)
    assert any(issue["reason"] == "DIFFERENT_CITY" for segment in segments for issue in segment.missing_boundaries)
    assert not any(stop.is_stay_anchor for stop in mapped.stops)


@pytest.mark.parametrize("day", [1, 2, 3])
def test_visit_only_at_a_day_boundary_is_a_visit_and_never_reserves_either_night(day):
    plan = boundary_plan(["北京", "北京", "北京"])
    plan.stops[day - 1] = plan.stops[day - 1].model_copy(update={"name": HOTEL, "category": "住宿", "lodging_event": "VISIT_ONLY"})
    mapped = plan_with_source_lodging(plan)
    assert any(stop.name == HOTEL and stop.day_index == day and not stop.is_stay_anchor for stop in mapped.stops)
    assert not mapped.lodging_constraints and not any(stop.is_stay_anchor for stop in mapped.stops)
    segments = overnight_segments(mapped)
    assert all(not segment.preserved_hotels and not segment.pending_lodging_roles for segment in segments)
    assert [night for segment in segments for night in segment.overnight_days] == [1, 2]


class PausedActivityRead:
    """Pause after a command has fetched source-bearing rows, without fake DB results."""
    def __init__(self, pool):
        self.pool = pool
        self.read = asyncio.Event()
        self.resume = asyncio.Event()
        self.did_pause = False

    def __getattr__(self, key):
        return getattr(self.pool, key)

    def acquire(self, *args, **kwargs):
        parent = self
        context = self.pool.acquire(*args, **kwargs)

        class Connection:
            def __getattr__(self, key):
                return getattr(self.conn, key)

            async def fetch(self, query, *params, **options):
                rows = await self.conn.fetch(query, *params, **options)
                if (not parent.did_pause and "SELECT * FROM trip_understanding_activities" in query
                        and "ORDER BY day_index NULLS LAST, sequence_index, activity_id" in query):
                    parent.did_pause = True
                    parent.read.set()
                    await parent.resume.wait()
                return rows

            async def __aenter__(self):
                self.conn = await context.__aenter__()
                return self

            async def __aexit__(self, *details):
                return await context.__aexit__(*details)

        return Connection()


@pytest.mark.asyncio
async def test_ttl_purge_cannot_be_undone_by_inflight_pending_source_copy():
    async with repository_for("postgres") as repo:
        resource, _, now = await ready(repo)
        await repo._pool.execute("UPDATE trip_understanding_sources SET retention_until=$2 WHERE understanding_id=$1",
            resource.understanding_id, now + timedelta(seconds=10))
        original_pool = repo._pool
        paused = PausedActivityRead(original_pool)
        repo._pool = paused
        edit = asyncio.create_task(edit_first(repo, resource, now, "edit-across-expiry"))
        try:
            await asyncio.wait_for(paused.read.wait(), timeout=10)
            # Maintenance sees expiry while the earlier edit is still in flight.
            cleanup = await asyncio.wait_for(repo.purge_expired_private_data(now=now + timedelta(seconds=20), limit=100), timeout=10)
            paused.resume.set()
            await asyncio.wait_for(edit, timeout=10)
            # Correct locking may defer maintenance. Run that deferred pass after the edit.
            if not cleanup["sources_purged"]:
                await repo.purge_expired_private_data(now=now + timedelta(seconds=20), limit=100)
            await assert_no_pending_names(repo, resource, "postgres")
        finally:
            paused.resume.set()
            if not edit.done():
                edit.cancel()
            await asyncio.gather(edit, return_exceptions=True)
            repo._pool = original_pool


@pytest.mark.asyncio
async def test_recovery_candidate_expiring_while_waiting_for_write_lock_is_rejected():
    async with repository_for("postgres") as repo:
        resource, _, _ = await ready(repo)
        stored = await current_result(repo, resource)
        now = datetime.now(timezone.utc)
        intent = LodgingRecoveryIntent(kind="NIGHTS", overnight_days=[1])
        pending = stored.result.pending_lodgings[0].pending_token
        credential = issue_candidate(hotel_candidate(), public_resource_id=resource.public_resource_id,
            activity_token=recovery_binding(pending, intent), expected_etag=stored.opaque_etag,
            now=now, expires_at=now + timedelta(milliseconds=300))
        command = LodgingRecoverCommand(command_type="LODGING_RECOVER", pending_token=pending,
            candidate_token=credential.candidate_token, intent=intent)
        async with repo._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("SELECT understanding_id FROM trip_understandings WHERE understanding_id=$1 FOR UPDATE",
                    resource.understanding_id)
                task = asyncio.create_task(TripUnderstandingApplicationService(repo).apply_command(resource, command,
                    expected_etag=stored.opaque_etag, idempotency_key="expire-in-lock-queue", now=now))
                # The request began with a valid token; a different writer holds the lock until expiry.
                await asyncio.sleep(0.4)
                assert not task.done()
        try:
            with pytest.raises(CommandTargetChangedError):
                await asyncio.wait_for(task, timeout=10)
            assert (await current_result(repo, resource)).opaque_etag == stored.opaque_etag
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
