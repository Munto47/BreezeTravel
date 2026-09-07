from datetime import datetime, timedelta, timezone

import pytest

from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.errors import ResourceAccessDeniedError, ResourceGoneError, ResourceNotFoundError, ResourceNotReadyError
from app.trip_understanding.map_render import MapRenderer
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_experience_v3_journey import repository_for, create, finish


async def exists(repo, kind, public_id):
    if kind == "memory":
        return public_id in repo.resources
    return await repo._pool.fetchval("SELECT EXISTS(SELECT 1 FROM trip_understandings WHERE public_resource_id=$1)", public_id)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_each_anonymous_trip_gets_24h_without_extending_previous_or_replay(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        first = await finish(repo, await create(repo, "first", now), now)
        old_job = await repo.claim_next_map(worker_id="before-expiry", now=now, lease_seconds=100_000)
        old_output = await MapRenderer().render(await repo.load_map_plan(old_job), observed_at=now)
        second = await finish(repo, await create(repo, "second", now + timedelta(hours=22)), now + timedelta(hours=22))
        assert second.expires_at == now + timedelta(hours=46)
        assert (await create(repo, "second", now + timedelta(hours=23))).replayed
        again = await repo.authorize(second.public_resource_id, capability_hash="a"*64, now=now + timedelta(hours=23))
        assert again.expires_at == now + timedelta(hours=46)
        for after_cleanup in (False, True):
            if after_cleanup:
                assert await repo.expire_retained_trips(now=now + timedelta(hours=24), limit=1) == {"trips_expired": 1}
            with pytest.raises(ResourceGoneError):
                await repo.authorize(first.public_resource_id, capability_hash="a"*64, now=now + timedelta(hours=24))
            with pytest.raises(ResourceAccessDeniedError):
                await repo.authorize(first.public_resource_id, capability_hash="b"*64, now=now + timedelta(hours=24))
            assert await repo.authorize(second.public_resource_id, capability_hash="a"*64, now=now + timedelta(hours=24))
        assert not await exists(repo, kind, first.public_resource_id)
        assert await exists(repo, kind, second.public_resource_id)
        with pytest.raises((ResourceNotFoundError, KeyError)):
            await repo.complete_map_job(old_job, old_output, now=now + timedelta(hours=24))
        assert not await exists(repo, kind, first.public_resource_id)
        assert await repo.expire_retained_trips(now=now + timedelta(hours=24), limit=100) == {"trips_expired": 0}
        assert await repo.expire_retained_trips(now=now + timedelta(hours=46), limit=100) == {"trips_expired": 1}


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_account_trip_expires_at_30d_even_after_manual_source_deletion(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        created = await repo.create_full(owner_user_id="experience-owner", source_text=DEMO_SOURCE_TEXT,
            idempotency_key="owner-retention", request_hash=canonical_sha256(DEMO_SOURCE_TEXT), now=now, retention_days=30)
        job = await repo.claim_next(worker_id="owner-retention", now=now, lease_seconds=60)
        await repo.complete_job(job, await build_demo_pipeline().run(DEMO_SOURCE_TEXT), now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash=None, user_id="experience-owner", now=now)
        await TripUnderstandingApplicationService(repo).delete_source(resource, user_id="experience-owner", idempotency_key="delete-source", now=now)
        await repo.purge_expired_private_data(now=now + timedelta(days=31), limit=100)
        assert await exists(repo, kind, resource.public_resource_id)  # Source-only compatibility path.
        retained = await repo.authorize(resource.public_resource_id, capability_hash=None, user_id="experience-owner", now=now + timedelta(days=29))
        assert retained.expires_at == now + timedelta(days=30)
        assert await repo.expire_retained_trips(now=now + timedelta(days=29), limit=1) == {"trips_expired": 0}
        for after_cleanup in (False, True):
            if after_cleanup:
                assert await repo.expire_retained_trips(now=now + timedelta(days=30), limit=1) == {"trips_expired": 1}
            with pytest.raises(ResourceAccessDeniedError):
                await repo.authorize(resource.public_resource_id, capability_hash=None, user_id="other-owner", now=now + timedelta(days=30))
            with pytest.raises(ResourceGoneError):
                await repo.authorize(resource.public_resource_id, capability_hash=None, user_id="experience-owner", now=now + timedelta(days=30))
        assert not await exists(repo, kind, resource.public_resource_id)
        assert await repo.expire_retained_trips(now=now + timedelta(days=31), limit=100) == {"trips_expired": 0}
        if kind == "postgres":
            for table in ("trip_understanding_sources", "trip_understanding_results", "trip_understanding_revisions", "trip_understanding_jobs", "trip_map_render_jobs"):
                assert await repo._pool.fetchval(f"SELECT count(*) FROM {table} WHERE understanding_id=$1", resource.understanding_id) == 0


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_retention_cleanup_rechecks_deadline_under_delete_lock(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "retention-guard", now), now)
        with pytest.raises(ResourceNotReadyError):
            await repo.delete_trip(resource, capability_hash="a"*64, user_id=None,
                idempotency_key="retention-expiry", request_hash="f"*64, now=now, retention_expiry=True)
        assert await exists(repo, kind, resource.public_resource_id)
        with pytest.raises(ValueError):
            await repo.expire_retained_trips(now=now, limit=0)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_expired_pending_inference_cannot_recreate_deleted_trip(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        created = await create(repo, "late-inference", now)
        job = await repo.claim_next(worker_id="late-inference", now=now, lease_seconds=100_000)
        output = await build_demo_pipeline().run(DEMO_SOURCE_TEXT)
        assert await repo.expire_retained_trips(now=now + timedelta(hours=25), limit=1) == {"trips_expired": 1}
        with pytest.raises((ResourceNotFoundError, KeyError)):
            await repo.complete_job(job, output, now=now + timedelta(hours=25))
        assert not await exists(repo, kind, created.accepted.public_resource_id)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_claim_does_not_extend_other_anonymous_trip(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        first = await finish(repo, await create(repo, "claim-first", now), now)
        second = await finish(repo, await create(repo, "claim-second", now + timedelta(hours=22)), now + timedelta(hours=22))
        outcome = await repo.claim_demo(first.public_resource_id, capability_hash="a"*64, user_id="experience-owner",
            idempotency_key="claim", request_hash="a"*64, now=now + timedelta(hours=23), retention_days=30)
        owned = await repo.authorize(outcome.claimed.public_resource_id, capability_hash=None, user_id="experience-owner", now=now + timedelta(hours=25))
        assert owned.expires_at == now + timedelta(hours=23, days=30)
        other = await repo.authorize(second.public_resource_id, capability_hash="a"*64, now=now + timedelta(hours=25))
        assert other.expires_at == now + timedelta(hours=46)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_claim_rejects_expired_trip_even_when_new_draft_keeps_session_active(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        first = await finish(repo, await create(repo, "expired-claim-first", now), now)
        second_at = now + timedelta(hours=22)
        second = await finish(repo, await create(repo, "expired-claim-second", second_at), second_at)
        expired_at = now + timedelta(hours=24)
        # Maintenance has not removed the old row yet; its own deadline still applies.
        assert await exists(repo, kind, first.public_resource_id)
        with pytest.raises(ResourceAccessDeniedError):
            await repo.claim_demo(first.public_resource_id, capability_hash="b"*64,
                user_id="experience-owner", idempotency_key="foreign-expired-claim",
                request_hash="b"*64, now=expired_at, retention_days=30)
        with pytest.raises(ResourceGoneError):
            await repo.claim_demo(first.public_resource_id, capability_hash="a"*64,
                user_id="experience-owner", idempotency_key="expired-claim",
                request_hash="c"*64, now=expired_at, retention_days=30)
        with pytest.raises(ResourceGoneError):
            await repo.authorize(first.public_resource_id, capability_hash="a"*64, now=expired_at)
        claimed = await repo.claim_demo(second.public_resource_id, capability_hash="a"*64,
            user_id="experience-owner", idempotency_key="unexpired-claim",
            request_hash="d"*64, now=expired_at, retention_days=30)
        owned = await repo.authorize(claimed.claimed.public_resource_id, capability_hash=None,
            user_id="experience-owner", now=expired_at)
        assert owned.expires_at == expired_at + timedelta(days=30)
        assert (await repo.claim_demo(second.public_resource_id, capability_hash="",
            user_id="experience-owner", idempotency_key="unexpired-claim",
            request_hash="d"*64, now=expired_at, retention_days=30)).replayed
