from datetime import datetime, timedelta, timezone

import pytest

from app.trip_understanding.candidates import CandidatePlace, GCJ02Position, issue_candidate
from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.models import ActivityRole, ActivityTimeSetCommand, PlaceConfirmCommand, ProposedMention, UndoCommand
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.map_render import MapRenderer
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_experience_v3_journey import repository_for

PRIVATE_QUOTE = "仅供导入隐私测试的原始备注甲乙丙"


async def create_private_trip(repo, now):
    source = DEMO_SOURCE_TEXT + "\n" + PRIVATE_QUOTE
    demo = build_demo_pipeline()

    class Proposal:
        async def propose(self, _source):
            import hashlib
            base = await demo.inference_provider.propose(DEMO_SOURCE_TEXT)
            return base.model_copy(update={
                "source_hash": hashlib.sha256(source.encode()).hexdigest(),
                "mentions": [*base.mentions, ProposedMention(mention_id="private-reference",
                    raw_text=PRIVATE_QUOTE, span_start=source.index(PRIVATE_QUOTE), span_end=len(source),
                    role=ActivityRole.REFERENCE, sequence_index=99)],
            })

    created = await repo.create_full(owner_user_id="experience-owner", source_text=source,
        idempotency_key="source-delete-create", request_hash=canonical_sha256(source), now=now, retention_days=30)
    job = await repo.claim_next(worker_id="source-test", now=now, lease_seconds=60)
    output = await TripUnderstandingPipeline(Proposal(), demo.place_resolver).run(source)
    for item in output.activities:
        item.resolver_receipt["raw_provider_response"] = PRIVATE_QUOTE
    await repo.complete_job(job, output, now=now)
    return created.accepted.public_resource_id


async def refresh(repo, public_id, now):
    resource = await repo.authorize(public_id, capability_hash=None, user_id="experience-owner", now=now)
    return resource, await repo.get_result(resource)


async def assert_source_erased(repo, kind, understanding_id):
    if kind == "postgres":
        assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_sources WHERE understanding_id=$1 AND encrypted_content IS NOT NULL", understanding_id) == 0
        assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_source_claims WHERE understanding_id=$1", understanding_id) == 0
        rows = await repo._pool.fetch("SELECT mention_text, resolver_receipt_json FROM trip_understanding_activities WHERE understanding_id=$1", understanding_id)
        assert rows and PRIVATE_QUOTE not in str(rows)
    else:
        assert all(PRIVATE_QUOTE not in value.text for value in repo.sources.values())
        assert PRIVATE_QUOTE not in str(repo.g03_pipeline_inputs)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_delete_source_keeps_versions_candidates_undo_and_manual_maps(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        public_id = await create_private_trip(repo, now)
        resource, original = await refresh(repo, public_id, now)
        service = TripUnderstandingApplicationService(repo)
        await service.apply_command(resource, ActivityTimeSetCommand(command_type="ACTIVITY_TIME_SET",
            activity_token=original.result.days[0].activities[0].activity_token, start_time="09:00", visit_duration_minutes=60),
            expected_etag=original.opaque_etag, idempotency_key="pre-delete-time", now=now)
        resource, before = await refresh(repo, public_id, now)
        before_map = await repo.get_map_view(resource, now=now)
        await service.request_map_render(resource, expected_etag=before.opaque_etag, idempotency_key="before-map", now=now)
        card = before.result.days[0].activities[0]
        candidate = issue_candidate(CandidatePlace(canonical_place_id="amap:private-test", city="北京",
            name="故宫博物院", category="景点", area_or_address="北京市东城区", position=GCJ02Position(longitude=116.397, latitude=39.918)),
            public_resource_id=public_id, activity_token=card.activity_token, expected_etag=before.opaque_etag, now=now)
        await service.delete_source(resource, user_id="experience-owner", idempotency_key="delete-source", now=now)
        resource, after = await refresh(repo, public_id, now)
        assert after.opaque_etag == before.opaque_etag
        assert after.result.days == before.result.days
        assert (await repo.get_map_view(resource, now=now)).points == before_map.points
        await assert_source_erased(repo, kind, resource.understanding_id)
        assert (await service.delete_source(resource, user_id="experience-owner", idempotency_key="delete-source", now=now)).replayed
        await service.request_map_render(resource, expected_etag=after.opaque_etag, idempotency_key="after-map", now=now)
        worker = MapRenderWorker(repo, renderer=MapRenderer())
        # Jobs queued before privacy deletion still bind to the same historical plans.
        assert await worker.run_once("old-map-after-delete", now=now)
        assert await worker.run_once("current-map-after-delete", now=now)
        assert (await repo.get_map_view(resource, now=now)).status in {"AVAILABLE", "LIMITED"}
        await service.apply_command(resource, PlaceConfirmCommand(command_type="PLACE_CONFIRM", activity_token=card.activity_token,
            candidate_token=candidate.candidate_token), expected_etag=after.opaque_etag, idempotency_key="after-candidate", now=now)
        resource, confirmed = await refresh(repo, public_id, now)
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=confirmed.opaque_etag,
            idempotency_key="after-undo", now=now)
        resource, undone = await refresh(repo, public_id, now)
        assert undone.result.days[0].activities[0].start_time == "09:00"
        assert len([point for point in (await repo.get_map_view(resource, now=now)).points if point.position]) == 6
        await service.request_map_render(resource, expected_etag=undone.opaque_etag, idempotency_key="undo-map", now=now)
        await assert_source_erased(repo, kind, resource.understanding_id)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_source_ttl_retains_sanitized_map_bindings(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        public_id = await create_private_trip(repo, now)
        resource, before = await refresh(repo, public_id, now)
        points = (await repo.get_map_view(resource, now=now)).points
        await repo.purge_expired_private_data(now=now + timedelta(days=31), limit=100)
        await assert_source_erased(repo, kind, resource.understanding_id)
        assert (await repo.get_map_view(resource, now=now)).points == points
        assert (await repo.get_result(resource)).opaque_etag == before.opaque_etag
