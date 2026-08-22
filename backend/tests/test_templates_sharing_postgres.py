"""P5/P7 data survives a fresh repository and process-pool boundary."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.itineraries.models import TripDateRange
from app.itineraries.repositories import PostgresItineraryRepository
from app.itineraries.revision_service import RevisionService
from app.members.sharing import (
    PostgresShareLinkRepository,
    ShareLinkService,
    ShareLinkUnavailableError,
    ShareResponseAction,
    ShareScope,
    token_digest,
)
from app.operations.repositories import PostgresCreationCommandRepository
from app.templates.application_service import TemplateApplicationService
from app.templates.models import TemplateProvenance, TemplateStatus
from app.templates.repositories import PostgresTemplateRepository
from app.templates.seed import model_generated_template_drafts


pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)


def _admin_dsn() -> str:
    return os.getenv(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )


@pytest.mark.asyncio
async def test_postgres_template_seed_apply_and_recipient_share_survive_repository_restart():
    """Exercise P5/P7 against only migration-created schema, not memory fakes."""
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")

    database_name = f"breezetravel_template_share_{uuid4().hex[:10]}"
    assert re.fullmatch(r"[a-z0-9_]+", database_name)
    admin = await asyncpg.connect(_admin_dsn())
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        database_dsn = f"{_admin_dsn().rsplit('/', 1)[0]}/{database_name}"
        bootstrap = await asyncpg.connect(database_dsn)
        try:
            await bootstrap.execute(Path("app/db/init.sql").read_text(encoding="utf-8"))
        finally:
            await bootstrap.close()

        environment = os.environ.copy()
        environment["DATABASE_URL"] = database_dsn.replace("postgresql://", "postgresql+asyncpg://")
        migrated = subprocess.run(
            [sys.executable, "-m", "scripts.migrate"],
            cwd=Path.cwd(),
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert migrated.returncode == 0, migrated.stderr

        pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=4)
        template_repository = PostgresTemplateRepository(pool)
        for template in model_generated_template_drafts():
            await template_repository.save_template(template)

        # A separately constructed repository proves the seed was committed,
        # and that DRAFT/MODEL_GENERATED labels were not rewritten at rest.
        durable_templates = PostgresTemplateRepository(pool)
        beijing_templates = await durable_templates.list_templates("北京")
        assert len(beijing_templates) == 5
        assert all(item.status is TemplateStatus.DRAFT for item in beijing_templates)
        assert all(item.provenance is TemplateProvenance.MODEL_GENERATED for item in beijing_templates)
        template = beijing_templates[0]
        assert (await durable_templates.get_template(template.template_id)) == template

        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users(user_id, nickname) VALUES ('template-owner', 'Owner'), ('template-recipient', 'Recipient')"
            )
            await conn.execute(
                "INSERT INTO rooms(room_id, thread_id, trip_city, trip_days) "
                "VALUES ('template-share-room', 'template-share-thread', '北京', 2)"
            )
            await conn.execute(
                "INSERT INTO room_members(room_id, user_id) VALUES "
                "('template-share-room', 'template-owner'), ('template-share-room', 'template-recipient')"
            )

        itineraries = PostgresItineraryRepository(pool)
        workspace = await RevisionService(itineraries).create_workspace(
            room_id="template-share-room",
            city="北京",
            date_range=TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2)),
            created_by="template-owner",
            workspace_id="template-share-workspace",
        )
        applied, replayed = await TemplateApplicationService(itineraries).apply_idempotent(
            workspace_id=workspace.workspace_id,
            template=template,
            actor_user_id="template-owner",
            idempotency_key="template-share-apply-v1",
            command_repository=PostgresCreationCommandRepository(pool),
        )
        assert replayed is False
        assert applied["revision"]["source_type"] == "TEMPLATE"
        assert applied["revision"]["change_summary"]["human_review_evidence"] is False

        # Recreate both client pool and repositories to model a backend restart.
        await pool.close()
        pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=4)
        restarted_templates = PostgresTemplateRepository(pool)
        restarted_itineraries = PostgresItineraryRepository(pool)
        restarted_template = await restarted_templates.get_template(template.template_id)
        assert restarted_template == template
        restarted_workspace = await restarted_itineraries.get_workspace(workspace.workspace_id)
        assert restarted_workspace is not None
        assert restarted_workspace.current_itinerary_revision == 1
        persisted_revision = await restarted_itineraries.get_revision(workspace.workspace_id, 1)
        assert persisted_revision is not None
        assert persisted_revision.source_type.value == "TEMPLATE"
        assert persisted_revision.change_summary["template_id"] == template.template_id
        assert persisted_revision.change_summary["template_provenance"] == "MODEL_GENERATED"

        replay, replayed = await TemplateApplicationService(restarted_itineraries).apply_idempotent(
            workspace_id=workspace.workspace_id,
            template=restarted_template,
            actor_user_id="template-owner",
            idempotency_key="template-share-apply-v1",
            command_repository=PostgresCreationCommandRepository(pool),
        )
        assert replayed is True
        assert replay["revision"]["content_hash"] == applied["revision"]["content_hash"]

        share_service = ShareLinkService(PostgresShareLinkRepository(pool))
        issued = await share_service.issue(
            workspace_id=workspace.workspace_id,
            itinerary_revision=1,
            report_id=None,
            scopes={ShareScope.REPORT_READ, ShareScope.ACKNOWLEDGE},
            recipient_member_id="template-recipient",
            created_by="template-owner",
            expires_at=_NOW + timedelta(days=7),
        )
        # The only stored bearer representation is the SHA-256 digest.  The
        # public read model cannot return a raw token after this issuance call.
        async with pool.acquire() as conn:
            token_row = await conn.fetchrow(
                "SELECT token_hash FROM trip_share_links WHERE share_link_id = $1",
                issued.link.share_link_id,
            )
            columns = await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'trip_share_links'"
            )
        assert token_row["token_hash"].strip() == token_digest(issued.token)
        assert issued.token not in token_row["token_hash"]
        assert "token" not in {row["column_name"] for row in columns}

        # A new repository has no process-local link/response state to rely on.
        restarted_shares = ShareLinkService(PostgresShareLinkRepository(pool))
        resolved = await restarted_shares.resolve(
            issued.token,
            required_scope=ShareScope.REPORT_READ,
            now=_NOW,
        )
        assert resolved == issued.link
        assert "token" not in resolved.model_dump()
        response = await restarted_shares.record_response(
            resolved,
            action=ShareResponseAction.ACKNOWLEDGE,
        )
        assert response.member_id == "template-recipient"
        assert response.itinerary_revision == 1

        # Expiry is checked from the persisted immutable link, and does not
        # delete or mutate the acknowledgement record.
        with pytest.raises(ShareLinkUnavailableError):
            await restarted_shares.resolve(
                issued.token,
                required_scope=ShareScope.REPORT_READ,
                now=issued.link.expires_at + timedelta(seconds=1),
            )
        revoked = await restarted_shares.revoke(workspace.workspace_id, issued.link.share_link_id)
        assert revoked is not None
        assert revoked.revoked_at is not None

        await pool.close()
        pool = await asyncpg.create_pool(database_dsn, min_size=1, max_size=4)
        final_repository = PostgresShareLinkRepository(pool)
        final_service = ShareLinkService(final_repository)
        links = await final_repository.list_links(workspace.workspace_id)
        assert links == [revoked]
        assert await final_repository.list_responses(workspace.workspace_id) == [response]
        with pytest.raises(ShareLinkUnavailableError):
            await final_service.resolve(issued.token, required_scope=ShareScope.REPORT_READ, now=_NOW)
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
