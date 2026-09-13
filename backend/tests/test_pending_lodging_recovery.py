"""A recoverable hotel is neither a fabricated Day 1 visit nor an overnight booking."""
import json
from datetime import datetime, timezone

import pytest

from app.trip_understanding.candidates import CandidatePlace, GCJ02Position, issue_candidate
from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.lodging_recovery import recovery_binding
from app.trip_understanding.models import ActivityDeleteCommand, ActivityTimeSetCommand, LodgingRecoverCommand, LodgingRecoveryIntent, UndoCommand
from app.trip_understanding.overnight_context import overnight_segments, stay_context_hash
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.repository import _persisted_proposal
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_experience_inference import Client, provider
from tests.test_experience_v3_journey import repository_for
from tests.test_semantic_partial_recovery import activity


SOURCE = "北京三日游。星河酒店，哪晚住尚未确定。\nDay1：故宫博物院。\nDay2：天坛公园。\nDay3：颐和园。"
ROWS = [activity("星河酒店", day=None, category="住宿", city="北京", city_evidence="北京三日游"),
    activity("故宫博物院", category="景点", city="北京", city_evidence="北京三日游"),
    activity("天坛公园", 2, category="景点", city="北京", city_evidence="北京三日游"),
    activity("颐和园", 3, category="景点", city="北京", city_evidence="北京三日游")]


async def pending_output():
    client = Client(json.dumps({"destination": "北京", "activities": ROWS}), json.dumps({"activities": []}))
    output = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run(SOURCE)
    assert len(client.calls) == 2
    return output


async def create_pending(repo, now):
    created = await repo.create_full(owner_user_id="experience-owner", source_text=SOURCE, idempotency_key="pending-import",
        request_hash=canonical_sha256(SOURCE), now=now, retention_days=30)
    job = await repo.claim_next(worker_id="pending-worker", now=now, lease_seconds=60)
    await repo.complete_job(job, await pending_output(), now=now)
    resource = await repo.authorize(created.accepted.public_resource_id, capability_hash=None, user_id="experience-owner", now=now)
    return resource, await repo.get_result(resource)


async def refreshed(repo, resource, now):
    resource = await repo.authorize(resource.public_resource_id, capability_hash=None, user_id="experience-owner", now=now)
    return await repo.get_result(resource)


def recovery_command(resource, stored, now, intent):
    pending = stored.result.pending_lodgings[0]
    place = CandidatePlace(canonical_place_id="amap:controlled-hotel", city="北京", name="星河酒店",
        category="住宿", area_or_address="北京市受控测试地址", position=GCJ02Position(longitude=116.4, latitude=39.91))
    credential = issue_candidate(place, public_resource_id=resource.public_resource_id,
        activity_token=recovery_binding(pending.pending_token, intent), expected_etag=stored.opaque_etag, now=now)
    return LodgingRecoverCommand(command_type="LODGING_RECOVER", pending_token=pending.pending_token,
        candidate_token=credential.candidate_token, intent=intent)


@pytest.mark.asyncio
async def test_pending_hotel_has_no_day_card_or_public_name_but_preserves_coverage_and_evidence():
    output = await pending_output()
    hotel = output.proposal.mentions[0]
    assert hotel.pending_lodging_scope and hotel.day_index is None
    assert not output.activities[0].compiled.eligible_for_place_search
    assert len(output.public_result.pending_lodgings) == 1 and not output.public_result.lodging_constraints
    assert "星河酒店" not in output.public_result.model_dump_json()
    assert "星河酒店" not in json.dumps(_persisted_proposal(output), ensure_ascii=False)
    assert any(claim.quote == "星河酒店" for claim in output.claims)
    coverage = output.public_result.coverage
    assert (coverage.recognized_place_count, coverage.confirmed_place_count, coverage.unresolved_place_count) == (4, 3, 1)
    assert coverage.unclassified_mention_count == 0 and coverage.unprocessed_count == 1 and not coverage.complete


@pytest.mark.asyncio
async def test_only_undated_hotel_still_has_a_recoverable_partial_result_without_a_fake_visit():
    client = Client(json.dumps({"activities": [ROWS[0]]}), json.dumps({"activities": []}))
    result = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run("星河酒店，哪晚住尚未确定。")
    assert len(result.public_result.pending_lodgings) == 1
    assert all(not day.activities for day in result.public_result.days)
    assert not result.public_result.coverage.complete and len(client.calls) == 2


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_pending_names_are_on_demand_and_survive_an_ordinary_edit_and_undo(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource, stored = await create_pending(repo, now)
        service = TripUnderstandingApplicationService(repo)
        assert not (await repo.get_supplementary_view(resource, now=now)).pending_lodgings
        private = await repo.get_supplementary_view(resource, now=now, include_pending_lodgings=True)
        assert [item.name for item in private.pending_lodgings] == ["星河酒店"]
        assert private.pending_lodgings[0].city == "北京"
        command = ActivityTimeSetCommand(command_type="ACTIVITY_TIME_SET", activity_token=stored.result.days[0].activities[0].activity_token, start_time="09:00")
        await service.apply_command(resource, command, expected_etag=stored.opaque_etag, idempotency_key="edit", now=now)
        changed = await refreshed(repo, resource, now)
        assert changed.result.pending_lodgings[0].pending_token != stored.result.pending_lodgings[0].pending_token
        assert changed.result.coverage == stored.result.coverage
        edited_private = await repo.get_supplementary_view(resource, now=now, include_pending_lodgings=True)
        assert [(item.name, item.city) for item in edited_private.pending_lodgings] == [("星河酒店", "北京")]
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=changed.opaque_etag, idempotency_key="undo", now=now)
        undone = await refreshed(repo, resource, now)
        assert len(undone.result.pending_lodgings) == 1
        assert [item.name for item in (await repo.get_supplementary_view(resource, now=now, include_pending_lodgings=True)).pending_lodgings] == ["星河酒店"]
        assert "星河酒店" not in undone.result.model_dump_json()


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("scope", ["WHOLE_TRIP", "NIGHTS", "VISIT_ONLY"])
@pytest.mark.asyncio
async def test_recovery_is_an_atomic_confirmed_constraint_or_explicit_visit_with_replay_and_undo(kind, scope):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource, stored = await create_pending(repo, now)
        service = TripUnderstandingApplicationService(repo)
        intent = LodgingRecoveryIntent(kind=scope, overnight_days=[2] if scope == "NIGHTS" else [],
            day_index=2 if scope == "VISIT_ONLY" else None,
            before_activity_token=stored.result.days[1].activities[0].activity_token if scope == "VISIT_ONLY" else None)
        command = recovery_command(resource, stored, now, intent)
        before_plan, _ = await repo.get_current_place_plan(resource)
        await service.apply_command(resource, command, expected_etag=stored.opaque_etag, idempotency_key="recover", now=now)
        assert (await service.apply_command(resource, command, expected_etag=stored.opaque_etag, idempotency_key="recover", now=now)).replayed
        current = await refreshed(repo, resource, now)
        plan, _ = await repo.get_current_place_plan(resource)
        assert not current.result.pending_lodgings and current.result.coverage.complete
        assert stay_context_hash(plan) != stay_context_hash(before_plan)
        assert not (await repo.get_supplementary_view(resource, now=now, include_pending_lodgings=True)).pending_lodgings
        if scope == "VISIT_ONLY":
            assert current.result.days[1].activities[0].name == "星河酒店"
            assert current.result.days[1].activities[0].lodging_event == "VISIT_ONLY"
            assert not current.result.lodging_constraints and not plan.lodging_constraints
            assert not any(segment.preserved_hotels for segment in overnight_segments(plan))
        else:
            assert not any(card.name == "星河酒店" for day in current.result.days for card in day.activities)
            assert current.result.lodging_constraints[0].overnight_days == ([2] if scope == "NIGHTS" else [1, 2])
            assert plan.lodging_constraints[0].day_index is None
            assert plan.lodging_constraints[0].overnight_days == ([2] if scope == "NIGHTS" else [1, 2])
            assert not any(stop.name == "星河酒店" and not stop.is_stay_anchor for stop in plan.stops)
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=current.opaque_etag, idempotency_key="undo-recovery", now=now)
        undone = await refreshed(repo, resource, now)
        assert len(undone.result.pending_lodgings) == 1 and not undone.result.lodging_constraints
        assert undone.result.coverage == stored.result.coverage


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_source_deletion_prevents_recovery_but_keeps_opaque_pending_and_other_edits(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource, stored = await create_pending(repo, now)
        command = recovery_command(resource, stored, now, LodgingRecoveryIntent(kind="WHOLE_TRIP"))
        service = TripUnderstandingApplicationService(repo)
        await service.delete_source(resource, user_id="experience-owner", idempotency_key="erase", now=now)
        with pytest.raises(CommandTargetChangedError):
            await service.apply_command(resource, command, expected_etag=stored.opaque_etag, idempotency_key="late-recovery", now=now)
        view = await repo.get_supplementary_view(resource, now=now, include_pending_lodgings=True)
        assert view.status == "DELETED" and not view.pending_lodgings
        await service.apply_command(resource, ActivityDeleteCommand(command_type="ACTIVITY_DELETE", activity_token=stored.result.days[0].activities[0].activity_token),
            expected_etag=stored.opaque_etag, idempotency_key="after-erase-edit", now=now)
        current = await refreshed(repo, resource, now)
        assert "星河酒店" not in current.result.model_dump_json()
        assert not (await repo.get_supplementary_view(resource, now=now, include_pending_lodgings=True)).pending_lodgings
