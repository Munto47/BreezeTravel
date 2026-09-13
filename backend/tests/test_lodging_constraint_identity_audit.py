"""Independent hotel identity/night boundaries using synthetic source and local DBs."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.trip_understanding.candidates import CandidatePlace, GCJ02Position, issue_candidate
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.lodging_recovery import recovery_binding
from app.trip_understanding.map_render import MapLodgingConstraint, MapRenderPlan, PlanRevisionRef
from app.trip_understanding.models import ActivityDeleteCommand, LodgingRecoverCommand, LodgingRecoveryIntent, MealSlotView, UndoCommand
from app.trip_understanding.overnight_context import overnight_segments
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.stay import StayRecommendationEngine, stay_plan_from_map
from tests.test_experience_inference import Client, provider
from tests.test_experience_v3_journey import repository_for
from tests.test_semantic_partial_recovery import activity
from tests.test_stay_manual_refresh import finish_stay


SOURCE = "北京三日游。星河酒店和月光酒店，具体哪晚住还未确定。\nDay1：故宫博物院。\nDay2：天坛公园。\nDay3：颐和园。"
SHARED_NAME = "同名连锁酒店"
A_ID = "amap:controlled-branch-a"
B_ID = "amap:controlled-branch-b"


async def setup(repo, *, with_meal_slots=False):
    now = datetime.now(timezone.utc)
    created = await repo.create_full(owner_user_id="experience-owner", source_text=SOURCE,
        idempotency_key="identity-import", request_hash=canonical_sha256(SOURCE), now=now, retention_days=30)
    job = await repo.claim_next(worker_id="identity-audit", now=now, lease_seconds=60)
    rows = [activity(name, day=None, category="住宿", city="北京", city_evidence="北京三日游")
        for name in ["星河酒店", "月光酒店"]]
    rows.extend(activity(name, day, category="景点", city="北京", city_evidence="北京三日游")
        for day, name in enumerate(["故宫博物院", "天坛公园", "颐和园"], 1))
    client = Client(json.dumps({"destination": "北京", "activities": rows}), json.dumps({"activities": []}))
    output = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run(SOURCE)
    assert len(client.calls) == 2
    assert len(output.public_result.pending_lodgings) == 2
    assert not output.public_result.lodging_constraints
    assert all(card.category != "住宿" for day in output.public_result.days for card in day.activities)
    if with_meal_slots:
        for index, field in enumerate(["after_activity_token", "before_activity_token"]):
            day = output.public_result.days[index]
            day.meal_slots = [MealSlotView(meal_role="LUNCH", **{field: day.activities[0].activity_token})]
    await repo.complete_job(job, output, now=now)
    return created.accepted.public_resource_id


async def current(repo, public_id):
    resource = await repo.authorize(public_id, capability_hash=None, user_id="experience-owner", now=datetime.now(timezone.utc))
    return resource, await repo.get_result(resource)


async def recover(repo, public_id, place_id, intent, key):
    resource, stored = await current(repo, public_id)
    pending = stored.result.pending_lodgings[0].pending_token
    now = datetime.now(timezone.utc)
    is_branch_a = place_id.removeprefix("amap:") == A_ID.removeprefix("amap:")
    place = CandidatePlace(canonical_place_id=place_id, name=SHARED_NAME, city="北京", category="住宿",
        area_or_address="北京市甲门店地址" if is_branch_a else "北京市乙门店地址",
        position=GCJ02Position(longitude=116.4 if is_branch_a else 116.45, latitude=39.91))
    issued = issue_candidate(place, public_resource_id=public_id, activity_token=recovery_binding(pending, intent),
        expected_etag=stored.opaque_etag, now=now)
    command = LodgingRecoverCommand(command_type="LODGING_RECOVER", pending_token=pending, intent=intent,
        candidate_token=issued.candidate_token)
    await TripUnderstandingApplicationService(repo).apply_command(resource, command, expected_etag=stored.opaque_etag,
        idempotency_key=key, now=now)


async def plan_for(repo, public_id):
    resource, _ = await current(repo, public_id)
    return (await repo.get_current_place_plan(resource))[0]


def anchors_by_id(plan):
    result = {}
    for stop in plan.stops:
        if stop.is_stay_anchor:
            result.setdefault(stop.canonical_place_id, []).append(stop.day_index)
    return {key: sorted(days) for key, days in result.items()}


def assert_split_identity(plan):
    assert {(item.canonical_place_id, tuple(item.overnight_days)) for item in plan.lodging_constraints} == {
        (A_ID, (1,)), (B_ID, (2,))}
    assert all(item.day_index is None for item in plan.lodging_constraints)
    assert anchors_by_id(plan) == {A_ID: [1, 2], B_ID: [2, 3]}
    assert not any(stop.category == "住宿" and not stop.is_stay_anchor for stop in plan.stops)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("second_night", [1, 2])
@pytest.mark.asyncio
async def test_same_display_name_different_id_keeps_nights_or_reports_same_night_conflict(kind, second_night):
    async with repository_for(kind) as repo:
        public_id = await setup(repo)
        await recover(repo, public_id, A_ID, LodgingRecoveryIntent(kind="NIGHTS", overnight_days=[1]), "recover-a")
        await recover(repo, public_id, B_ID, LodgingRecoveryIntent(kind="NIGHTS", overnight_days=[second_night]), "recover-b")
        plan = await plan_for(repo, public_id)
        if second_night == 2:
            assert_split_identity(plan)
        else:
            assert anchors_by_id(plan) == {}
            night = next(segment for segment in overnight_segments(plan) if 1 in segment.overnight_days)
            assert night.uncertain
            assert len(night.preserved_place_ids) == 2
            assert any(item["reason"] == "MULTIPLE_HOTELS" for item in night.missing_boundaries)
            assert {(item.canonical_place_id, tuple(item.overnight_days)) for item in plan.lodging_constraints} == {
                (A_ID, (1,)), (B_ID, (1,))}
            resource, _ = await current(repo, public_id)
            stay_view = await repo.get_stay_view(resource)
            assert stay_view.segments[0].status == "LIMITED"
            assert "多家不同酒店" in stay_view.segments[0].message


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_same_named_visit_only_branch_does_not_gain_an_overnight(kind):
    async with repository_for(kind) as repo:
        public_id = await setup(repo)
        await recover(repo, public_id, A_ID, LodgingRecoveryIntent(kind="NIGHTS", overnight_days=[1]), "recover-stay")
        await recover(repo, public_id, B_ID, LodgingRecoveryIntent(kind="VISIT_ONLY", day_index=3), "recover-visit")
        plan = await plan_for(repo, public_id)
        assert anchors_by_id(plan) == {A_ID: [1, 2]}
        assert [(item.canonical_place_id, item.overnight_days) for item in plan.lodging_constraints] == [(A_ID, [1])]
        visits = [stop for stop in plan.stops if stop.canonical_place_id == B_ID]
        assert len(visits) == 1 and visits[0].day_index == 3 and visits[0].lodging_event == "VISIT_ONLY"
        assert not visits[0].is_stay_anchor
        night_two = next(segment for segment in overnight_segments(plan) if 2 in segment.overnight_days)
        assert not night_two.preserved_hotels and not night_two.pending_lodging_roles


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("second_id", [A_ID, A_ID.removeprefix("amap:")])
@pytest.mark.asyncio
async def test_same_actual_hotel_id_repeated_for_one_night_has_one_pair_of_map_anchors(kind, second_id):
    async with repository_for(kind) as repo:
        public_id = await setup(repo)
        await recover(repo, public_id, A_ID, LodgingRecoveryIntent(kind="NIGHTS", overnight_days=[1]), "recover-first-evidence")
        await recover(repo, public_id, second_id, LodgingRecoveryIntent(kind="NIGHTS", overnight_days=[1]), "recover-second-evidence")
        plan = await plan_for(repo, public_id)
        anchors = [stop for stop in plan.stops if stop.is_stay_anchor]
        assert [(stop.canonical_place_id.removeprefix("amap:"), stop.day_index) for stop in anchors] == [
            (A_ID.removeprefix("amap:"), 1), (A_ID.removeprefix("amap:"), 2)]
        night = next(segment for segment in overnight_segments(plan) if 1 in segment.overnight_days)
        assert not night.uncertain and len(night.preserved_place_ids) == 1


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_source_delete_and_undo_preserve_current_confirmed_branch_identities(kind):
    async with repository_for(kind) as repo:
        public_id = await setup(repo)
        await recover(repo, public_id, A_ID, LodgingRecoveryIntent(kind="NIGHTS", overnight_days=[1]), "recover-a")
        await recover(repo, public_id, B_ID, LodgingRecoveryIntent(kind="NIGHTS", overnight_days=[2]), "recover-b")
        resource, stored = await current(repo, public_id)
        service = TripUnderstandingApplicationService(repo)
        await service.delete_source(resource, user_id="experience-owner", idempotency_key="erase-source", now=datetime.now(timezone.utc))
        assert_split_identity(await plan_for(repo, public_id))
        # Delete only branch B, then undo that edit after source erasure.
        b_token = next(item.activity_token for item in stored.result.lodging_constraints if item.overnight_days == [2])
        await service.apply_command(resource, ActivityDeleteCommand(command_type="ACTIVITY_DELETE", activity_token=b_token),
            expected_etag=stored.opaque_etag, idempotency_key="delete-b", now=datetime.now(timezone.utc))
        assert anchors_by_id(await plan_for(repo, public_id)) == {A_ID: [1, 2]}
        resource, after = await current(repo, public_id)
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=after.opaque_etag,
            idempotency_key="undo-delete-b", now=datetime.now(timezone.utc))
        assert_split_identity(await plan_for(repo, public_id))
        private = await repo.get_supplementary_view(resource, now=datetime.now(timezone.utc), include_pending_lodgings=True)
        assert private.status == "DELETED" and not private.pending_lodgings


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_undo_last_recovery_after_source_delete_cannot_leave_the_removed_branch_on_map(kind):
    async with repository_for(kind) as repo:
        public_id = await setup(repo)
        await recover(repo, public_id, A_ID, LodgingRecoveryIntent(kind="NIGHTS", overnight_days=[1]), "recover-a")
        await recover(repo, public_id, B_ID, LodgingRecoveryIntent(kind="NIGHTS", overnight_days=[2]), "recover-b")
        resource, stored = await current(repo, public_id)
        service = TripUnderstandingApplicationService(repo)
        await service.delete_source(resource, user_id="experience-owner", idempotency_key="erase-before-undo", now=datetime.now(timezone.utc))
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=stored.opaque_etag,
            idempotency_key="undo-recovery-b", now=datetime.now(timezone.utc))
        plan = await plan_for(repo, public_id)
        assert anchors_by_id(plan) == {A_ID: [1, 2]}
        assert {item.canonical_place_id for item in plan.lodging_constraints} == {A_ID}
        assert not any(stop.canonical_place_id == B_ID for stop in plan.stops)
        private = await repo.get_supplementary_view(resource, now=datetime.now(timezone.utc), include_pending_lodgings=True)
        assert private.status == "DELETED" and not private.pending_lodgings


@pytest.mark.asyncio
async def test_missing_visit_boundaries_cannot_hide_a_confirmed_multi_hotel_conflict_as_preserved():
    constraints = [MapLodgingConstraint(name=SHARED_NAME, category="住宿", city="北京", lodging_event="OVERNIGHT",
        canonical_place_id=place_id, resolution_status="AUTO_MATCHED", longitude=116.4 + index * 0.05,
        latitude=39.91, overnight_days=[1]) for index, place_id in enumerate([A_ID, B_ID])]
    plan = MapRenderPlan(understanding_id="missing-boundaries-audit", day_count=2, stops=[], lodging_constraints=constraints,
        plan_ref=PlanRevisionRef(kind="UNDERSTANDING", aggregate_id="missing-boundaries-audit", revision=3, stop_set_hash="a" * 64),
        route_config_hash="b" * 64)

    class NoCandidateCalls:
        async def search(self, **kwargs):
            raise AssertionError("conflicting source hotels must be resolved before any recommendation search")

    output = await StayRecommendationEngine(candidate_provider=NoCandidateCalls()).recommend(stay_plan_from_map(plan))
    segment = output.provider_binding["segments"][0]
    assert segment["status"] == "LIMITED"
    assert "多家不同酒店" in segment["message"]


async def current_lodging_identity(repo, kind, public_id):
    resource, stored = await current(repo, public_id)
    assert len(stored.result.lodging_constraints) == 1
    card = stored.result.lodging_constraints[0]
    if kind == "postgres":
        binding = await repo._pool.fetchrow("""SELECT a.canonical_place_id, a.resolution_status,
            a.resolver_receipt_json FROM trip_understanding_activities a
            JOIN trip_understandings u ON u.understanding_id=a.understanding_id
              AND u.current_revision=a.revision
            WHERE a.understanding_id=$1 AND a.public_activity_token=$2""",
            resource.understanding_id, card.activity_token)
        assert binding is not None, "public lodging token must still bind the current database identity"
        receipt = binding["resolver_receipt_json"]
        receipt = json.loads(receipt) if isinstance(receipt, str) else receipt
    else:
        revision = repo.resources[public_id]["current_revision"]
        binding = repo.g03_pipeline_inputs[(resource.understanding_id, revision)]["bindings"].get(card.activity_token)
        assert binding is not None, "public lodging token must still bind the current memory identity"
        receipt = binding["resolver_receipt"]
    plan = await plan_for(repo, public_id)
    constraint = plan.lodging_constraints[0]
    assert constraint.canonical_place_id == A_ID
    assert constraint.resolution_status == "AUTO_MATCHED"
    assert constraint.longitude == 116.4 and constraint.latitude == 39.91
    assert constraint.overnight_days == [2] and constraint.day_index is None
    assert anchors_by_id(plan)[A_ID] == [2, 3]
    return {
        "card": card.model_dump(exclude={"activity_token"}),
        "canonical_place_id": binding["canonical_place_id"],
        "resolution_status": binding["resolution_status"],
        "receipt": receipt,
        "constraint": constraint.model_dump(exclude={"activity_token"}),
    }


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("source_state", ["kept", "deleted", "expired"])
@pytest.mark.asyncio
async def test_select_other_night_and_undo_keep_recovered_hotel_identity(kind, source_state):
    """A recommendation edit cannot detach an independent source hotel's identity."""
    async with repository_for(kind) as repo:
        public_id = await setup(repo, with_meal_slots=True)
        await finish_stay(repo, datetime.now(timezone.utc))
        await recover(repo, public_id, A_ID, LodgingRecoveryIntent(kind="NIGHTS", overnight_days=[2]), "recover-night-two")
        service = TripUnderstandingApplicationService(repo)
        resource, stored = await current(repo, public_id)
        if source_state == "deleted":
            await service.delete_source(resource, user_id="experience-owner", idempotency_key="erase-before-select",
                now=datetime.now(timezone.utc))
        elif source_state == "expired":
            expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            if kind == "postgres":
                await repo._pool.execute("UPDATE trip_understanding_sources SET retention_until=$2 WHERE understanding_id=$1",
                    resource.understanding_id, expired_at)
            else:
                for job_id, item in repo.jobs.items():
                    if item["understanding_id"] == resource.understanding_id:
                        repo.source_expiries[job_id] = expired_at
        before = await current_lodging_identity(repo, kind, public_id)
        original_days = [[card.model_dump(exclude={"activity_token"}) for card in day.activities] for day in stored.result.days]
        await repo.refresh_stay_suggestions(resource, expected_etag=stored.opaque_etag, idempotency_key="night-one-refresh",
            now=datetime.now(timezone.utc))
        await finish_stay(repo, datetime.now(timezone.utc))
        resource, stored = await current(repo, public_id)
        stay = await repo.get_stay_view(resource)
        segment = next(item for item in stay.segments if item.overnight_days == ["Day 1"])
        assert segment.candidates
        await service.select_stay(resource, candidate_token=segment.candidates[0].candidate_token,
            expected_etag=stored.opaque_etag, idempotency_key="select-night-one", now=datetime.now(timezone.utc))
        assert await current_lodging_identity(repo, kind, public_id) == before
        resource, selected = await current(repo, public_id)
        assert selected.result.days[0].meal_slots[0].after_activity_token == selected.result.days[0].activities[0].activity_token
        assert selected.result.days[1].meal_slots[0].before_activity_token == selected.result.days[1].activities[0].activity_token
        selected_view = await repo.get_stay_view(resource)
        assert any(candidate.selected for item in selected_view.segments if item.overnight_days == ["Day 1"]
            for candidate in item.candidates)
        private = await repo.get_supplementary_view(resource, now=datetime.now(timezone.utc), include_pending_lodgings=True)
        if source_state != "kept":
            assert private.status == ("DELETED" if source_state == "deleted" else "UNAVAILABLE") and not private.pending_lodgings
            if kind == "postgres":
                assert await repo._pool.fetchval("""SELECT count(*) FROM trip_understanding_activities a
                    JOIN trip_understandings u ON u.understanding_id=a.understanding_id AND u.current_revision=a.revision
                    WHERE a.understanding_id=$1 AND a.atomic_place_name=$2""", resource.understanding_id, "月光酒店") == 0
            else:
                revision = repo.resources[public_id]["current_revision"]
                assert not repo.g03_pipeline_inputs[(resource.understanding_id, revision)].get("pending_lodgings")
        else:
            assert len(private.pending_lodgings) == 1
            assert private.pending_lodgings[0].pending_token == selected.result.pending_lodgings[0].pending_token
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=selected.opaque_etag,
            idempotency_key="undo-night-one-selection", now=datetime.now(timezone.utc))
        assert await current_lodging_identity(repo, kind, public_id) == before
        resource, undone = await current(repo, public_id)
        assert [[card.model_dump(exclude={"activity_token"}) for card in day.activities] for day in undone.result.days] == original_days
        assert undone.result.days[0].meal_slots[0].after_activity_token == undone.result.days[0].activities[0].activity_token
        assert undone.result.days[1].meal_slots[0].before_activity_token == undone.result.days[1].activities[0].activity_token
        assert anchors_by_id(await plan_for(repo, public_id)) == {A_ID: [2, 3]}
        assert not any(candidate.selected for item in (await repo.get_stay_view(resource)).segments for candidate in item.candidates)
        private = await repo.get_supplementary_view(resource, now=datetime.now(timezone.utc), include_pending_lodgings=True)
        if source_state != "kept":
            assert private.status == ("DELETED" if source_state == "deleted" else "UNAVAILABLE") and not private.pending_lodgings
        else:
            assert len(private.pending_lodgings) == 1
