from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol

from app.db.connection import get_pool
from app.itineraries.errors import ResourceNotFound, RevisionConflictError
from app.itineraries.models import WorkspaceStatus
from app.members.models import MemberConstraint, MemberConstraintDraft, TravelerProfile


class MemberConstraintRepository(Protocol):
    async def save_profile(self, profile: TravelerProfile) -> TravelerProfile: ...

    async def get_profile(self, workspace_id: str, member_id: str) -> TravelerProfile | None: ...

    async def list_profiles(self, workspace_id: str) -> list[TravelerProfile]: ...

    async def append_constraint(
        self,
        workspace_id: str,
        draft: MemberConstraintDraft,
        *,
        expected_base_revision: int,
    ) -> tuple[MemberConstraint, str | None]: ...

    async def list_effective_constraints(
        self,
        workspace_id: str,
        revision: int,
    ) -> list[MemberConstraint]: ...


def _profile_from_json(workspace_id: str, member_id: str, value: Any, confirmed_revision: int | None):
    data = json.loads(value) if isinstance(value, str) else dict(value)
    data.update({
        "workspace_id": workspace_id,
        "member_id": member_id,
        "confirmed_revision": confirmed_revision,
    })
    return TravelerProfile.model_validate(data)


def _constraint_from_row(row: Any) -> MemberConstraint:
    value = row["value_json"]
    if isinstance(value, str):
        value = json.loads(value)
    return MemberConstraint(
        constraint_id=row["constraint_id"],
        workspace_id=row["workspace_id"],
        owner_member_id=row["owner_member_id"],
        type=row["constraint_type"],
        operator=row["operator"],
        value=value,
        hardness=row["hardness"],
        priority=row["priority"],
        source=row["source"],
        confirmation_status=row["confirmation_status"],
        waivable_by=list(row["waivable_by"] or []),
        revision=row["revision"],
    )


class PostgresMemberConstraintRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    async def save_profile(self, profile: TravelerProfile) -> TravelerProfile:
        pool = await self._get_pool()
        payload = profile.model_dump(mode="json", exclude={"workspace_id", "member_id", "confirmed_revision"})
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO traveler_profiles (
                    workspace_id, member_id, profile_json, confirmed_revision
                ) VALUES ($1, $2, $3::jsonb, $4)
                ON CONFLICT (workspace_id, member_id) DO UPDATE
                SET profile_json = EXCLUDED.profile_json,
                    confirmed_revision = EXCLUDED.confirmed_revision,
                    updated_at = NOW()
                RETURNING *
                """,
                profile.workspace_id,
                profile.member_id,
                json.dumps(payload, ensure_ascii=False),
                profile.confirmed_revision,
            )
        return _profile_from_json(
            row["workspace_id"], row["member_id"], row["profile_json"], row["confirmed_revision"]
        )

    async def get_profile(self, workspace_id: str, member_id: str) -> TravelerProfile | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM traveler_profiles WHERE workspace_id = $1 AND member_id = $2",
                workspace_id,
                member_id,
            )
        if row is None:
            return None
        return _profile_from_json(
            row["workspace_id"], row["member_id"], row["profile_json"], row["confirmed_revision"]
        )

    async def list_profiles(self, workspace_id: str) -> list[TravelerProfile]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM traveler_profiles WHERE workspace_id = $1 ORDER BY member_id", workspace_id
            )
        return [_profile_from_json(row["workspace_id"], row["member_id"], row["profile_json"], row["confirmed_revision"]) for row in rows]

    async def append_constraint(
        self,
        workspace_id: str,
        draft: MemberConstraintDraft,
        *,
        expected_base_revision: int,
    ) -> tuple[MemberConstraint, str | None]:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            workspace = await conn.fetchrow(
                "SELECT current_member_constraint_revision, current_report_id "
                "FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                workspace_id,
            )
            if workspace is None:
                raise ResourceNotFound("workspace does not exist")
            actual = workspace["current_member_constraint_revision"] or 0
            if actual != expected_base_revision:
                raise RevisionConflictError(
                    "member constraint base revision is stale",
                    context={"expected_revision": expected_base_revision, "actual_revision": actual},
                )
            next_revision = actual + 1
            await conn.execute(
                """
                INSERT INTO member_constraints (
                    constraint_id, workspace_id, owner_member_id, constraint_type,
                    operator, value_json, hardness, priority, source,
                    confirmation_status, waivable_by, revision
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, $12)
                """,
                draft.constraint_id,
                workspace_id,
                draft.owner_member_id,
                draft.type,
                draft.operator,
                json.dumps(draft.value, ensure_ascii=False),
                draft.hardness.value,
                draft.priority,
                draft.source.value,
                draft.confirmation_status.value,
                draft.waivable_by,
                next_revision,
            )
            stale_report_id = workspace["current_report_id"]
            await conn.execute(
                """
                UPDATE trip_workspaces
                SET current_member_constraint_revision = $2,
                    current_report_id = NULL,
                    status = $3,
                    updated_at = NOW()
                WHERE workspace_id = $1
                """,
                workspace_id,
                next_revision,
                WorkspaceStatus.NEEDS_CONFIRMATION.value,
            )
        return MemberConstraint(
            **draft.model_dump(), workspace_id=workspace_id, revision=next_revision
        ), stale_report_id

    async def list_effective_constraints(
        self,
        workspace_id: str,
        revision: int,
    ) -> list[MemberConstraint]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (constraint_id) *
                FROM member_constraints
                WHERE workspace_id = $1 AND revision <= $2
                ORDER BY constraint_id, revision DESC
                """,
                workspace_id,
                revision,
            )
        return sorted((_constraint_from_row(row) for row in rows), key=lambda item: item.constraint_id)


class InMemoryMemberConstraintRepository:
    def __init__(self, workspaces: dict[str, Any]):
        self.workspaces = workspaces
        self.profiles: dict[tuple[str, str], TravelerProfile] = {}
        self.constraints: list[MemberConstraint] = []
        self._lock = asyncio.Lock()

    async def save_profile(self, profile: TravelerProfile) -> TravelerProfile:
        if profile.workspace_id not in self.workspaces:
            raise ResourceNotFound("workspace does not exist")
        self.profiles[(profile.workspace_id, profile.member_id)] = profile
        return profile

    async def get_profile(self, workspace_id: str, member_id: str) -> TravelerProfile | None:
        return self.profiles.get((workspace_id, member_id))

    async def list_profiles(self, workspace_id: str) -> list[TravelerProfile]:
        return [profile for (profile_workspace_id, _), profile in self.profiles.items() if profile_workspace_id == workspace_id]

    async def append_constraint(
        self,
        workspace_id: str,
        draft: MemberConstraintDraft,
        *,
        expected_base_revision: int,
    ) -> tuple[MemberConstraint, str | None]:
        async with self._lock:
            workspace = self.workspaces.get(workspace_id)
            if workspace is None:
                raise ResourceNotFound("workspace does not exist")
            actual = workspace.current_member_constraint_revision or 0
            if actual != expected_base_revision:
                raise RevisionConflictError(
                    "member constraint base revision is stale",
                    context={"expected_revision": expected_base_revision, "actual_revision": actual},
                )
            next_revision = actual + 1
            constraint = MemberConstraint(
                **draft.model_dump(), workspace_id=workspace_id, revision=next_revision
            )
            self.constraints.append(constraint)
            stale_report_id = workspace.current_report_id
            self.workspaces[workspace_id] = workspace.model_copy(update={
                "current_member_constraint_revision": next_revision,
                "current_report_id": None,
                "status": WorkspaceStatus.NEEDS_CONFIRMATION,
            })
            return constraint, stale_report_id

    async def list_effective_constraints(
        self,
        workspace_id: str,
        revision: int,
    ) -> list[MemberConstraint]:
        latest: dict[str, MemberConstraint] = {}
        for item in self.constraints:
            if item.workspace_id == workspace_id and item.revision <= revision:
                current = latest.get(item.constraint_id)
                if current is None or item.revision > current.revision:
                    latest[item.constraint_id] = item
        return [latest[key] for key in sorted(latest)]
