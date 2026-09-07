"""Private, on-demand views over retained text and the current account trips."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from cryptography.exceptions import InvalidTag
from pydantic import Field

from app.config import get_settings
from app.trip_understanding.demo import DEMO_SOURCE_SHA256, DEMO_SOURCE_TEXT
from app.trip_understanding.errors import ResourceNotFoundError
from app.trip_understanding.models import StrictModel, UserFacingTripResult


class AccountTripItem(StrictModel):
    public_resource_id: str
    title: str
    city: str
    day_count: int
    updated_at: datetime
    expires_at: datetime
    is_demo: bool


class AccountTripListView(StrictModel):
    items: list[AccountTripItem]
    next_cursor: str | None = None


class ActivitySourceView(StrictModel):
    activity_token: str
    name: str
    quote: str


class SourceReadView(StrictModel):
    status: Literal["AVAILABLE", "DELETED", "UNAVAILABLE"]
    text: str | None = None
    activities: list[ActivitySourceView] = Field(default_factory=list)


class SupplementaryItem(StrictModel):
    name: str
    time_hint: str | None = None
    role: Literal["OPTIONAL", "EXCLUDED"]


class SupplementaryDay(StrictModel):
    day_index: int | None
    day_label: str
    items: list[SupplementaryItem]


class SupplementaryView(StrictModel):
    status: Literal["AVAILABLE", "DELETED", "UNAVAILABLE"]
    days: list[SupplementaryDay] = Field(default_factory=list)


class InvalidTripCursor(ValueError):
    pass


def _cursor_cipher():
    cfg = get_settings()
    secret = cfg.trip_understanding_cookie_signing_key or cfg.jwt_secret_key
    key = hashlib.sha256(("account-trip-list-v1:" + secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _decode_cursor(cursor, user_id, now):
    if cursor is None:
        return None
    try:
        payload = json.loads(_cursor_cipher().decrypt_at_time(cursor.encode(), ttl=86400 * 7, current_time=int(now.timestamp())))
        if not hmac.compare_digest(payload["owner"], user_id):
            raise ValueError
        stamp = datetime.fromisoformat(payload["updated_at"])
        if stamp.tzinfo is None or not isinstance(payload["public_resource_id"], str):
            raise ValueError
        return stamp, payload["public_resource_id"]
    except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise InvalidTripCursor("invalid trip list cursor") from exc


def _list_view(items, user_id, limit, now):
    cursor = None
    if len(items) > limit:
        tail = items[limit - 1]
        cursor = _cursor_cipher().encrypt_at_time(json.dumps({
            "owner": user_id, "updated_at": tail.updated_at.isoformat(),
            "public_resource_id": tail.public_resource_id,
        }).encode(), current_time=int(now.timestamp())).decode()
    return AccountTripListView(items=items[:limit], next_cursor=cursor)


def _trip_item(row, result, is_demo):
    city = next((item.value for item in result.assumptions if item.key == "destination"), "目的地待确认")
    return AccountTripItem(public_resource_id=row["public_resource_id"],
        title=f"{city} · {len(result.days)}日行程", city=city, day_count=len(result.days),
        updated_at=row["updated_at"], expires_at=row["expires_at"], is_demo=is_demo)


@dataclass
class _ImportView:
    status: str
    text: str | None = None
    result: UserFacingTripResult | None = None
    mentions: list[dict] = field(default_factory=list)
    bindings: dict = field(default_factory=dict)


class _ReadbackProjection:
    async def get_source_view(self, resource, *, now):
        data = await self._read_import(resource, now=now)
        items = []
        if data.status == "AVAILABLE" and data.result:
            for day in data.result.days:
                for card in day.activities:
                    canonical_id = data.bindings.get(card.activity_token)
                    for mention in data.mentions:
                        if mention["role"] != "PLANNED":
                            continue
                        original_id = mention.get("canonical_place_id")
                        # Current identity must still correspond to this import; a renamed/
                        # replaced location must not acquire another location's source quote.
                        matches = (canonical_id == original_id if canonical_id and original_id
                            else card.name == mention.get("atomic_place_name") or card.activity_token == mention.get("public_activity_token"))
                        quote = mention["mention_text"]
                        if matches and quote in (data.text or ""):
                            items.append(ActivitySourceView(activity_token=card.activity_token, name=card.name, quote=quote[:240]))
                            break
                    if len(items) >= 12:
                        break
                if len(items) >= 12:
                    break
        return SourceReadView(status=data.status, text=data.text, activities=items)

    async def get_supplementary_view(self, resource, *, now):
        data = await self._read_import(resource, now=now)
        groups = {}
        if data.status == "AVAILABLE":
            for mention in data.mentions:
                if mention["role"] not in {"OPTIONAL", "EXCLUDED"}:
                    continue
                index = mention.get("day_index")
                label = (data.result.days[index - 1].label if data.result and index and index <= len(data.result.days)
                    else f"Day {index}" if index else "未指定日期")
                name = mention.get("atomic_place_name") or mention["mention_text"]
                if "http://" in name.lower() or "https://" in name.lower():
                    name = "备选安排" if mention["role"] == "OPTIONAL" else "已取消安排"
                groups.setdefault(index, SupplementaryDay(day_index=index, day_label=label, items=[])).items.append(
                    SupplementaryItem(name=name[:80], time_hint=(mention.get("time_hint") or None), role=mention["role"]))
        return SupplementaryView(status=data.status, days=[groups[key] for key in sorted(groups, key=lambda key: key or 100)])


class PostgresReadbackMixin(_ReadbackProjection):
    async def list_account_trips(self, *, user_id, limit=20, cursor=None, now):
        seek = _decode_cursor(cursor, user_id, now)
        pool = await self._get_pool()
        rows = await pool.fetch("""SELECT u.public_resource_id,u.updated_at,u.source_expires_at AS expires_at,
                r.public_json,
                EXISTS(SELECT 1 FROM trip_understanding_sources s WHERE s.understanding_id=u.understanding_id
                    AND s.source_type='FIXED_DEMO') AS is_demo
            FROM trip_understandings u JOIN trip_understanding_results r ON r.result_id=u.current_result_id
            WHERE u.owner_user_id=$1 AND u.deleted_at IS NULL AND u.source_expires_at>$2
                AND u.state IN ('READY','PARTIAL')
                AND ($3::timestamptz IS NULL OR (u.updated_at,u.public_resource_id)<($3,$4::text))
            ORDER BY u.updated_at DESC,u.public_resource_id DESC LIMIT $5""",
            user_id, now, seek[0] if seek else None, seek[1] if seek else None, limit + 1)
        items = [_trip_item(row, UserFacingTripResult.model_validate(_json(row["public_json"])), row["is_demo"]) for row in rows]
        return _list_view(items, user_id, limit, now)

    async def _read_import(self, resource, *, now):
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            row = await conn.fetchrow("""SELECT s.*,u.current_revision,u.current_result_id
                FROM trip_understandings u JOIN trip_understanding_revisions r
                  ON r.understanding_id=u.understanding_id AND r.revision=u.current_revision
                JOIN trip_understanding_sources s ON s.source_id=r.source_id
                WHERE u.understanding_id=$1 AND u.public_resource_id=$2 AND u.deleted_at IS NULL
                  AND u.source_expires_at>$3""", resource.understanding_id, resource.public_resource_id, now)
            if row is None:
                raise ResourceNotFoundError("trip resource does not exist")
            if row["deleted_at"] is not None:
                return _ImportView("DELETED")
            if row["retention_until"] <= now:
                return _ImportView("UNAVAILABLE")
            content_hash = row["content_hash"].strip()
            if row["source_type"] == "FIXED_DEMO" and content_hash == DEMO_SOURCE_SHA256:
                text = DEMO_SOURCE_TEXT
            elif row["source_type"] == "TEXT" and row["encrypted_content"] is not None:
                cipher = self._get_source_cipher()
                if cipher.key_ref != row["encryption_key_ref"]:
                    return _ImportView("UNAVAILABLE")
                try:
                    text = cipher.decrypt(bytes(row["encrypted_content"]), source_id=row["source_id"], content_hash=content_hash)
                except (ValueError, UnicodeError, InvalidTag):
                    return _ImportView("UNAVAILABLE")
                if hashlib.sha256(text.encode()).hexdigest() != content_hash:
                    return _ImportView("UNAVAILABLE")
            else:
                return _ImportView("UNAVAILABLE")
            payload = await conn.fetchval("SELECT public_json FROM trip_understanding_results WHERE result_id=$1", row["current_result_id"])
            result = UserFacingTripResult.model_validate(_json(payload)) if payload else None
            mentions = await conn.fetch("""SELECT a.* FROM trip_understanding_activities a
                WHERE a.understanding_id=$1 AND a.revision=(SELECT min(a2.revision)
                    FROM trip_understanding_activities a2 JOIN trip_understanding_revisions r2
                      ON r2.understanding_id=a2.understanding_id AND r2.revision=a2.revision
                    WHERE a2.understanding_id=$1 AND r2.source_id=$2)
                ORDER BY a.day_index NULLS LAST,a.sequence_index,a.activity_id""", resource.understanding_id, row["source_id"])
            current = await conn.fetch("SELECT public_activity_token,canonical_place_id FROM trip_understanding_activities WHERE understanding_id=$1 AND revision=$2",
                resource.understanding_id, row["current_revision"])
            return _ImportView("AVAILABLE", text, result, [dict(item) for item in mentions],
                {item["public_activity_token"]: item["canonical_place_id"] for item in current})


class InMemoryReadbackMixin(_ReadbackProjection):
    async def list_account_trips(self, *, user_id, limit=20, cursor=None, now):
        seek = _decode_cursor(cursor, user_id, now)
        rows = [row for row in self.resources.values() if row["owner_user_id"] == user_id
            and row["state"] in {"READY", "PARTIAL"} and row["expires_at"] > now and row["current_result_id"]
            and (seek is None or (row["updated_at"], row["public_resource_id"]) < seek)]
        rows.sort(key=lambda row: (row["updated_at"], row["public_resource_id"]), reverse=True)
        return _list_view([_trip_item(row, self.results[row["current_result_id"]].result, row.get("is_demo", False)) for row in rows[:limit + 1]], user_id, limit, now)

    async def _read_import(self, resource, *, now):
        row = self.resources.get(resource.public_resource_id)
        if row is None or row["expires_at"] <= now:
            raise ResourceNotFoundError("trip resource does not exist")
        jobs = [(key, value) for key, value in self.jobs.items() if value["understanding_id"] == resource.understanding_id]
        if not jobs:
            return _ImportView("UNAVAILABLE")
        job_id, job = max(jobs, key=lambda item: item[1]["revision"])
        source = self.sources.get(job_id)
        if source is None:
            return _ImportView("DELETED")
        if (self.source_expiries[job_id] <= now or source.source_type not in {"TEXT", "FIXED_DEMO"}
                or hashlib.sha256(source.text.encode()).hexdigest() != job["input_hash"]):
            return _ImportView("UNAVAILABLE")
        result = self.results.get(row["current_result_id"])
        bindings = self.g03_pipeline_inputs.get((resource.understanding_id, row["current_revision"]), {}).get("bindings", {})
        return _ImportView("AVAILABLE", source.text, result.result if result else None,
            self.source_readback_mentions.get(resource.understanding_id, []),
            {token: value.get("canonical_place_id") for token, value in bindings.items()})


def _json(value):
    return json.loads(value) if isinstance(value, str) else value
