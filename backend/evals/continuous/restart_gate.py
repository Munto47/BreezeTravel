from __future__ import annotations

import json
import hashlib
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_COMMAND_ID = "frontend-dual-user-backend-yjs-restart-v3-nine-case-matrix"
_EVIDENCE_PATH = Path(
    "backend/evidence/full_stack/dual_user_backend_yjs_restart_2026-08-20.json"
)
_SCRIPT_PATH = Path("frontend/scripts/run-dual-user-restart-e2e.ps1")
_REQUIRED_ASSERTIONS = {
    "exactly_nine_independent_cases",
    "all_pre_restart_browser_contexts_closed",
    "backend_process_restarted",
    "yjs_process_restarted_with_same_named_volume",
    "backend_and_yjs_boot_generation_changed",
    "stopped_ports_were_unavailable_before_start",
    "postgres_container_not_restarted",
    "all_fresh_yjs_reads_preceded_browser_reconnect",
    "all_case_http_yjs_browser_refs_recovered_exactly",
    "public_http_and_authenticated_yjs_only",
}
_REQUIRED_CASE_ASSERTIONS = {
    "independent_seed_and_storage_keys",
    "exact_revision_and_content_hash",
    "exact_audit_report_and_revision",
    "exact_member_constraints_and_revision",
    "exact_available_map_projection_and_hash",
    "exact_nonempty_recommendation_event_ledger",
    "exact_yjs_places_and_builder_events",
    "fresh_yjs_read_preceded_browser_reconnect",
    "two_fresh_browser_contexts_match_authority",
}
_EXPECTED_CITIES = {"北京": 3, "上海": 3, "杭州": 3}


@dataclass(frozen=True)
class RestartGateResult:
    status: str
    reason_code: str
    receipt: dict[str, Any]

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _valid_boot_witness(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return bool(
        isinstance(value.get("instance_id"), str)
        and len(value["instance_id"]) == 36
        and _parse_timestamp(value.get("started_at")) is not None
        and isinstance(value.get("pid"), int)
        and value["pid"] > 0
    )


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_case(case: Any, index: int) -> list[str]:
    prefix = f"CASE_{index + 1}"
    errors: list[str] = []
    if not isinstance(case, dict):
        return [f"{prefix}_NOT_OBJECT"]
    if case.get("status") != "PASS":
        errors.append(f"{prefix}_STATUS_NOT_PASS")
    for field in ("case_id", "seed_id", "room_id", "workspace_id", "city", "operation"):
        if not isinstance(case.get(field), str) or not case[field]:
            errors.append(f"{prefix}_{field.upper()}_MISSING")

    expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
    authoritative = (
        expected.get("authoritative")
        if isinstance(expected.get("authoritative"), dict)
        else {}
    )
    resume = (
        authoritative.get("resume")
        if isinstance(authoritative.get("resume"), dict)
        else {}
    )
    map_projection = (
        authoritative.get("map_projection")
        if isinstance(authoritative.get("map_projection"), dict)
        else {}
    )
    events = authoritative.get("recommendation_events")
    members = authoritative.get("members")
    if not isinstance(resume.get("revision"), int) or resume["revision"] < 2:
        errors.append(f"{prefix}_REVISION_INVALID")
    content_hash = resume.get("content_hash")
    if not isinstance(content_hash, str) or len(content_hash) != 64:
        errors.append(f"{prefix}_CONTENT_HASH_INVALID")
    if not isinstance(resume.get("report_id"), str) or not resume["report_id"]:
        errors.append(f"{prefix}_AUDIT_REPORT_ID_MISSING")
    if resume.get("report_revision") != resume.get("revision"):
        errors.append(f"{prefix}_AUDIT_REVISION_MISMATCH")
    if not isinstance(resume.get("member_constraint_revision"), int):
        errors.append(f"{prefix}_MEMBER_REVISION_MISSING")
    if not isinstance(members, list) or len(members) < 2:
        errors.append(f"{prefix}_MEMBER_READBACK_MISSING")
    if map_projection.get("status") != "AVAILABLE" or len(map_projection.get("stops") or []) < 2:
        errors.append(f"{prefix}_MAP_PROJECTION_NOT_AVAILABLE")
    map_hash = authoritative.get("map_projection_sha256")
    if not isinstance(map_hash, str) or map_hash != _sha256_json(map_projection):
        errors.append(f"{prefix}_MAP_PROJECTION_HASH_MISMATCH")
    if not isinstance(events, list) or not events:
        errors.append(f"{prefix}_RECOMMENDATION_EVENT_LEDGER_EMPTY")
    elif len({event.get("event_id") for event in events if isinstance(event, dict)}) != len(events):
        errors.append(f"{prefix}_RECOMMENDATION_EVENT_IDS_NOT_UNIQUE")

    yjs_expected = expected.get("yjs") if isinstance(expected.get("yjs"), dict) else {}
    if (
        yjs_expected.get("itinerary_revision") != resume.get("revision")
        or yjs_expected.get("itinerary_content_hash") != content_hash
        or yjs_expected.get("audit_report_id") != resume.get("report_id")
        or yjs_expected.get("audit_revision") != resume.get("report_revision")
        or yjs_expected.get("member_constraint_revision")
        != resume.get("member_constraint_revision")
        or yjs_expected.get("map_revision") != resume.get("revision")
        or yjs_expected.get("map_projection_sha256") != map_hash
    ):
        errors.append(f"{prefix}_YJS_AUTHORITY_REF_MISMATCH")
    if not yjs_expected.get("places") or not yjs_expected.get("builder_events"):
        errors.append(f"{prefix}_YJS_PLACE_OR_EVENT_EMPTY")

    before = case.get("before_restart") if isinstance(case.get("before_restart"), dict) else {}
    after = case.get("after_restart") if isinstance(case.get("after_restart"), dict) else {}
    if before.get("authoritative_http") != authoritative:
        errors.append(f"{prefix}_PRE_RESTART_HTTP_MISMATCH")
    if before.get("yjs_fresh_client") != yjs_expected:
        errors.append(f"{prefix}_PRE_RESTART_YJS_MISMATCH")
    if after.get("authoritative_http") != authoritative:
        errors.append(f"{prefix}_POST_RESTART_HTTP_MISMATCH")
    if after.get("yjs_fresh_client_before_browser") != yjs_expected:
        errors.append(f"{prefix}_POST_RESTART_YJS_MISMATCH")
    for phase, container in (("PRE", before), ("POST", after)):
        browser = container.get("browser") if isinstance(container.get("browser"), dict) else {}
        if browser.get("browser_a") != authoritative or browser.get("browser_b") != authoritative:
            errors.append(f"{prefix}_{phase}_RESTART_BROWSER_MISMATCH")

    assertions = case.get("assertions") if isinstance(case.get("assertions"), dict) else {}
    missing = sorted(key for key in _REQUIRED_CASE_ASSERTIONS if assertions.get(key) is not True)
    if missing:
        errors.append(f"{prefix}_ASSERTIONS_FALSE:{','.join(missing)}")
    return errors


def validate_restart_evidence(
    payload: Any,
    *,
    launched_at: datetime,
) -> RestartGateResult:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return RestartGateResult("FAIL", "RESTART_EVIDENCE_NOT_OBJECT", {"errors": []})
    started_at = _parse_timestamp(payload.get("started_at"))
    finished_at = _parse_timestamp(payload.get("finished_at"))
    if payload.get("status") != "PASSED":
        errors.append("STATUS_NOT_PASSED")
    if payload.get("schema_version") != "3.0":
        errors.append("RESTART_EVIDENCE_SCHEMA_NOT_V3")
    if started_at is None or started_at < launched_at:
        errors.append("STALE_OR_INVALID_STARTED_AT")
    if finished_at is None or (started_at is not None and finished_at < started_at):
        errors.append("INVALID_FINISHED_AT")

    services = payload.get("services") if isinstance(payload.get("services"), dict) else {}
    before = services.get("boot_before") if isinstance(services.get("boot_before"), dict) else {}
    after = services.get("boot_after") if isinstance(services.get("boot_after"), dict) else {}
    for service in ("backend", "y_websocket"):
        old = before.get(service)
        new = after.get(service)
        if not _valid_boot_witness(old) or not _valid_boot_witness(new):
            errors.append(f"{service.upper()}_BOOT_WITNESS_INVALID")
            continue
        if old["instance_id"] == new["instance_id"]:
            errors.append(f"{service.upper()}_BOOT_INSTANCE_UNCHANGED")
        if old["started_at"] == new["started_at"]:
            errors.append(f"{service.upper()}_BOOT_TIME_UNCHANGED")
    if services.get("stopped_ports_observed_unavailable") is not True:
        errors.append("STOPPED_PORT_UNAVAILABILITY_NOT_OBSERVED")
    service_before = (
        services.get("before_restart")
        if isinstance(services.get("before_restart"), dict)
        else {}
    )
    service_after = (
        services.get("after_restart")
        if isinstance(services.get("after_restart"), dict)
        else {}
    )
    for service in ("backend", "y_websocket"):
        old = service_before.get(service) if isinstance(service_before.get(service), dict) else {}
        new = service_after.get(service) if isinstance(service_after.get(service), dict) else {}
        if old.get("id") != new.get("id"):
            errors.append(f"{service.upper()}_CONTAINER_REPLACED_UNEXPECTEDLY")
        if old.get("host_pid") == new.get("host_pid"):
            errors.append(f"{service.upper()}_HOST_PID_UNCHANGED")
        if old.get("started_at") == new.get("started_at"):
            errors.append(f"{service.upper()}_CONTAINER_START_TIME_UNCHANGED")
    postgres_before = (
        service_before.get("postgres")
        if isinstance(service_before.get("postgres"), dict)
        else {}
    )
    postgres_after = (
        service_after.get("postgres")
        if isinstance(service_after.get("postgres"), dict)
        else {}
    )
    if (
        not postgres_before
        or postgres_before.get("id") != postgres_after.get("id")
        or postgres_before.get("started_at") != postgres_after.get("started_at")
    ):
        errors.append("POSTGRES_PROCESS_CHANGED")
    if not isinstance(services.get("yjs_named_volume_preserved"), str):
        errors.append("YJS_NAMED_VOLUME_NOT_PROVEN")

    cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    if len(cases) != 9:
        errors.append("EXACTLY_NINE_RECOVERY_CASES_REQUIRED")
    identifiers = {
        field: [case.get(field) for case in cases if isinstance(case, dict)]
        for field in ("case_id", "seed_id", "room_id", "workspace_id")
    }
    for field, values in identifiers.items():
        if len(values) != 9 or len(set(values)) != 9:
            errors.append(f"CASE_{field.upper()}S_NOT_UNIQUE")
    city_counts = {
        city: sum(1 for case in cases if isinstance(case, dict) and case.get("city") == city)
        for city in _EXPECTED_CITIES
    }
    if city_counts != _EXPECTED_CITIES:
        errors.append("THREE_CITY_CASE_MATRIX_INVALID")
    for index, case in enumerate(cases):
        errors.extend(_validate_case(case, index))

    assertions = payload.get("assertions") if isinstance(payload.get("assertions"), dict) else {}
    missing = sorted(key for key in _REQUIRED_ASSERTIONS if assertions.get(key) is not True)
    if missing:
        errors.append(f"REQUIRED_ASSERTIONS_FALSE:{','.join(missing)}")

    cleanup = payload.get("cleanup") if isinstance(payload.get("cleanup"), dict) else {}
    if (
        cleanup.get("postgres") != "CLEARED"
        or cleanup.get("postgres_room_count") != 9
        or cleanup.get("yjs_documents") != "CLEARED"
        or cleanup.get("yjs_room_count") != 9
    ):
        errors.append("ISOLATED_TEST_STATE_NOT_CLEARED")

    safety = payload.get("safety_contract") if isinstance(payload.get("safety_contract"), dict) else {}
    if any(safety.get(field) != 0 for field in ("direct_domain_calls", "direct_sql_calls", "direct_leveldb_calls")):
        errors.append("PROHIBITED_INTERNAL_ACCESS_REPORTED")
    if safety.get("repository_rebuild_substitute") is not False:
        errors.append("REPOSITORY_REBUILD_SUBSTITUTE_NOT_REJECTED")

    receipt = {
        "schema_version": "backend-yjs-restart-gate-v2",
        "status": "PASS" if not errors else "FAIL",
        "reason_code": "RESTART_EVIDENCE_VALID" if not errors else "RESTART_EVIDENCE_INVALID",
        "claim_scope": "local_fixture_public_http_yjs_browser",
        "provider_or_human_claim": False,
        "errors": errors,
        "source_evidence": payload,
    }
    return RestartGateResult(receipt["status"], receipt["reason_code"], receipt)


def run_checked_in_restart_gate(
    repo_root: Path,
    *,
    environ: Mapping[str, str],
    command_id: str,
    timeout_seconds: float = 420.0,
) -> RestartGateResult:
    if command_id != _COMMAND_ID:
        return RestartGateResult(
            "FAIL",
            "RESTART_COMMAND_NOT_ALLOWLISTED",
            {"schema_version": "backend-yjs-restart-gate-v2", "status": "FAIL"},
        )
    if environ.get("BREEZE_EVAL_ALLOW_SERVICE_RESTART") != "1":
        return RestartGateResult(
            "UNAVAILABLE",
            "SERVICE_RESTART_OPT_IN_REQUIRED",
            {
                "schema_version": "backend-yjs-restart-gate-v2",
                "status": "UNAVAILABLE",
                "claim_scope": "local_fixture_public_http_yjs_browser",
            },
        )
    script = (repo_root / _SCRIPT_PATH).resolve()
    evidence_path = (repo_root / _EVIDENCE_PATH).resolve()
    if not script.is_file():
        return RestartGateResult(
            "FAIL",
            "RESTART_SCRIPT_MISSING",
            {"schema_version": "backend-yjs-restart-gate-v2", "status": "FAIL"},
        )
    launched_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            cwd=repo_root,
            env={**os.environ, **dict(environ)},
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RestartGateResult(
            "FAIL",
            "RESTART_PROCESS_FAILED",
            {
                "schema_version": "backend-yjs-restart-gate-v2",
                "status": "FAIL",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    process_receipt = {
        "exit_code": completed.returncode,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }
    if completed.returncode != 0:
        return RestartGateResult(
            "FAIL",
            "RESTART_PROCESS_NONZERO_EXIT",
            {"schema_version": "backend-yjs-restart-gate-v2", "status": "FAIL", "process": process_receipt},
        )
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return RestartGateResult(
            "FAIL",
            "RESTART_EVIDENCE_READ_FAILED",
            {
                "schema_version": "backend-yjs-restart-gate-v2",
                "status": "FAIL",
                "process": process_receipt,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    result = validate_restart_evidence(payload, launched_at=launched_at)
    result.receipt["process"] = process_receipt
    result.receipt["command_id"] = command_id
    return result
