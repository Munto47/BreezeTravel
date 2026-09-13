from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from fastapi import HTTPException, status

from app.config import get_settings
from app.db.connection import get_pool
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.models import MAX_TRIP_ACTIVITIES, ActivityRole, DestinationBasis, ProposedMention, SourceSemanticPlan
from app.trip_understanding.models import TripUnderstandingAcceptedView
from app.schemas.itinerary import Itinerary


class CollaborationRouteUnavailableError(ValueError):
    """The signed-in member has no usable saved collaboration route."""

    def __init__(self, detail: str, *, code: str = "COLLABORATION_ROUTE_UNAVAILABLE",
                 message: str = "请先在协同规划中保存一条可用路线"):
        super().__init__(detail)
        self.code = code
        self.public_message = message


@dataclass(frozen=True)
class CollaborationImportSource:
    source_text: str
    request_hash: str
    internal_idempotency_key: str
    internal_binding: dict[str, object]
    initial_plan: SourceSemanticPlan | None = None


@dataclass(frozen=True)
class CollaborationImportReplay:
    """Only an already completed member/request may recover an older acceptance."""
    accepted: TripUnderstandingAcceptedView


_CATEGORY_LABELS = {
    "attraction": "景点",
    "food": "餐饮",
    "hotel": "住宿",
    "transport": "交通",
}
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ATOMIC_TEXT_PATTERN = re.compile(
    r"^[A-Za-z0-9\u3400-\u9fff\u00b7\u2022\u30fb\-\u2014\uff08\uff09()&\uff06+'\u2019 ]+$"
)
_STRUCTURE_DIRECTIVE_PATTERN = re.compile(
    r"(?:\bday\s*\d+\b|\u7b2c\s*\d+\s*\u5929|https?://|www\.|\u524d\u5f80|\u7136\u540e|\u63a5\u7740|\u6539\u5230|\u53d6\u6d88|\u6062\u590d|\u5907\u9009|\u6216\u8005|\u6216\u662f|\u53bb)",
    re.IGNORECASE,
)


def _secret() -> bytes:
    settings = get_settings()
    value = (
        settings.trip_understanding_cookie_signing_key
        or settings.trip_understanding_source_encryption_key
        or settings.jwt_secret_key
    )
    return value.encode("utf-8")


def _private_hmac(kind: str, value: str) -> str:
    return hmac.new(
        _secret(),
        f"collaboration-import:{kind}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _atomic_text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    if any(ord(character) < 32 for character in value):
        return ""
    text = re.sub(r" +", " ", unicodedata.normalize("NFKC", value).strip())
    if not text or len(text) > limit:
        return ""
    if not _ATOMIC_TEXT_PATTERN.fullmatch(text):
        return ""
    if _STRUCTURE_DIRECTIVE_PATTERN.search(text):
        return ""
    return text


def _guard_text(value: str, *, city: bool = False) -> str:
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()
    if city:
        normalized = normalized.removesuffix("市")
    return normalized


def collaboration_place_guard_token(
    *,
    day_index: int,
    sequence_index: int,
    name: str,
    category: str,
) -> str:
    value = ":".join(
        (
            str(day_index),
            str(sequence_index),
            _guard_text(name),
            _guard_text(category),
        )
    )
    return _private_hmac("place-guard", value)


def collaboration_city_guard_token(city: str) -> str:
    return _private_hmac("city-guard", _guard_text(city, city=True))


def _date(value: Any) -> str | None:
    text = str(value or "")[:10]
    if not _DATE_PATTERN.fullmatch(text):
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _time(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if _TIME_PATTERN.fullmatch(text) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _itinerary_json(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CollaborationRouteUnavailableError("saved route is not usable") from exc
    if not isinstance(value, Mapping):
        raise CollaborationRouteUnavailableError("saved route is not usable")
    return value


def prepare_collaboration_import(
    *,
    user_id: str,
    room_id: str,
    saved_itinerary_id: str,
    city: Any,
    itinerary_data: Any,
    idempotency_key: str,
    room_route_version: int | None = None,
) -> CollaborationImportSource:
    itinerary = _itinerary_json(itinerary_data)
    raw_days = itinerary.get("days")
    if not isinstance(raw_days, list) or not raw_days:
        raise CollaborationRouteUnavailableError("saved route is empty")
    if len(raw_days) > 14:
        raise CollaborationRouteUnavailableError("saved route exceeds 14 days", code="COLLABORATION_ROUTE_TOO_LARGE", message="当前支持最多14天，请缩短已保存路线后再转入")

    backup = itinerary.get("backupPool", itinerary.get("backup_pool", []))
    if not isinstance(backup, list):
        raise CollaborationRouteUnavailableError("saved alternatives are not usable")
    if "backupPool" in itinerary and "backup_pool" in itinerary and itinerary["backupPool"] != itinerary["backup_pool"]:
        raise CollaborationRouteUnavailableError("saved alternatives conflict")

    city_name = _atomic_text(city or itinerary.get("city"), limit=40)
    lines = [f"{city_name}{len(raw_days)}日行程。" if city_name else f"{len(raw_days)}日行程。"]
    valid_place_count = 0
    mentions: list[ProposedMention] = []
    day_labels: dict[int, str] = {}
    unassigned: list[str] = []
    guard_tokens: list[str] = []
    for day_index, raw_day in enumerate(raw_days, 1):
        if not isinstance(raw_day, Mapping):
            raise CollaborationRouteUnavailableError("saved day is not usable")
        day = _mapping(raw_day)
        day_sequence_index = 0
        # New imports adopt only relative days/order, including from old saved schedules.
        lines.append(f"Day {day_index}")
        raw_slots = day.get("slots")
        if not isinstance(raw_slots, list):
            if raw_slots is not None:
                raise CollaborationRouteUnavailableError("saved day slots are not usable")
            raw_slots = day.get("activities")
        if not isinstance(raw_slots, list):
            if raw_slots is not None:
                raise CollaborationRouteUnavailableError("saved day activities are not usable")
            raw_slots = []
        for raw_slot in raw_slots:
            slot = _mapping(raw_slot)
            place = _mapping(slot.get("place")) or slot
            name = _atomic_text(place.get("name"), limit=40)
            if not name:
                raise CollaborationRouteUnavailableError("saved route contains an invalid place name", code="COLLABORATION_INVALID_PLACE", message="已保存路线中有地点名称无效，请修正后再转入")
            category_key = str(place.get("category") or "").casefold()
            category = _CATEGORY_LABELS.get(category_key)
            detail = f"去{name}{f'（{category}）' if category else ''}。"
            line = detail
            offset = sum(len(value) + 1 for value in lines) + line.index(name)
            lines.append(line)
            mentions.append(ProposedMention(mention_id=f"collaboration-slot-{day_index}-{day_sequence_index}",
                raw_text=name, span_start=offset, span_end=offset + len(name), role=ActivityRole.PLANNED,
                day_index=day_index, sequence_index=day_sequence_index, atomic_place_name=name,
                category_hint=category, city_hint=city_name or None))
            guard_tokens.append(
                collaboration_place_guard_token(
                    day_index=day_index,
                    sequence_index=day_sequence_index,
                    name=name,
                    category=category or "",
                )
            )
            day_sequence_index += 1
            valid_place_count += 1

    if backup:
        lines.append("未指定日期的备选：")
    for index, raw_place in enumerate(backup):
        place = _mapping(raw_place)
        name = _atomic_text(place.get("name"), limit=40)
        if not name:
            raise CollaborationRouteUnavailableError("saved alternative contains an invalid place name", code="COLLABORATION_INVALID_PLACE", message="已保存备选中有地点名称无效，请修正后再转入")
        category = _CATEGORY_LABELS.get(str(place.get("category") or "").casefold())
        line = f"备选{name}{f'（{category}）' if category else ''}。"
        offset = sum(len(value) + 1 for value in lines) + line.index(name)
        lines.append(line)
        mention_id = f"collaboration-alternative-{index}"
        mentions.append(ProposedMention(mention_id=mention_id, raw_text=name,
            span_start=offset, span_end=offset + len(name), role=ActivityRole.OPTIONAL,
            day_index=None, sequence_index=index, atomic_place_name=name, category_hint=category,
            city_hint=city_name or None))
        unassigned.append(mention_id)
        valid_place_count += 1

    if valid_place_count == 0:
        raise CollaborationRouteUnavailableError("saved route has no usable places")
    if valid_place_count > MAX_TRIP_ACTIVITIES:
        raise CollaborationRouteUnavailableError("saved route and alternatives exceed 160 places", code="COLLABORATION_ROUTE_TOO_LARGE", message="路线与备选合计超过160个地点，请精简后再转入")
    source_text = "\n".join(lines)
    if len(source_text) > 50_000:
        raise CollaborationRouteUnavailableError("saved route is too large")

    room_ref_hash = _private_hmac("room", room_id)
    saved_ref_hash = _private_hmac("saved-itinerary", saved_itinerary_id)
    saved_content_hash = canonical_sha256(itinerary)
    normalized_text_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    internal_binding = {
        "status": "NOT_RUN",
        "source_origin": "COLLABORATION",
        "room_ref_hash": room_ref_hash,
        "saved_itinerary_ref_hash": saved_ref_hash,
        "saved_content_hash": saved_content_hash,
        "normalized_text_hash": normalized_text_hash,
        "collaboration_guard_version": "HMAC_V1",
        "collaboration_place_guard_tokens": guard_tokens,
        "collaboration_city_guard_token": (
            collaboration_city_guard_token(city_name) if city_name else None
        ),
    }
    request_hash = canonical_sha256(
        {
            "action": "FROM_COLLABORATION_V1",
            "owner_ref_hash": _private_hmac("owner", user_id),
            "source_origin": internal_binding["source_origin"],
            "room_ref_hash": room_ref_hash,
            "saved_content_hash": saved_content_hash,
            "normalized_text_hash": normalized_text_hash,
            **({"room_route_version": room_route_version} if room_route_version is not None else {}),
        }
    )
    return CollaborationImportSource(
        source_text=source_text,
        request_hash=request_hash,
        internal_idempotency_key=f"collaboration_{_private_hmac('idempotency', f'{user_id}:{idempotency_key}')}",
        internal_binding=internal_binding,
        initial_plan=SourceSemanticPlan(source_hash=normalized_text_hash,
            destination_name=city_name or "目的地待确认",
            destination_basis=DestinationBasis.EXPLICIT if city_name else DestinationBasis.SOFT_ASSUMPTION,
            mentions=mentions, binding={"provider": "SAVED_COLLABORATION", "external_calls": 0},
            day_count=len(raw_days), day_labels=day_labels, unassigned_alternative_ids=unassigned),
    )


async def load_collaboration_import(
    *,
    user_id: str,
    room_id: str,
    idempotency_key: str,
    room_route_version: int | None = None,
) -> CollaborationImportSource | CollaborationImportReplay:
    if room_route_version is not None:
        from app.services.room_current_itinerary import get_current_itinerary, version_conflict
        current = await get_current_itinerary(room_id, user_id)
        if current.version == room_route_version and current.itinerary_data is not None:
            return prepare_collaboration_import(user_id=user_id, room_id=room_id,
                saved_itinerary_id=f"room-route:{current.version}", city=current.itinerary_data.city,
                itinerary_data=current.itinerary_data.model_dump(mode="json"), idempotency_key=idempotency_key,
                room_route_version=room_route_version)
        # A lost response is recoverable after another member republishes. This
        # does not grant permission to create a new import from arbitrary history.
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""SELECT old.itinerary_data FROM room_members m
                LEFT JOIN room_itinerary_revisions old ON old.room_id=m.room_id AND old.version=$3
                WHERE m.room_id=$1 AND m.user_id=$2""", room_id, user_id, room_route_version)
            if row is None:
                raise HTTPException(403, detail="不是该房间成员")
            if row["itinerary_data"] is None:
                raise version_conflict()
            itinerary = Itinerary.model_validate(_itinerary_json(row["itinerary_data"]))
            prepared = prepare_collaboration_import(user_id=user_id, room_id=room_id,
                saved_itinerary_id=f"room-route:{room_route_version}", city=itinerary.city,
                itinerary_data=itinerary.model_dump(mode="json"), idempotency_key=idempotency_key,
                room_route_version=room_route_version)
            accepted = await conn.fetchval("""SELECT i.response_json
                FROM trip_understanding_idempotency_records i JOIN trip_understandings u
                    ON u.public_resource_id=i.response_json->>'public_resource_id' AND u.owner_user_id=$2
                WHERE i.scope=$1 AND i.key_hash=$3 AND i.request_hash=$4 AND i.state='COMPLETED'""",
                f"user:{user_id}:create", user_id,
                hashlib.sha256(prepared.internal_idempotency_key.encode("utf-8")).hexdigest(), prepared.request_hash)
        if accepted is None:
            raise version_conflict()
        # Return the existing acceptance directly: calling create_full here could
        # recreate deleted personal data if deletion raced with the historical read.
        return CollaborationImportReplay(TripUnderstandingAcceptedView.model_validate(_itinerary_json(accepted)))
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT saved.id, saved.city, saved.itinerary_data
            FROM room_members AS member
            LEFT JOIN LATERAL (
                SELECT id, city, itinerary_data
                FROM saved_itineraries
                WHERE room_id = member.room_id AND user_id = member.user_id
                ORDER BY created_at DESC NULLS LAST, id DESC
                LIMIT 1
            ) AS saved ON TRUE
            WHERE member.room_id = $1 AND member.user_id = $2
            """,
            room_id,
            user_id,
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="不是该房间成员",
        )
    if row["id"] is None:
        raise CollaborationRouteUnavailableError("no saved route")
    return prepare_collaboration_import(
        user_id=user_id,
        room_id=room_id,
        saved_itinerary_id=str(row["id"]),
        city=row["city"],
        itinerary_data=row["itinerary_data"],
        idempotency_key=idempotency_key,
    )
