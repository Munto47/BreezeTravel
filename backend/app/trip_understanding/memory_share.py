from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from pydantic import Field, field_validator

from app.db.connection import get_pool
from app.trip_understanding.errors import (
    IdempotencyConflictError,
    ResourceAccessDeniedError,
    ResourceNotFoundError,
)
from app.trip_understanding.models import (
    PublicResourceRecord,
    StrictModel,
    UserFacingTripResult,
)


DINING_PREFERENCES = frozenset({"LOCAL", "VEGETARIAN", "HALAL", "NO_SPICY", "QUICK"})
HOTEL_PREFERENCES = frozenset({"CHAIN", "NEAR_TRANSIT", "QUIET", "CENTRAL"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_hash(value: object) -> str:
    return _sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _derive_secret(signing_key: str, *parts: str) -> str:
    material = "\0".join(parts).encode("utf-8")
    digest = hmac.new(signing_key.encode("utf-8"), material, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class DataConsentView(StrictModel):
    memory_enabled: bool = False
    feedback_enabled: bool = False
    training_eval_enabled: bool = False


class ConsentUpdateRequest(StrictModel):
    enabled: bool


class PreferenceMemoryView(StrictModel):
    walking_tolerance_minutes: int | None = Field(default=None, ge=5, le=120)
    preferred_start_time: str | None = Field(
        default=None,
        pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$",
    )
    dining_preferences: list[str] = Field(default_factory=list, max_length=3)
    hotel_preferences: list[str] = Field(default_factory=list, max_length=3)
    intensity: Literal["RELAXED", "BALANCED", "FULL"] | None = None

    @field_validator("dining_preferences")
    @classmethod
    def valid_dining_preferences(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or not set(value) <= DINING_PREFERENCES:
            raise ValueError("unsupported or duplicate dining preference")
        return value

    @field_validator("hotel_preferences")
    @classmethod
    def valid_hotel_preferences(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or not set(value) <= HOTEL_PREFERENCES:
            raise ValueError("unsupported or duplicate hotel preference")
        return value


class FeedbackRequest(StrictModel):
    event_type: Literal["CORRECTION", "ADOPTED", "REJECTED", "VOLUNTARY"]
    subject_type: Literal["TRIP", "ACTIVITY", "KNOWLEDGE_SUGGESTION"]
    subject_ref: str | None = Field(default=None, min_length=20, max_length=160)


class FeedbackAcceptedView(StrictModel):
    status: Literal["RECORDED"] = "RECORDED"


class ShareCreateRequest(StrictModel):
    expires_in_days: int = Field(default=7, ge=1, le=30)


class ShareCreatedView(StrictModel):
    share_url: str
    expires_at: datetime


class ShareListItemView(StrictModel):
    share_ref: str
    expires_at: datetime
    status: Literal["ACTIVE", "REVOKED", "EXPIRED"]


class SharedActivityView(StrictModel):
    name: str
    area_or_address: str
    time_hint: str | None = None
    note: Literal["可直接查看", "地点待确认"]


class SharedDayView(StrictModel):
    label: str
    activities: list[SharedActivityView]


class ShareProjectionView(StrictModel):
    title: str
    destination: str
    schedule: str
    party_size: str
    days: list[SharedDayView]
    accommodation: str | None = None
    message: str = "这是朋友分享的只读行程。"


class ShareExchangeRequest(StrictModel):
    secret: str = Field(min_length=40, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class ShareSessionOutcome(StrictModel):
    capability: str
    expires_at: datetime


def build_share_projection(result: UserFacingTripResult) -> ShareProjectionView:
    assumptions = {item.key: item.value for item in result.assumptions}
    destination = assumptions.get("destination", "目的地待确认")
    calendar = assumptions.get("calendar", "按天安排")
    party_size = assumptions.get("party_size", "2人")
    accommodation = next(
        (candidate.name for candidate in result.stay.candidates if candidate.selected),
        None,
    )
    return ShareProjectionView(
        title=f"{destination}行程",
        destination=destination,
        schedule=calendar,
        party_size=party_size,
        days=[
            SharedDayView(
                label=day.label,
                activities=[
                    SharedActivityView(
                        name=activity.name,
                        area_or_address=activity.area_or_address,
                        time_hint=activity.time_hint,
                        note=(
                            "可直接查看"
                            if activity.status == "READY"
                            else "地点待确认"
                        ),
                    )
                    for activity in day.activities
                ],
            )
            for day in result.days
        ],
        accommodation=accommodation,
    )


class MemoryShareRepository(Protocol):
    async def get_data_consents(self, user_id: str) -> DataConsentView: ...
    async def set_data_consent(
        self, user_id: str, purpose: str, enabled: bool, *, now: datetime
    ) -> DataConsentView: ...
    async def get_preference_memory(self, user_id: str) -> PreferenceMemoryView | None: ...
    async def save_preference_memory(
        self, user_id: str, value: PreferenceMemoryView, *, now: datetime
    ) -> PreferenceMemoryView: ...
    async def clear_preference_memory(self, user_id: str) -> None: ...
    async def record_feedback(
        self,
        resource: PublicResourceRecord,
        user_id: str,
        value: FeedbackRequest,
        *,
        idempotency_key: str,
        now: datetime,
    ) -> bool: ...
    async def create_share(
        self,
        resource: PublicResourceRecord,
        user_id: str,
        result: UserFacingTripResult,
        *,
        idempotency_key: str,
        expires_in_days: int,
        signing_key: str,
        now: datetime,
    ) -> tuple[ShareCreatedView, bool]: ...
    async def list_shares(self, user_id: str, *, now: datetime) -> list[ShareListItemView]: ...
    async def revoke_share(self, share_ref: str, user_id: str, *, now: datetime) -> bool: ...
    async def exchange_share_secret(
        self, share_ref: str, secret: str, *, now: datetime
    ) -> ShareSessionOutcome: ...
    async def read_share(
        self, share_ref: str, capability: str | None, *, now: datetime
    ) -> ShareProjectionView: ...


class PostgresMemoryShareRepositoryMixin:
    async def _memory_share_pool(self):
        pool = getattr(self, "_pool", None)
        return pool or await get_pool()

    async def get_data_consents(self, user_id: str) -> DataConsentView:
        pool = await self._memory_share_pool()
        row = await pool.fetchrow(
            """
            SELECT memory_enabled, feedback_enabled, training_eval_enabled
            FROM g06_data_consents WHERE user_id = $1
            """,
            user_id,
        )
        return DataConsentView.model_validate(dict(row)) if row else DataConsentView()

    async def set_data_consent(
        self, user_id: str, purpose: str, enabled: bool, *, now: datetime
    ) -> DataConsentView:
        columns = {
            "memory": "memory_enabled",
            "feedback": "feedback_enabled",
            "training-eval": "training_eval_enabled",
        }
        column = columns.get(purpose)
        if column is None:
            raise ValueError("unsupported consent purpose")
        pool = await self._memory_share_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                f"""
                INSERT INTO g06_data_consents(user_id, {column}, updated_at)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) DO UPDATE
                SET {column} = EXCLUDED.{column}, updated_at = EXCLUDED.updated_at
                """,
                user_id,
                enabled,
                now,
            )
            if not enabled and purpose == "memory":
                await conn.execute("DELETE FROM g06_preference_profiles WHERE user_id = $1", user_id)
            if not enabled and purpose == "feedback":
                await conn.execute("DELETE FROM g06_feedback_events WHERE owner_user_id = $1", user_id)
        return await self.get_data_consents(user_id)

    async def get_preference_memory(self, user_id: str) -> PreferenceMemoryView | None:
        pool = await self._memory_share_pool()
        row = await pool.fetchrow(
            """
            SELECT walking_tolerance_minutes, preferred_start_time,
                   dining_preferences, hotel_preferences, intensity
            FROM g06_preference_profiles WHERE user_id = $1
            """,
            user_id,
        )
        return PreferenceMemoryView.model_validate(dict(row)) if row else None

    async def save_preference_memory(
        self, user_id: str, value: PreferenceMemoryView, *, now: datetime
    ) -> PreferenceMemoryView:
        pool = await self._memory_share_pool()
        async with pool.acquire() as conn, conn.transaction():
            enabled = await conn.fetchval(
                "SELECT memory_enabled FROM g06_data_consents WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            if enabled is not True:
                raise ResourceAccessDeniedError("preference memory is not enabled")
            row = await conn.fetchrow(
                """
                INSERT INTO g06_preference_profiles(
                    user_id, walking_tolerance_minutes, preferred_start_time,
                    dining_preferences, hotel_preferences, intensity, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (user_id) DO UPDATE SET
                    walking_tolerance_minutes = EXCLUDED.walking_tolerance_minutes,
                    preferred_start_time = EXCLUDED.preferred_start_time,
                    dining_preferences = EXCLUDED.dining_preferences,
                    hotel_preferences = EXCLUDED.hotel_preferences,
                    intensity = EXCLUDED.intensity,
                    updated_at = EXCLUDED.updated_at
                RETURNING walking_tolerance_minutes, preferred_start_time,
                          dining_preferences, hotel_preferences, intensity
                """,
                user_id,
                value.walking_tolerance_minutes,
                value.preferred_start_time,
                value.dining_preferences,
                value.hotel_preferences,
                value.intensity,
                now,
            )
        return PreferenceMemoryView.model_validate(dict(row))

    async def clear_preference_memory(self, user_id: str) -> None:
        pool = await self._memory_share_pool()
        await pool.execute("DELETE FROM g06_preference_profiles WHERE user_id = $1", user_id)

    async def record_feedback(
        self,
        resource: PublicResourceRecord,
        user_id: str,
        value: FeedbackRequest,
        *,
        idempotency_key: str,
        now: datetime,
    ) -> bool:
        key_hash = _sha256(idempotency_key)
        payload = value.model_dump(mode="json")
        request_hash = _canonical_hash(payload)
        pool = await self._memory_share_pool()
        async with pool.acquire() as conn, conn.transaction():
            owner_user_id = await conn.fetchval(
                "SELECT owner_user_id FROM trip_understandings WHERE understanding_id = $1 FOR UPDATE",
                resource.understanding_id,
            )
            if owner_user_id != user_id:
                raise ResourceAccessDeniedError("only the trip owner can submit feedback")
            enabled = await conn.fetchval(
                "SELECT feedback_enabled FROM g06_data_consents WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            if enabled is not True:
                raise ResourceAccessDeniedError("product feedback is not enabled")
            existing = await conn.fetchrow(
                """
                SELECT request_hash FROM g06_feedback_events
                WHERE owner_user_id = $1 AND understanding_id = $2 AND key_hash = $3
                """,
                user_id,
                resource.understanding_id,
                key_hash,
            )
            if existing:
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyConflictError("feedback idempotency key was reused")
                return True
            await conn.execute(
                """
                INSERT INTO g06_feedback_events(
                    feedback_id, owner_user_id, understanding_id, event_type,
                    subject_type, subject_ref_hash, key_hash, request_hash, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                secrets.token_urlsafe(24),
                user_id,
                resource.understanding_id,
                value.event_type,
                value.subject_type,
                _sha256(value.subject_ref) if value.subject_ref else None,
                key_hash,
                request_hash,
                now,
            )
        return False

    async def create_share(
        self,
        resource: PublicResourceRecord,
        user_id: str,
        result: UserFacingTripResult,
        *,
        idempotency_key: str,
        expires_in_days: int,
        signing_key: str,
        now: datetime,
    ) -> tuple[ShareCreatedView, bool]:
        key_hash = _sha256(idempotency_key)
        projection = build_share_projection(result)
        request_hash = _canonical_hash(
            {"expires_in_days": expires_in_days, "projection": projection.model_dump(mode="json")}
        )
        share_ref = _derive_secret(
            signing_key, "g06-share-ref", user_id, resource.understanding_id, key_hash
        )[:32]
        secret = _derive_secret(
            signing_key, "g06-share-secret", user_id, resource.understanding_id, key_hash
        )
        expires_at = now + timedelta(days=expires_in_days)
        pool = await self._memory_share_pool()
        async with pool.acquire() as conn, conn.transaction():
            owner_user_id = await conn.fetchval(
                "SELECT owner_user_id FROM trip_understandings WHERE understanding_id = $1 FOR UPDATE",
                resource.understanding_id,
            )
            if owner_user_id != user_id:
                raise ResourceAccessDeniedError("only the trip owner can share")
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"g06-share:{user_id}:{resource.understanding_id}:{key_hash}",
            )
            existing = await conn.fetchrow(
                """
                SELECT request_hash, expires_at FROM g06_share_links
                WHERE owner_user_id = $1 AND understanding_id = $2 AND key_hash = $3
                """,
                user_id,
                resource.understanding_id,
                key_hash,
            )
            if existing:
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyConflictError("share idempotency key was reused")
                expires_at = existing["expires_at"]
                replayed = True
            else:
                await conn.execute(
                    """
                    INSERT INTO g06_share_links(
                        share_ref, understanding_id, owner_user_id, secret_hash,
                        projection_json, key_hash, request_hash, expires_at, created_at
                    ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                    """,
                    share_ref,
                    resource.understanding_id,
                    user_id,
                    _sha256(secret),
                    projection.model_dump_json(),
                    key_hash,
                    request_hash,
                    expires_at,
                    now,
                )
                replayed = False
        return ShareCreatedView(
            share_url=f"/share/{share_ref}#s={secret}", expires_at=expires_at
        ), replayed

    async def list_shares(self, user_id: str, *, now: datetime) -> list[ShareListItemView]:
        pool = await self._memory_share_pool()
        rows = await pool.fetch(
            """
            SELECT share_ref, expires_at, revoked_at
            FROM g06_share_links WHERE owner_user_id = $1
            ORDER BY created_at DESC
            """,
            user_id,
        )
        return [
            ShareListItemView(
                share_ref=row["share_ref"],
                expires_at=row["expires_at"],
                status=(
                    "REVOKED"
                    if row["revoked_at"] is not None
                    else "EXPIRED"
                    if row["expires_at"] <= now
                    else "ACTIVE"
                ),
            )
            for row in rows
        ]

    async def revoke_share(self, share_ref: str, user_id: str, *, now: datetime) -> bool:
        pool = await self._memory_share_pool()
        async with pool.acquire() as conn, conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE g06_share_links SET revoked_at = COALESCE(revoked_at, $3)
                WHERE share_ref = $1 AND owner_user_id = $2
                RETURNING share_ref
                """,
                share_ref,
                user_id,
                now,
            )
            if updated:
                await conn.execute("DELETE FROM g06_share_sessions WHERE share_ref = $1", share_ref)
        return updated is not None

    async def exchange_share_secret(
        self, share_ref: str, secret: str, *, now: datetime
    ) -> ShareSessionOutcome:
        pool = await self._memory_share_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT secret_hash, expires_at, revoked_at
                FROM g06_share_links WHERE share_ref = $1 FOR UPDATE
                """,
                share_ref,
            )
            if (
                row is None
                or row["revoked_at"] is not None
                or row["expires_at"] <= now
                or not hmac.compare_digest(row["secret_hash"].strip(), _sha256(secret))
            ):
                raise ResourceNotFoundError("share is unavailable")
            capability = secrets.token_urlsafe(32)
            expires_at = min(row["expires_at"], now + timedelta(hours=1))
            await conn.execute(
                """
                INSERT INTO g06_share_sessions(
                    session_id, share_ref, capability_hash, expires_at,
                    created_at, last_seen_at
                ) VALUES ($1, $2, $3, $4, $5, $5)
                """,
                secrets.token_urlsafe(24),
                share_ref,
                _sha256(capability),
                expires_at,
                now,
            )
        return ShareSessionOutcome(capability=capability, expires_at=expires_at)

    async def read_share(
        self, share_ref: str, capability: str | None, *, now: datetime
    ) -> ShareProjectionView:
        if not capability:
            raise ResourceNotFoundError("share is unavailable")
        pool = await self._memory_share_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT l.projection_json, l.expires_at AS link_expires_at,
                       l.revoked_at, s.session_id, s.expires_at AS session_expires_at
                FROM g06_share_sessions s
                JOIN g06_share_links l ON l.share_ref = s.share_ref
                WHERE l.share_ref = $1 AND s.capability_hash = $2
                FOR UPDATE OF s
                """,
                share_ref,
                _sha256(capability),
            )
            if (
                row is None
                or row["revoked_at"] is not None
                or row["link_expires_at"] <= now
                or row["session_expires_at"] <= now
            ):
                raise ResourceNotFoundError("share is unavailable")
            await conn.execute(
                "UPDATE g06_share_sessions SET last_seen_at = $2 WHERE session_id = $1",
                row["session_id"],
                now,
            )
        value = row["projection_json"]
        return ShareProjectionView.model_validate(
            json.loads(value) if isinstance(value, str) else value
        )


class InMemoryMemoryShareRepositoryMixin:
    def _init_memory_share_store(self) -> None:
        self.g06_consents: dict[str, DataConsentView] = {}
        self.g06_preferences: dict[str, PreferenceMemoryView] = {}
        self.g06_feedback: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {}
        self.g06_shares: dict[str, dict[str, Any]] = {}
        self.g06_share_keys: dict[tuple[str, str, str], str] = {}
        self.g06_share_sessions: dict[str, dict[str, Any]] = {}

    async def get_data_consents(self, user_id: str) -> DataConsentView:
        return self.g06_consents.get(user_id, DataConsentView()).model_copy(deep=True)

    async def set_data_consent(
        self, user_id: str, purpose: str, enabled: bool, *, now: datetime
    ) -> DataConsentView:
        del now
        fields = {
            "memory": "memory_enabled",
            "feedback": "feedback_enabled",
            "training-eval": "training_eval_enabled",
        }
        field = fields.get(purpose)
        if field is None:
            raise ValueError("unsupported consent purpose")
        current = self.g06_consents.get(user_id, DataConsentView())
        current = current.model_copy(update={field: enabled})
        self.g06_consents[user_id] = current
        if not enabled and purpose == "memory":
            self.g06_preferences.pop(user_id, None)
        if not enabled and purpose == "feedback":
            for key in [key for key in self.g06_feedback if key[0] == user_id]:
                self.g06_feedback.pop(key, None)
        return current.model_copy(deep=True)

    async def get_preference_memory(self, user_id: str) -> PreferenceMemoryView | None:
        value = self.g06_preferences.get(user_id)
        return value.model_copy(deep=True) if value else None

    async def save_preference_memory(
        self, user_id: str, value: PreferenceMemoryView, *, now: datetime
    ) -> PreferenceMemoryView:
        del now
        if not (await self.get_data_consents(user_id)).memory_enabled:
            raise ResourceAccessDeniedError("preference memory is not enabled")
        self.g06_preferences[user_id] = value.model_copy(deep=True)
        return value.model_copy(deep=True)

    async def clear_preference_memory(self, user_id: str) -> None:
        self.g06_preferences.pop(user_id, None)

    async def record_feedback(
        self,
        resource: PublicResourceRecord,
        user_id: str,
        value: FeedbackRequest,
        *,
        idempotency_key: str,
        now: datetime,
    ) -> bool:
        del now
        row = self.resources.get(resource.public_resource_id)
        if row is None or row["owner_user_id"] != user_id:
            raise ResourceAccessDeniedError("only the trip owner can submit feedback")
        if not (await self.get_data_consents(user_id)).feedback_enabled:
            raise ResourceAccessDeniedError("product feedback is not enabled")
        key = (user_id, resource.understanding_id, _sha256(idempotency_key))
        request_hash = _canonical_hash(value.model_dump(mode="json"))
        existing = self.g06_feedback.get(key)
        if existing:
            if existing[0] != request_hash:
                raise IdempotencyConflictError("feedback idempotency key was reused")
            return True
        self.g06_feedback[key] = (
            request_hash,
            {
                "event_type": value.event_type,
                "subject_type": value.subject_type,
                "subject_ref_hash": _sha256(value.subject_ref) if value.subject_ref else None,
            },
        )
        return False

    async def create_share(
        self,
        resource: PublicResourceRecord,
        user_id: str,
        result: UserFacingTripResult,
        *,
        idempotency_key: str,
        expires_in_days: int,
        signing_key: str,
        now: datetime,
    ) -> tuple[ShareCreatedView, bool]:
        row = self.resources.get(resource.public_resource_id)
        if row is None or row["owner_user_id"] != user_id:
            raise ResourceAccessDeniedError("only the trip owner can share")
        key_hash = _sha256(idempotency_key)
        projection = build_share_projection(result)
        request_hash = _canonical_hash(
            {"expires_in_days": expires_in_days, "projection": projection.model_dump(mode="json")}
        )
        key = (user_id, resource.understanding_id, key_hash)
        existing_ref = self.g06_share_keys.get(key)
        secret = _derive_secret(
            signing_key, "g06-share-secret", user_id, resource.understanding_id, key_hash
        )
        if existing_ref:
            existing = self.g06_shares[existing_ref]
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflictError("share idempotency key was reused")
            return ShareCreatedView(
                share_url=f"/share/{existing_ref}#s={secret}",
                expires_at=existing["expires_at"],
            ), True
        share_ref = _derive_secret(
            signing_key, "g06-share-ref", user_id, resource.understanding_id, key_hash
        )[:32]
        expires_at = now + timedelta(days=expires_in_days)
        self.g06_share_keys[key] = share_ref
        self.g06_shares[share_ref] = {
            "understanding_id": resource.understanding_id,
            "owner_user_id": user_id,
            "secret_hash": _sha256(secret),
            "projection": projection,
            "request_hash": request_hash,
            "expires_at": expires_at,
            "revoked_at": None,
        }
        return ShareCreatedView(
            share_url=f"/share/{share_ref}#s={secret}", expires_at=expires_at
        ), False

    async def list_shares(self, user_id: str, *, now: datetime) -> list[ShareListItemView]:
        return [
            ShareListItemView(
                share_ref=ref,
                expires_at=row["expires_at"],
                status=(
                    "REVOKED"
                    if row["revoked_at"] is not None
                    else "EXPIRED"
                    if row["expires_at"] <= now
                    else "ACTIVE"
                ),
            )
            for ref, row in reversed(self.g06_shares.items())
            if row["owner_user_id"] == user_id
        ]

    async def revoke_share(self, share_ref: str, user_id: str, *, now: datetime) -> bool:
        row = self.g06_shares.get(share_ref)
        if row is None or row["owner_user_id"] != user_id:
            return False
        row["revoked_at"] = row["revoked_at"] or now
        for digest in [
            digest
            for digest, session in self.g06_share_sessions.items()
            if session["share_ref"] == share_ref
        ]:
            self.g06_share_sessions.pop(digest, None)
        return True

    async def exchange_share_secret(
        self, share_ref: str, secret: str, *, now: datetime
    ) -> ShareSessionOutcome:
        row = self.g06_shares.get(share_ref)
        if (
            row is None
            or row["revoked_at"] is not None
            or row["expires_at"] <= now
            or not hmac.compare_digest(row["secret_hash"], _sha256(secret))
        ):
            raise ResourceNotFoundError("share is unavailable")
        capability = secrets.token_urlsafe(32)
        expires_at = min(row["expires_at"], now + timedelta(hours=1))
        self.g06_share_sessions[_sha256(capability)] = {
            "share_ref": share_ref,
            "expires_at": expires_at,
        }
        return ShareSessionOutcome(capability=capability, expires_at=expires_at)

    async def read_share(
        self, share_ref: str, capability: str | None, *, now: datetime
    ) -> ShareProjectionView:
        session = self.g06_share_sessions.get(_sha256(capability or ""))
        row = self.g06_shares.get(share_ref)
        if (
            session is None
            or row is None
            or session["share_ref"] != share_ref
            or session["expires_at"] <= now
            or row["expires_at"] <= now
            or row["revoked_at"] is not None
        ):
            raise ResourceNotFoundError("share is unavailable")
        return row["projection"].model_copy(deep=True)

    def _delete_g06_trip_memory(self, understanding_id: str) -> None:
        for key in [key for key in self.g06_feedback if key[1] == understanding_id]:
            self.g06_feedback.pop(key, None)
        for share_ref in [
            share_ref
            for share_ref, row in self.g06_shares.items()
            if row["understanding_id"] == understanding_id
        ]:
            self.g06_shares.pop(share_ref, None)
            for digest in [
                digest
                for digest, session in self.g06_share_sessions.items()
                if session["share_ref"] == share_ref
            ]:
                self.g06_share_sessions.pop(digest, None)
        for key in [key for key in self.g06_share_keys if key[1] == understanding_id]:
            self.g06_share_keys.pop(key, None)

    def _clear_g06_account_memory(self, user_id: str) -> None:
        self.g06_preferences.pop(user_id, None)
        for key in [key for key in self.g06_feedback if key[0] == user_id]:
            self.g06_feedback.pop(key, None)
        for share_ref in [
            share_ref
            for share_ref, row in self.g06_shares.items()
            if row["owner_user_id"] == user_id
        ]:
            row = self.g06_shares[share_ref]
            row["revoked_at"] = _now()
            for digest in [
                digest
                for digest, session in self.g06_share_sessions.items()
                if session["share_ref"] == share_ref
            ]:
                self.g06_share_sessions.pop(digest, None)
