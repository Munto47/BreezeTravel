from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone


_SECRET = re.compile(r"(bearer\s+|token[=:]\s*|api[_-]?key[=:]\s*)[^\s,;]+", re.IGNORECASE)


def redact(value: object) -> str:
    text = str(value)
    text = _SECRET.sub(lambda match: match.group(1) + "[REDACTED]", text)
    return text[:2000]


def log_event(logger: logging.Logger, event: str, *, trace_id: str, duration_ms: int | None = None, error_category: str | None = None, **safe_fields):
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trace_id": trace_id,
        "event": event,
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if error_category:
        payload["error_category"] = error_category
    payload.update({key: redact(value) for key, value in safe_fields.items() if key not in {"prompt", "query", "message", "token"}})
    logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
