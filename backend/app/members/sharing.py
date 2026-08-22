"""Capability-scoped, revocable links for an immutable workspace revision.

Raw bearer values are never persisted.  A link only grants its captured scope,
revision and (for input links) named recipient; it never becomes a generic
workspace-edit credential.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ShareScope(str, Enum):
    REPORT_READ = "REPORT_READ"
    CONSTRAINT_WRITE = "CONSTRAINT_WRITE"
    ACKNOWLEDGE = "ACKNOWLEDGE"
    WORKSPACE_EDIT = "WORKSPACE_EDIT"


class ShareResponseAction(str, Enum):
    ACKNOWLEDGE = "ACKNOWLEDGE"
    CONSTRAINT = "CONSTRAINT"


class ShareLink(BaseModel):
    model_config = ConfigDict(frozen=True)

    share_link_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    itinerary_revision: int = Field(gt=0)
    report_id: str | None = None
    scopes: set[ShareScope] = Field(min_length=1)
    recipient_member_id: str | None = Field(default=None, min_length=1)
    created_by: str = Field(min_length=1)
    expires_at: datetime
    revoked_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_input_scope_recipient(self) -> "ShareLink":
        input_scopes = {ShareScope.CONSTRAINT_WRITE, ShareScope.ACKNOWLEDGE}
        if self.scopes & input_scopes and not self.recipient_member_id:
            raise ValueError("input share scopes require recipient_member_id")
        return self


class IssuedShareLink(BaseModel):
    """The raw token is deliberately returned only from create."""

    link: ShareLink
    token: str = Field(min_length=32)


class ShareResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    response_id: str = Field(min_length=1)
    share_link_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    itinerary_revision: int = Field(gt=0)
    member_id: str = Field(min_length=1)
    action: ShareResponseAction
    member_constraint_revision: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ShareLinkUnavailableError(Exception):
    """Unknown, expired and revoked links deliberately share one result."""


class ShareScopeDeniedError(Exception):
    pass


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_bearer_token() -> str:
    return secrets.token_urlsafe(32)


class ShareLinkRepository(Protocol):
    async def create_link(self, link: ShareLink, *, token_hash: str) -> ShareLink: ...
    async def find_by_token_hash(self, token_hash: str) -> ShareLink | None: ...
    async def revoke_link(self, workspace_id: str, share_link_id: str, *, revoked_at: datetime) -> ShareLink | None: ...
    async def list_links(self, workspace_id: str) -> list[ShareLink]: ...
    async def append_response(self, response: ShareResponse) -> ShareResponse: ...
    async def list_responses(self, workspace_id: str) -> list[ShareResponse]: ...


class InMemoryShareLinkRepository:
    def __init__(self):
        self.links: dict[str, ShareLink] = {}
        self._by_hash: dict[str, str] = {}
        self.responses: list[ShareResponse] = []
        self._lock = asyncio.Lock()

    async def create_link(self, link: ShareLink, *, token_hash: str) -> ShareLink:
        async with self._lock:
            if token_hash in self._by_hash:
                raise RuntimeError("share token digest collision")
            self.links[link.share_link_id] = link
            self._by_hash[token_hash] = link.share_link_id
            return link

    async def find_by_token_hash(self, token_hash: str) -> ShareLink | None:
        link_id = self._by_hash.get(token_hash)
        return self.links.get(link_id) if link_id else None

    async def revoke_link(self, workspace_id: str, share_link_id: str, *, revoked_at: datetime) -> ShareLink | None:
        async with self._lock:
            link = self.links.get(share_link_id)
            if link is None or link.workspace_id != workspace_id:
                return None
            if link.revoked_at is None:
                link = link.model_copy(update={"revoked_at": revoked_at})
                self.links[share_link_id] = link
            return link

    async def list_links(self, workspace_id: str) -> list[ShareLink]:
        return sorted((item for item in self.links.values() if item.workspace_id == workspace_id), key=lambda item: (item.created_at, item.share_link_id))

    async def append_response(self, response: ShareResponse) -> ShareResponse:
        async with self._lock:
            self.responses.append(response)
        return response

    async def list_responses(self, workspace_id: str) -> list[ShareResponse]:
        return [item for item in self.responses if item.workspace_id == workspace_id]


class PostgresShareLinkRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        if self._pool is not None:
            return self._pool
        from app.db.connection import get_pool
        return await get_pool()

    @staticmethod
    def _link(row: Any) -> ShareLink:
        return ShareLink(share_link_id=row["share_link_id"], workspace_id=row["workspace_id"], itinerary_revision=row["itinerary_revision"], report_id=row["report_id"], scopes=set(row["scopes"]), recipient_member_id=row["recipient_member_id"], created_by=row["created_by"], expires_at=row["expires_at"], revoked_at=row["revoked_at"], created_at=row["created_at"])

    @staticmethod
    def _response(row: Any) -> ShareResponse:
        return ShareResponse(response_id=row["response_id"], share_link_id=row["share_link_id"], workspace_id=row["workspace_id"], itinerary_revision=row["itinerary_revision"], member_id=row["member_id"], action=row["action"], member_constraint_revision=row["member_constraint_revision"], created_at=row["created_at"])

    async def create_link(self, link: ShareLink, *, token_hash: str) -> ShareLink:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""INSERT INTO trip_share_links (share_link_id, workspace_id, itinerary_revision, report_id, token_hash, scopes, recipient_member_id, created_by, expires_at, created_at) VALUES ($1,$2,$3,$4,$5,$6::text[],$7,$8,$9,$10) RETURNING *""", link.share_link_id, link.workspace_id, link.itinerary_revision, link.report_id, token_hash, [item.value for item in sorted(link.scopes, key=lambda item: item.value)], link.recipient_member_id, link.created_by, link.expires_at, link.created_at)
        return self._link(row)

    async def find_by_token_hash(self, token_hash: str) -> ShareLink | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM trip_share_links WHERE token_hash = $1", token_hash)
        return self._link(row) if row else None

    async def revoke_link(self, workspace_id: str, share_link_id: str, *, revoked_at: datetime) -> ShareLink | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE trip_share_links SET revoked_at = COALESCE(revoked_at, $3) WHERE workspace_id = $1 AND share_link_id = $2 RETURNING *", workspace_id, share_link_id, revoked_at)
        return self._link(row) if row else None

    async def list_links(self, workspace_id: str) -> list[ShareLink]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM trip_share_links WHERE workspace_id = $1 ORDER BY created_at, share_link_id", workspace_id)
        return [self._link(row) for row in rows]

    async def append_response(self, response: ShareResponse) -> ShareResponse:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""INSERT INTO trip_share_link_responses (response_id, share_link_id, workspace_id, itinerary_revision, member_id, action, member_constraint_revision, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *""", response.response_id, response.share_link_id, response.workspace_id, response.itinerary_revision, response.member_id, response.action.value, response.member_constraint_revision, response.created_at)
        return self._response(row)

    async def list_responses(self, workspace_id: str) -> list[ShareResponse]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM trip_share_link_responses WHERE workspace_id = $1 ORDER BY created_at, response_id", workspace_id)
        return [self._response(row) for row in rows]


class ShareLinkService:
    def __init__(self, repository: ShareLinkRepository):
        self.repository = repository

    async def issue(self, *, workspace_id: str, itinerary_revision: int, report_id: str | None, scopes: set[ShareScope], recipient_member_id: str | None, created_by: str, expires_at: datetime) -> IssuedShareLink:
        token = new_bearer_token()
        link = ShareLink(share_link_id=str(uuid4()), workspace_id=workspace_id, itinerary_revision=itinerary_revision, report_id=report_id, scopes=scopes, recipient_member_id=recipient_member_id, created_by=created_by, expires_at=expires_at)
        await self.repository.create_link(link, token_hash=token_digest(token))
        return IssuedShareLink(link=link, token=token)

    async def resolve(self, token: str, *, required_scope: ShareScope, now: datetime | None = None) -> ShareLink:
        link = await self.repository.find_by_token_hash(token_digest(token))
        now = now or datetime.now(timezone.utc)
        if link is None or link.revoked_at is not None or link.expires_at <= now:
            raise ShareLinkUnavailableError()
        if required_scope not in link.scopes:
            raise ShareScopeDeniedError()
        return link

    async def revoke(self, workspace_id: str, share_link_id: str) -> ShareLink | None:
        return await self.repository.revoke_link(workspace_id, share_link_id, revoked_at=datetime.now(timezone.utc))

    async def record_response(self, link: ShareLink, *, action: ShareResponseAction, member_constraint_revision: int | None = None) -> ShareResponse:
        if link.recipient_member_id is None:
            raise ShareScopeDeniedError()
        result = ShareResponse(response_id=str(uuid4()), share_link_id=link.share_link_id, workspace_id=link.workspace_id, itinerary_revision=link.itinerary_revision, member_id=link.recipient_member_id, action=action, member_constraint_revision=member_constraint_revision)
        return await self.repository.append_response(result)
