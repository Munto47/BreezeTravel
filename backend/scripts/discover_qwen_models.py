from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    BACKEND_ROOT
    / "eval_data"
    / "trip_text_cards_agent_v2"
    / "qwen_model_panel.json"
)
DEFAULT_CATALOG_URL = "https://dashscope.aliyuncs.com/api/v1/models"


class QwenDiscoveryError(ValueError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _model_rows(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in ("data", "models", "model_list", "output"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = value.get("data") or value.get("models")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def _first(value: dict[str, Any], *paths: tuple[str, ...]) -> object | None:
    for path in paths:
        current: object = value
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if current is not None:
            return current
    return None


def _structured_output_status(row: dict[str, Any]) -> bool | str:
    features = _first(
        row,
        ("features",),
        ("model_info", "features"),
        ("capabilities",),
    )
    if isinstance(features, dict):
        normalized = {str(key).lower().replace("_", "-") for key, enabled in features.items() if enabled}
    elif isinstance(features, list):
        normalized = {str(item).lower().replace("_", "-") for item in features}
    else:
        return "NOT_EXPOSED_BY_PROVIDER"
    return bool(
        normalized
        & {
            "structured-output",
            "structured-outputs",
            "json-schema",
            "json-schema-output",
        }
    )


def _context(row: dict[str, Any]) -> int | str:
    value = _first(
        row,
        ("context",),
        ("context_length",),
        ("context_size",),
        ("model_info", "context"),
        ("model_info", "context_length"),
        ("model_info", "context_size"),
        ("model_info", "max_input_tokens"),
    )
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return "NOT_EXPOSED_BY_PROVIDER"


def _pricing(row: dict[str, Any]) -> object:
    value = _first(row, ("prices",), ("pricing",), ("model_info", "prices"))
    if isinstance(value, (dict, list)):
        return value
    return "NOT_EXPOSED_BY_PROVIDER"


def _request_model_id(row: dict[str, Any]) -> str | None:
    value = row.get("id") or row.get("model") or row.get("name")
    return value if isinstance(value, str) and value else None


def _effective_snapshot_id(row: dict[str, Any]) -> str | None:
    snapshot = _first(
        row,
        ("equivalent_snapshot",),
        ("model_info", "equivalent_snapshot"),
    )
    return snapshot if isinstance(snapshot, str) and snapshot else None


def _candidate(rows: list[dict[str, Any]], family: str, role: str) -> dict[str, object]:
    matches: list[tuple[int, int, str, dict[str, Any]]] = []
    for row in rows:
        model_id = _request_model_id(row)
        advertised = row.get("id") or row.get("model") or row.get("name")
        haystack = " ".join(
            str(item).lower() for item in (model_id, advertised) if item is not None
        )
        if "qwen" not in haystack or family.lower() not in haystack:
            continue
        snapshot_score = int(
            bool(re.search(r"(?:20\d{2}[-_]?[01]\d[-_]?[0-3]\d|20\d{6})", model_id or ""))
        )
        structured_score = int(_structured_output_status(row) is True)
        matches.append((structured_score, snapshot_score, model_id or "", row))
    matches.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    if not matches:
        return {
            "role": role,
            "family": f"QWEN_{family.upper()}",
            "exact_model_id": "NOT_EXPOSED_BY_PROVIDER",
            "effective_snapshot_id": "NOT_EXPOSED_BY_PROVIDER",
            "context": "NOT_EXPOSED_BY_PROVIDER",
            "structured_output": "NOT_EXPOSED_BY_PROVIDER",
            "pricing": "NOT_EXPOSED_BY_PROVIDER",
        }
    _, snapshot_score, exact_model_id, row = matches[0]
    effective_snapshot_id = _effective_snapshot_id(row)
    return {
        "role": role,
        "family": f"QWEN_{family.upper()}",
        "exact_model_id": exact_model_id,
        "effective_snapshot_id": effective_snapshot_id or exact_model_id,
        "binding_mode": (
            "EXACT_SNAPSHOT_ID"
            if snapshot_score
            else (
                "ALIAS_WITH_EQUIVALENT_SNAPSHOT_READBACK"
                if effective_snapshot_id
                else "PROVIDER_EXACT_ID_NO_EQUIVALENT_SNAPSHOT"
            )
        ),
        "context": _context(row),
        "structured_output": _structured_output_status(row),
        "pricing": _pricing(row),
    }


def discover(
    *,
    api_key: str,
    base_url: str,
    catalog_url: str,
    timeout_seconds: float = 20.0,
) -> dict[str, object]:
    if not api_key:
        raise QwenDiscoveryError("QWEN_API_KEY is not configured")
    urls = []
    for url in (catalog_url, f"{base_url.rstrip('/')}/models"):
        if url and url not in urls:
            urls.append(url)
    failures: list[str] = []
    payload: object | None = None
    response_bytes = b""
    discovered_url = ""
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
        for url in urls:
            if not url.startswith("https://"):
                failures.append("NON_HTTPS_ENDPOINT")
                continue
            try:
                response = client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                failures.append(type(exc).__name__)
                continue
            if response.status_code != 200:
                failures.append(f"HTTP_{response.status_code}")
                continue
            try:
                payload = response.json()
            except ValueError:
                failures.append("INVALID_JSON")
                continue
            response_bytes = response.content
            discovered_url = url
            break
    if payload is None:
        raise QwenDiscoveryError(
            "Qwen model discovery failed: " + ",".join(failures or ["NO_ENDPOINT"])
        )
    rows = _model_rows(payload)
    if not rows:
        raise QwenDiscoveryError("Qwen model discovery returned no model rows")
    candidates = [
        _candidate(rows, "max", "QUALITY_CEILING"),
        _candidate(rows, "plus", "PRODUCTION_CANDIDATE"),
        _candidate(rows, "flash", "LOW_LATENCY_CANDIDATE"),
    ]
    payload_dict = payload if isinstance(payload, dict) else {}
    region = _first(payload_dict, ("region",), ("data", "region"))
    workspace = _first(payload_dict, ("workspace",), ("workspace_id",))
    complete = all(
        item["exact_model_id"] != "NOT_EXPOSED_BY_PROVIDER"
        for item in candidates
    )
    return {
        "schema_version": "g01-qwen-model-panel-v1",
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "discovery_source": "QWEN_ACCOUNT_MODEL_LIST_API",
        "discovered_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "region": region if isinstance(region, str) and region else "NOT_EXPOSED_BY_PROVIDER",
        "workspace": (
            workspace
            if isinstance(workspace, str) and workspace
            else "NOT_EXPOSED_BY_PROVIDER"
        ),
        "endpoint": discovered_url,
        "endpoint_sha256": _sha256_text(discovered_url),
        "provider_response_sha256": _sha256_bytes(response_bytes),
        "provider_model_count": len(rows),
        "candidates": candidates,
        "frozen_candidate": None,
        "status": "DISCOVERED" if complete else "DISCOVERY_INCOMPLETE",
        "human_evidence": False,
        "raw_provider_response_retained": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--base-url", default=os.getenv("QWEN_API_URL", ""))
    parser.add_argument(
        "--catalog-url",
        default=os.getenv("QWEN_MODEL_CATALOG_URL", DEFAULT_CATALOG_URL),
    )
    args = parser.parse_args()
    api_key = os.getenv("QWEN_API_KEY", "")
    result = discover(
        api_key=api_key,
        base_url=args.base_url,
        catalog_url=args.catalog_url,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "region": result["region"],
                "endpoint_sha256": result["endpoint_sha256"],
                "provider_model_count": result["provider_model_count"],
                "exact_model_ids": [
                    item["exact_model_id"] for item in result["candidates"]
                ],
                "output": str(args.output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
