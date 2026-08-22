from __future__ import annotations

from app.audit.models import AuditSeverity, AuditStatus


class SeverityPolicy:
    version = "severity-v1"

    _blocker_reasons = {
        "FIXED_COMMITMENT_CONFLICT",
        "RETURN_DEPARTURE_CONFLICT",
        "PLACE_AMBIGUOUS",
    }
    _high_reasons = {
        "TIME_CHAIN_BROKEN",
        "TIME_DATA_INVALID",
        "OUTSIDE_OPENING_HOURS",
        "OPENING_HOURS_MISSING",
        "OPENING_HOURS_STALE",
        "OPENING_HOURS_UNPARSEABLE",
        "TRAVEL_TIME_EXCEEDED",
        "TRAVEL_TIME_MISSING",
        "DAILY_HOTEL_MISSING",
        "PLACE_NOT_RESOLVED",
    }
    _medium_prefixes = (
        "MEAL_",
        "DAILY_CAPACITY_",
        "DAILY_FOOD_",
        "ADJACENT_CATEGORY_",
        "WEATHER_",
        "RAIN_",
        "BUDGET_",
    )

    def classify(self, *, status: AuditStatus, reason_code: str) -> AuditSeverity:
        if status == AuditStatus.SATISFIED:
            return AuditSeverity.INFO
        if reason_code in self._blocker_reasons:
            return AuditSeverity.BLOCKER
        if reason_code in self._high_reasons:
            return AuditSeverity.HIGH
        if reason_code.startswith(self._medium_prefixes):
            return AuditSeverity.MEDIUM
        return AuditSeverity.HIGH if status == AuditStatus.VIOLATED else AuditSeverity.MEDIUM
