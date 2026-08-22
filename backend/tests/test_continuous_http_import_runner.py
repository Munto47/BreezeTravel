from __future__ import annotations

import copy
import json
import urllib.parse
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from evals.continuous import HttpResponse, run_import_http
from evals.continuous import http_import as http_import_module
from evals.continuous.core import preflight


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SPEC = BACKEND_ROOT / "evals" / "run_specs" / "dual-entry-pr-offline.json"
CONTROLLED_30_SPEC = BACKEND_ROOT / "evals" / "run_specs" / "dual-entry-import-controlled-30.json"
FROZEN_IMPORT_BLOCKED_SPEC = (
    BACKEND_ROOT / "evals" / "run_specs" / "dual-entry-import-nightly-frozen-blocked.json"
)
DATASET = BACKEND_ROOT / "eval_data" / "dual_entry_v1"


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _selected_fixture_data(spec_path: Path = SPEC) -> tuple[dict[str, dict], dict[str, dict]]:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    selected = set(spec["dataset"]["case_ids"])
    cases = {
        row["case_id"]: row
        for path in DATASET.glob("*.inputs.jsonl")
        for row in _rows(path)
        if row["case_id"] in selected
    }
    labels = {
        row["case_id"]: row
        for path in DATASET.glob("*.labels.jsonl")
        for row in _rows(path)
        if row["case_id"] in selected
    }
    return cases, labels


def _valid_preflight(*, resolved_spec: dict | None = None, spec_path: Path = SPEC):
    result = preflight(spec_path, environ={})
    checks = tuple(
        {**check, "status": "PASS"} if check["id"] == "SOURCE_BINDING" else check
        for check in result.checks
    )
    return replace(
        result,
        resolved_spec=resolved_spec or result.resolved_spec,
        checks=checks,
        errors=(),
    )


class FixtureHttpTransport:
    def __init__(self, spec_path: Path = SPEC) -> None:
        self.cases, self.labels = _selected_fixture_data(spec_path)
        self.case_by_raw = {case["input"]["raw_itinerary"]: case_id for case_id, case in self.cases.items()}
        self.workspace_case: dict[str, str] = {}
        self.imports: dict[str, dict] = {}
        self.audits: dict[str, dict] = {}
        self.repairs: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _receipt(
        case: dict,
        name: str,
        index: int,
        *,
        city: str | None = None,
        place_id: str | None = None,
    ) -> dict:
        place_id = place_id or f"fixture-{case['city']}-{index}"
        return {
            "canonical_place_id": place_id,
            "provider": "amap_fixture",
            "provider_place_id": place_id,
            "name": name,
            "city": city or case["city"],
            "district": "受控测试区",
            "address": "受控测试地址",
            "category": "attraction",
            "longitude": 120.0 + index / 100,
            "latitude": 30.0 + index / 100,
            "request_hash": "a" * 64,
            "response_hash": "b" * 64,
            "observed_at": "2026-08-21T00:00:00+00:00",
            "execution_mode": "fixture",
            "source_url": None,
        }

    def _build_import(self, case_id: str, import_id: str) -> dict:
        case = self.cases[case_id]
        truth = self.labels[case_id]["deterministic_truth"]
        expected_parse = truth.get("expected_parse", {})
        names = list(expected_parse.get("stop_names", []))
        if not names:
            names = [item["raw_name"] for item in truth.get("expected_resolutions", [])]
        if not names:
            names = ["景山公园"]
        expected_entities = {
            item["raw_name"]: item for item in truth.get("expected_resolutions", [])
        }
        raw_stops = []
        resolutions = []
        for index, name in enumerate(names):
            raw_id = f"raw-{index}"
            raw_stops.append(
                {
                    "raw_stop_id": raw_id,
                    "day_index": min(index, case["trip_days"] - 1),
                    "raw_name": name,
                    "source_span": name,
                    "start_time": None,
                    "end_time": None,
                    "note": None,
                    "fixed_commitment": name in expected_parse.get("fixed_commitment_names", []),
                }
            )
            expected_entity = expected_entities.get(name, {})
            status = expected_entity.get("status", "AUTO_MATCHED")
            rejected_city = (
                case.get("input", {}).get("controlled_facts", {}).get(name, {}).get("top_candidate_city")
                if status == "NOT_FOUND"
                else None
            )
            receipt = self._receipt(
                case,
                name,
                index,
                city=rejected_city,
                place_id=expected_entity.get("canonical_place_id"),
            )
            candidates = [] if status == "NOT_FOUND" else [
                {
                    "place_id": receipt["canonical_place_id"],
                    "name": name,
                    "city": case["city"],
                    "coords": {"lng": receipt["longitude"], "lat": receipt["latitude"]},
                    "retrieval_provider": "amap_fixture",
                    "execution_mode": "fixture",
                    "resolved_place_receipt": receipt,
                    "score": 0.95,
                    "reasons": ["controlled fixture"],
                }
            ]
            rejected_candidates = (
                [
                    {
                        "place_id": receipt["canonical_place_id"],
                        "name": name,
                        "reason": "WRONG_CITY",
                        "target_city": case["city"],
                        "resolved_place_receipt": receipt,
                    }
                ]
                if status == "NOT_FOUND" and rejected_city
                else []
            )
            resolutions.append(
                {
                    "raw_stop_id": raw_id,
                    "canonical_place_id": receipt["canonical_place_id"] if status == "AUTO_MATCHED" else None,
                    "candidates": candidates,
                    "rejected_candidates": rejected_candidates,
                    "confidence": 0.95 if status == "AUTO_MATCHED" else 0.6,
                    "resolution_status": status,
                    "resolution_version": 1,
                    "confirmed_by": None,
                    "confirmed_at": None,
                }
            )
        status = "READY" if all(item["resolution_status"] == "AUTO_MATCHED" for item in resolutions) else "NEEDS_RESOLUTION"
        return {
            "import_id": import_id,
            "workspace_id": "",
            "source_type": "AI_TEXT",
            "raw_text": case["input"]["raw_itinerary"],
            "parse_version": "fixture-v1",
            "status": status,
            "raw_stops": raw_stops,
            "resolutions": resolutions,
            "member_summary": [],
            "parse_errors": [],
            "state_version": 2,
            "applied_revision": None,
            "created_by": "eval-user",
            "created_at": "2026-08-21T00:00:00+00:00",
            "updated_at": "2026-08-21T00:00:00+00:00",
        }

    def _revision(self, item: dict, revision: int = 1) -> dict:
        case = self.cases[self.workspace_case[item["workspace_id"]]]
        days = [{"day_index": index, "date": None, "stops": []} for index in range(case["trip_days"])]
        resolutions = {row["raw_stop_id"]: row for row in item["resolutions"]}
        for raw in item["raw_stops"]:
            resolution = resolutions[raw["raw_stop_id"]]
            days[raw["day_index"]]["stops"].append(
                {
                    "stop_id": f"stop-{raw['raw_stop_id']}",
                    "place_id": resolution["canonical_place_id"],
                    "raw_name": raw["raw_name"],
                    "day_index": raw["day_index"],
                    "start_time": raw["start_time"],
                    "end_time": raw["end_time"],
                    "locked": raw["fixed_commitment"],
                    "fixed_commitment": raw["fixed_commitment"],
                    "commitment_kind": "FIXED_VISIT" if raw["fixed_commitment"] else "FLEXIBLE",
                }
            )
        return {"revision": revision, "days": days}

    def _audit(self, workspace_id: str, report_id: str, *, postcheck: bool = False) -> dict:
        case_id = self.workspace_case[workspace_id]
        oracle = self.labels[case_id]["metric_oracles"]["finding_precision_recall"]
        findings = []
        if oracle["applicability"] == "APPLICABLE":
            for index, expected in enumerate(oracle["ground_truth_items"]):
                findings.append(
                    {
                        "finding_id": f"finding-{report_id}-{index}",
                        "rule_id": f"fixture.rule.{index}",
                        "rule_version": "1.0.0",
                        "status": expected["status"],
                        "severity": "HIGH",
                        "reason_code": expected["reason_code"],
                        "subject": expected["subject"],
                        "affected_member": expected["affected_member"],
                        "message": "controlled fixture finding",
                        "input_values": {"subject": expected["subject"]},
                        "affected_days": [],
                        "affected_stop_ids": [],
                        "affected_member_ids": [],
                        "evidence_fact_ids": [],
                        "repairable": not postcheck,
                        "confirmation_action": None,
                    }
                )
        item = next(value for value in self.imports.values() if value["workspace_id"] == workspace_id)
        report = {
            "report_id": report_id,
            "workspace_id": workspace_id,
            "itinerary_id": f"itinerary-{workspace_id}",
            "itinerary_revision": 1,
            "task_id": f"task-{workspace_id}",
            "task_revision": 1,
            "member_constraint_revision_set": {},
            "evidence_snapshot_id": f"snapshot-{report_id}",
            "audit_rule_set_version": "fixture-v1",
            "report_input_hash": "c" * 64,
            "overall_status": "UNKNOWN" if any(row["status"] == "UNKNOWN" for row in findings) else "VIOLATED",
            "findings": findings,
            "created_at": "2026-08-21T00:00:00+00:00",
            "supersedes_report_id": None,
            "revision": self._revision(item),
        }
        self.audits[report_id] = report
        return report

    def _repair_options(self, audit_id: str) -> list[dict]:
        audit = self.audits[audit_id]
        workspace_id = audit["workspace_id"]
        case_id = self.workspace_case[workspace_id]
        oracle = self.labels[case_id]["metric_oracles"]["repair_postcheck"]
        if oracle["applicability"] != "APPLICABLE":
            return []
        metric_operation = oracle["allowed_operation_types"][0]
        domain_operation = {
            "MOVE": "MOVE_WITHIN_DAY",
            "SHIFT": "ADJUST_TIME",
            "REPLACE": "REPLACE_STOP",
            "REMOVE": "REMOVE_STOP",
        }.get(metric_operation, metric_operation)
        repair_id = f"repair-{case_id}"
        postcheck_id = f"postcheck-{case_id}"
        postcheck = self._audit(workspace_id, postcheck_id, postcheck=True)
        postcheck["findings"] = copy.deepcopy(audit["findings"])
        item = next(value for value in self.imports.values() if value["workspace_id"] == workspace_id)
        option = {
            "repair_id": repair_id,
            "source_report_id": audit_id,
            "base_itinerary_revision": 1,
            "operations": [{"operation": domain_operation, "payload": {}, "rationale": "controlled fixture"}],
            "targeted_finding_ids": [row["finding_id"] for row in audit["findings"]] or ["fixture-target"],
            "edit_cost": 1,
            "risk_cost": 0,
            "route_cost_delta": 0,
            "new_unknown_count": 0,
            "tradeoffs": [],
            "affected_member_ids": [],
            "result_preview": self._revision(item),
            "postcheck_report_id": postcheck_id,
            "status": "PROPOSED",
            "decided_by": None,
            "decision_reason": None,
            "decided_at": None,
            "created_at": "2026-08-21T00:00:00+00:00",
        }
        self.repairs[repair_id] = option
        return [option]

    def request(self, method, url, *, headers, json_body, timeout_seconds):
        path = urllib.parse.urlparse(url).path
        self.calls.append((method, path))
        if path == "/api/auth/test-login":
            return HttpResponse(200, {}, {"token": "secret-eval-token", "user_id": "eval-user"})
        if path == "/api/room":
            return HttpResponse(200, {}, {"status": "ok", "room_id": json_body["room_id"]})
        if path == "/api/trip-workspaces" and method == "POST":
            workspace_id = json_body["workspace_id"]
            return HttpResponse(201, {}, {"workspace_id": workspace_id, **json_body})
        if path.endswith("/imports") and method == "POST":
            workspace_id = path.split("/")[3]
            case_id = self.case_by_raw[json_body["raw_text"]]
            self.workspace_case[workspace_id] = case_id
            item = self._build_import(case_id, f"import-{len(self.imports)}")
            item["workspace_id"] = workspace_id
            self.imports[item["import_id"]] = item
            return HttpResponse(201, {"ETag": '"2"'}, item)
        if path.endswith("/resolutions") and method == "PATCH":
            import_id = path.split("/")[-2]
            item = self.imports[import_id]
            selected = {row["raw_stop_id"]: row["place_id"] for row in json_body["confirmations"]}
            for resolution in item["resolutions"]:
                if resolution["raw_stop_id"] in selected:
                    resolution["canonical_place_id"] = selected[resolution["raw_stop_id"]]
                    resolution["resolution_status"] = "USER_CONFIRMED"
                    resolution["confirmed_by"] = "eval-user"
            item["state_version"] = 3
            item["status"] = "READY" if all(r["canonical_place_id"] for r in item["resolutions"]) else "NEEDS_RESOLUTION"
            return HttpResponse(200, {"ETag": '"3"'}, item)
        if "/imports/" in path and path.endswith("/apply") and method == "POST":
            import_id = path.split("/")[-2]
            item = self.imports[import_id]
            if item["status"] != "READY":
                return HttpResponse(409, {}, {"detail": {"code": "DRAFT_AMBIGUOUS"}})
            item["status"] = "APPLIED"
            item["applied_revision"] = 1
            receipts = [
                candidate["resolved_place_receipt"]
                for resolution in item["resolutions"]
                for candidate in resolution["candidates"]
                if candidate["place_id"] == resolution["canonical_place_id"]
            ]
            return HttpResponse(
                200,
                {},
                {
                    "itinerary_import": item,
                    "revision": {"revision": 1},
                    "resolved_place_receipts": receipts,
                    "idempotent_replay": False,
                },
            )
        if "/imports/" in path and method == "GET":
            return HttpResponse(200, {}, self.imports[path.split("/")[-1]])
        if path.endswith("/snapshot"):
            workspace_id = path.split("/")[-2]
            item = next(value for value in self.imports.values() if value["workspace_id"] == workspace_id)
            revision = self._revision(item) if item["status"] == "APPLIED" else None
            return HttpResponse(200, {}, {"workspace": {"workspace_id": workspace_id}, "current_revision": revision})
        if path.endswith("/audits") and method == "POST":
            workspace_id = path.split("/")[3]
            report_id = f"audit-{self.workspace_case[workspace_id]}"
            return HttpResponse(200, {}, copy.deepcopy(self._audit(workspace_id, report_id)))
        if path.endswith("/repairs") and method == "POST":
            audit_id = path.split("/")[3]
            return HttpResponse(201, {}, copy.deepcopy(self._repair_options(audit_id)))
        if "/repairs/" in path and path.endswith("/apply") and method == "POST":
            repair_id = path.split("/")[5]
            option = self.repairs[repair_id]
            return HttpResponse(
                200,
                {},
                {
                    "repair": copy.deepcopy(option),
                    "new_revision": 2,
                    "postcheck_report_id": option["postcheck_report_id"],
                    "idempotent_replay": False,
                },
            )
        if "/repairs/" in path and method == "GET":
            repair_id = path.split("/")[5]
            return HttpResponse(200, {}, copy.deepcopy(self.repairs[repair_id]))
        if path.startswith("/api/audits/") and method == "GET":
            report_id = path.split("/")[3]
            return HttpResponse(200, {}, copy.deepcopy(self.audits[report_id]))
        raise AssertionError(f"unexpected request: {method} {path}")


class MissingRejectedReceiptTransport(FixtureHttpTransport):
    def _build_import(self, case_id: str, import_id: str) -> dict:
        item = super()._build_import(case_id, import_id)
        if case_id == "dev.sh.import.wrong-city-name":
            resolution = item["resolutions"][0]
            rejected = resolution["rejected_candidates"][0]
            resolution["rejected_candidates"] = [
                {
                    "place_id": rejected["place_id"],
                    "name": rejected["name"],
                    "reason": "WRONG_CITY",
                    "target_city": rejected["target_city"],
                    # Objective-looking fields are deliberately insufficient:
                    # only resolved_place_receipt is admissible evidence.
                    "city": "杭州",
                    "longitude": 120.1,
                    "latitude": 30.2,
                }
            ]
        return item


class NoFeasibleRepairTransport(FixtureHttpTransport):
    def request(self, method, url, *, headers, json_body, timeout_seconds):
        path = urllib.parse.urlparse(url).path
        if path.endswith("/repairs") and method == "POST":
            self.calls.append((method, path))
            return HttpResponse(
                422,
                {},
                {
                    "detail": {
                        "code": "REPAIR_NO_FEASIBLE_OPTION",
                        "message": "no candidate passed postcheck",
                    }
                },
            )
        return super().request(
            method,
            url,
            headers=headers,
            json_body=json_body,
            timeout_seconds=timeout_seconds,
        )


class AmbiguousAppliedRevisionTransport(FixtureHttpTransport):
    def _build_import(self, case_id: str, import_id: str) -> dict:
        item = super()._build_import(case_id, import_id)
        if case_id == "pilot.bj.import.classic-3d":
            resolution = item["resolutions"][0]
            resolution["canonical_place_id"] = None
            resolution["resolution_status"] = "AMBIGUOUS"
            item["status"] = "NEEDS_RESOLUTION"
        return item


def test_pr_offline_http_adapter_executes_all_12_cases_and_redacts_auth(tmp_path, monkeypatch):
    transport = FixtureHttpTransport()
    monkeypatch.setattr(http_import_module, "preflight", lambda *args, **kwargs: _valid_preflight())

    result = run_import_http(SPEC, runs_root=tmp_path / "runs", transport=transport, environ={})

    assert result.gate["status"] == "PASS"
    assert result.gate["decision"] == "PROMOTE"
    assert result.gate["execution"]["selected_case_count"] == 12
    assert result.gate["execution"]["completed_case_count"] == 12
    assert result.gate["failed_cases"] == []
    assert result.gate["execution"]["direct_domain_calls"] == 0
    assert result.gate["execution"]["sql_seed_operations"] == 0
    run_spec = json.loads((result.run_dir / "run_spec.json").read_text(encoding="utf-8"))
    assert run_spec["run_id"] == result.run_id
    assert datetime.fromisoformat(run_spec["started_at"]).tzinfo is not None
    outputs = _rows(result.run_dir / "product_outputs.jsonl")
    receipts = _rows(result.run_dir / "provider_receipts.jsonl")
    transactions = _rows(result.run_dir / "http_transactions.jsonl")
    assert len(outputs) == 12
    assert receipts
    assert all(row["receipt"]["execution_mode"] == "fixture" for row in receipts)
    rejected = [row for row in receipts if row["disposition"] == "REJECTED"]
    assert rejected
    assert all(row["rejection_reason"] == "WRONG_CITY" for row in rejected)
    assert all(row["receipt"]["city"] != row["target_city"] for row in rejected)
    serialized = json.dumps(transactions, ensure_ascii=False)
    assert "secret-eval-token" not in serialized
    assert "<redacted>" in serialized
    assert json.loads((result.run_dir / "cost.json").read_text(encoding="utf-8"))["total_cost"] == 0
    deterministic = json.loads((result.run_dir / "deterministic_scores.json").read_text(encoding="utf-8"))
    assert all(row["metric_score"]["status"] == "SCORED" for row in deterministic["cases"])
    aggregate = deterministic["metric_aggregate"]["metrics"]
    assert aggregate["parse_f1"]["coverage"] == {"numerator": 5, "denominator": 5, "value": 1.0}
    assert aggregate["entity_precision_recall"]["coverage"] == {
        "numerator": 4,
        "denominator": 4,
        "value": 1.0,
    }
    assert aggregate["finding_precision_recall"]["coverage"] == {
        "numerator": 6,
        "denominator": 6,
        "value": 1.0,
    }
    assert aggregate["repair_postcheck"]["coverage"] == {
        "numerator": 3,
        "denominator": 3,
        "value": 1.0,
    }
    assert all(aggregate[name]["value"] == 1 for name in aggregate if aggregate[name]["coverage"]["denominator"])
    assert sum(output["audit_report"] is not None for output in outputs) == 6
    assert sum(bool(output["repair_readbacks"]) for output in outputs) == 3


def test_controlled_fixture_development_30_runs_exact_public_http_matrix(tmp_path, monkeypatch):
    preflight_result = _valid_preflight(spec_path=CONTROLLED_30_SPEC)
    selected = preflight_result.resolved_spec["dataset"]["case_ids"]
    cases, _ = _selected_fixture_data(CONTROLLED_30_SPEC)
    assert preflight_result.valid
    assert len(selected) == len(set(selected)) == 30
    assert {city: sum(cases[case_id]["city"] == city for case_id in selected) for city in ("北京", "上海", "杭州")} == {
        "北京": 11,
        "上海": 9,
        "杭州": 10,
    }
    assert all(cases[case_id]["execution"]["provider_mode"] == "controlled_fixture" for case_id in selected)
    monkeypatch.setattr(http_import_module, "preflight", lambda *args, **kwargs: preflight_result)

    result = run_import_http(
        CONTROLLED_30_SPEC,
        runs_root=tmp_path / "runs",
        transport=FixtureHttpTransport(CONTROLLED_30_SPEC),
        environ={},
    )

    assert result.gate["status"] == "PASS"
    assert result.gate["claim_scope"] == "controlled_fixture_development_30"
    assert result.gate["execution"]["selected_case_count"] == 30
    assert result.gate["execution"]["completed_case_count"] == 30
    assert next(item for item in result.gate["gates"] if item["id"] == "HTTP_IMPORT_CASE_COUNT")["status"] == "PASS"
    receipt_gates = {
        item["id"]: item
        for item in result.gate["gates"]
        if item["id"].startswith("RECEIPT_THRESHOLD:")
    }
    assert set(receipt_gates) == {
        "RECEIPT_THRESHOLD:provider_receipt_contract_rate",
        "RECEIPT_THRESHOLD:offered_receipt_case_rate",
        "RECEIPT_THRESHOLD:materialized_receipt_eligible_case_rate",
        "RECEIPT_THRESHOLD:wrong_city_rejected_receipt_rate",
    }
    assert all(item["status"] == "PASS" and item["actual"] == 1 for item in receipt_gates.values())
    scores = json.loads((result.run_dir / "deterministic_scores.json").read_text(encoding="utf-8"))["cases"]
    placeholder_scores = [row for row in scores if "placeholder-not-found" in row["case_id"]]
    assert len(placeholder_scores) == 3
    assert all(
        not any(check["id"].startswith("REJECTED_WRONG_CITY_RECEIPT:") for check in row["checks"])
        for row in placeholder_scores
    )
    wrong_city = next(row for row in scores if row["case_id"] == "dev.sh.import.wrong-city-name")
    assert any(
        check["id"] == "REJECTED_WRONG_CITY_RECEIPT:西湖" and check["status"] == "PASS"
        for check in wrong_city["checks"]
    )


def test_import_frozen_nightly_seam_fails_closed_without_snapshot_or_adapter():
    result = preflight(FROZEN_IMPORT_BLOCKED_SPEC, environ={})

    assert result.valid is False
    assert result.bindings["selected_case_count"] == 13
    codes = {item["code"] for item in result.errors}
    assert "IMPORT_SNAPSHOT_ARTIFACT_NOT_PROVISIONED" in codes
    assert "IMPORT_SNAPSHOT_ADAPTER_NOT_ACTIVE" in codes
    assert "UNRESOLVED_REQUIRED_PLACEHOLDER" in codes
    assert result.resolved_spec["provider"]["import_snapshot_contract"] == {
        "schema_version": "import-frozen-provider-contract-v1",
        "runtime_adapter_id": "import-frozen-entity-route-weather-v1",
        "required_fact_types": ["ENTITY_RESOLUTION", "ROUTE_TIME", "WEATHER"],
        "status": "BLOCKED_MISSING_ARTIFACT",
    }


def test_unavailable_real_localhost_fails_closed_and_persists_transport_error(tmp_path, monkeypatch):
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    spec["sut"]["base_url"] = "http://127.0.0.1:1"
    spec_path = tmp_path / "unavailable.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        http_import_module,
        "preflight",
        lambda *args, **kwargs: replace(_valid_preflight(resolved_spec=spec), spec_path=spec_path),
    )

    result = run_import_http(spec_path, runs_root=tmp_path / "runs", timeout_seconds=0.2, environ={})

    assert result.gate["status"] == "INVALID"
    assert result.gate["decision"] == "REJECT"
    assert result.gate["execution"]["reason"] == "PRODUCT_HTTP_ADAPTER_UNAVAILABLE"
    transactions = _rows(result.run_dir / "http_transactions.jsonl")
    assert transactions[0]["step"] == "auth_test_login"
    assert transactions[0]["status_code"] is None
    assert "transport_error" in transactions[0]
    assert _rows(result.run_dir / "product_outputs.jsonl") == []


def test_incomplete_rejected_candidate_is_not_promoted_to_provider_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(http_import_module, "preflight", lambda *args, **kwargs: _valid_preflight())

    result = run_import_http(
        SPEC,
        runs_root=tmp_path / "runs",
        transport=MissingRejectedReceiptTransport(),
        environ={},
    )

    assert result.gate["status"] == "INVALID"
    assert result.gate["decision"] == "REJECT"
    assert result.gate["failed_cases"] == ["dev.sh.import.wrong-city-name"]
    receipts = _rows(result.run_dir / "provider_receipts.jsonl")
    wrong_city_receipts = [
        row
        for row in receipts
        if row["case_id"] == "dev.sh.import.wrong-city-name" and row["disposition"] == "REJECTED"
    ]
    assert wrong_city_receipts == []


def test_two_runs_use_disjoint_workspace_namespaces(tmp_path, monkeypatch):
    transport = FixtureHttpTransport()
    monkeypatch.setattr(http_import_module, "preflight", lambda *args, **kwargs: _valid_preflight())

    first = run_import_http(SPEC, runs_root=tmp_path / "runs", transport=transport, environ={})
    second = run_import_http(SPEC, runs_root=tmp_path / "runs", transport=transport, environ={})

    first_ids = {row["workspace_id"] for row in _rows(first.run_dir / "product_outputs.jsonl")}
    second_ids = {row["workspace_id"] for row in _rows(second.run_dir / "product_outputs.jsonl")}
    assert first_ids
    assert second_ids
    assert first_ids.isdisjoint(second_ids)


def test_no_feasible_repair_is_persisted_as_product_result_not_http_failure(tmp_path, monkeypatch):
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    spec["dataset"]["case_ids"] = ["pilot.bj.import.classic-3d"]
    resolved = copy.deepcopy(_valid_preflight().resolved_spec)
    resolved["dataset"]["case_ids"] = spec["dataset"]["case_ids"]
    resolved["thresholds"] = {}
    monkeypatch.setattr(
        http_import_module,
        "preflight",
        lambda *args, **kwargs: replace(
            _valid_preflight(resolved_spec=resolved),
            bindings={**_valid_preflight().bindings, "selected_case_count": 1},
        ),
    )

    result = run_import_http(
        SPEC,
        runs_root=tmp_path / "runs",
        transport=NoFeasibleRepairTransport(),
        environ={},
    )

    outputs = _rows(result.run_dir / "product_outputs.jsonl")
    assert len(outputs) == 1
    assert outputs[0]["repair_generation"]["status"] == "NO_FEASIBLE_OPTION"
    assert outputs[0]["repair_generation"]["apply_status"] == "NOT_APPLIED_NO_FEASIBLE_OPTION"
    bad_cases = _rows(result.run_dir / "bad_cases.jsonl")
    assert bad_cases == [{"case_id": "pilot.bj.import.classic-3d", "reason": "DETERMINISTIC_SCORE_FAILED"}]


def test_runner_never_blindly_confirms_ambiguous_candidate_and_skips_revision_audit(
    tmp_path,
    monkeypatch,
):
    resolved = copy.deepcopy(_valid_preflight().resolved_spec)
    resolved["dataset"]["case_ids"] = ["pilot.bj.import.classic-3d"]
    resolved["thresholds"] = {}
    monkeypatch.setattr(
        http_import_module,
        "preflight",
        lambda *args, **kwargs: replace(
            _valid_preflight(resolved_spec=resolved),
            bindings={**_valid_preflight().bindings, "selected_case_count": 1},
        ),
    )
    transport = AmbiguousAppliedRevisionTransport()

    result = run_import_http(
        SPEC,
        runs_root=tmp_path / "runs",
        transport=transport,
        environ={},
    )

    outputs = _rows(result.run_dir / "product_outputs.jsonl")
    assert len(outputs) == 1
    output = outputs[0]
    assert output["apply_status_code"] == 409
    assert output["import_readback"]["resolutions"][0]["resolution_status"] == "AMBIGUOUS"
    assert output["audit_report"] is None
    assert output["audit_execution"] == {
        "status": "NOT_EXECUTED_NO_APPLIED_REVISION",
        "reason_code": "AUDIT_REQUIRES_APPLIED_REVISION",
    }
    assert output["repair_generation"] == {
        "status": "NOT_EXECUTED_NO_AUDIT",
        "reason_code": "REPAIR_REQUIRES_COMPLETED_AUDIT",
    }
    assert "confirm_resolutions" not in output["executed_steps"]
    assert not any(path.endswith("/resolutions") for _, path in transport.calls)
    assert not any(path.endswith("/audits") for _, path in transport.calls)
