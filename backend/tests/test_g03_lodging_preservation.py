"""Controlled inference plus real repositories: adopting a day edit preserves hotels.

No model/POI/route network calls are made. The preview and adoption use the real
meal-gap rule, transaction, immutable revisions, identity projection and undo.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.trip_understanding.candidates import CandidatePlace, GCJ02Position, issue_candidate
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.lodging_recovery import recovery_binding
from app.trip_understanding.models import LodgingRecoverCommand, LodgingRecoveryIntent, UndoCommand
from app.trip_understanding.overnight_context import overnight_segments
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_experience_inference import Client, provider
from tests.test_experience_v3_journey import repository_for
from tests.test_semantic_partial_recovery import activity


CONFIRMED_HOTEL = "星河酒店"
PENDING_HOTEL = "月影酒店"
OPTIONAL_PLACE = "林间公园"
SOURCE = ("北京三日游。星河酒店和月影酒店，各住哪晚尚未确定。\n"
          "Day1：故宫博物院、景山公园。备选：林间公园。\nDay2：天坛公园。\nDay3：颐和园。")
HOTEL_ID = "amap:controlled-g03-confirmed-hotel"


async def current(repo, resource, now):
    fresh = await repo.authorize(resource.public_resource_id, capability_hash=None, user_id="experience-owner", now=now)
    return await repo.get_result(fresh)


async def create_lodging_trip(repo, now):
    created = await repo.create_full(owner_user_id="experience-owner", source_text=SOURCE,
        idempotency_key="g03-lodging-source", request_hash=canonical_sha256(SOURCE), now=now, retention_days=30)
    job = await repo.claim_next(worker_id="g03-lodging-controlled", now=now, lease_seconds=60)
    rows = [activity(CONFIRMED_HOTEL, day=None, category="住宿"), activity(PENDING_HOTEL, day=None, category="住宿"),
        activity("故宫博物院", category="景点"), activity("景山公园", category="景点"),
        activity(OPTIONAL_PLACE, category="景点", role="OPTIONAL"), activity("天坛公园", 2, category="景点"),
        activity("颐和园", 3, category="景点")]
    client = Client(json.dumps({"destination": "北京", "activities": rows}), json.dumps({"activities": []}))
    output = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run(SOURCE)
    assert len(client.calls) == 2
    assert len(output.public_result.pending_lodgings) == 2
    await repo.complete_job(job, output, now=now)
    resource = await repo.authorize(created.accepted.public_resource_id, capability_hash=None, user_id="experience-owner", now=now)
    stored = await current(repo, resource, now)
    details = await repo.get_supplementary_view(resource, now=now, include_pending_lodgings=True)
    pending = next(item for item in details.pending_lodgings if item.name == CONFIRMED_HOTEL)
    intent = LodgingRecoveryIntent(kind="NIGHTS", overnight_days=[2])
    place = CandidatePlace(canonical_place_id=HOTEL_ID, city="北京", name=CONFIRMED_HOTEL, category="住宿",
        area_or_address="北京市受控酒店地址", position=GCJ02Position(longitude=116.4, latitude=39.91))
    token = issue_candidate(place, public_resource_id=resource.public_resource_id,
        activity_token=recovery_binding(pending.pending_token, intent), expected_etag=stored.opaque_etag, now=now)
    command = LodgingRecoverCommand(command_type="LODGING_RECOVER", pending_token=pending.pending_token,
        candidate_token=token.candidate_token, intent=intent)
    await TripUnderstandingApplicationService(repo).apply_command(resource, command, expected_etag=stored.opaque_etag,
        idempotency_key="confirm-second-night", now=now)
    return resource, job


async def hotel_facts(repo, resource):
    plan, _ = await repo.get_current_place_plan(resource)
    return [(item.name, item.canonical_place_id, item.resolution_status, item.longitude, item.latitude,
        item.overnight_days, item.day_index) for item in plan.lodging_constraints]


async def assert_pending_privacy(repo, resource, kind, lifecycle, now):
    stored = await current(repo, resource, now)
    assert len(stored.result.pending_lodgings) == 1
    assert PENDING_HOTEL not in stored.result.model_dump_json()
    assert not (await repo.get_supplementary_view(resource, now=now)).pending_lodgings
    private = await repo.get_supplementary_view(resource, now=now, include_pending_lodgings=True)
    if lifecycle == "available":
        assert [(item.pending_token, item.name) for item in private.pending_lodgings] == [
            (stored.result.pending_lodgings[0].pending_token, PENDING_HOTEL)]
    else:
        assert private.status == ("DELETED" if lifecycle == "deleted" else "UNAVAILABLE")
        assert not private.pending_lodgings
    if kind == "postgres":
        rows = await repo._pool.fetch("""SELECT a.atomic_place_name,a.public_activity_token,a.role FROM trip_understanding_activities a
            JOIN trip_understandings u ON u.understanding_id=a.understanding_id AND u.current_revision=a.revision
            WHERE a.understanding_id=$1 AND a.atomic_place_name=ANY($2::text[])""", resource.understanding_id,
            [PENDING_HOTEL, OPTIONAL_PLACE])
        if lifecycle == "available":
            pending = next(row for row in rows if row["atomic_place_name"] == PENDING_HOTEL)
            assert pending["public_activity_token"] == stored.result.pending_lodgings[0].pending_token
            assert any(row["atomic_place_name"] == OPTIONAL_PLACE and row["role"] == "OPTIONAL" for row in rows)
        else:
            assert not rows, "Unavailable source names must not be copied into the new revision"


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("lifecycle", ["available", "deleted", "expired"])
@pytest.mark.asyncio
async def test_adopt_unrelated_meal_gap_preserves_lodging_identity_scope_pending_and_undo(kind, lifecycle):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource, job = await create_lodging_trip(repo, now)
        service = TripUnderstandingApplicationService(repo)
        original = await current(repo, resource, now)
        expected = [(CONFIRMED_HOTEL, HOTEL_ID, "AUTO_MATCHED", 116.4, 39.91, [2], None)]
        assert await hotel_facts(repo, resource) == expected
        if lifecycle == "deleted":
            await service.delete_source(resource, user_id="experience-owner", idempotency_key="delete-before-adopt", now=now)
        elif lifecycle == "expired":
            if kind == "postgres":
                await repo._pool.execute("UPDATE trip_understanding_sources SET retention_until=$2 WHERE understanding_id=$1",
                    resource.understanding_id, now - timedelta(seconds=1))
            else:
                repo.source_expiries[job.job_id] = now - timedelta(seconds=1)
        await service.materialize_trip(resource, expected_etag=original.opaque_etag, idempotency_key="materialize-for-adopt", now=now)
        checks = await service.get_trip_checks(resource)
        meal = next(item for item in checks.items if item.can_preview and "用餐" in item.title)
        preview = await service.preview_trip_change(resource, check_token=meal.check_token, idempotency_key="meal-preview", now=now)
        adopted = await service.adopt_trip_change(resource, change_token=preview.preview.change_token,
            expected_etag=original.opaque_etag, idempotency_key="meal-adopt", now=now)
        latest = await current(repo, resource, now)
        assert latest.opaque_etag == adopted.opaque_etag != original.opaque_etag
        assert any(card.category == "用餐安排" for card in latest.result.days[0].activities)
        assert len(latest.result.lodging_constraints) == 1
        assert latest.result.lodging_constraints[0].overnight_days == [2]
        assert not any(card.category == "住宿" for day in latest.result.days for card in day.activities)
        assert await hotel_facts(repo, resource) == expected
        plan, _ = await repo.get_current_place_plan(resource)
        night = next(item for item in overnight_segments(plan) if item.overnight_days == [2])
        assert night.preserved_hotels == [CONFIRMED_HOTEL]
        assert not night.missing_boundaries
        await assert_pending_privacy(repo, resource, kind, lifecycle, now)
        replay = await service.adopt_trip_change(resource, change_token=preview.preview.change_token,
            expected_etag=original.opaque_etag, idempotency_key="meal-adopt", now=now)
        assert replay.replayed and replay.opaque_etag == latest.opaque_etag
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=latest.opaque_etag,
            idempotency_key="undo-unrelated-adopt", now=now)
        undone = await current(repo, resource, now)
        assert [[card.name for card in day.activities] for day in undone.result.days] == [
            [card.name for card in day.activities] for day in original.result.days]
        assert await hotel_facts(repo, resource) == expected
        await assert_pending_privacy(repo, resource, kind, lifecycle, now)
