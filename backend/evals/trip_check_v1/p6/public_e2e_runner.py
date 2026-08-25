"""Fail-closed P6 G5 public health and controlled-snapshot full-chain runner."""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from evals.trip_check_v1.p6.contracts_v1 import (
    P6ContractError,
    digest,
    read_actual_repo_state,
    validate_candidate_run_spec,
    validate_public_receipt,
)


PUBLIC_CREDENTIAL_KEYS = {"P6_PUBLIC_TEST_EMAIL", "P6_PUBLIC_TEST_PASSWORD"}


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        raise urllib.error.HTTPError(req.full_url, code, "redirect rejected", headers, fp)


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P6ContractError(reason) from exc
    if not isinstance(value, dict):
        raise P6ContractError(reason)
    return value


def _write_json_new(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except OSError as exc:
        raise P6ContractError("P6_G5_PUBLIC_ARTIFACT_WRITE_FAILED") from exc


def _external_file(path: Path | None, repo_root: Path, reason: str) -> Path:
    if path is None:
        raise P6ContractError(reason)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(repo_root.resolve(strict=True))
    except ValueError:
        if resolved.is_file():
            return resolved
    except (OSError, RuntimeError) as exc:
        raise P6ContractError(reason) from exc
    raise P6ContractError(reason)


def _credentials(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise P6ContractError("P6_G5_PUBLIC_CREDENTIAL_FILE_INVALID") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise P6ContractError("P6_G5_PUBLIC_CREDENTIAL_FILE_INVALID")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key not in PUBLIC_CREDENTIAL_KEYS or key in values or not value:
            raise P6ContractError("P6_G5_PUBLIC_CREDENTIAL_FILE_INVALID")
        values[key] = value
    if set(values) != PUBLIC_CREDENTIAL_KEYS:
        raise P6ContractError("P6_G5_PUBLIC_CREDENTIALS_MISSING")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", values["P6_PUBLIC_TEST_EMAIL"]):
        raise P6ContractError("P6_G5_PUBLIC_CREDENTIAL_FILE_INVALID")
    if len(values["P6_PUBLIC_TEST_PASSWORD"]) < 8:
        raise P6ContractError("P6_G5_PUBLIC_CREDENTIAL_FILE_INVALID")
    return values


class _HttpClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.opener = urllib.request.build_opener(_RejectRedirects())
        self.token: str | None = None
        self.steps: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        route: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
        record: bool = True,
    ) -> tuple[Any, str, int]:
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers = {"Accept": "application/json", "User-Agent": "BreezeTravel-P6-Public-E2E/1"}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        if self.token:
            request_headers["Authorization"] = f"Bearer {self.token}"
        request_headers.update(headers or {})
        request = urllib.request.Request(
            f"{self.base_url}{route}",
            data=payload,
            headers=request_headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            except OSError as read_exc:
                raise P6ContractError("P6_G5_PUBLIC_HTTP_FAILED") from read_exc
        except (OSError, urllib.error.URLError) as exc:
            raise P6ContractError("P6_G5_PUBLIC_HTTP_FAILED") from exc
        if status not in expected:
            raise P6ContractError("P6_G5_PUBLIC_HTTP_STATUS_INVALID")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise P6ContractError("P6_G5_PUBLIC_HTTP_JSON_INVALID") from exc
        body_sha = hashlib.sha256(raw).hexdigest()
        if record:
            self.steps.append({"method": method, "route": route, "status": status, "body_sha256": body_sha})
        return parsed, body_sha, status


def _execute_public_flow(spec: Mapping[str, Any], credentials: Mapping[str, str]) -> dict[str, Any]:
    client = _HttpClient(spec["public_candidate"]["base_url"])
    health, health_sha, health_status = client.request("GET", "/health", record=False)
    if not isinstance(health, Mapping) or health.get("status") != "ok":
        raise P6ContractError("P6_G5_PUBLIC_HEALTH_INVALID")
    login, _, login_status = client.request(
        "POST",
        "/api/auth/email-login",
        body={
            "email": credentials["P6_PUBLIC_TEST_EMAIL"],
            "password": credentials["P6_PUBLIC_TEST_PASSWORD"],
        },
        expected=(200, 401),
    )
    account_created = False
    if login_status == 401:
        registered, _, register_status = client.request(
            "POST",
            "/api/auth/email-register",
            body={
                "email": credentials["P6_PUBLIC_TEST_EMAIL"],
                "password": credentials["P6_PUBLIC_TEST_PASSWORD"],
                "nickname": "P6 Candidate E2E",
            },
            expected=(200, 409),
        )
        if register_status != 200 or not isinstance(registered, Mapping):
            raise P6ContractError("P6_G5_PUBLIC_TEST_ACCOUNT_CONFLICT")
        account_created = True
        login, _, _ = client.request(
            "POST",
            "/api/auth/email-login",
            body={
                "email": credentials["P6_PUBLIC_TEST_EMAIL"],
                "password": credentials["P6_PUBLIC_TEST_PASSWORD"],
            },
        )
    if not isinstance(login, Mapping) or not isinstance(login.get("token"), str):
        raise P6ContractError("P6_G5_PUBLIC_LOGIN_FAILED")
    client.token = login["token"]
    suffix = uuid.uuid4().hex[:12]
    room_id = f"e2e-p6-{suffix}"
    thread_id = f"e2e-p6-thread-{suffix}"
    client.request(
        "POST",
        "/api/room",
        body={
            "room_id": room_id,
            "thread_id": thread_id,
            "trip_city": "北京",
            "trip_days": 2,
            "nickname": "P6 Candidate E2E",
        },
    )
    start = date.today() + timedelta(days=30)
    end = start + timedelta(days=1)
    workspace, _, _ = client.request(
        "POST",
        "/api/trip-workspaces",
        body={
            "room_id": room_id,
            "city": "北京",
            "trip_date_range": {"start": start.isoformat(), "end": end.isoformat()},
        },
        expected=(201,),
    )
    if not isinstance(workspace, Mapping) or not isinstance(workspace.get("workspace_id"), str):
        raise P6ContractError("P6_G5_PUBLIC_WORKSPACE_INVALID")
    workspace_id = workspace["workspace_id"]
    imported, _, _ = client.request(
        "POST",
        f"/api/trip-workspaces/{workspace_id}/imports",
        body={
            "source_type": "MANUAL_TEXT",
            "raw_text": (
                f"北京2人。第1天 {start.isoformat()} 09:00-12:00 故宫博物院，"
                "11:00-13:00 景山公园；第2天 09:00-11:00 颐和园。"
            ),
        },
        headers={"Idempotency-Key": f"p6-import-{suffix}"},
        expected=(201,),
    )
    if not isinstance(imported, Mapping) or imported.get("status") != "READY":
        raise P6ContractError("P6_G5_PUBLIC_IMPORT_NOT_READY")
    resume, _, _ = client.request("GET", f"/api/trip-workspaces/{workspace_id}/resume")
    brief = resume.get("current_brief") if isinstance(resume, Mapping) else None
    if not isinstance(brief, Mapping) or not isinstance(brief.get("revision"), int):
        raise P6ContractError("P6_G5_PUBLIC_BRIEF_INVALID")
    brief_revision = brief["revision"]
    confirmed, _, _ = client.request(
        "POST",
        f"/api/trip-workspaces/{workspace_id}/trip-briefs/{brief_revision}/confirm",
        body={},
        headers={
            "If-Match": f'"{brief_revision}"',
            "Idempotency-Key": f"p6-confirm-{suffix}",
        },
    )
    if not isinstance(confirmed, Mapping) or confirmed.get("status") != "CONFIRMED":
        raise P6ContractError("P6_G5_PUBLIC_BRIEF_CONFIRM_FAILED")
    import_id = imported.get("import_id")
    state_version = imported.get("state_version")
    if not isinstance(import_id, str) or not isinstance(state_version, int):
        raise P6ContractError("P6_G5_PUBLIC_IMPORT_INVALID")
    applied, _, _ = client.request(
        "POST",
        f"/api/trip-workspaces/{workspace_id}/imports/{import_id}/apply",
        body={},
        headers={
            "If-Match": f'"{state_version}"',
            "Idempotency-Key": f"p6-apply-import-{suffix}",
        },
    )
    revision = applied.get("revision") if isinstance(applied, Mapping) else None
    if not isinstance(revision, Mapping) or revision.get("revision") != 1:
        raise P6ContractError("P6_G5_PUBLIC_REVISION_ONE_MISSING")
    run_spec = {
        "schema_version": "trip-check-run-spec-v1",
        "commit_sha": spec["subject_commit"],
        "prompt_version": "none-p6",
        "model_version": "none-p6",
        "provider_version": "p3-provider-integrity-v1",
        "rule_set_version": "audit-v1",
        "execution_mode": "snapshot",
        "dataset_hash": spec["bindings"]["ocr_dataset_manifest_sha256"],
        "snapshot_hash": spec["bindings"]["snapshot_manifest_sha256"],
        "fault_profile": "none",
        "random_seed": 7,
        "budget": {
            "max_tokens": 0,
            "max_provider_queries": 6,
            "max_retries": 0,
            "timeout_seconds": 45,
            "max_cost_usd": 0,
        },
    }
    run, _, _ = client.request(
        "POST",
        f"/api/trip-workspaces/{workspace_id}/trip-check-runs",
        body={
            "itinerary_revision": 1,
            "brief_revision": brief_revision,
            "run_spec": run_spec,
        },
        headers={"Idempotency-Key": f"p6-create-run-{suffix}"},
        expected=(201,),
    )
    run_id = run.get("run_id") if isinstance(run, Mapping) else None
    if not isinstance(run_id, str):
        raise P6ContractError("P6_G5_PUBLIC_RUN_INVALID")
    terminal: Mapping[str, Any] | None = None
    for _ in range(180):
        current, _, _ = client.request("GET", f"/api/trip-check-runs/{run_id}", record=False)
        if isinstance(current, Mapping) and (
            current.get("status") in {"WAITING", "SUCCEEDED", "PARTIAL", "FAILED", "PRIVACY_BLOCKED"}
        ):
            terminal = current
            break
        time.sleep(0.25)
    if not isinstance(terminal, Mapping) or not (
        terminal.get("status") == "WAITING"
        and terminal.get("stage") == "WAIT_ADOPTION"
        and isinstance(terminal.get("report_id"), str)
        and isinstance(terminal.get("advice_bundle_id"), str)
        and not terminal.get("partial_failures")
    ):
        raise P6ContractError("P6_G5_PUBLIC_RUN_NOT_WAITING_ADOPTION")
    client.request("GET", f"/api/trip-check-runs/{run_id}")
    report_id = terminal["report_id"]
    report, _, _ = client.request("GET", f"/api/audits/{report_id}")
    evidence, _, _ = client.request("GET", f"/api/audits/{report_id}/evidence")
    advice, _, _ = client.request(
        "GET",
        f"/api/trip-workspaces/{workspace_id}/reports/{report_id}/advice",
    )
    repairs, _, _ = client.request("GET", f"/api/audits/{report_id}/repairs")
    if not (
        isinstance(report, Mapping)
        and report.get("overall_status") == "VIOLATED"
        and isinstance(evidence, Mapping)
        and not evidence.get("provider_failures")
        and isinstance(advice, Mapping)
        and advice.get("actions")
        and isinstance(repairs, list)
        and repairs
        and isinstance(repairs[0], Mapping)
    ):
        raise P6ContractError("P6_G5_PUBLIC_ADVICE_OR_REPAIR_MISSING")
    repair = repairs[0]
    repair_id = repair.get("repair_id")
    base_revision = repair.get("base_itinerary_revision")
    if not isinstance(repair_id, str) or base_revision != 1:
        raise P6ContractError("P6_G5_PUBLIC_REPAIR_INVALID")
    applied_repair, _, _ = client.request(
        "POST",
        f"/api/audits/{report_id}/repairs/{repair_id}/apply",
        body={"base_revision": 1},
        headers={"If-Match": '"1"', "Idempotency-Key": f"p6-apply-repair-{suffix}"},
    )
    postcheck_report_id = applied_repair.get("postcheck_report_id") if isinstance(applied_repair, Mapping) else None
    if not isinstance(postcheck_report_id, str) or applied_repair.get("new_revision") != 2:
        raise P6ContractError("P6_G5_PUBLIC_REPAIR_APPLY_FAILED")
    postcheck, _, _ = client.request("GET", f"/api/audits/{postcheck_report_id}")
    final_resume, final_resume_sha, _ = client.request(
        "GET",
        f"/api/trip-workspaces/{workspace_id}/resume",
    )
    final_run = final_resume.get("current_trip_check_run") if isinstance(final_resume, Mapping) else None
    current_revision = final_resume.get("current_revision") if isinstance(final_resume, Mapping) else None
    if not (
        isinstance(postcheck, Mapping)
        and postcheck.get("itinerary_revision") == 2
        and isinstance(current_revision, Mapping)
        and current_revision.get("revision") == 2
        and isinstance(final_run, Mapping)
        and final_run.get("stage") == "POSTCHECK"
        and final_run.get("status") == "SUCCEEDED"
    ):
        raise P6ContractError("P6_G5_PUBLIC_POSTCHECK_INVALID")
    step_set = {(item["method"], item["route"]) for item in client.steps}
    return {
        "health_http_status": health_status,
        "health_response_body_sha256": health_sha,
        "step_count": len(client.steps),
        "step_set_sha256": digest(sorted(f"{method} {route}" for method, route in step_set)),
        "step_receipt_set_sha256": digest(client.steps),
        "final_resume_body_sha256": final_resume_sha,
        "initial_revision": 1,
        "final_revision": 2,
        "advice_action_count": len(advice["actions"]),
        "repair_count": len(repairs),
        "provider_failure_count": 0,
        "postcheck_status": "SUCCEEDED",
        "controlled_snapshot": True,
        "test_account_created": account_created,
    }


def _secret_leaks(root: Path, credentials: Mapping[str, str]) -> int:
    secrets = [value.encode("utf-8") for value in credentials.values() if len(value) >= 6]
    try:
        return sum(
            secret in path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
            for secret in secrets
        )
    except OSError as exc:
        raise P6ContractError("P6_G5_PUBLIC_SECRET_SCAN_FAILED") from exc


def run_public_e2e(
    *,
    candidate_run_spec_path: Path,
    output_root: Path,
    repo_root: Path,
    credential_file: Path | None = None,
    formal: bool = True,
    flow_runner: Callable[[Mapping[str, Any], Mapping[str, str]], dict[str, Any]] = _execute_public_flow,
    credentials: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    spec = validate_candidate_run_spec(
        _load_json(candidate_run_spec_path, "P6_CANDIDATE_RUN_SPEC_INVALID")
    )
    repo_resolved = repo_root.resolve(strict=True)
    output_resolved = output_root.resolve(strict=False)
    try:
        output_resolved.relative_to(repo_resolved)
    except ValueError:
        pass
    else:
        raise P6ContractError("P6_G5_PUBLIC_EXTERNAL_ROOT_REQUIRED")
    if formal:
        if flow_runner is not _execute_public_flow or credentials is not None:
            raise P6ContractError("P6_G5_PUBLIC_FORMAL_INJECTION_FORBIDDEN")
        expected = {
            "subject_commit": spec["subject_commit"],
            "upstream_ref": spec["upstream_ref"],
            "upstream_commit": spec["upstream_commit"],
            "dirty_tree": False,
        }
        if read_actual_repo_state(repo_resolved) != expected:
            raise P6ContractError("P6_G5_PUBLIC_REPO_BINDING_INVALID")
        if output_resolved != (Path(spec["evidence_root"]) / "g5" / "public").resolve(strict=False):
            raise P6ContractError("P6_G5_PUBLIC_OUTPUT_ROOT_INVALID")
        if output_resolved.exists() and any(output_resolved.iterdir()):
            raise P6ContractError("P6_G5_PUBLIC_OUTPUT_NOT_EMPTY")
        current_credentials = _credentials(
            _external_file(
                credential_file,
                repo_resolved,
                "P6_G5_PUBLIC_CREDENTIAL_FILE_INVALID",
            )
        )
    else:
        current_credentials = dict(credentials or {
            "P6_PUBLIC_TEST_EMAIL": "e2e@example.org",
            "P6_PUBLIC_TEST_PASSWORD": "test-password-1",
        })
    try:
        flow = flow_runner(spec, current_credentials)
    except P6ContractError:
        raise
    except Exception as exc:
        raise P6ContractError("P6_G5_PUBLIC_FLOW_FAILED") from exc
    required = {
        "health_http_status": 200,
        "initial_revision": 1,
        "final_revision": 2,
        "provider_failure_count": 0,
        "postcheck_status": "SUCCEEDED",
        "controlled_snapshot": True,
    }
    if any(flow.get(key) != value for key, value in required.items()) or not (
        isinstance(flow.get("health_response_body_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", flow["health_response_body_sha256"])
        and isinstance(flow.get("final_resume_body_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", flow["final_resume_body_sha256"])
        and isinstance(flow.get("step_count"), int)
        and flow["step_count"] >= 16
        and isinstance(flow.get("advice_action_count"), int)
        and flow["advice_action_count"] >= 1
        and isinstance(flow.get("repair_count"), int)
        and flow["repair_count"] >= 1
    ):
        raise P6ContractError("P6_G5_PUBLIC_FLOW_INVALID")
    readback: dict[str, Any] = {
        "schema_version": "trip-check-p6-public-full-chain-readback-v1",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "base_url": spec["public_candidate"]["base_url"],
        "flow": flow,
        "human_evidence": False,
    }
    readback["receipt_hash"] = digest(readback)
    output_resolved.mkdir(parents=True, exist_ok=True)
    _write_json_new(output_resolved / "public_full_chain_readback.json", readback)
    if _secret_leaks(output_resolved, current_credentials):
        raise P6ContractError("P6_G5_PUBLIC_SECRET_LEAK")
    observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    health_receipt: dict[str, Any] = {
        "schema_version": "trip-check-p6-public-receipt-v1",
        "kind": "health",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "base_url": spec["public_candidate"]["base_url"],
        "target": "/health",
        "http_status": 200,
        "response_body_sha256": flow["health_response_body_sha256"],
        "status": "PASS",
        "controlled_snapshot": True,
        "observed_at": observed_at,
    }
    health_receipt["receipt_hash"] = digest(health_receipt)
    health_receipt = validate_public_receipt(health_receipt, "health", spec)
    e2e_receipt: dict[str, Any] = {
        "schema_version": "trip-check-p6-public-receipt-v1",
        "kind": "e2e",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "base_url": spec["public_candidate"]["base_url"],
        "target": "trip_check_full_chain",
        "http_status": 200,
        "response_body_sha256": digest(readback),
        "status": "PASS",
        "controlled_snapshot": True,
        "observed_at": observed_at,
    }
    e2e_receipt["receipt_hash"] = digest(e2e_receipt)
    e2e_receipt = validate_public_receipt(e2e_receipt, "e2e", spec)
    _write_json_new(output_resolved / "public_health_receipt.json", health_receipt)
    _write_json_new(output_resolved / "public_e2e_receipt.json", e2e_receipt)
    return health_receipt, e2e_receipt
