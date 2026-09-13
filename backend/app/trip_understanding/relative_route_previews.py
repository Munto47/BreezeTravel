"""Public route comparisons and short-lived, version-bound adoption credentials.

Only the already-compared changed edges are shown. Source quotes, stable private
visit identities and provider diagnostics are never projected into the response.
The ordinary command transaction must call ``verify_route_preview`` again after
locking the current result; checking only in the HTTP handler is insufficient.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Sequence

from cryptography.fernet import Fernet, InvalidToken
from pydantic import Field, model_validator

from app.config import get_settings
from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.map_render import ROUTE_CONFIG_SHA256
from app.trip_understanding.route_connection import compared_path_scope
from app.trip_understanding.models import ActivityMoveCommand, StrictModel, UserFacingTripResult
from app.trip_understanding.relative_route_options import (
    ComparedRouteEdge, RelativeRouteOption, RelativeRouteVisit,
)

ROUTE_PREVIEW_PREFIX = "rr1_"
MAX_ROUTE_PREVIEW_TOKEN_LENGTH = 24000


class PublicComparedRouteEdge(StrictModel):
    from_name: str
    to_name: str
    mode: Literal["walking", "transit"]
    duration_minutes: int = Field(ge=0)
    distance_meters: int = Field(ge=0)


class PublicRelativeRoutePreview(StrictModel):
    kind: Literal["RELATIVE_ORDER"] = "RELATIVE_ORDER"
    change_token: str
    title: str
    summary: str
    day_index: int = Field(ge=1, le=14)
    before: list[str]
    after: list[str]
    routes_before: list[PublicComparedRouteEdge]
    routes_after: list[PublicComparedRouteEdge]
    duration_minutes_before: int = Field(ge=0)
    duration_minutes_after: int = Field(ge=0)
    minutes_saved: int = Field(gt=0)
    distance_meters_before: int = Field(ge=0)
    distance_meters_after: int = Field(ge=0)
    comparison_scope: Literal["CHANGED_EDGES_ONLY"] = "CHANGED_EDGES_ONLY"
    route_coverage_scope: Literal["REQUESTED_POINTS", "RETURNED_SEGMENTS"] | None = None


class PublicRelativeRouteOptions(StrictModel):
    kind: Literal["RELATIVE_ORDER"] = "RELATIVE_ORDER"
    status: Literal["AVAILABLE", "NO_IMPROVEMENT", "NEEDS_CONFIRMATION", "NEEDS_UPDATE", "UNAVAILABLE"]
    message: str
    day_index: int = Field(ge=1, le=14)
    options: list[PublicRelativeRoutePreview] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def omit_unscoped_legacy_comparisons(self):
        if any(option.route_coverage_scope is None for option in self.options):
            self.options = [option for option in self.options if option.route_coverage_scope is not None]
            if not self.options:
                self.status = "UNAVAILABLE"
                self.message = "旧比较的起终点衔接尚未核实，请重新比较。"
        return self


@dataclass(frozen=True)
class VerifiedRouteMove:
    command: ActivityMoveCommand
    before_tokens: tuple[str, ...]


def _cipher() -> Fernet:
    settings = get_settings()
    secret = settings.trip_understanding_cookie_signing_key or settings.jwt_secret_key
    key = hashlib.sha256(("relative-route-selection:" + secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _public_edge(edge: ComparedRouteEdge) -> PublicComparedRouteEdge:
    return PublicComparedRouteEdge(
        from_name=edge.from_name, to_name=edge.to_name, mode=edge.selected_mode,
        duration_minutes=edge.duration_minutes, distance_meters=edge.distance_meters,
    )


def issue_route_preview(
    option: RelativeRouteOption, visits: Sequence[RelativeRouteVisit], *,
    public_resource_id: str, expected_etag: str, now: datetime,
    map_job_id: str | None = None,
) -> PublicRelativeRoutePreview:
    coverage_scope = compared_path_scope(
        [getattr(edge.facts, edge.selected_mode) for edge in option.changed_edges_before],
        [getattr(edge.facts, edge.selected_mode) for edge in option.changed_edges_after])
    if coverage_scope is None:
        raise CommandTargetChangedError("route comparison endpoints are not connected")
    by_id = {visit.visit_id: visit for visit in visits}
    before = [by_id[value] for value in option.before_visit_order]
    after = [by_id[value] for value in option.after_visit_order]
    tokens = [visit.stop.activity_token for visit in before]
    if (
        not tokens or any(not token for token in tokens) or len(set(tokens)) != len(tokens)
        or any(visit.stop.day_index != option.day_index for visit in before + after)
        or set(option.before_visit_order) != set(option.after_visit_order)
        or len(before) != len(after)
        or now.utcoffset() is None or option.expires_at <= now
        or option.minutes_saved <= 0
    ):
        raise CommandTargetChangedError("route comparison is no longer usable")
    moved_token = by_id[option.moved_visit_id].stop.activity_token
    simulated = list(tokens)
    simulated.remove(moved_token)
    simulated.insert(option.target_position, moved_token)
    if simulated != [visit.stop.activity_token for visit in after]:
        raise CommandTargetChangedError("route comparison does not match its move")
    payload = {
        "kind": "RELATIVE_ORDER", "resource": public_resource_id, "etag": expected_etag,
        "day": option.day_index, "before": tokens, "activity": moved_token,
        "position": option.target_position, "config": ROUTE_CONFIG_SHA256,
        "expires": min(now + timedelta(minutes=10), option.expires_at).timestamp(),
        "route_coverage_scope": coverage_scope,
        "geometry_boundaries_checked": True,
        "map_job_id": map_job_id,
    }
    token = ROUTE_PREVIEW_PREFIX + _cipher().encrypt(json.dumps(payload).encode()).decode()
    if len(token) > MAX_ROUTE_PREVIEW_TOKEN_LENGTH:
        raise CommandTargetChangedError("route comparison exceeds the supported size")
    return PublicRelativeRoutePreview(
        change_token=token, day_index=option.day_index,
        title=f"Day {option.day_index} · 调整相邻地点顺序",
        summary="仅比较下列变化路段；其余地点、日期与安排保持原样。采纳后需手动更新地图。",
        before=[visit.stop.name for visit in before], after=[visit.stop.name for visit in after],
        routes_before=[_public_edge(edge) for edge in option.changed_edges_before],
        routes_after=[_public_edge(edge) for edge in option.changed_edges_after],
        duration_minutes_before=option.duration_minutes_before,
        duration_minutes_after=option.duration_minutes_after, minutes_saved=option.minutes_saved,
        distance_meters_before=option.distance_meters_before,
        distance_meters_after=option.distance_meters_after,
        route_coverage_scope=coverage_scope,
    )


def verify_route_preview(
    token: str, *, public_resource_id: str, expected_etag: str, now: datetime,
    current_result: UserFacingTripResult | None = None,
    check_freshness: bool = True,
    current_map_job_id: str | None = None,
) -> VerifiedRouteMove:
    try:
        if not token.startswith(ROUTE_PREVIEW_PREFIX) or len(token) > MAX_ROUTE_PREVIEW_TOKEN_LENGTH:
            raise ValueError("unsupported preview")
        data = json.loads(_cipher().decrypt(token[len(ROUTE_PREVIEW_PREFIX):].encode()))
        if (
            data["kind"] != "RELATIVE_ORDER" or data["resource"] != public_resource_id
            or data.get("route_coverage_scope") not in {"REQUESTED_POINTS", "RETURNED_SEGMENTS"}
            or data.get("geometry_boundaries_checked") is not True
            or data["etag"] != expected_etag or now.utcoffset() is None
            or current_map_job_id is not None and data.get("map_job_id") != current_map_job_id
            or check_freshness and (data["config"] != ROUTE_CONFIG_SHA256 or data["expires"] <= now.timestamp())
        ):
            raise ValueError("expired or changed preview")
        command = ActivityMoveCommand(command_type="ACTIVITY_MOVE", activity_token=data["activity"],
            target_day_index=data["day"], target_position=data["position"])
        before = data["before"]
        if (not isinstance(before, list) or not all(isinstance(t, str) for t in before)
            or len(before) != len(set(before)) or command.activity_token not in before
            or command.target_position >= len(before)):
            raise ValueError("invalid comparison order")
        if current_result is not None:
            day = current_result.days[command.target_day_index - 1]
            if before != [card.activity_token for card in day.activities]:
                raise ValueError("comparison no longer matches the complete day")
        return VerifiedRouteMove(command, tuple(before))
    except (InvalidToken, ValueError, KeyError, TypeError, IndexError) as exc:
        raise CommandTargetChangedError("route comparison expired or changed") from exc


def verify_route_move(command, result, *, public_resource_id, expected_etag, now, current_map_job_id):
    if not isinstance(command, ActivityMoveCommand) or command.route_preview_token is None:
        return
    verified = verify_route_preview(command.route_preview_token, public_resource_id=public_resource_id,
        expected_etag=expected_etag, now=now, current_result=result, current_map_job_id=current_map_job_id)
    if command.model_dump(exclude={"route_preview_token"}) != verified.command.model_dump(exclude={"route_preview_token"}):
        raise CommandTargetChangedError("move differs from the compared route")
