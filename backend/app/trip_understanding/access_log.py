from __future__ import annotations

import logging
import re


PRIVATE_PATH_ID_RES = (
    (re.compile(r"(/api/v3/trip-understandings/)[^/?\s]+"), "public_resource_id"),
    (re.compile(r"(/api/v3/shares/)[^/?\s]+"), "share_ref"),
    (re.compile(r"(/api/v3/me/shares/)[^/?\s]+"), "share_ref"),
)


def redact_trip_understanding_path(value: str) -> str:
    redacted = value
    for pattern, placeholder in PRIVATE_PATH_ID_RES:
        redacted = pattern.sub(rf"\1{{{placeholder}}}", redacted)
    return redacted


class TripUnderstandingAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_trip_understanding_path(value) if isinstance(value, str) else value
                for value in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: redact_trip_understanding_path(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        return True


def install_trip_understanding_access_log_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, TripUnderstandingAccessLogFilter) for item in logger.filters):
        logger.addFilter(TripUnderstandingAccessLogFilter())
