from __future__ import annotations

import re
from datetime import datetime, timezone

from app.constraints.base import RuleContext
from app.constraints.rules._utils import all_slots, minutes
from app.schemas.verification import ConstraintCheck, ConstraintStatus


def _windows(raw: str) -> list[tuple[int, int]]:
    normalized = re.sub(r"\s+", "", raw or "")
    if normalized == "全天" or any(
        token in normalized for token in ("24小时营业", "24小时开放", "全天营业", "全天开放")
    ):
        return [(0, 24 * 60)]
    result = []
    for start_h, start_m, end_h, end_m in re.findall(r"(\d{1,2}):(\d{2})\s*[-—至]\s*(\d{1,2}):(\d{2})", raw or ""):
        opening = int(start_h) * 60 + int(start_m)
        closing = int(end_h) * 60 + int(end_m)
        if opening > 24 * 60 or closing > 24 * 60:
            continue
        if closing <= opening:
            closing += 24 * 60
        result.append((opening, closing))
    return result


def _window_covers(opening: int, closing: int, start: int, end: int) -> bool:
    if end <= start:
        end += 24 * 60
    if opening <= start and end <= closing:
        return True
    # A window such as 17:00-00:30 also covers a slot represented on the
    # following calendar day as 00:05-00:20.
    return closing > 24 * 60 and opening <= start + 24 * 60 and end + 24 * 60 <= closing


class OpeningHoursRule:
    rule_id = "opening_hours"

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        checks = []
        for day, slot in all_slots(context.itinerary):
            place = slot.place or {}
            meta = context.place_meta.get(slot.place_id, {})
            raw = meta.get("opening_hours") or place.get("opening_hours")
            expires_at = meta.get("expires_at")
            stale = False
            if expires_at:
                try:
                    observed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                    stale = observed < context.now.astimezone(timezone.utc)
                except ValueError:
                    stale = True
            windows = _windows(str(raw or ""))
            start, end = minutes(slot.start_time), minutes(slot.end_time)
            if not raw or stale or start is None or end is None:
                status, code = ConstraintStatus.UNKNOWN, "OPENING_HOURS_STALE" if stale else "OPENING_HOURS_MISSING"
                message = "营业时间过期" if stale else "缺少可验证营业时间"
            elif not windows:
                status, code, message = ConstraintStatus.UNKNOWN, "OPENING_HOURS_UNPARSEABLE", "营业时间格式无法可靠解析"
            else:
                ok = any(_window_covers(open_at, close_at, start, end) for open_at, close_at in windows)
                status = ConstraintStatus.SATISFIED if ok else ConstraintStatus.VIOLATED
                code = "WITHIN_OPENING_HOURS" if ok else "OUTSIDE_OPENING_HOURS"
                message = "安排在营业时段内" if ok else "安排时间不在营业时段内"
            checks.append(ConstraintCheck(
                constraint_id=f"opening_hours:{slot.place_id}:{day}",
                status=status,
                reason_code=code,
                message=f"{place.get('name', slot.place_id)}：{message}",
                day_index=day,
                place_id=slot.place_id,
                evidence_refs=[f"poi:{slot.place_id}"] if raw else [],
                repairable=status == ConstraintStatus.VIOLATED,
            ))
        return checks
