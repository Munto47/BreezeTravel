"""Actual source -> PG/map snapshot -> bounded route preview -> saved order.

The model response, POI identities/coordinates and AMap HTTP responses are fixed.
Only randomly named, disposable integration databases are used; no live service.
"""

import asyncio
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.trip_understanding.amap_route import AmapRouteProvider
from app.trip_understanding.errors import CommandTargetChangedError, IdempotencyInProgressError, RevisionConflictError
from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.map_render import MapRenderer
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.models import (
    CreateFullRequest,
    ActivityMoveCommand,
    UndoCommand,
    RedoCommand,
    ResolvedPlace,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.relative_route_previews import verify_route_preview
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.source_order import PrivateSourceOrderState
from tests.test_experience_v3_journey import repository_for
from tests.test_relative_route_options import FixedAmap
from tests.test_semantic_supplement_budget import Client


NAMES = ("故宫博物院", "景山公园", "颐和园", "圆明园", "天坛公园", "前门大街")
SCOPES = (
    "Day1：故宫博物院、景山公园、颐和园、圆明园。以上只是初步顺序，可以根据交通调整。",
    "Day2：天坛公园、前门大街。",
)
SOURCE = "北京两日游。\n" + "\n".join(SCOPES)


class FixedPlaces:
    async def resolve(self, *, city, atomic_place_name, category_hint=None):
        assert city == "北京" and atomic_place_name in NAMES
        index = NAMES.index(atomic_place_name)
        return ResolvedPlace(
            canonical_place_id=f"poi-{index}",
            name=atomic_place_name,
            category="景点",
            area_or_address="固定测试地址",
            provider_binding={
                "city": "北京",
                "fixed_provider": True,
                "external_calls": 0,
                "coordinates": {"longitude": 116.30 + index / 100, "latitude": 39.9},
            },
        )


class FixedGeometryAmap(FixedAmap):
    """Include fixed supplier polylines so current PG map has actual geometry."""

    async def respond(self, request):
        response = await super().respond(request)
        payload = response.json()
        polyline = request.url.params["origin"] + ";" + request.url.params["destination"]
        if "/walking" in str(request.url):
            payload["route"]["paths"][0]["steps"] = [{"polyline": polyline}]
        else:
            payload["route"]["transits"][0]["segments"] = [{"walking": {"steps": [{"polyline": polyline}]}}]
        return httpx.Response(200, json=payload)


async def build_route_flow_result():
    raw = {
        "destination": "北京",
        "day_labels": ["Day1", "Day2"],
        "unprocessed_quotes": [],
        "activities": [
            {
                "source_quote": name,
                "place_name": name,
                "role": "PLANNED",
                "day_index": 1 if i < 4 else 2,
                "category": "景点",
                "city": "北京",
                "city_evidence": "北京两日游",
            }
            for i, name in enumerate(NAMES)
        ],
        "order_groups": [
            {"kind": "INITIAL_ORDER", "activity_indices": list(indices), "scope_quote": scope}
            for indices, scope in zip((range(4), range(4, 6)), SCOPES)
        ],
    }
    client = Client(raw)
    provider = ExperienceQwenProvider(
        api_key="fixed-test-only",
        base_url="https://fixed.invalid",
        model="fixed-order-flow",
        client=client,
        deadline_seconds=10,
        enable_source_visits=False,
    )
    result = await TripUnderstandingPipeline(provider, FixedPlaces()).run(SOURCE)
    assert len(client.calls) == 1
    assert [[c.name for c in d.activities] for d in result.public_result.days] == [list(NAMES[:4]), list(NAMES[4:])]
    assert len(result.proposal.order_assessment.groups) == 2
    assert not result.proposal.order_assessment.unknown_mention_ids
    assert all(not group.hard_precedence for group in result.proposal.order_assessment.groups)
    return result


async def create_trip(repo, *, key="route-flow", render=True):
    now = datetime.now(UTC)
    service = TripUnderstandingApplicationService(repo)
    created = await service.create_full(
        CreateFullRequest.model_validate({"mode": "FULL", "source": {"type": "TEXT", "text": SOURCE}}),
        owner_user_id="experience-owner",
        idempotency_key=key,
        now=now,
    )
    job = await repo.claim_next(worker_id="fixed-route-model", now=now, lease_seconds=60)
    await repo.complete_job(job, await build_route_flow_result(), now=now)
    resource = await repo.authorize(
        created.accepted.public_resource_id, capability_hash=None, user_id="experience-owner", now=now
    )
    stored = await repo.get_result(resource)
    if render:
        # Four current day edges x two real AMap adapter modes, fixed HTTP.
        minutes = {
            (a, b, mode): (25 if mode == "walking" else 40)
            for a, b in [(0, 1), (1, 2), (2, 3), (4, 5)]
            for mode in ["walking", "transit"]
        }
        transport = FixedGeometryAmap(minutes=minutes)
        await service.request_map_render(
            resource, expected_etag=stored.opaque_etag, idempotency_key=key + "-map", now=now
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.respond)) as client:
            renderer = MapRenderer(AmapRouteProvider(api_key="fixed-test-only", client=client))
            assert await MapRenderWorker(repo, renderer=renderer).run_once("fixed-route-map", now=now)
        assert len(transport.calls) == 8
        view = await repo.get_map_view(resource, now=datetime.now(UTC))
        assert view.status == "AVAILABLE" and len(view.days) == 2
    return resource, stored


@dataclass
class Snapshot:
    result: object
    etag: str
    revision: int
    source: PrivateSourceOrderState
    identities: dict


async def snapshot(repo, resource, kind):
    # Each HTTP read authorizes again; a prior resource record points at its
    # immutable old result and must not be mixed with the current place plan.
    resource = await repo.authorize(
        resource.public_resource_id, capability_hash=None, user_id="experience-owner", now=datetime.now(UTC)
    )
    stored = await repo.get_result(resource)
    plan, tag = await repo.get_current_place_plan(resource)
    revision = plan.plan_ref.revision
    if kind == "memory":
        proposal = repo.g03_pipeline_inputs[(resource.understanding_id, revision)]
    else:
        async with repo._pool.acquire() as conn:
            proposal = await conn.fetchval(
                "SELECT proposal_json FROM trip_understanding_revisions WHERE understanding_id=$1 AND revision=$2",
                resource.understanding_id,
                revision,
            )
            proposal = json.loads(proposal) if isinstance(proposal, str) else proposal
            identities = await conn.fetch(
                "SELECT atomic_place_name, canonical_place_id FROM trip_understanding_activities WHERE understanding_id=$1 AND revision=$2",
                resource.understanding_id,
                revision,
            )
            assert {r["atomic_place_name"]: r["canonical_place_id"] for r in identities} == {
                name: f"poi-{i}" for i, name in enumerate(NAMES)
            }
    return Snapshot(
        stored.result,
        tag,
        revision,
        PrivateSourceOrderState.model_validate(proposal["source_order"]),
        {s.name: (s.canonical_place_id, s.longitude, s.latitude) for s in plan.stops},
    )


def visit_identity(state):
    return {
        card.name: state.source.bindings[card.activity_token].visit_id
        for day in state.result.days
        for card in day.activities
    }


async def preview(repo, resource, tag, provider, *, key="route-preview", day=1):
    return await repo.preview_relative_routes(
        resource,
        expected_etag=tag,
        day_index=day,
        idempotency_key=key,
        request_hash=canonical_sha256({"day_index": day, "if_match": tag}),
        provider=provider,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_route_preview_and_adoption_create_version_and_preserve_source_identity_undo_redo(kind, monkeypatch):
    import app.trip_understanding.relative_route_previews as preview_module

    async with repository_for(kind) as repo:
        resource, _ = await create_trip(repo)
        original = await snapshot(repo, resource, kind)
        transport = FixedAmap()
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.respond)) as client:
            provider = AmapRouteProvider(api_key="fixed-test-only", client=client)
            comparison, replayed = await preview(repo, resource, original.etag, provider)
            assert not replayed and comparison.status == "AVAILABLE"
            assert len(comparison.options) == 1 and len(transport.calls) == 6
            duplicate, replayed = await preview(repo, resource, original.etag, provider)
            assert replayed and duplicate == comparison and len(transport.calls) == 6
        assert (await snapshot(repo, resource, kind)).revision == original.revision
        option = comparison.options[0]
        assert option.before == list(NAMES[:4]) and option.after == [NAMES[i] for i in (0, 2, 1, 3)]
        assert option.comparison_scope == "CHANGED_EDGES_ONLY" and option.minutes_saved == 45
        service = TripUnderstandingApplicationService(repo)
        with monkeypatch.context() as context:
            context.setattr(preview_module, "ROUTE_CONFIG_SHA256", "changed-test-route-config")
            with pytest.raises(CommandTargetChangedError):
                await service.adopt_trip_change(
                    resource,
                    change_token=option.change_token,
                    expected_etag=original.etag,
                    idempotency_key="first-adoption-stale-config",
                )
        assert (await snapshot(repo, resource, kind)).revision == original.revision
        adopted = await service.adopt_trip_change(
            resource, change_token=option.change_token, expected_etag=original.etag, idempotency_key="route-adopt"
        )
        changed = await snapshot(repo, resource, kind)
        assert adopted.opaque_etag == changed.etag != original.etag
        assert changed.revision > original.revision
        assert [[c.name for c in d.activities] for d in changed.result.days] == [option.after, list(NAMES[4:])]
        assert changed.result.map.status == "NEEDS_UPDATE"
        assert changed.identities == original.identities
        assert visit_identity(changed) == visit_identity(original)
        assert changed.source.groups == original.source.groups
        # A saved operation must remain recoverable after both time and route
        # policy advance; only first-time adoption is subject to freshness.
        with monkeypatch.context() as context:
            context.setattr(preview_module, "ROUTE_CONFIG_SHA256", "changed-test-route-config")
            duplicate_adopt = await service.adopt_trip_change(
                resource,
                change_token=option.change_token,
                expected_etag=original.etag,
                idempotency_key="route-adopt",
                now=datetime.now(UTC) + timedelta(minutes=11),
            )
        assert duplicate_adopt.replayed
        assert (await snapshot(repo, resource, kind)).revision == changed.revision
        for command, expected in [
            (UndoCommand(command_type="UNDO"), original),
            (RedoCommand(command_type="REDO"), changed),
        ]:
            current = await snapshot(repo, resource, kind)
            await service.apply_command(
                resource, command, expected_etag=current.etag, idempotency_key="flow-" + command.command_type
            )
            restored = await snapshot(repo, resource, kind)
            assert [[c.name for c in d.activities] for d in restored.result.days] == [
                [c.name for c in d.activities] for d in expected.result.days
            ]
            assert restored.identities == original.identities
            assert visit_identity(restored) == visit_identity(original)
            assert restored.source.groups == original.source.groups


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("problem", ["expired", "different_resource", "tampered_move", "old_version"])
async def test_route_selection_cannot_authorize_a_different_target_or_stale_basis(kind, problem):
    async with repository_for(kind) as repo:
        resource, _ = await create_trip(repo)
        original = await snapshot(repo, resource, kind)
        async with httpx.AsyncClient(transport=httpx.MockTransport(FixedAmap().respond)) as client:
            view, _ = await preview(
                repo, resource, original.etag, AmapRouteProvider(api_key="fixed-test-only", client=client)
            )
        option = view.options[0]
        service = TripUnderstandingApplicationService(repo)
        target = resource
        now = datetime.now(UTC)
        if problem == "different_resource":
            target, _ = await create_trip(repo, key="other-route", render=False)
        elif problem == "old_version":
            await service.apply_command(
                resource,
                ActivityMoveCommand(
                    command_type="ACTIVITY_MOVE",
                    activity_token=original.result.days[1].activities[0].activity_token,
                    target_day_index=2,
                    target_position=1,
                ),
                expected_etag=original.etag,
                idempotency_key="edit-before-adopt",
            )
        elif problem == "expired":
            now += timedelta(minutes=11)
        before = await snapshot(repo, target, kind)
        with pytest.raises((CommandTargetChangedError, RevisionConflictError)):
            if problem == "tampered_move":
                command = verify_route_preview(
                    option.change_token,
                    public_resource_id=resource.public_resource_id,
                    expected_etag=original.etag,
                    now=now,
                ).command.model_copy(update={"target_position": 0, "route_preview_token": option.change_token})
                await service.apply_command(
                    target, command, expected_etag=before.etag, idempotency_key="tampered-route", now=now
                )
            else:
                await service.adopt_trip_change(
                    target,
                    change_token=option.change_token,
                    expected_etag=before.etag,
                    idempotency_key="bad-" + problem,
                    now=now,
                )
        after = await snapshot(repo, target, kind)
        assert after.revision == before.revision and after.result == before.result


@pytest.mark.asyncio
async def test_postgres_preview_releases_current_row_lock_before_fixed_http_and_rechecks_edit():
    async with repository_for("postgres") as repo:
        resource, _ = await create_trip(repo)
        original = await snapshot(repo, resource, "postgres")
        entered, release = asyncio.Event(), asyncio.Event()
        fixture = FixedAmap()
        started_calls = []

        async def gated_response(request):
            started_calls.append(request.url.path)
            entered.set()
            await release.wait()
            return await fixture.respond(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(gated_response)) as client:
            task = asyncio.create_task(
                preview(repo, resource, original.etag, AmapRouteProvider(api_key="fixed-test-only", client=client))
            )
            try:
                await asyncio.wait_for(entered.wait(), 2)
                calls_before_duplicate = len(started_calls)
                with pytest.raises(IdempotencyInProgressError):
                    await preview(
                        repo, resource, original.etag, AmapRouteProvider(api_key="fixed-test-only", client=client)
                    )
                assert len(started_calls) == calls_before_duplicate
                async with repo._pool.acquire() as conn, conn.transaction():
                    await conn.execute("SET LOCAL lock_timeout='500ms'")
                    locked = await repo._lock_current_result(conn, resource)
                    assert locked["opaque_etag"] == original.etag
                await TripUnderstandingApplicationService(repo).apply_command(
                    resource,
                    ActivityMoveCommand(
                        command_type="ACTIVITY_MOVE",
                        activity_token=original.result.days[1].activities[0].activity_token,
                        target_day_index=2,
                        target_position=1,
                    ),
                    expected_etag=original.etag,
                    idempotency_key="edit-during-routes",
                )
                release.set()
                with pytest.raises(RevisionConflictError):
                    await asyncio.wait_for(task, 4)
                current = await snapshot(repo, resource, "postgres")
                assert [c.name for c in current.result.days[0].activities] == list(NAMES[:4])
                assert [c.name for c in current.result.days[1].activities] == list(reversed(NAMES[4:]))
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_postgres_adoption_rechecks_expiry_after_waiting_for_current_row_lock(monkeypatch):
    import app.trip_understanding.relative_route_repository as module

    actual_issue = module.issue_route_preview
    expiry = None

    def short_lived_option(option, visits, **kwargs):
        nonlocal expiry
        expiry = datetime.now(UTC) + timedelta(seconds=2)
        return actual_issue(replace(option, expires_at=expiry), visits, **kwargs)

    monkeypatch.setattr(module, "issue_route_preview", short_lived_option)
    async with repository_for("postgres") as repo:
        resource, _ = await create_trip(repo)
        original = await snapshot(repo, resource, "postgres")
        async with httpx.AsyncClient(transport=httpx.MockTransport(FixedAmap().respond)) as client:
            view, _ = await preview(
                repo, resource, original.etag, AmapRouteProvider(api_key="fixed-test-only", client=client)
            )
        token = view.options[0].change_token
        request_started = datetime.now(UTC)
        verify_route_preview(
            token, public_resource_id=resource.public_resource_id, expected_etag=original.etag, now=request_started
        )
        task = None
        try:
            async with repo._pool.acquire() as conn, conn.transaction():
                await repo._lock_current_result(conn, resource)
                task = asyncio.create_task(
                    TripUnderstandingApplicationService(repo).adopt_trip_change(
                        resource,
                        change_token=token,
                        expected_etag=original.etag,
                        idempotency_key="expires-while-waiting",
                        now=request_started,
                    )
                )
                # Read-only pg_stat_activity proves the request reached a lock
                # wait while still authorized, rather than starting it expired.
                for _ in range(100):
                    await conn.execute("SELECT pg_stat_clear_snapshot()")
                    waiting = await conn.fetchval("""SELECT count(*) FROM pg_stat_activity
                        WHERE datname=current_database() AND pid<>pg_backend_pid()
                        AND wait_event_type='Lock'""")
                    if waiting:
                        break
                    await asyncio.sleep(0.01)
                assert waiting and not task.done() and datetime.now(UTC) < expiry
                await asyncio.sleep(max(0, (expiry - datetime.now(UTC)).total_seconds()) + 0.03)
            with pytest.raises(CommandTargetChangedError):
                await asyncio.wait_for(task, 3)
            unchanged = await snapshot(repo, resource, "postgres")
            assert unchanged.revision == original.revision and unchanged.result == original.result
        finally:
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_expired_preview_replay_returns_needs_update_without_another_provider_call(kind, monkeypatch):
    import app.trip_understanding.relative_route_repository as module

    async with repository_for(kind) as repo:
        resource, _ = await create_trip(repo)
        original = await snapshot(repo, resource, kind)
        fixture = FixedAmap()
        async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.respond)) as client:
            provider = AmapRouteProvider(api_key="fixed-test-only", client=client)
            view, _ = await preview(repo, resource, original.etag, provider)
            assert view.status == "AVAILABLE"
            call_count = len(fixture.calls)
            future = datetime.now(UTC) + timedelta(minutes=11)

            class FutureClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return future if tz else future.replace(tzinfo=None)

            monkeypatch.setattr(module, "datetime", FutureClock)
            expired, replayed = await preview(repo, resource, original.etag, provider)
            assert replayed and expired.status == "NEEDS_UPDATE" and expired.options == []
            assert len(fixture.calls) == call_count
