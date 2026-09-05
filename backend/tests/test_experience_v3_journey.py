from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.trip_understanding.anonymous import AnonymousDailyLimitError
from app.trip_understanding.candidates import CandidatePlace, GCJ02Position, issue_candidate
from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.errors import CommandTargetChangedError, ConcurrentJobLimitError, ResourceNotReadyError, RevisionConflictError
from app.trip_understanding.map_render import MapRenderer
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.models import ActivityTimeSetCommand, AssumptionSetCommand, PlaceConfirmCommand, UndoCommand
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository, PostgresTripUnderstandingRepository
from app.trip_understanding.route_geometry import InMemoryRouteGeometryCache
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.source_crypto import SourceCipher
from app.audit.repositories import PostgresAuditRepository
from app.trip_understanding.g03_repository import _stable_stop_id
from app.trip_understanding.map_render import InternalRouteModeFact


@asynccontextmanager
async def repository_for(kind):
    if kind == "memory":
        yield InMemoryTripUnderstandingRepository()
        return
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("controlled PostgreSQL integration not enabled")
    dsn = os.getenv("TEST_DATABASE_ADMIN_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")
    name = "experience_test_" + uuid4().hex[:12]
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        pool = await asyncpg.create_pool(dsn.rsplit("/", 1)[0] + "/" + name, min_size=1, max_size=3)
        async with pool.acquire() as conn:
            await conn.execute(Path("app/db/init.sql").read_text(encoding="utf-8"))
            for migration in sorted(Path("app/db/migrations").glob("*.sql")):
                await conn.execute(migration.read_text(encoding="utf-8"))
            await conn.execute("INSERT INTO users (user_id,nickname) VALUES ('experience-owner','Owner')")
        yield PostgresTripUnderstandingRepository(pool, SourceCipher("experience-controlled-test-secret"), InMemoryRouteGeometryCache())
    finally:
        if pool:
            await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def create(repo, key, now):
    return await repo.create_demo(capability_hash="a" * 64, source_text=DEMO_SOURCE_TEXT,
        idempotency_key=key, request_hash=canonical_sha256({"mode": "FULL", "text": DEMO_SOURCE_TEXT}), now=now, ttl_hours=24)


async def finish(repo, created, now):
    job = await repo.claim_next(worker_id="experience-test", now=now, lease_seconds=60)
    await repo.complete_job(job, await build_demo_pipeline().run(DEMO_SOURCE_TEXT), now=now)
    return await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)


async def refresh(repo, resource, now):
    resource = await repo.authorize(resource.public_resource_id, capability_hash="a" * 64, now=now)
    return resource, await repo.get_result(resource)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_anonymous_quota_claim_preserves_other_drafts(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        first = await create(repo, "first", now)
        assert (await create(repo, "first", now)).replayed
        with pytest.raises(ConcurrentJobLimitError):
            await create(repo, "concurrent", now)
        first_resource = await finish(repo, first, now)
        first_stored = await repo.get_result(first_resource)
        assert first_stored.result.ownership == "ANONYMOUS"
        assert first_stored.result.expires_at <= now + timedelta(hours=24)
        second = await create(repo, "second", now)
        second_resource = await finish(repo, second, now)
        claimed = await repo.claim_demo(first_resource.public_resource_id, capability_hash="a" * 64,
            user_id="experience-owner", idempotency_key="claim", request_hash="b" * 64, now=now, retention_days=30)
        # The same anonymous session still authorizes the other draft.
        assert await repo.authorize(second_resource.public_resource_id, capability_hash="a" * 64, now=now)
        owned = await repo.authorize(claimed.claimed.public_resource_id, capability_hash=None, user_id="experience-owner", now=now)
        assert (await repo.get_result(owned)).result.ownership == "ACCOUNT"
        third = await create(repo, "third", now)
        await finish(repo, third, now)
        with pytest.raises(AnonymousDailyLimitError):
            await create(repo, "fourth", now)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_candidate_bound_confirmation_timing_undo_and_map_points(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "candidate", now), now)
        service = TripUnderstandingApplicationService(repo)
        stored = await repo.get_result(resource)
        card = stored.result.days[0].activities[0]
        place = CandidatePlace(canonical_place_id="amap:real-test-poi", city="北京", name="故宫博物院",
            category="景点", area_or_address="北京市东城区景山前街4号", position=GCJ02Position(longitude=116.397, latitude=39.918))
        candidate = issue_candidate(place, public_resource_id=resource.public_resource_id,
            activity_token=card.activity_token, expected_etag=stored.opaque_etag, now=now)
        wrong = PlaceConfirmCommand(command_type="PLACE_CONFIRM", activity_token=stored.result.days[0].activities[1].activity_token, candidate_token=candidate.candidate_token)
        with pytest.raises(CommandTargetChangedError):
            await service.apply_command(resource, wrong, expected_etag=stored.opaque_etag, idempotency_key="wrong-card", now=now)
        command = PlaceConfirmCommand(command_type="PLACE_CONFIRM", activity_token=card.activity_token, candidate_token=candidate.candidate_token)
        with pytest.raises(CommandTargetChangedError):
            await service.apply_command(resource, command, expected_etag=stored.opaque_etag, idempotency_key="expired", now=now + timedelta(minutes=11))
        outcome = await service.apply_command(resource, command, expected_etag=stored.opaque_etag, idempotency_key="confirm", now=now)
        assert outcome.opaque_etag != stored.opaque_etag
        resource, confirmed = await refresh(repo, resource, now)
        point = (await repo.get_map_view(resource, now=now)).points[0]
        assert point.activity_token == confirmed.result.days[0].activities[0].activity_token
        assert point.position.longitude == 116.397
        # A replay remains safe even after the short credential expires.
        assert (await service.apply_command(resource, command, expected_etag=stored.opaque_etag, idempotency_key="confirm", now=now + timedelta(minutes=11))).replayed
        before_jobs = len(repo.map_jobs) if kind == "memory" else await repo._pool.fetchval("SELECT COUNT(*) FROM trip_map_render_jobs")
        timed = ActivityTimeSetCommand(command_type="ACTIVITY_TIME_SET", activity_token=confirmed.result.days[0].activities[0].activity_token,
            start_time="10:00", end_time="12:00", visit_duration_minutes=120, locked=True)
        await service.apply_command(resource, timed, expected_etag=confirmed.opaque_etag, idempotency_key="time", now=now)
        resource, edited = await refresh(repo, resource, now)
        assert edited.result.days[0].activities[0].locked
        assert edited.result.map.status == "NEEDS_UPDATE"
        after_jobs = len(repo.map_jobs) if kind == "memory" else await repo._pool.fetchval("SELECT COUNT(*) FROM trip_map_render_jobs")
        assert after_jobs == before_jobs
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=edited.opaque_etag, idempotency_key="undo", now=now)
        resource, undone = await refresh(repo, resource, now)
        assert undone.opaque_etag != edited.opaque_etag
        assert undone.result.days[0].activities[0].start_time == confirmed.result.days[0].activities[0].start_time
        assert not undone.result.can_undo
        assert (await repo.get_map_view(resource, now=now)).points[0].position.longitude == 116.397
        await service.apply_command(resource, AssumptionSetCommand(command_type="ASSUMPTION_SET", key="destination", value="上海"),
            expected_etag=undone.opaque_etag, idempotency_key="change-city", now=now)
        resource, changed_city = await refresh(repo, resource, now)
        assert all(card.status == "NEEDS_CONFIRMATION" for day in changed_city.result.days for card in day.activities)
        assert all(point.position is None for point in (await repo.get_map_view(resource, now=now)).points)
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=changed_city.opaque_etag, idempotency_key="undo-city", now=now)
        resource, _ = await refresh(repo, resource, now)
        assert (await repo.get_map_view(resource, now=now)).points[0].position.longitude == 116.397


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_time_conflict_preview_adopt_stale_reject_and_undo(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "schedule", now), now)
        service = TripUnderstandingApplicationService(repo)
        for index, start, duration in [(0, "10:00", 120), (1, "11:00", 60)]:
            resource, stored = await refresh(repo, resource, now)
            await service.apply_command(resource, ActivityTimeSetCommand(command_type="ACTIVITY_TIME_SET",
                activity_token=stored.result.days[0].activities[index].activity_token, start_time=start, visit_duration_minutes=duration),
                expected_etag=stored.opaque_etag, idempotency_key=f"time-{index}", now=now)
        resource, stored = await refresh(repo, resource, now)
        # The UI checks immediately after editing, before the user updates the map.
        await service.materialize_trip(resource, expected_etag=stored.opaque_etag, idempotency_key="before-map", now=now)
        assert not any(item.title == "这段时间来不及" for item in (await repo.get_trip_checks(resource)).items)
        await service.request_map_render(resource, expected_etag=stored.opaque_etag, idempotency_key="map", now=now)
        worker = MapRenderWorker(repo, renderer=MapRenderer())
        while await worker.run_once("schedule-map", now=now):
            pass
        await service.materialize_trip(resource, expected_etag=stored.opaque_etag, idempotency_key="check", now=now)
        checks = await repo.get_trip_checks(resource)
        conflict = next(item for item in checks.items if item.title == "这段时间来不及")
        assert conflict.label == "必须调整" and conflict.can_preview
        assert len(conflict.affected_activity_tokens) == 2
        preview = await service.preview_trip_change(resource, check_token=conflict.check_token, idempotency_key="preview", now=now)
        change = preview.preview.changes[0]
        assert change.before.start_time == "11:00" and change.after.start_time > "12:00"
        with pytest.raises(ResourceNotReadyError):
            await service.adopt_trip_change(resource, change_token=preview.preview.change_token,
                expected_etag=stored.opaque_etag, idempotency_key="expired-preview", now=now + timedelta(minutes=16))
        await service.adopt_trip_change(resource, change_token=preview.preview.change_token,
            expected_etag=stored.opaque_etag, idempotency_key="adopt", now=now)
        resource, adopted = await refresh(repo, resource, now)
        assert adopted.result.days[0].activities[1].start_time == change.after.start_time
        assert adopted.result.map.status == "NEEDS_UPDATE"
        with pytest.raises((RevisionConflictError, ResourceNotReadyError)):
            await service.adopt_trip_change(resource, change_token=preview.preview.change_token,
                expected_etag=adopted.opaque_etag, idempotency_key="old-preview", now=now)
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=adopted.opaque_etag, idempotency_key="undo", now=now)
        _, undone = await refresh(repo, resource, now)
        assert undone.result.days[0].activities[1].start_time == "11:00"


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_nonretryable_failure_is_terminal_and_does_not_retain_private_payload(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        created = await create(repo, "failure", now)
        job = await repo.claim_next(worker_id="failure-test", now=now, lease_seconds=30)
        await repo.fail_job(job, category="PROVIDER_UNAVAILABLE", now=now, allow_retry=False,
            provider_binding={"provider": "TEST", "latency_ms": 12.5, "repair_call_count": 1,
                "input_tokens": 10, "estimated_cost_cny": None, "raw_response": "PRIVATE", "source_text": "PRIVATE"})
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)
        assert resource.state == "FAILED"
        assert await repo.claim_next(worker_id="retry-test", now=now + timedelta(minutes=1), lease_seconds=30) is None
        assert (await repo.list_events(resource, after_event_id=0))[-1].payload.status == "FAILED"
        if kind == "memory":
            binding = repo.jobs[job.job_id]["provider_binding"]
        else:
            import json
            binding = json.loads(await repo._pool.fetchval("SELECT inference_binding_json FROM trip_understanding_revisions WHERE understanding_id=$1 AND status='FAILED' ORDER BY revision DESC LIMIT 1", resource.understanding_id))
        assert binding["latency_ms"] == 12.5 and binding["repair_call_count"] == 1
        assert "PRIVATE" not in str(binding)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_repeated_place_routes_keep_each_occurrences_own_duration(kind):
    class OccurrenceRoutes:
        async def route(self, origin, destination, mode, *, observed_at):
            duration = [12, 34, 98][origin.sequence_index] + (20 if mode == "transit" else 0)
            return InternalRouteModeFact(mode=mode, status="AVAILABLE", duration_minutes=duration,
                distance_meters=1000, transfer_count=0, response_hash=canonical_sha256([origin.sequence_index, mode, duration]),
                request_hash=canonical_sha256([origin.sequence_index, mode]), provider_binding={"execution_mode": "TEST"},
                external_call_count=0, observed_at=observed_at, expires_at=observed_at + timedelta(hours=1))

    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        created = await create(repo, "repeat-places", now)
        output = await build_demo_pipeline().run(DEMO_SOURCE_TEXT)
        templates = [activity for activity in output.activities if activity.compiled.mention.day_index == 1 and activity.compiled.mention.role.value == "PLANNED"]
        cards = []
        activities = []
        for index in range(4):
            card = output.public_result.days[0].activities[index % 2].model_copy(deep=True)
            card.activity_token = uuid4().hex
            card.start_time = f"{index + 9:02d}:00"
            card.end_time = None
            card.visit_duration_minutes = 10
            card.timing_source = "USER"
            cards.append(card)
            activity = templates[index % 2].model_copy(deep=True)
            activity.compiled.activity_id = str(uuid4())
            activity.compiled.public_activity_token = card.activity_token
            activity.compiled.mention.sequence_index = index
            activities.append(activity)
        output.public_result.days = [output.public_result.days[0].model_copy(update={"activities": cards})]
        output.activities = activities
        output.claims = []
        job = await repo.claim_next(worker_id="repeated-places", now=now, lease_seconds=30)
        await repo.complete_job(job, output, now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)
        stored = await repo.get_result(resource)
        await MapRenderWorker(repo, renderer=MapRenderer(OccurrenceRoutes())).run_once("repeat-map", now=now)
        await TripUnderstandingApplicationService(repo).materialize_trip(resource, expected_etag=stored.opaque_etag,
            idempotency_key="repeat-check", now=now)
        if kind == "memory":
            snapshot = repo.g03_materialized[resource.understanding_id]["snapshot"]
        else:
            async with repo._pool.acquire() as conn:
                snapshot_id = await conn.fetchval("""SELECT ar.evidence_snapshot_id FROM trip_materialized_trips mt
                    JOIN trip_workspaces tw ON tw.workspace_id=mt.workspace_id
                    JOIN audit_reports ar ON ar.report_id=tw.current_report_id WHERE mt.understanding_id=$1""", resource.understanding_id)
                snapshot = await PostgresAuditRepository(repo._pool).get_snapshot_with_conn(conn, snapshot_id)
        by_edge = {fact.subject_id: fact.value["selected_duration_minutes"] for fact in snapshot.facts if fact.fact_type == "ROUTE_MODE_SET"}
        assert [by_edge[f"{_stable_stop_id(left.activity_token)}->{_stable_stop_id(right.activity_token)}"]
            for left, right in zip(cards, cards[1:])] == [12, 34, 98]
        conflicts = [item for item in (await repo.get_trip_checks(resource)).items if item.title == "这段时间来不及"]
        assert len(conflicts) == 1
        assert conflicts[0].affected_activity_tokens == [cards[2].activity_token, cards[3].activity_token]
