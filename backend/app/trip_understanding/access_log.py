from __future__ import annotations

import logging
import re


RESOURCE_PATH_RE = re.compile(r"(/api/v3/trip-understandings/)[^/?\s]+")


def redact_trip_understanding_path(value: str) -> str:
    return RESOURCE_PATH_RE.sub(r"\1{public_resource_id}", value)


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
