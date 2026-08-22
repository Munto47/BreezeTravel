from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.importing.models import ResolvedPlaceReceipt
from app.itineraries.errors import IdempotencyKeyReusedError, RevisionConflictError
from app.itineraries.command_service import RevisionCommandService
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    EditOperation,
    ItineraryDay,
    ItineraryEditCommand,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import PostgresItineraryRepository
from app.schemas.place import Coordinates, RetrievalExecutionMode
from app.suggestions.models import (
    EvidenceFreshness,
    FreshnessStatus,
    FrozenCanonicalPlace,
    HardGate,
    RouteDelta,
    RouteReceipt,
    RouteReceiptLeg,
    SuggestionCandidateDraft,
    SuggestionClassification,
    SuggestionIntent,
    SuggestionSetCreateInput,
)
from app.suggestions.repositories import PostgresSuggestionRepository
from app.suggestions.service import AtomicSuggestionUndoService, SuggestionSetService


pytestmark = pytest.mark.integration
BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _admin_dsn() -> str:
    return os.getenv("TEST_DATABASE_ADMIN_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")


def _input(
    *,
    set_id: str,
    base_revision: int,
    after_stop_id: str,
    candidate_id: str,
    place_id: str,
    workspace_id: str = "workspace-suggestion-pg",
    after_place_id: str = "anchor-place-pg",
):
    now = datetime.now(timezone.utc)
    coords = Coordinates(lng=116.39 + base_revision / 1000, lat=39.91 + base_revision / 1000)
    receipt = ResolvedPlaceReceipt(
        canonical_place_id=place_id,
        provider="controlled_pg_provider",
        provider_place_id=f"provider-{place_id}",
        name=f"受控地点{base_revision}",
        city="北京",
        district="东城区",
        address="受控地址",
        category="attraction",
        longitude=coords.lng,
        latitude=coords.lat,
        request_hash=sha_char(base_revision),
        response_hash=sha_char(base_revision + 3),
        observed_at=now,
        execution_mode=RetrievalExecutionMode.FIXTURE,
    )
    return SuggestionSetCreateInput(
        suggestion_set_id=set_id,
        workspace_id=workspace_id,
        base_revision=base_revision,
        day_index=0,
        insert_after_stop_id=after_stop_id,
        intents=[SuggestionIntent.NEARBY],
        context_hash=sha_char(base_revision + 6),
        policy_version="controlled-ranker-v1",
        provider_snapshot_id=f"controlled-provider-snapshot-{base_revision}",
        expires_at=now + timedelta(hours=1),
        candidates=[SuggestionCandidateDraft(
            candidate_id=candidate_id,
            canonical_place=FrozenCanonicalPlace(
                place_id=place_id,
                name=receipt.name,
                city="北京",
                district="东城区",
                address="受控地址",
                category="attraction",
                coords=coords,
            ),
            provider_receipt=receipt,
            provider_receipt_id=f"receipt-{candidate_id}",
            rank_position=1,
            classification=SuggestionClassification.ON_ROUTE,
            source_prior_refs=["controlled-official-route:v1", "controlled-ugc:v1"],
            score_components={"route": 0.9, "source_prior": 0.7},
            total_score=0.84,
            hard_gate=HardGate(passed=True),
            route_delta=RouteDelta(
                status="AVAILABLE",
                delta_route_minutes=5,
                previous_to_candidate_minutes=5,
                route_receipts=(RouteReceipt(
                    leg=RouteReceiptLeg.PREVIOUS_TO_CANDIDATE,
                    transport_mode="walking",
                    origin_place_id=after_place_id,
                    origin_coords=Coordinates(lng=116.38, lat=39.90),
                    destination_place_id=place_id,
                    destination_coords=coords,
                    duration_minutes=5,
                    provider="controlled_pg_route",
                    request_hash=sha_char(base_revision + 8),
                    response_hash=sha_char(base_revision + 9),
                    observed_at=now,
                    snapshot_id=f"controlled-pg-route-{base_revision}",
                    execution_mode=RetrievalExecutionMode.FIXTURE,
                    max_age_seconds=3600,
                    source_url=f"fixture://pg-route/{base_revision}",
                ),),
            ),
            evidence_freshness=EvidenceFreshness(
                status=FreshnessStatus.FRESH,
                observed_at=now,
                max_age_seconds=3600,
            ),
            explanation_codes=["CONTROLLED_NEARBY"],
        )],
        session_id="session-pg",
        created_by="suggestion-pg-user",
    )


def sha_char(index: int) -> str:
    return "0123456789abcdef"[index % 16] * 64


@pytest.mark.asyncio
async def test_postgres_suggestion_accept_replay_concurrency_and_rollback():
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    database_name = f"breezetravel_suggestion_{uuid4().hex[:10]}"
    assert re.fullmatch(r"[a-z0-9_]+", database_name)
    admin = await asyncpg.connect(_admin_dsn())
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        database_dsn = f"{_admin_dsn().rsplit('/', 1)[0]}/{database_name}"
        bootstrap = await asyncpg.connect(database_dsn)
        try:
            await bootstrap.execute((BACKEND_ROOT / "app/db/init.sql").read_text(encoding="utf-8"))
        finally:
            await bootstrap.close()
        environment = os.environ.copy()
        environment["DATABASE_URL"] = database_dsn.replace("postgresql://", "postgresql+asyncpg://")
        migrated = subprocess.run(
            [sys.executable, "-m", "scripts.migrate"],
            cwd=BACKEND_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert migrated.returncode == 0, migrated.stderr
        pool = await asyncpg.create_pool(database_dsn, min_size=2, max_size=4)
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO users(user_id, nickname) VALUES ('suggestion-pg-user', 'Suggestion PG')")
            await conn.execute(
                "INSERT INTO rooms(room_id, thread_id, trip_city, trip_days) "
                "VALUES ('suggestion-pg-room', 'suggestion-pg-thread', '北京', 2)"
            )
            await conn.execute(
                "INSERT INTO room_members(room_id, user_id) VALUES ('suggestion-pg-room', 'suggestion-pg-user')"
            )

        dates = TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2))
        revision = with_content_hash(ItineraryRevisionContent(
            itinerary_id="itin-suggestion-pg",
            workspace_id="workspace-suggestion-pg",
            revision=1,
            source_type=RevisionSource.MANUAL,
            city="北京",
            date_range=dates,
            days=[
                ItineraryDay(day_index=0, date=dates.start, stops=[
                    ItineraryStop(stop_id="anchor-pg", place_id="anchor-place-pg", day_index=0, order_index=0)
                ]),
                ItineraryDay(day_index=1, date=dates.end, stops=[]),
            ],
            created_by="suggestion-pg-user",
        ))
        workspace = TripWorkspace(
            workspace_id="workspace-suggestion-pg",
            room_id="suggestion-pg-room",
            city="北京",
            trip_date_range=dates,
            current_itinerary_revision=1,
            created_by="suggestion-pg-user",
        )
        itineraries = PostgresItineraryRepository(pool)
        await itineraries.create_workspace(workspace, revision)
        service = SuggestionSetService(PostgresSuggestionRepository(pool), itineraries)

        await service.create_from_ranked(_input(
            set_id="set-pg-1", base_revision=1, after_stop_id="anchor-pg",
            candidate_id="candidate-pg-1", place_id="place-pg-1",
        ))

        preview = await service.record_candidate_previewed(
            workspace_id=workspace.workspace_id,
            suggestion_set_id="set-pg-1",
            candidate_id="candidate-pg-1",
            actor_user_id="suggestion-pg-user",
            idempotency_key="preview-pg-1",
        )
        preview_replay = await service.record_candidate_previewed(
            workspace_id=workspace.workspace_id,
            suggestion_set_id="set-pg-1",
            candidate_id="candidate-pg-1",
            actor_user_id="suggestion-pg-user",
            idempotency_key="preview-pg-1",
        )
        assert preview_replay.idempotent_replay is True
        assert preview_replay.event == preview.event
        with pytest.raises(IdempotencyKeyReusedError):
            await service.record_candidate_dismissed(
                workspace_id=workspace.workspace_id,
                suggestion_set_id="set-pg-1",
                candidate_id="candidate-pg-1",
                actor_user_id="suggestion-pg-user",
                idempotency_key="preview-pg-1",
                reason_code="TOO_FAR",
            )

        async with pool.acquire() as conn:
            event_count = await conn.fetchval("SELECT COUNT(*) FROM recommendation_events")
            command_count = await conn.fetchval("SELECT COUNT(*) FROM recommendation_event_commands")
            await conn.execute(
                """
                CREATE OR REPLACE FUNCTION fail_recommendation_event_command()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.idempotency_key = 'event-command-rollback' THEN
                        RAISE EXCEPTION 'controlled recommendation event rollback';
                    END IF;
                    RETURN NEW;
                END;
                $$;
                CREATE TRIGGER fail_recommendation_event_command_trigger
                BEFORE INSERT ON recommendation_event_commands
                FOR EACH ROW EXECUTE FUNCTION fail_recommendation_event_command();
                """
            )
        with pytest.raises(Exception, match="controlled recommendation event rollback"):
            await service.record_candidate_previewed(
                workspace_id=workspace.workspace_id,
                suggestion_set_id="set-pg-1",
                candidate_id="candidate-pg-1",
                actor_user_id="suggestion-pg-user",
                idempotency_key="event-command-rollback",
            )
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM recommendation_events") == event_count
            assert await conn.fetchval("SELECT COUNT(*) FROM recommendation_event_commands") == command_count
            await conn.execute("DROP TRIGGER fail_recommendation_event_command_trigger ON recommendation_event_commands")

        # A valid foreign workspace/set proves the composite FK rejects a
        # cross-workspace event; a missing workspace would only exercise the
        # pre-existing workspace FK and is not sufficient evidence.
        foreign_revision = with_content_hash(ItineraryRevisionContent.model_validate({
            **revision.model_dump(exclude={"content_hash"}),
            "itinerary_id": "itin-suggestion-pg-foreign",
            "workspace_id": "workspace-suggestion-pg-foreign",
        }))
        foreign_workspace = workspace.model_copy(update={
            "workspace_id": "workspace-suggestion-pg-foreign",
        })
        await itineraries.create_workspace(foreign_workspace, foreign_revision)
        await service.create_from_ranked(_input(
            set_id="set-pg-foreign",
            base_revision=1,
            after_stop_id="anchor-pg",
            candidate_id="candidate-pg-foreign",
            place_id="place-pg-foreign",
            workspace_id="workspace-suggestion-pg-foreign",
        ))
        raw_cross_scope_event = {
            "event_id": "raw-cross-workspace-event",
            "session_id": "session-pg",
            "workspace_id": workspace.workspace_id,
            "actor_id": "suggestion-pg-user",
            "event_type": "candidate_previewed",
            "revision_before": 1,
            "revision_after": None,
            "suggestion_set_id": "set-pg-foreign",
            "candidate_id": "candidate-pg-foreign",
            "context_hash": sha_char(7),
            "policy_version": "controlled-ranker-v1",
            "provider_snapshot_id": "controlled-provider-snapshot-1",
            "rank_position": 1,
            "latency_ms": None,
            "reason_code": None,
            "payload": {},
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    """
                    INSERT INTO recommendation_events (
                        event_id, session_id, workspace_id, actor_id, event_type,
                        suggestion_set_id, candidate_id, revision_before, revision_after,
                        context_hash, policy_version, provider_snapshot_id, rank_position,
                        occurred_at, event_json
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::jsonb)
                    """,
                    raw_cross_scope_event["event_id"],
                    "session-pg",
                    workspace.workspace_id,
                    "suggestion-pg-user",
                    "candidate_previewed",
                    "set-pg-foreign",
                    "candidate-pg-foreign",
                    1,
                    None,
                    sha_char(7),
                    "controlled-ranker-v1",
                    "controlled-provider-snapshot-1",
                    1,
                    datetime.fromisoformat(raw_cross_scope_event["occurred_at"]),
                    json.dumps(raw_cross_scope_event),
                )
            tampered_json = {
                **raw_cross_scope_event,
                "event_id": "raw-tampered-json-event",
                "workspace_id": "workspace-suggestion-pg-foreign",
                "suggestion_set_id": "set-pg-1",
                "candidate_id": "candidate-pg-1",
            }
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO recommendation_events (
                        event_id, session_id, workspace_id, actor_id, event_type,
                        suggestion_set_id, candidate_id, revision_before, revision_after,
                        context_hash, policy_version, provider_snapshot_id, rank_position,
                        occurred_at, event_json
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::jsonb)
                    """,
                    tampered_json["event_id"],
                    "session-pg",
                    workspace.workspace_id,
                    "suggestion-pg-user",
                    "candidate_previewed",
                    "set-pg-1",
                    "candidate-pg-1",
                    1,
                    None,
                    sha_char(7),
                    "controlled-ranker-v1",
                    "controlled-provider-snapshot-1",
                    1,
                    datetime.fromisoformat(tampered_json["occurred_at"]),
                    json.dumps(tampered_json),
                )
        accepted = await service.accept(
            workspace_id=workspace.workspace_id,
            suggestion_set_id="set-pg-1",
            candidate_id="candidate-pg-1",
            if_match_revision=1,
            idempotency_key="accept-pg-1",
            actor_user_id="suggestion-pg-user",
        )
        async with pool.acquire() as conn:
            stored_change_summary = await conn.fetchval(
                "SELECT change_summary_json FROM itinerary_revisions "
                "WHERE workspace_id=$1 AND revision=$2",
                workspace.workspace_id,
                accepted.new_revision,
            )
        stored_change_summary = (
            json.loads(stored_change_summary)
            if isinstance(stored_change_summary, str)
            else stored_change_summary
        )
        route_receipts = stored_change_summary["route_delta"]["route_receipts"]
        assert len(route_receipts) == 1
        assert route_receipts[0]["origin_place_id"] == "anchor-place-pg"
        assert route_receipts[0]["destination_place_id"] == "place-pg-1"
        assert route_receipts[0]["execution_mode"] == "fixture"
        replay = await SuggestionSetService(
            PostgresSuggestionRepository(pool), PostgresItineraryRepository(pool)
        ).accept(
            workspace_id=workspace.workspace_id,
            suggestion_set_id="set-pg-1",
            candidate_id="candidate-pg-1",
            if_match_revision=1,
            idempotency_key="accept-pg-1",
            actor_user_id="suggestion-pg-user",
        )
        assert accepted.new_revision == replay.new_revision == 2
        assert replay.idempotent_replay is True
        assert (await PostgresSuggestionRepository(pool).get_set(workspace.workspace_id, "set-pg-1")) is not None

        # The newly accepted stop is a valid persisted anchor for the next set.
        await service.create_from_ranked(_input(
            set_id="set-pg-race", base_revision=2, after_stop_id=accepted.stop_id,
            candidate_id="candidate-pg-race", place_id="place-pg-race",
            after_place_id="place-pg-1",
        ))
        race_services = [
            SuggestionSetService(PostgresSuggestionRepository(pool), PostgresItineraryRepository(pool))
            for _ in range(2)
        ]

        async def race(index: int):
            return await race_services[index].accept(
                workspace_id=workspace.workspace_id,
                suggestion_set_id="set-pg-race",
                candidate_id="candidate-pg-race",
                if_match_revision=2,
                idempotency_key=f"race-pg-{index}",
                actor_user_id="suggestion-pg-user",
            )

        outcomes = await asyncio.gather(race(0), race(1), return_exceptions=True)
        assert sum(not isinstance(item, Exception) for item in outcomes) == 1
        assert sum(isinstance(item, RevisionConflictError) for item in outcomes) == 1
        successful = next(item for item in outcomes if not isinstance(item, Exception))
        assert successful.new_revision == 3

        await service.create_from_ranked(_input(
            set_id="set-pg-rollback", base_revision=3, after_stop_id=successful.stop_id,
            candidate_id="candidate-pg-rollback", place_id="place-pg-rollback",
            after_place_id="place-pg-race",
        ))
        async with pool.acquire() as conn:
            before = {
                "revisions": await conn.fetchval("SELECT COUNT(*) FROM itinerary_revisions"),
                "places": await conn.fetchval("SELECT COUNT(*) FROM room_places"),
                "receipts": await conn.fetchval("SELECT COUNT(*) FROM itinerary_place_receipts"),
                "events": await conn.fetchval("SELECT COUNT(*) FROM recommendation_events"),
                "commands": await conn.fetchval("SELECT COUNT(*) FROM suggestion_accept_commands"),
            }
            await conn.execute(
                """
                CREATE OR REPLACE FUNCTION fail_suggestion_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.place_id = 'place-pg-rollback' THEN
                        RAISE EXCEPTION 'controlled suggestion rollback';
                    END IF;
                    RETURN NEW;
                END;
                $$;
                CREATE TRIGGER fail_suggestion_receipt_trigger
                BEFORE INSERT ON itinerary_place_receipts
                FOR EACH ROW EXECUTE FUNCTION fail_suggestion_receipt();
                """
            )
        with pytest.raises(Exception, match="controlled suggestion rollback"):
            await service.accept(
                workspace_id=workspace.workspace_id,
                suggestion_set_id="set-pg-rollback",
                candidate_id="candidate-pg-rollback",
                if_match_revision=3,
                idempotency_key="rollback-pg",
                actor_user_id="suggestion-pg-user",
            )
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT current_itinerary_revision FROM trip_workspaces WHERE workspace_id=$1",
                workspace.workspace_id,
            ) == 3
            assert await conn.fetchval("SELECT COUNT(*) FROM itinerary_revisions") == before["revisions"]
            assert await conn.fetchval("SELECT COUNT(*) FROM room_places") == before["places"]
            assert await conn.fetchval("SELECT COUNT(*) FROM itinerary_place_receipts") == before["receipts"]
            assert await conn.fetchval("SELECT COUNT(*) FROM recommendation_events") == before["events"]
            assert await conn.fetchval("SELECT COUNT(*) FROM suggestion_accept_commands") == before["commands"]

        # The successful race acceptance is revision 3. Public Undo must bind
        # revision 4 and stop_undone to that exact accepted lineage in one TX.
        undo_command = ItineraryEditCommand(
            command_id="undo-pg-atomic",
            workspace_id=workspace.workspace_id,
            base_revision=3,
            actor_user_id="suggestion-pg-user",
            operation=EditOperation.UNDO,
            payload={"target_revision": 2},
        )
        atomic_undo = AtomicSuggestionUndoService(PostgresSuggestionRepository(pool))
        undone = await atomic_undo.apply_if_accepted_suggestion(
            undo_command,
            if_match_revision=3,
            idempotency_key="undo-pg-atomic-key",
        )
        replayed_undo = await atomic_undo.apply_if_accepted_suggestion(
            undo_command,
            if_match_revision=3,
            idempotency_key="undo-pg-atomic-key",
        )
        assert undone is not None and undone.new_revision == 4
        assert replayed_undo is not None and replayed_undo.idempotent_replay is True
        async with pool.acquire() as conn:
            link = await conn.fetchrow(
                "SELECT * FROM suggestion_undo_links WHERE undo_command_id='undo-pg-atomic'"
            )
            event = await conn.fetchrow(
                "SELECT * FROM recommendation_events WHERE event_id=$1",
                link["event_id"],
            )
            source_event = await conn.fetchrow(
                "SELECT * FROM recommendation_events WHERE event_id=$1",
                link["source_accept_event_id"],
            )
            assert link["accepted_revision"] == 3
            assert link["target_revision"] == 2
            assert link["result_revision"] == 4
            assert event["event_type"] == "stop_undone"
            assert event["revision_before"] == 3 and event["revision_after"] == 4
            assert source_event["event_type"] == "candidate_accepted"
            assert source_event["revision_after"] == 3

        # A trigger failure at the final lineage-link write proves revision,
        # pointer, itinerary command and event all roll back together.
        await service.create_from_ranked(_input(
            set_id="set-pg-undo-rollback",
            base_revision=4,
            after_stop_id=accepted.stop_id,
            candidate_id="candidate-pg-undo-rollback",
            place_id="place-pg-undo-rollback",
            after_place_id="place-pg-1",
        ))
        accepted_for_rollback = await service.accept(
            workspace_id=workspace.workspace_id,
            suggestion_set_id="set-pg-undo-rollback",
            candidate_id="candidate-pg-undo-rollback",
            if_match_revision=4,
            idempotency_key="accept-pg-before-undo-rollback",
            actor_user_id="suggestion-pg-user",
        )
        assert accepted_for_rollback.new_revision == 5
        async with pool.acquire() as conn:
            undo_before = {
                "revisions": await conn.fetchval("SELECT COUNT(*) FROM itinerary_revisions"),
                "commands": await conn.fetchval("SELECT COUNT(*) FROM itinerary_edit_commands"),
                "events": await conn.fetchval("SELECT COUNT(*) FROM recommendation_events"),
                "links": await conn.fetchval("SELECT COUNT(*) FROM suggestion_undo_links"),
            }
            await conn.execute(
                """
                CREATE OR REPLACE FUNCTION fail_atomic_suggestion_undo_link()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.undo_command_id = 'undo-pg-controlled-rollback' THEN
                        RAISE EXCEPTION 'controlled atomic suggestion undo rollback';
                    END IF;
                    RETURN NEW;
                END;
                $$;
                CREATE TRIGGER fail_atomic_suggestion_undo_link_trigger
                BEFORE INSERT ON suggestion_undo_links
                FOR EACH ROW EXECUTE FUNCTION fail_atomic_suggestion_undo_link();
                """
            )
        with pytest.raises(Exception, match="controlled atomic suggestion undo rollback"):
            await atomic_undo.apply_if_accepted_suggestion(
                ItineraryEditCommand(
                    command_id="undo-pg-controlled-rollback",
                    workspace_id=workspace.workspace_id,
                    base_revision=5,
                    actor_user_id="suggestion-pg-user",
                    operation=EditOperation.UNDO,
                    payload={"target_revision": 4},
                ),
                if_match_revision=5,
                idempotency_key="undo-pg-controlled-rollback-key",
            )
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT current_itinerary_revision FROM trip_workspaces WHERE workspace_id=$1",
                workspace.workspace_id,
            ) == 5
            assert await conn.fetchval("SELECT COUNT(*) FROM itinerary_revisions") == undo_before["revisions"]
            assert await conn.fetchval("SELECT COUNT(*) FROM itinerary_edit_commands") == undo_before["commands"]
            assert await conn.fetchval("SELECT COUNT(*) FROM recommendation_events") == undo_before["events"]
            assert await conn.fetchval("SELECT COUNT(*) FROM suggestion_undo_links") == undo_before["links"]
            await conn.execute("DROP TRIGGER fail_atomic_suggestion_undo_link_trigger ON suggestion_undo_links")

        # Raw SQL cannot bind a command/event owned by the first workspace to
        # the valid foreign workspace; the composite workspace FKs reject it.
        manual_command = ItineraryEditCommand(
            command_id="manual-command-for-cross-scope-link",
            workspace_id=workspace.workspace_id,
            base_revision=5,
            actor_user_id="suggestion-pg-user",
            operation=EditOperation.LOCK_STOP,
            payload={"stop_id": "anchor-pg"},
        )
        await RevisionCommandService(itineraries).apply(
            manual_command,
            if_match_revision=5,
            idempotency_key="manual-command-for-cross-scope-link",
        )
        async with pool.acquire() as conn:
            source_event_id = await conn.fetchval(
                "SELECT event_id FROM recommendation_events "
                "WHERE workspace_id=$1 AND event_type='candidate_accepted' AND revision_after=5",
                workspace.workspace_id,
            )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    """
                    INSERT INTO suggestion_undo_links (
                        undo_command_id, event_id, source_accept_event_id, workspace_id,
                        suggestion_set_id, candidate_id, accepted_revision,
                        target_revision, result_revision
                    ) VALUES ($1,$2,$3,$4,$5,$6,2,1,3)
                    """,
                    manual_command.command_id,
                    source_event_id,
                    source_event_id,
                    "workspace-suggestion-pg-foreign",
                    "set-pg-undo-rollback",
                    "candidate-pg-undo-rollback",
                )
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
