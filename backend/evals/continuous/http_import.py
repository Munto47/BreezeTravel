from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urljoin

from evals.dual_entry_scorer import (
    aggregate_metric_scores,
    evaluate_metric_thresholds,
    import_metric_actuals,
    score_metric_oracles,
)

from .core import (
    RunResult,
    _atomic_write_json,
    _canonical_bytes,
    _new_run_dir,
    _sha256_bytes,
    preflight,
)


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: Any


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Any | None,
        timeout_seconds: float,
    ) -> HttpResponse: ...


class HttpTransportError(RuntimeError):
    pass


class UrllibTransport:
    """Small synchronous transport so the runner stays independent of app code."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Any | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        body = None if json_body is None else _canonical_bytes(json_body)
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read()
                return HttpResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    body=_decode_body(payload),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status_code=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=_decode_body(exc.read()),
            )
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise HttpTransportError(str(exc)) from exc


def _decode_body(payload: bytes) -> Any:
    if not payload:
        return None
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload.decode("utf-8", errors="replace")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if key.lower() in {"token", "access_token", "authorization"} else _redact(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact(child) for child in value]
    return value


def _atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(_canonical_bytes(row) + b"\n" for row in rows)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_selected_cases_and_labels(
    resolved_spec: Mapping[str, Any], repo_root: Path
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    dataset = resolved_spec["dataset"]
    if resolved_spec.get("lane") not in {"pr_offline", "nightly_snapshot"} or dataset.get(
        "label_access"
    ) != "development_scorer":
        raise ValueError("IMPORT_HTTP_ADAPTER_ONLY_SUPPORTS_DEVELOPMENT_LABELS")
    manifest_path = (repo_root / dataset["manifest"]).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    requested = set(dataset.get("case_ids") or [])
    cases: dict[str, dict[str, Any]] = {}
    labels: dict[str, dict[str, Any]] = {}
    entries = {entry["split"]: entry for entry in manifest["files"]}
    for split in dataset.get("splits", []):
        if split == "frozen_blind":
            raise ValueError("BLIND_LABEL_ACCESS_PROHIBITED")
        entry = entries[split]
        label_name = str(entry["labels"])
        if "sealed" in label_name.replace("\\", "/").split("/"):
            raise ValueError("BLIND_LABEL_ACCESS_PROHIBITED")
        for row in _load_rows((manifest_path.parent / entry["inputs"]).resolve()):
            if not requested or row.get("case_id") in requested:
                cases[row["case_id"]] = row
        for row in _load_rows((manifest_path.parent / label_name).resolve()):
            if not requested or row.get("case_id") in requested:
                labels[row["case_id"]] = row
    ordered_ids = dataset.get("case_ids") or sorted(cases)
    ordered = [cases[case_id] for case_id in ordered_ids if case_id in cases]
    if len(ordered) != len(ordered_ids) or any(case_id not in labels for case_id in ordered_ids):
        raise ValueError("SELECTED_CASE_OR_DEVELOPMENT_LABEL_MISSING")
    expected_provider_mode = resolved_spec.get("provider", {}).get("mode")
    for case in ordered:
        if (
            case.get("entry") != "IMPORT"
            or case.get("execution", {}).get("provider_mode") != expected_provider_mode
        ):
            raise ValueError("IMPORT_HTTP_ADAPTER_CASE_SCOPE_MISMATCH")
    return ordered, labels


class _Recorder:
    def __init__(self, transport: HttpTransport, base_url: str, timeout_seconds: float) -> None:
        self.transport = transport
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds
        self.transactions: list[dict[str, Any]] = []
        self.call_count = 0
        self._record_lock = threading.Lock()

    def request(
        self,
        case_id: str,
        step: str,
        method: str,
        path: str,
        *,
        bearer_token: str | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
    ) -> HttpResponse:
        safe_headers = {"Accept": "application/json"}
        if json_body is not None:
            safe_headers["Content-Type"] = "application/json"
        if bearer_token:
            safe_headers["Authorization"] = f"Bearer {bearer_token}"
        safe_headers.update(headers or {})
        started = time.perf_counter()
        with self._record_lock:
            self.call_count += 1
            request_index = self.call_count
        transaction: dict[str, Any] = {
            "schema_version": "continuous-http-transaction-v1",
            "request_index": request_index,
            "case_id": case_id,
            "step": step,
            "method": method,
            "path": path,
            "request_body": _redact(json_body),
        }
        try:
            response = self.transport.request(
                method,
                urljoin(self.base_url, path.lstrip("/")),
                headers=safe_headers,
                json_body=json_body,
                timeout_seconds=self.timeout_seconds,
            )
        except Exception as exc:
            transaction.update(
                {
                    "status_code": None,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "response_body": None,
                    "transport_error": {"type": type(exc).__name__, "message": str(exc)},
                }
            )
            with self._record_lock:
                self.transactions.append(transaction)
            raise HttpTransportError(str(exc)) from exc
        transaction.update(
            {
                "status_code": response.status_code,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "response_body": _redact(response.body),
            }
        )
        with self._record_lock:
            self.transactions.append(transaction)
        return response


def _expect(response: HttpResponse, expected: set[int], step: str) -> dict[str, Any]:
    if response.status_code not in expected:
        raise RuntimeError(f"{step} returned HTTP {response.status_code}, expected {sorted(expected)}")
    if not isinstance(response.body, dict):
        raise RuntimeError(f"{step} did not return a JSON object")
    return response.body


def _detail_code(response: HttpResponse) -> str | None:
    body = response.body if isinstance(response.body, dict) else {}
    detail = body.get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def _selected_candidate(resolution: Mapping[str, Any]) -> Mapping[str, Any] | None:
    selected = resolution.get("canonical_place_id")
    candidates = resolution.get("candidates") if isinstance(resolution.get("candidates"), list) else []
    return next((item for item in candidates if isinstance(item, dict) and item.get("place_id") == selected), None)


def _collect_receipts(case_id: str, payload: Mapping[str, Any], phase: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for resolution in payload.get("resolutions", []):
        if not isinstance(resolution, dict):
            continue
        for candidate in resolution.get("candidates", []):
            if not isinstance(candidate, dict) or not isinstance(candidate.get("resolved_place_receipt"), dict):
                continue
            rows.append(
                {
                    "schema_version": "continuous-provider-receipt-v1",
                    "case_id": case_id,
                    "phase": phase,
                    "raw_stop_id": resolution.get("raw_stop_id"),
                    "selected": candidate.get("place_id") == resolution.get("canonical_place_id"),
                    "disposition": "OFFERED",
                    "receipt": candidate["resolved_place_receipt"],
                }
            )
        for candidate in resolution.get("rejected_candidates", []):
            if not isinstance(candidate, dict) or not isinstance(candidate.get("resolved_place_receipt"), dict):
                continue
            rows.append(
                {
                    "schema_version": "continuous-provider-receipt-v1",
                    "case_id": case_id,
                    "phase": phase,
                    "raw_stop_id": resolution.get("raw_stop_id"),
                    "selected": False,
                    "disposition": "REJECTED",
                    "rejection_reason": candidate.get("reason"),
                    "target_city": candidate.get("target_city"),
                    "receipt": candidate["resolved_place_receipt"],
                }
            )
    for receipt in payload.get("resolved_place_receipts", []):
        if isinstance(receipt, dict):
            rows.append(
                {
                    "schema_version": "continuous-provider-receipt-v1",
                    "case_id": case_id,
                    "phase": phase,
                    "raw_stop_id": None,
                    "selected": True,
                    "disposition": "MATERIALIZED",
                    "receipt": receipt,
                }
            )
    return rows


def _check_receipt(receipt: Mapping[str, Any], provider_mode: str) -> bool:
    required = {
        "canonical_place_id",
        "provider",
        "provider_place_id",
        "city",
        "longitude",
        "latitude",
        "request_hash",
        "response_hash",
        "observed_at",
        "execution_mode",
    }
    if not required.issubset(receipt):
        return False
    if not all(
        isinstance(receipt[key], str) and receipt[key].strip()
        for key in ("canonical_place_id", "provider", "provider_place_id", "city", "execution_mode")
    ):
        return False
    longitude = receipt["longitude"]
    latitude = receipt["latitude"]
    if (
        isinstance(longitude, bool)
        or isinstance(latitude, bool)
        or not isinstance(longitude, (int, float))
        or not isinstance(latitude, (int, float))
        or not (-180 <= longitude <= 180 and -90 <= latitude <= 90)
    ):
        return False
    if not all(
        isinstance(receipt[key], str) and re.fullmatch(r"[0-9a-f]{64}", receipt[key])
        for key in ("request_hash", "response_hash")
    ):
        return False
    try:
        observed = datetime.fromisoformat(str(receipt["observed_at"]).replace("Z", "+00:00"))
    except ValueError:
        return False
    if observed.tzinfo is None or observed.utcoffset() is None:
        return False
    if provider_mode == "controlled_fixture" and receipt.get("execution_mode") != "fixture":
        return False
    return True


def _score_case(
    case: Mapping[str, Any],
    label: Mapping[str, Any],
    import_readback: Mapping[str, Any],
    apply_response: HttpResponse | None,
    receipts: list[dict[str, Any]],
    metric_actuals: Mapping[str, Any],
) -> dict[str, Any]:
    truth = label.get("deterministic_truth", {})
    checks: list[dict[str, Any]] = []
    expected_parse = truth.get("expected_parse")
    raw_stops = import_readback.get("raw_stops", [])
    actual_names = [item.get("raw_name") for item in raw_stops if isinstance(item, dict)]
    if isinstance(expected_parse, dict):
        expected_names = expected_parse.get("stop_names", [])
        checks.append(
            {
                "id": "EXPECTED_STOP_NAMES",
                "status": "PASS" if actual_names == expected_names else "FAIL",
                "expected": expected_names,
                "actual": actual_names,
            }
        )
        if expected_parse.get("source_span_readback_rate") is not None:
            with_span = sum(bool(item.get("source_span")) for item in raw_stops if isinstance(item, dict))
            rate = with_span / len(raw_stops) if raw_stops else 0.0
            expected_rate = float(expected_parse["source_span_readback_rate"])
            checks.append(
                {
                    "id": "SOURCE_SPAN_READBACK_RATE",
                    "status": "PASS" if rate >= expected_rate else "FAIL",
                    "expected": expected_rate,
                    "actual": rate,
                }
            )
    resolutions_by_id = {
        item.get("raw_stop_id"): item for item in import_readback.get("resolutions", []) if isinstance(item, dict)
    }
    names_by_id = {item.get("raw_stop_id"): item.get("raw_name") for item in raw_stops if isinstance(item, dict)}
    for expected in truth.get("expected_resolutions", []):
        matching = next(
            (item for raw_id, item in resolutions_by_id.items() if names_by_id.get(raw_id) == expected.get("raw_name")),
            None,
        )
        actual_status = matching.get("resolution_status") if matching else None
        checks.append(
            {
                "id": f"RESOLUTION:{expected.get('raw_name')}",
                "status": "PASS" if actual_status == expected.get("status") else "FAIL",
                "expected": expected.get("status"),
                "actual": actual_status,
            }
        )
        if expected.get("status") == "NOT_FOUND":
            controlled_fact = case.get("input", {}).get("controlled_facts", {}).get(
                expected.get("raw_name"), {}
            )
            requires_wrong_city_receipt = "wrong-city" in case.get("tags", []) or (
                isinstance(controlled_fact, Mapping) and controlled_fact.get("top_candidate_city")
            )
            if requires_wrong_city_receipt:
                rejected = matching.get("rejected_candidates", []) if isinstance(matching, dict) else []
                complete_wrong_city_receipts = [
                    item
                    for item in rejected
                    if isinstance(item, dict)
                    and item.get("reason") == "WRONG_CITY"
                    and item.get("target_city") == case.get("city")
                    and isinstance(item.get("resolved_place_receipt"), dict)
                    and item["resolved_place_receipt"].get("city") != case.get("city")
                    and _check_receipt(
                        item["resolved_place_receipt"], case["execution"]["provider_mode"]
                    )
                ]
                checks.append(
                    {
                        "id": f"REJECTED_WRONG_CITY_RECEIPT:{expected.get('raw_name')}",
                        "status": "PASS" if complete_wrong_city_receipts else "FAIL",
                        "receipt_count": len(complete_wrong_city_receipts),
                    }
                )
    must_not = set(truth.get("must_not_happen", []))
    if {"APPLY_AMBIGUOUS_DRAFT", "SILENT_APPLY"} & must_not:
        rejected = apply_response is not None and apply_response.status_code == 409
        checks.append({"id": "UNRESOLVED_APPLY_REJECTED", "status": "PASS" if rejected else "FAIL"})
    receipt_values = [row["receipt"] for row in receipts]
    offered_receipts = [row["receipt"] for row in receipts if row.get("disposition") == "OFFERED"]
    checks.append(
        {
            "id": "PROVIDER_RECEIPT_CONTRACT",
            "status": "PASS"
            if receipt_values and all(_check_receipt(item, case["execution"]["provider_mode"]) for item in receipt_values)
            else "FAIL",
            "receipt_count": len(receipt_values),
        }
    )
    expected_resolutions = truth.get("expected_resolutions", [])
    offered_receipt_applicable = not expected_resolutions or any(
        item.get("status") != "NOT_FOUND" for item in expected_resolutions
    )
    if offered_receipt_applicable:
        checks.append(
            {
                "id": "OFFERED_RECEIPT_CONTRACT",
                "status": "PASS"
                if offered_receipts
                and all(
                    _check_receipt(item, case["execution"]["provider_mode"])
                    for item in offered_receipts
                )
                else "FAIL",
                "receipt_count": len(offered_receipts),
            }
        )
    if apply_response is not None and apply_response.status_code == 200:
        materialized_receipts = [
            row["receipt"] for row in receipts if row.get("disposition") == "MATERIALIZED"
        ]
        checks.append(
            {
                "id": "MATERIALIZED_RECEIPT_CONTRACT",
                "status": "PASS"
                if materialized_receipts
                and all(
                    _check_receipt(item, case["execution"]["provider_mode"])
                    for item in materialized_receipts
                )
                else "FAIL",
                "receipt_count": len(materialized_receipts),
            }
        )
    evaluated = [item for item in checks if item["status"] != "NOT_EVALUATED"]
    metric_score = score_metric_oracles(label, metric_actuals)
    passed = all(item["status"] == "PASS" for item in evaluated) and metric_score["status"] == "SCORED"
    return {
        "schema_version": "continuous-deterministic-score-v1",
        "case_id": case["case_id"],
        "status": "PASS" if passed else "INVALID" if metric_score["status"] == "INVALID" else "FAIL",
        "checks": checks,
        "metric_score": metric_score,
        "not_evaluated_steps": [
            step
            for step in case.get("execution", {}).get("steps", [])
            if step
            not in {
                "create_workspace",
                "import_text",
                "resolve_candidates",
                "apply_import",
                "collect_evidence",
                "full_audit",
                "generate_repair",
                "preview_repair",
                "apply_repair",
                "postcheck",
                "readback",
            }
        ],
    }


_REPAIR_OPERATION_METRIC_NAMES = {
    "ADJUST_TIME": "SHIFT",
    "MOVE_WITHIN_DAY": "MOVE",
    "MOVE_TO_DAY": "MOVE",
    "REPLACE_STOP": "REPLACE",
    "REMOVE_STOP": "REMOVE",
    "INSERT_BREAK": "INSERT_BREAK",
    "INSERT_MEAL": "INSERT_MEAL",
}


def _revision_stops(revision: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(revision, Mapping):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for day in revision.get("days", []):
        if not isinstance(day, dict):
            continue
        for stop in day.get("stops", []):
            if isinstance(stop, dict) and isinstance(stop.get("stop_id"), str):
                result[stop["stop_id"]] = {**stop, "day_index": day.get("day_index")}
    return result


def _locked_items_preserved(
    base_revision: Mapping[str, Any] | None,
    preview_revision: Mapping[str, Any] | None,
) -> bool:
    base = _revision_stops(base_revision)
    preview = _revision_stops(preview_revision)
    protected_fields = (
        "place_id",
        "raw_name",
        "day_index",
        "start_time",
        "end_time",
        "locked",
        "fixed_commitment",
        "commitment_kind",
    )
    for stop_id, stop in base.items():
        if not (stop.get("locked") is True or stop.get("fixed_commitment") is True):
            continue
        candidate = preview.get(stop_id)
        if candidate is None or any(candidate.get(field) != stop.get(field) for field in protected_fields):
            return False
    return True


def _hard_violation_signatures(report: Mapping[str, Any] | None) -> set[tuple[Any, ...]]:
    if not isinstance(report, Mapping):
        return set()
    return {
        (
            finding.get("reason_code"),
            tuple(finding.get("affected_days", [])),
            tuple(finding.get("affected_stop_ids", [])),
            tuple(finding.get("affected_member_ids", [])),
        )
        for finding in report.get("findings", [])
        if isinstance(finding, dict)
        and finding.get("status") == "VIOLATED"
        and finding.get("severity") in {"BLOCKER", "HIGH"}
    }


def _repair_metric_option(
    option: Mapping[str, Any],
    *,
    base_revision: Mapping[str, Any] | None,
    source_audit: Mapping[str, Any],
    postcheck_audit: Mapping[str, Any] | None,
) -> dict[str, Any]:
    operations = option.get("operations") if isinstance(option.get("operations"), list) else []
    operation_types = [
        _REPAIR_OPERATION_METRIC_NAMES.get(str(operation.get("operation")), str(operation.get("operation")))
        for operation in operations
        if isinstance(operation, dict)
    ]
    postcheck_executed = bool(
        postcheck_audit
        and isinstance(option.get("postcheck_report_id"), str)
        and postcheck_audit.get("report_id") == option.get("postcheck_report_id")
    )
    no_new_hard = bool(
        postcheck_executed
        and _hard_violation_signatures(postcheck_audit).issubset(_hard_violation_signatures(source_audit))
    )
    return {
        "repair_id": option.get("repair_id"),
        "operation_types": operation_types,
        "predicates": {
            "postcheck_executed": postcheck_executed,
            "locked_items_preserved": _locked_items_preserved(
                base_revision,
                option.get("result_preview") if isinstance(option.get("result_preview"), dict) else None,
            ),
            "no_new_hard_violation": no_new_hard,
        },
    }


def _execute_case(
    recorder: _Recorder,
    case: Mapping[str, Any],
    label: Mapping[str, Any],
    bearer_token: str,
    *,
    run_namespace: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    case_id = str(case["case_id"])
    suffix = _sha256_bytes(f"{run_namespace}:{case_id}".encode("utf-8"))[:16]
    room_id = f"eval-room-{suffix}"
    workspace_id = f"eval-workspace-{suffix}"
    start = date(2026, 10, 1)
    end = start + timedelta(days=int(case["trip_days"]) - 1)
    _expect(
        recorder.request(
            case_id,
            "create_room",
            "POST",
            "/api/room",
            bearer_token=bearer_token,
            json_body={
                "room_id": room_id,
                "thread_id": f"eval-thread-{suffix}",
                "trip_city": case["city"],
                "trip_days": case["trip_days"],
                "nickname": "Continuous Eval",
            },
        ),
        {200},
        "create_room",
    )
    _expect(
        recorder.request(
            case_id,
            "create_workspace",
            "POST",
            "/api/trip-workspaces",
            bearer_token=bearer_token,
            json_body={
                "workspace_id": workspace_id,
                "room_id": room_id,
                "city": case["city"],
                "trip_date_range": {"start": start.isoformat(), "end": end.isoformat()},
            },
        ),
        {201},
        "create_workspace",
    )
    created_response = recorder.request(
        case_id,
        "import_text",
        "POST",
        f"/api/trip-workspaces/{workspace_id}/imports",
        bearer_token=bearer_token,
        headers={"Idempotency-Key": f"continuous-import-{suffix}"},
        json_body={"source_type": "AI_TEXT", "raw_text": case["input"]["raw_itinerary"]},
    )
    created = _expect(created_response, {201}, "import_text")
    initial_import = copy.deepcopy(created)
    import_id = created.get("import_id")
    if not isinstance(import_id, str):
        raise RuntimeError("import_text response has no import_id")
    receipts = _collect_receipts(case_id, created, "create")
    apply_response: HttpResponse | None = None
    steps = set(case.get("execution", {}).get("steps", []))
    truth = label.get("deterministic_truth", {})
    must_not = set(truth.get("must_not_happen", []))
    if "apply_import" in steps:
        resolutions = created.get("resolutions", [])
        unresolved = [
            item
            for item in resolutions
            if isinstance(item, dict) and item.get("resolution_status") in {"AMBIGUOUS", "NOT_FOUND"}
        ]
        prohibit_apply = bool({"APPLY_AMBIGUOUS_DRAFT", "SILENT_APPLY"} & must_not)
        if unresolved and not prohibit_apply and "confirm" in steps:
            raw_names_by_id = {
                str(raw.get("raw_stop_id")): str(raw.get("raw_name") or "")
                for raw in initial_import.get("raw_stops", [])
                if isinstance(raw, dict) and raw.get("raw_stop_id")
            }
            confirmation_instructions = case.get("input", {}).get(
                "confirmation_instructions",
                [],
            )
            confirmations = []
            for resolution in unresolved:
                if resolution.get("resolution_status") != "AMBIGUOUS":
                    continue
                raw_name = raw_names_by_id.get(str(resolution.get("raw_stop_id")), "")
                matching_instructions = [
                    instruction
                    for instruction in confirmation_instructions
                    if isinstance(instruction, dict)
                    and instruction.get("raw_name") == raw_name
                    and isinstance(instruction.get("canonical_place_id"), str)
                ]
                if len(matching_instructions) != 1:
                    continue
                instructed_place_id = matching_instructions[0]["canonical_place_id"]
                candidates = resolution.get("candidates", [])
                matching_candidates = [
                    candidate
                    for candidate in candidates
                    if isinstance(candidate, dict)
                    and candidate.get("place_id") == instructed_place_id
                ]
                if len(matching_candidates) == 1:
                    confirmations.append(
                        {
                            "raw_stop_id": resolution["raw_stop_id"],
                            "place_id": instructed_place_id,
                        }
                    )
            if confirmations:
                confirmed_response = recorder.request(
                    case_id,
                    "confirm_resolutions",
                    "PATCH",
                    f"/api/trip-workspaces/{workspace_id}/imports/{import_id}/resolutions",
                    bearer_token=bearer_token,
                    headers={"If-Match": f'"{created["state_version"]}"'},
                    json_body={"confirmations": confirmations},
                )
                created = _expect(confirmed_response, {200}, "confirm_resolutions")
                receipts.extend(_collect_receipts(case_id, created, "confirm"))
        apply_response = recorder.request(
            case_id,
            "apply_import",
            "POST",
            f"/api/trip-workspaces/{workspace_id}/imports/{import_id}/apply",
            bearer_token=bearer_token,
            headers={
                "If-Match": f'"{created["state_version"]}"',
                "Idempotency-Key": f"continuous-apply-{suffix}",
            },
        )
        unresolved_now = any(
            isinstance(item, dict) and item.get("resolution_status") in {"AMBIGUOUS", "NOT_FOUND"}
            for item in created.get("resolutions", [])
        )
        if unresolved_now or created.get("status") == "FAILED" or prohibit_apply:
            if apply_response.status_code not in {409, 422}:
                raise RuntimeError(f"unresolved apply returned HTTP {apply_response.status_code}")
        else:
            applied = _expect(apply_response, {200}, "apply_import")
            receipts.extend(_collect_receipts(case_id, applied, "apply"))
    readback_response = recorder.request(
        case_id,
        "readback_import",
        "GET",
        f"/api/trip-workspaces/{workspace_id}/imports/{import_id}",
        bearer_token=bearer_token,
    )
    readback = _expect(readback_response, {200}, "readback_import")
    receipts.extend(_collect_receipts(case_id, readback, "readback"))
    snapshot = _expect(
        recorder.request(
            case_id,
            "readback_workspace",
            "GET",
            f"/api/trip-workspaces/{workspace_id}/snapshot",
            bearer_token=bearer_token,
        ),
        {200},
        "readback_workspace",
    )
    audit_report: dict[str, Any] | None = None
    audit_execution: dict[str, Any] | None = None
    repair_metric_options: list[dict[str, Any]] | None = None
    repair_readbacks: list[dict[str, Any]] = []
    repair_generation: dict[str, Any] | None = None
    if "full_audit" in steps:
        if not isinstance(snapshot.get("current_revision"), dict):
            audit_execution = {
                "status": "NOT_EXECUTED_NO_APPLIED_REVISION",
                "reason_code": "AUDIT_REQUIRES_APPLIED_REVISION",
            }
        else:
            audit_report = _expect(
                recorder.request(
                    case_id,
                    "full_audit",
                    "POST",
                    f"/api/trip-workspaces/{workspace_id}/audits",
                    bearer_token=bearer_token,
                    headers={"Idempotency-Key": f"continuous-audit-{suffix}"},
                    json_body={"task_id": None},
                ),
                {200},
                "full_audit",
            )
            audit_execution = {"status": "EXECUTED", "report_id": audit_report.get("report_id")}
    if "generate_repair" in steps and audit_report is None:
        repair_generation = {
            "status": "NOT_EXECUTED_NO_AUDIT",
            "reason_code": "REPAIR_REQUIRES_COMPLETED_AUDIT",
        }
        steps.discard("generate_repair")
    if "generate_repair" in steps:
        if not isinstance(audit_report.get("report_id"), str):
            raise RuntimeError("completed audit response has no report_id")
        audit_id = audit_report["report_id"]
        proposed_response = recorder.request(
            case_id,
            "generate_repair",
            "POST",
            f"/api/audits/{audit_id}/repairs",
            bearer_token=bearer_token,
            headers={"Idempotency-Key": f"continuous-repair-{suffix}"},
        )
        if proposed_response.status_code == 422 and _detail_code(proposed_response) == "REPAIR_NO_FEASIBLE_OPTION":
            proposed = []
            repair_generation = {
                "status": "NO_FEASIBLE_OPTION",
                "error_code": "REPAIR_NO_FEASIBLE_OPTION",
                "detail": proposed_response.body.get("detail")
                if isinstance(proposed_response.body, dict)
                else None,
            }
        elif proposed_response.status_code != 201 or not isinstance(proposed_response.body, list):
            raise RuntimeError(f"generate_repair returned HTTP {proposed_response.status_code} or non-list JSON")
        else:
            proposed = [item for item in proposed_response.body if isinstance(item, dict)]
            if len(proposed) != len(proposed_response.body):
                raise RuntimeError("generate_repair returned a malformed option")
            repair_generation = {"status": "PROPOSED", "option_count": len(proposed)}
        repair_metric_options = []
        base_revision = snapshot.get("current_revision") if isinstance(snapshot.get("current_revision"), dict) else None
        for index, option in enumerate(proposed, start=1):
            repair_id = option.get("repair_id")
            if not isinstance(repair_id, str):
                raise RuntimeError("generate_repair returned an option without repair_id")
            readback = option
            if "preview_repair" in steps:
                readback = _expect(
                    recorder.request(
                        case_id,
                        f"preview_repair_{index}",
                        "GET",
                        f"/api/audits/{audit_id}/repairs/{repair_id}",
                        bearer_token=bearer_token,
                    ),
                    {200},
                    f"preview_repair_{index}",
                )
            postcheck: dict[str, Any] | None = None
            postcheck_id = readback.get("postcheck_report_id")
            if "postcheck" in steps and isinstance(postcheck_id, str):
                postcheck = _expect(
                    recorder.request(
                        case_id,
                        f"postcheck_readback_{index}",
                        "GET",
                        f"/api/audits/{postcheck_id}",
                        bearer_token=bearer_token,
                    ),
                    {200},
                    f"postcheck_readback_{index}",
                )
            repair_readbacks.append({"option": readback, "postcheck_report": postcheck})
            repair_metric_options.append(
                _repair_metric_option(
                    readback,
                    base_revision=base_revision,
                    source_audit=audit_report,
                    postcheck_audit=postcheck,
                )
            )
        if "apply_repair" in steps:
            if not proposed:
                repair_generation = {
                    **(repair_generation or {}),
                    "apply_status": "NOT_APPLIED_NO_FEASIBLE_OPTION",
                }
            else:
                base_revision_number = base_revision.get("revision") if isinstance(base_revision, dict) else None
                if not isinstance(base_revision_number, int):
                    raise RuntimeError("apply_repair requires a current itinerary revision")
                selected_repair_id = proposed[0]["repair_id"]
                _expect(
                    recorder.request(
                        case_id,
                        "apply_repair",
                        "POST",
                        f"/api/audits/{audit_id}/repairs/{selected_repair_id}/apply",
                        bearer_token=bearer_token,
                        headers={
                            "If-Match": f'"{base_revision_number}"',
                            "Idempotency-Key": f"continuous-repair-apply-{suffix}",
                        },
                        json_body={"base_revision": base_revision_number},
                    ),
                    {200},
                    "apply_repair",
                )
    stop_names_by_id = {
        stop_id: str(stop.get("raw_name") or stop.get("place_id") or stop_id)
        for stop_id, stop in _revision_stops(
            snapshot.get("current_revision") if isinstance(snapshot.get("current_revision"), dict) else None
        ).items()
    }
    metric_actuals = import_metric_actuals(
        initial_import,
        audit_report=audit_report,
        repair_options=repair_metric_options,
        stop_names_by_id=stop_names_by_id,
    )
    output = {
        "schema_version": "continuous-import-product-output-v1",
        "case_id": case_id,
        "workspace_id": workspace_id,
        "import_id": import_id,
        "import_create": initial_import,
        "import_readback": readback,
        "workspace_snapshot": snapshot,
        "audit_report": audit_report,
        "audit_execution": audit_execution,
        "repair_readbacks": repair_readbacks,
        "repair_generation": repair_generation,
        "metric_actuals": metric_actuals,
        "apply_status_code": apply_response.status_code if apply_response else None,
        "apply_error_code": _detail_code(apply_response) if apply_response else None,
        "executed_steps": sorted(
            {
                item["step"]
                for item in recorder.transactions
                if item["case_id"] == case_id and item.get("status_code") is not None
            }
        ),
    }
    score = _score_case(case, label, initial_import, apply_response, receipts, metric_actuals)
    return output, receipts, score


def _receipt_threshold_gates(
    scores: list[dict[str, Any]], thresholds: Mapping[str, Any]
) -> list[dict[str, Any]]:
    bindings = {
        "provider_receipt_contract_rate": "PROVIDER_RECEIPT_CONTRACT",
        "offered_receipt_case_rate": "OFFERED_RECEIPT_CONTRACT",
        "materialized_receipt_eligible_case_rate": "MATERIALIZED_RECEIPT_CONTRACT",
        "wrong_city_rejected_receipt_rate": "REJECTED_WRONG_CITY_RECEIPT:",
    }
    gates: list[dict[str, Any]] = []
    for threshold_name, check_id in bindings.items():
        if threshold_name not in thresholds:
            continue
        eligible = [
            check
            for score in scores
            for check in score.get("checks", [])
            if (
                check.get("id", "").startswith(check_id)
                if check_id.endswith(":")
                else check.get("id") == check_id
            )
        ]
        numerator = sum(check.get("status") == "PASS" for check in eligible)
        denominator = len(eligible)
        actual = numerator / denominator if denominator else None
        required = float(thresholds[threshold_name])
        reasons = []
        if not denominator:
            reasons.append("NO_ELIGIBLE_CASE")
        elif actual is None or actual < required:
            reasons.append("THRESHOLD_NOT_MET")
        gates.append(
            {
                "id": f"RECEIPT_THRESHOLD:{threshold_name}",
                "status": "PASS" if not reasons else "FAIL",
                "threshold": required,
                "actual": actual,
                "numerator": numerator,
                "denominator": denominator,
                "reason_codes": reasons,
            }
        )
    return gates


def run_import_http(
    spec_path: str | Path,
    *,
    runs_root: str | Path | None = None,
    repo_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    transport: HttpTransport | None = None,
    bearer_token: str | None = None,
    timeout_seconds: float = 3.0,
) -> RunResult:
    started_at = time.time()
    started_at_iso = datetime.now(timezone.utc).isoformat()
    preflight_result = preflight(spec_path, repo_root=repo_root, environ=environ)
    lane = preflight_result.resolved_spec.get("lane", "invalid") if preflight_result.resolved_spec else "invalid"
    output_root = (
        Path(runs_root).resolve()
        if runs_root is not None
        else preflight_result.repo_root / "backend" / "evidence" / "runs"
    )
    run_id, run_dir = _new_run_dir(output_root, str(lane))
    artifact_spec = copy.deepcopy(preflight_result.resolved_spec) if preflight_result.resolved_spec else {
        "schema_version": "invalid-run-spec-v1",
        "source_path": str(preflight_result.spec_path),
    }
    artifact_spec["run_id"] = run_id
    artifact_spec["started_at"] = started_at_iso
    _atomic_write_json(run_dir / "run_spec.json", artifact_spec)
    product_outputs: list[dict[str, Any]] = []
    provider_receipts: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    bad_cases: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    recorder: _Recorder | None = None

    if not preflight_result.valid:
        reason = "PREFLIGHT_FAILED"
        errors.extend(preflight_result.errors)
    else:
        try:
            cases, labels = _load_selected_cases_and_labels(artifact_spec, preflight_result.repo_root)
            sut = artifact_spec.get("sut", {})
            if sut.get("allow_direct_domain_calls") is not False or sut.get("allow_sql_seed") is not False:
                raise ValueError("HTTP_ONLY_AND_NO_SQL_SEED_CONTRACT_REQUIRED")
            recorder = _Recorder(transport or UrllibTransport(), str(sut["base_url"]), timeout_seconds)
            token = bearer_token
            if not token:
                login = recorder.request("__run__", "auth_test_login", "POST", "/api/auth/test-login")
                login_body = _expect(login, {200}, "auth_test_login")
                token = login_body.get("token")
                if not isinstance(token, str) or not token:
                    raise RuntimeError("auth_test_login returned no bearer token")
            for case in cases:
                try:
                    output, receipts, score = _execute_case(
                        recorder,
                        case,
                        labels[case["case_id"]],
                        token,
                        run_namespace=run_id,
                    )
                    product_outputs.append(output)
                    provider_receipts.extend(receipts)
                    scores.append(score)
                    if score["status"] != "PASS":
                        bad_cases.append({"case_id": case["case_id"], "reason": "DETERMINISTIC_SCORE_FAILED"})
                except Exception as exc:
                    bad_cases.append(
                        {"case_id": case["case_id"], "reason": "HTTP_WORKFLOW_FAILED", "message": str(exc)}
                    )
            reason = "IMPORT_HTTP_SLICE_COMPLETE" if not bad_cases else "IMPORT_HTTP_CASES_FAILED"
        except Exception as exc:
            reason = "PRODUCT_HTTP_ADAPTER_UNAVAILABLE"
            errors.append({"code": reason, "message": str(exc)})

    transactions = recorder.transactions if recorder else []
    metric_scores = [score["metric_score"] for score in scores]
    metric_aggregate = aggregate_metric_scores(metric_scores)
    metric_gates = evaluate_metric_thresholds(metric_aggregate, artifact_spec.get("thresholds", {}))
    receipt_gates = _receipt_threshold_gates(scores, artifact_spec.get("thresholds", {}))
    minimum_cases = artifact_spec.get("thresholds", {}).get("http_import_cases_min")
    count_gate = None
    if minimum_cases is not None:
        count_gate = {
            "id": "HTTP_IMPORT_CASE_COUNT",
            "status": "PASS" if len(product_outputs) >= int(minimum_cases) else "FAIL",
            "threshold": int(minimum_cases),
            "actual": len(product_outputs),
        }
    all_pass = (
        preflight_result.valid
        and reason == "IMPORT_HTTP_SLICE_COMPLETE"
        and all(item["status"] == "PASS" for item in metric_gates)
        and all(item["status"] == "PASS" for item in receipt_gates)
        and (count_gate is None or count_gate["status"] == "PASS")
    )
    cost = {
        "schema_version": "continuous-cost-v1",
        "currency": "CNY",
        "paid_api_calls": 0,
        "total_cost": 0,
        "declared_budget": artifact_spec.get("budget", {}).get("max_total_cost_cny"),
    }
    _atomic_write_jsonl(run_dir / "product_outputs.jsonl", product_outputs)
    _atomic_write_jsonl(run_dir / "provider_receipts.jsonl", provider_receipts)
    _atomic_write_jsonl(run_dir / "http_transactions.jsonl", transactions)
    _atomic_write_json(
        run_dir / "deterministic_scores.json",
        {"cases": scores, "metric_aggregate": metric_aggregate, "metric_gates": metric_gates},
    )
    _atomic_write_jsonl(run_dir / "bad_cases.jsonl", bad_cases)
    _atomic_write_json(run_dir / "cost.json", cost)
    gate = {
        "schema_version": "continuous-gate-v1",
        "run_id": run_id,
        "lane": lane,
        "status": "PASS" if all_pass else "INVALID",
        "decision": "PROMOTE" if all_pass else "REJECT",
        "phase": "IMPORT_HTTP",
        "claim_scope": artifact_spec.get("execution", {}).get("claim_scope"),
        "started_at_epoch": started_at,
        "completed_at_epoch": time.time(),
        "run_spec_artifact_sha256": _sha256_bytes((run_dir / "run_spec.json").read_bytes()),
        "bindings": preflight_result.bindings,
        "gates": [
            *preflight_result.checks,
            *metric_gates,
            *receipt_gates,
            *([count_gate] if count_gate else []),
            {"id": "PRODUCT_HTTP_EXECUTION", "status": "PASS" if all_pass else "FAIL", "reason": reason},
        ],
        "failed_cases": [item["case_id"] for item in bad_cases],
        "execution": {
            "attempted": bool(transactions),
            "product_http_calls": len(transactions),
            "adapter": "breezetravel-import-http-v1",
            "direct_domain_calls": 0,
            "sql_seed_operations": 0,
            "selected_case_count": preflight_result.bindings.get("selected_case_count", 0),
            "completed_case_count": len(product_outputs),
            "reason": reason,
        },
        "errors": errors,
    }
    _atomic_write_json(run_dir / "gate.json", gate)
    return RunResult(run_id=run_id, run_dir=run_dir, gate=gate)
