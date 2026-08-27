"""Local three-city Trip Intake E2E with real DeepSeek and frozen Provider facts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

from app.itineraries.hash_service import sha256_canonical
from app.trip_check.provider_integrity import provider_snapshot_sha256


SCENARIOS = {
    "北京": ("故宫博物院", "景山公园", "颐和园"),
    "上海": ("外滩", "豫园", "东方明珠广播电视塔"),
    "杭州": ("西湖风景名胜区", "灵隐寺", "雷峰塔"),
}
TERMINAL = {"WAITING", "SUCCEEDED", "PARTIAL", "FAILED", "PRIVACY_BLOCKED", "CANCELLED"}


class LocalE2EError(RuntimeError):
    pass


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        raise urllib.error.HTTPError(req.full_url, code, "redirect rejected", headers, fp)


@dataclass(frozen=True)
class HttpResult:
    body: Any
    status: int
    body_sha256: str
    headers: dict[str, str]


class HttpClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
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
    ) -> HttpResult:
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers = {"Accept": "application/json", "User-Agent": "BreezeTravel-Intake-E2E/1"}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        if self.token:
            request_headers["Authorization"] = f"Bearer {self.token}"
        request_headers.update(headers or {})
        request = urllib.request.Request(
            f"{self.base_url}{route}", data=payload, headers=request_headers, method=method
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                status = response.status
                raw = response.read()
                response_headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
            response_headers = {key.lower(): value for key, value in exc.headers.items()}
        except (OSError, urllib.error.URLError) as exc:
            raise LocalE2EError(f"HTTP transport failed for {method} {route}") from exc
        body_sha = hashlib.sha256(raw).hexdigest()
        if record:
            self.steps.append(
                {"method": method, "route": route, "status": status, "body_sha256": body_sha}
            )
        if status not in expected:
            raise LocalE2EError(f"unexpected HTTP {status} for {method} {route}")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise LocalE2EError(f"invalid JSON for {method} {route}") from exc
        return HttpResult(parsed, status, body_sha, response_headers)

    def sse_event_ids(self, route: str, *, last_event_id: int | None = None) -> list[int]:
        headers = {"Accept": "text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        request = urllib.request.Request(f"{self.base_url}{route}", headers=headers, method="GET")
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read()
                status = response.status
        except (OSError, urllib.error.URLError) as exc:
            raise LocalE2EError(f"SSE transport failed for {route}") from exc
        if status != 200:
            raise LocalE2EError(f"unexpected SSE HTTP {status} for {route}")
        event_ids = [int(value) for value in re.findall(rb"(?m)^id:\s*(\d+)\s*$", raw)]
        self.steps.append(
            {
                "method": "GET",
                "route": route,
                "status": status,
                "body_sha256": hashlib.sha256(raw).hexdigest(),
                "last_event_id": last_event_id,
            }
        )
        return event_ids


def _git_commit(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _read_runtime_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise LocalE2EError("Trip Intake runtime ledger is missing")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise LocalE2EError("Trip Intake runtime ledger row is invalid")
            rows.append(value)
    return rows


def _run_spec(commit_sha: str, *, fault_profile: str = "none") -> dict[str, Any]:
    return {
        "schema_version": "trip-check-run-spec-v1",
        "commit_sha": commit_sha,
        "prompt_version": "none-intake-e2e",
        "model_version": "none-intake-e2e",
        "provider_version": "p3-provider-integrity-v1",
        "rule_set_version": "audit-v1",
        "execution_mode": "snapshot",
        "dataset_hash": sha256_canonical(SCENARIOS),
        "snapshot_hash": provider_snapshot_sha256(),
        "fault_profile": fault_profile,
        "random_seed": 7,
        "budget": {
            "max_tokens": 0,
            "max_provider_queries": 12,
            "max_retries": 0,
            "timeout_seconds": 45,
            "max_cost_usd": 0,
        },
    }


def _poll_run(client: HttpClient, run_id: str) -> dict[str, Any]:
    for _ in range(240):
        current = client.request("GET", f"/api/trip-check-runs/{run_id}", record=False).body
        if isinstance(current, dict) and current.get("status") in TERMINAL:
            return current
        time.sleep(0.25)
    raise LocalE2EError(f"Trip Check run {run_id} did not reach a terminal/adoption state")


def _resolve_import(client: HttpClient, workspace_id: str, itinerary_import: dict[str, Any]) -> dict[str, Any]:
    current = itinerary_import
    for raw_stop in current.get("raw_stops", []):
        raw_stop_id = raw_stop["raw_stop_id"]
        current = client.request(
            "POST",
            f"/api/trip-workspaces/{workspace_id}/imports/{current['import_id']}"
            f"/raw-stops/{raw_stop_id}/candidates:search",
            body={"query": raw_stop["raw_name"]},
            headers={"If-Match": f'"{current["state_version"]}"'},
        ).body
    confirmations = []
    for resolution in current.get("resolutions", []):
        if resolution.get("resolution_status") in {"AUTO_MATCHED", "USER_CONFIRMED"}:
            continue
        candidates = resolution.get("candidates") or []
        if not candidates:
            raise LocalE2EError(f"no fixture candidate for raw stop {resolution.get('raw_stop_id')}")
        confirmations.append(
            {"raw_stop_id": resolution["raw_stop_id"], "place_id": candidates[0]["place_id"]}
        )
    if confirmations:
        current = client.request(
            "PATCH",
            f"/api/trip-workspaces/{workspace_id}/imports/{current['import_id']}/resolutions",
            body={"confirmations": confirmations},
            headers={"If-Match": f'"{current["state_version"]}"'},
        ).body
    if current.get("status") != "READY":
        raise LocalE2EError(f"import resolution did not become READY: {current.get('status')}")
    return current


def _normal_city_flow(
    client: HttpClient,
    *,
    city: str,
    places: tuple[str, str, str],
    commit_sha: str,
    ledger_path: Path,
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:12]
    start = date.today() + timedelta(days=30)
    end = start + timedelta(days=1)
    room_id = f"intake-e2e-{suffix}"
    client.request(
        "POST",
        "/api/room",
        body={
            "room_id": room_id,
            "thread_id": f"intake-e2e-thread-{suffix}",
            "trip_city": city,
            "trip_days": 2,
            "nickname": "Trip Intake E2E",
        },
    )
    raw_text = (
        f"{start.year}年{start.month}月{start.day}日到{end.month}月{end.day}日去{city}，2人。"
        f"第1天 09:00-12:00 {places[0]}，11:00-13:00 {places[1]}；"
        f"第2天 09:00-11:00 {places[2]}。"
    )
    source_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    ledger_before = len(_read_runtime_ledger(ledger_path)) if ledger_path.exists() else 0
    created = client.request(
        "POST",
        f"/api/rooms/{room_id}/trip-intakes",
        body={"source_type": "MANUAL_TEXT", "raw_text": raw_text},
        expected=(201,),
    ).body
    intake_id = created.get("intake_id")
    intake_revision = created.get("revision")
    if not isinstance(intake_id, str) or intake_revision != 1:
        raise LocalE2EError("Trip Intake creation receipt is invalid")
    ledger_rows = _read_runtime_ledger(ledger_path)
    new_rows = ledger_rows[ledger_before:]
    matching = [row for row in new_rows if row.get("source_sha256") == [source_sha]]
    if len(matching) != 1:
        raise LocalE2EError("real model call receipt cannot be uniquely matched to the Intake")
    model_receipt = matching[0]
    if not (
        model_receipt.get("requested_model") == "deepseek-v4-flash"
        and model_receipt.get("actual_model") == "deepseek-v4-flash"
        and model_receipt.get("fallback_used") is False
        and model_receipt.get("error_category") is None
    ):
        raise LocalE2EError("normal Intake did not use the real configured DeepSeek model")
    extraction = created.get("extraction") or {}
    primary_id = (extraction.get("locations") or {}).get("primary_mention_id")
    primary = next(
        (
            mention
            for mention in (extraction.get("locations") or {}).get("mentions", [])
            if mention.get("mention_id") == primary_id
        ),
        None,
    )
    extracted_dates = (extraction.get("temporal") or {}).get("date_range") or {}
    extracted_party = (extraction.get("party_size") or {}).get("total") or {}
    if not (
        primary is not None
        and primary.get("normalized_name") == f"{city}市"
        and extracted_party.get("min") == 2
        and extracted_party.get("max") == 2
        and extracted_dates.get("start")
        == {"year": start.year, "month": start.month, "day": start.day}
        and extracted_dates.get("end")
        == {"year": end.year, "month": end.month, "day": end.day}
    ):
        raise LocalE2EError("real DeepSeek hybrid extraction did not match the confirmed core fields")
    latest = client.request("GET", f"/api/rooms/{room_id}/trip-intakes/latest").body
    if latest.get("content_hash") != created.get("content_hash"):
        raise LocalE2EError("refresh recovery changed the Intake revision")
    confirmation_body = {
        "confirmed_values": {
            "city": city,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "party_size": 2,
        }
    }
    patch_key = f"intake-user-confirm-values-{suffix}"
    patched = client.request(
        "PATCH",
        f"/api/trip-intakes/{intake_id}/revisions/1",
        body=confirmation_body,
        headers={"If-Match": '"1"', "Idempotency-Key": patch_key},
    ).body
    patched_replay = client.request(
        "PATCH",
        f"/api/trip-intakes/{intake_id}/revisions/1",
        body=confirmation_body,
        headers={"If-Match": '"1"', "Idempotency-Key": patch_key},
    ).body
    if patched.get("revision") != 2 or patched_replay.get("content_hash") != patched.get("content_hash"):
        raise LocalE2EError("confirmed-value patch replay created a duplicate revision")
    confirm_key = f"intake-confirm-{suffix}"
    confirmed = client.request(
        "POST",
        f"/api/trip-intakes/{intake_id}/revisions/2/confirm",
        body={},
        headers={"If-Match": '"2"', "Idempotency-Key": confirm_key},
    ).body
    if confirmed.get("status") != "READY" or confirmed.get("revision") != 3:
        raise LocalE2EError("Trip Intake confirmation failed")
    materialize_key = f"intake-materialize-{suffix}"
    materialized_result = client.request(
        "POST",
        f"/api/trip-intakes/{intake_id}/revisions/3/materialize",
        body={},
        headers={"If-Match": '"3"', "Idempotency-Key": materialize_key},
    )
    replayed_materialization = client.request(
        "POST",
        f"/api/trip-intakes/{intake_id}/revisions/3/materialize",
        body={},
        headers={"If-Match": '"3"', "Idempotency-Key": materialize_key},
    )
    materialized = materialized_result.body["materialization"]
    if not (
        replayed_materialization.body.get("idempotent_replay") is True
        and replayed_materialization.body["materialization"]["workspace"]["workspace_id"]
        == materialized["workspace"]["workspace_id"]
    ):
        raise LocalE2EError("materialization idempotency replay failed")
    workspace_id = materialized["workspace"]["workspace_id"]
    itinerary_import = _resolve_import(client, workspace_id, materialized["itinerary_import"])
    apply_key = f"intake-apply-import-{suffix}"
    apply_headers = {
        "If-Match": f'"{itinerary_import["state_version"]}"',
        "Idempotency-Key": apply_key,
    }
    applied = client.request(
        "POST",
        f"/api/trip-workspaces/{workspace_id}/imports/{itinerary_import['import_id']}/apply",
        body={},
        headers=apply_headers,
    )
    applied_replay = client.request(
        "POST",
        f"/api/trip-workspaces/{workspace_id}/imports/{itinerary_import['import_id']}/apply",
        body={},
        headers=apply_headers,
    )
    initial_revision = applied.body.get("revision", {}).get("revision")
    if initial_revision != 1 or applied_replay.body.get("revision", {}).get("revision") != 1:
        raise LocalE2EError("import apply idempotency replay created a duplicate revision")
    brief_revision = materialized["brief"]["revision"]
    run_body = {
        "itinerary_revision": 1,
        "brief_revision": brief_revision,
        "run_spec": _run_spec(commit_sha),
    }
    run_key = f"intake-create-run-{suffix}"
    run = client.request(
        "POST",
        f"/api/trip-workspaces/{workspace_id}/trip-check-runs",
        body=run_body,
        headers={"Idempotency-Key": run_key},
        expected=(201,),
    ).body
    run_replay = client.request(
        "POST",
        f"/api/trip-workspaces/{workspace_id}/trip-check-runs",
        body=run_body,
        headers={"Idempotency-Key": run_key},
        expected=(200, 201),
    ).body
    if run_replay.get("run_id") != run.get("run_id"):
        raise LocalE2EError("Trip Check create replay produced a duplicate run")
    terminal = _poll_run(client, run["run_id"])
    if not (
        terminal.get("status") == "WAITING"
        and terminal.get("stage") == "WAIT_ADOPTION"
        and not terminal.get("partial_failures")
    ):
        raise LocalE2EError("normal Trip Check did not reach clean adoption state")
    events = client.sse_event_ids(f"/api/trip-check-runs/{run['run_id']}/events")
    if not events or events != sorted(set(events)):
        raise LocalE2EError("SSE event stream is missing or duplicated")
    reconnected = client.sse_event_ids(
        f"/api/trip-check-runs/{run['run_id']}/events", last_event_id=max(0, events[-1] - 1)
    )
    if events[-1] not in reconnected:
        raise LocalE2EError("SSE reconnect did not resume from Last-Event-ID")
    resume_before = client.request("GET", f"/api/trip-workspaces/{workspace_id}/resume").body
    if resume_before.get("current_trip_check_run", {}).get("run_id") != run["run_id"]:
        raise LocalE2EError("workspace refresh recovery lost the active Trip Check run")
    report_id = terminal.get("report_id")
    report = client.request("GET", f"/api/audits/{report_id}").body
    evidence = client.request("GET", f"/api/audits/{report_id}/evidence").body
    advice = client.request(
        "GET", f"/api/trip-workspaces/{workspace_id}/reports/{report_id}/advice"
    ).body
    repairs = client.request("GET", f"/api/audits/{report_id}/repairs").body
    actions = advice.get("actions") or []
    linked_ids = {action.get("repair_id") for action in actions if action.get("repair_id")}
    repair = next((item for item in repairs if item.get("repair_id") in linked_ids), None)
    if not (
        report.get("overall_status") == "VIOLATED"
        and not evidence.get("provider_failures")
        and repair is not None
    ):
        raise LocalE2EError("Audit/Advice did not produce an adoptable repair")
    repair_key = f"intake-apply-repair-{suffix}"
    repair_headers = {"If-Match": '"1"', "Idempotency-Key": repair_key}
    adopted = client.request(
        "POST",
        f"/api/audits/{report_id}/repairs/{repair['repair_id']}/apply",
        body={"base_revision": 1},
        headers=repair_headers,
    )
    adopted_replay = client.request(
        "POST",
        f"/api/audits/{report_id}/repairs/{repair['repair_id']}/apply",
        body={"base_revision": 1},
        headers=repair_headers,
    )
    if adopted.body.get("new_revision") != 2 or adopted_replay.body.get("new_revision") != 2:
        raise LocalE2EError("repair apply replay created a duplicate revision")
    postcheck_id = adopted.body.get("postcheck_report_id")
    postcheck = client.request("GET", f"/api/audits/{postcheck_id}").body
    final_resume = client.request("GET", f"/api/trip-workspaces/{workspace_id}/resume").body
    final_run = final_resume.get("current_trip_check_run") or {}
    if not (
        postcheck.get("itinerary_revision") == 2
        and final_resume.get("current_revision", {}).get("revision") == 2
        and final_run.get("stage") == "POSTCHECK"
        and final_run.get("status") == "SUCCEEDED"
    ):
        raise LocalE2EError("full postcheck did not become authoritative revision 2")
    provider_failure_readback: dict[str, Any] | None = None
    if city == "北京":
        failure_body = {
            "itinerary_revision": 2,
            "brief_revision": brief_revision,
            "run_spec": _run_spec(commit_sha, fault_profile="weather_unavailable"),
        }
        failure_run = client.request(
            "POST",
            f"/api/trip-workspaces/{workspace_id}/trip-check-runs",
            body=failure_body,
            headers={"Idempotency-Key": f"intake-provider-failure-{suffix}"},
            expected=(201,),
        ).body
        failure_terminal = _poll_run(client, failure_run["run_id"])
        failure_report_id = failure_terminal.get("report_id")
        if not isinstance(failure_report_id, str):
            raise LocalE2EError("Provider partial-failure run did not produce a report")
        failure_evidence = client.request(
            "GET", f"/api/audits/{failure_report_id}/evidence"
        ).body
        failure_report = client.request("GET", f"/api/audits/{failure_report_id}").body
        failures = failure_evidence.get("provider_failures") or []
        weather_unknown = [
            finding
            for finding in failure_report.get("findings", [])
            if finding.get("reason_code") == "WEATHER_DATA_MISSING"
            and finding.get("status") == "UNKNOWN"
        ]
        if not (
            any(item.get("error_category") == "PROVIDER_WEATHER_UNAVAILABLE" for item in failures)
            and weather_unknown
        ):
            raise LocalE2EError("Provider failure did not preserve WEATHER UNKNOWN")
        provider_failure_readback = {
            "fault_profile": "weather_unavailable",
            "provider_failure_category": "PROVIDER_WEATHER_UNAVAILABLE",
            "weather_unknown_count": len(weather_unknown),
            "successful_fact_count": len(failure_evidence.get("facts") or []),
            "status": "PASS",
        }
    return {
        "city": city,
        "status": "PASS",
        "workspace_id_sha256": hashlib.sha256(workspace_id.encode()).hexdigest(),
        "intake_content_hash": created["content_hash"],
        "requested_model": model_receipt["requested_model"],
        "actual_model": model_receipt["actual_model"],
        "input_tokens": model_receipt["input_tokens"],
        "output_tokens": model_receipt["output_tokens"],
        "latency_ms": model_receipt["latency_ms"],
        "estimated_cost_cny": model_receipt["estimated_cost_cny"],
        "fallback_count": 0,
        "provider_execution_mode": "snapshot",
        "provider_snapshot_sha256": provider_snapshot_sha256(),
        "initial_revision": 1,
        "final_revision": 2,
        "sse_event_count": len(events),
        "sse_reconnect_count": len(reconnected),
        "idempotency_replays": 5,
        "postcheck_status": "SUCCEEDED",
        "report_id_sha256": hashlib.sha256(str(postcheck_id).encode()).hexdigest(),
        "provider_partial_failure": provider_failure_readback,
    }


def _fault_fallback_flow(
    *,
    base_url: str,
    ledger_path: Path,
    expected_category: str,
) -> dict[str, Any]:
    client = HttpClient(base_url)
    login = client.request("POST", "/api/auth/test-login").body
    client.token = login.get("token")
    if not isinstance(client.token, str):
        raise LocalE2EError(f"fault backend login failed for {expected_category}")
    suffix = uuid.uuid4().hex[:12]
    room_id = f"intake-fault-{suffix}"
    client.request(
        "POST",
        "/api/room",
        body={
            "room_id": room_id,
            "thread_id": f"intake-fault-thread-{suffix}",
            "trip_city": "杭州",
            "trip_days": 2,
            "nickname": "Trip Intake Fault E2E",
        },
    )
    raw_text = "2026年10月1日到10月2日去杭州，2人。第1天 09:00-11:00 灵隐寺。"
    source_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    before = len(_read_runtime_ledger(ledger_path)) if ledger_path.exists() else 0
    intake = client.request(
        "POST",
        f"/api/rooms/{room_id}/trip-intakes",
        body={"source_type": "MANUAL_TEXT", "raw_text": raw_text},
        expected=(201,),
    ).body
    matching = [
        row
        for row in _read_runtime_ledger(ledger_path)[before:]
        if row.get("source_sha256") == [source_sha]
    ]
    if len(matching) != 1:
        raise LocalE2EError(f"fault receipt cannot be matched for {expected_category}")
    row = matching[0]
    primary_id = intake.get("extraction", {}).get("locations", {}).get("primary_mention_id")
    mentions = intake.get("extraction", {}).get("locations", {}).get("mentions") or []
    primary = next((item for item in mentions if item.get("mention_id") == primary_id), None)
    if not (
        row.get("fallback_used") is True
        and row.get("error_category") == expected_category
        and intake.get("status") == "EXTRACTION_FAILED"
        and primary is not None
        and primary.get("normalized_name") == "杭州市"
        and intake.get("extraction", {}).get("party_size", {}).get("total", {}).get("min") == 2
    ):
        raise LocalE2EError(f"safe deterministic fallback failed for {expected_category}")
    return {
        "status": "PASS",
        "error_category": expected_category,
        "requested_model": row.get("requested_model"),
        "actual_model": row.get("actual_model"),
        "fallback_used": True,
        "input_tokens": row.get("input_tokens"),
        "output_tokens": row.get("output_tokens"),
        "latency_ms": row.get("latency_ms"),
        "estimated_cost_cny": row.get("estimated_cost_cny"),
        "deterministic_primary_city": "杭州市",
        "raw_input_retained": False,
        "unexpected_5xx_count": sum(step["status"] >= 500 for step in client.steps),
    }


def run_local_e2e(
    *,
    base_url: str,
    ledger_path: Path,
    schema_fault_url: str,
    schema_fault_ledger_path: Path,
    timeout_fault_url: str,
    timeout_fault_ledger_path: Path,
    output_path: Path,
    repo_root: Path,
) -> dict[str, Any]:
    client = HttpClient(base_url)
    health = client.request("GET", "/health", record=False)
    if health.body.get("status") != "ok":
        raise LocalE2EError("backend health check failed")
    login = client.request("POST", "/api/auth/test-login").body
    if not isinstance(login.get("token"), str):
        raise LocalE2EError("local test login failed")
    client.token = login["token"]
    commit_sha = _git_commit(repo_root)
    cases = [
        _normal_city_flow(
            client,
            city=city,
            places=places,
            commit_sha=commit_sha,
            ledger_path=ledger_path,
        )
        for city, places in SCENARIOS.items()
    ]
    fallback_cases = [
        _fault_fallback_flow(
            base_url=schema_fault_url,
            ledger_path=schema_fault_ledger_path,
            expected_category="schema_invalid",
        ),
        _fault_fallback_flow(
            base_url=timeout_fault_url,
            ledger_path=timeout_fault_ledger_path,
            expected_category="timeout",
        ),
    ]
    statuses = [step["status"] for step in client.steps]
    if any(status >= 500 for status in statuses):
        raise LocalE2EError("unexpected 5xx was observed")
    receipt = {
        "schema_version": "trip-intake-deepseek-local-e2e-v1",
        "status": "PASS" if all(case["status"] == "PASS" for case in cases) else "FAIL",
        "subject_commit": commit_sha,
        "base_url": base_url,
        "runtime_mode": "hybrid",
        "model": "deepseek-v4-flash",
        "provider_mode": "frozen_snapshot",
        "provider_snapshot_sha256": provider_snapshot_sha256(),
        "cases": cases,
        "city_pass_count": sum(case["status"] == "PASS" for case in cases),
        "city_total": len(cases),
        "fallback_count": sum(case["fallback_count"] for case in cases),
        "fallback_cases": fallback_cases,
        "model_calls": len(cases),
        "input_tokens": sum(case["input_tokens"] for case in cases),
        "output_tokens": sum(case["output_tokens"] for case in cases),
        "estimated_cost_cny": round(sum(case["estimated_cost_cny"] for case in cases), 8),
        "http_step_count": len(client.steps),
        "unexpected_5xx_count": sum(status >= 500 for status in statuses),
        "step_receipts_sha256": sha256_canonical(client.steps),
        "raw_input_retained": False,
        "public_deployment": "NOT_RUN",
        "h1": "NOT_RUN",
        "production_release": "NOT_RUN",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--schema-fault-url", default="http://127.0.0.1:8002")
    parser.add_argument("--schema-fault-ledger", type=Path, required=True)
    parser.add_argument("--timeout-fault-url", default="http://127.0.0.1:8003")
    parser.add_argument("--timeout-fault-ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    receipt = run_local_e2e(
        base_url=args.base_url,
        ledger_path=args.ledger.resolve(),
        schema_fault_url=args.schema_fault_url,
        schema_fault_ledger_path=args.schema_fault_ledger.resolve(),
        timeout_fault_url=args.timeout_fault_url,
        timeout_fault_ledger_path=args.timeout_fault_ledger.resolve(),
        output_path=args.output.resolve(),
        repo_root=args.repo_root.resolve(),
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
