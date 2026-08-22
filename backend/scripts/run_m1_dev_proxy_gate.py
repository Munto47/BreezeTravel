"""Build and evaluate the fail-closed M1-dev synthetic proxy panel.

The deterministic system report and frozen simulated labels are not evaluator
runs.  Until three separately generated role artifacts are supplied, the gate
stays non-passing in ``PROXY_CONTRACT_READY``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from scripts.auditor_proxy_contract import (  # noqa: E402
    CALIBRATION_LANE,
    CONTRACT_VERSION,
    EVIDENCE_TYPE,
    EXPECTED_MODEL,
    ROLE_IDS,
    build_role_contracts,
    canonical_sha256,
)


DEFAULT_DATASET = BACKEND / "eval_data" / "auditor_simulated"
DEFAULT_SOURCE_REPORT = BACKEND / "results" / "auditor_simulated" / "latest.json"
DEFAULT_BUNDLE = BACKEND / "results" / "auditor_simulated" / "proxy_blind_bundle.json"
DEFAULT_REPORT = BACKEND / "results" / "auditor_simulated" / "m1_dev_proxy_gate.json"
ARTIFACT_SCHEMA_VERSION = "auditor-proxy-evaluation-v1"
REPORT_SCHEMA_VERSION = "m1-dev-proxy-gate-v1"
CRITICAL_SEVERITIES = {"BLOCKER", "HIGH"}
ACTIVE_STATUSES = {"VIOLATED", "UNKNOWN"}


def _file_sha256(path: Path) -> str:
    raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(raw).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _contains_forbidden_human_field(value: Any) -> str | None:
    forbidden = {
        "human_label",
        "human_labels",
        "human_findings",
        "human_validated",
        "real_organizers",
        "consent_recorded",
        "human_reviewer",
        "human_feedback",
    }
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in forbidden:
                return key
            found = _contains_forbidden_human_field(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _contains_forbidden_human_field(nested)
            if found:
                return found
    return None


def _critical_categories(case: dict[str, Any]) -> list[str]:
    findings = (case.get("audit") or {}).get("findings") or []
    return sorted({
        str(item.get("reason_code") or item.get("rule_id") or "")
        for item in findings
        if item.get("severity") in CRITICAL_SEVERITIES
        and item.get("status") in ACTIVE_STATUSES
        and (item.get("reason_code") or item.get("rule_id"))
    })


def _validate_source_scope(
    manifest: dict[str, Any], cases: list[dict[str, Any]], source: dict[str, Any]
) -> None:
    if manifest.get("evidence_type") != EVIDENCE_TYPE:
        raise ValueError("dataset is not explicit synthetic_proxy evidence")
    if manifest.get("human_labels") is not False or manifest.get("human_validated") is not False:
        raise ValueError("dataset is contaminated by a human-validation claim")
    if source.get("evidence_type") != EVIDENCE_TYPE or source.get("human_validated") is not False:
        raise ValueError("source report is not explicit synthetic_proxy-only evidence")
    expected_ids = {str(case["case_id"]) for case in cases}
    source_rows = list(source.get("cases") or [])
    source_ids = {str(case.get("case_id")) for case in source_rows}
    if len(cases) != 150 or len(source_rows) != 150 or source_ids != expected_ids:
        raise ValueError("M1-dev proxy scope must cover the same 150 unique cases")
    city_counts = Counter(str(case.get("city")) for case in cases)
    kind_counts = Counter(str(case.get("source_kind")) for case in cases)
    if city_counts != {"北京": 50, "上海": 50, "杭州": 50}:
        raise ValueError("M1-dev proxy scope must contain 50 cases per city")
    if kind_counts != {
        "SIMULATED_AI_ITINERARY": 60,
        "SIMULATED_CONTROLLED_MUTATION": 60,
        "SIMULATED_BOUNDARY": 30,
    }:
        raise ValueError("M1-dev proxy source-kind split must be 60/60/30")


def export_blind_bundle(source_report_path: Path, dataset_dir: Path) -> dict[str, Any]:
    manifest_path = dataset_dir / "manifest.json"
    cases_path = dataset_dir / "cases.jsonl"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = [
        json.loads(line)
        for line in cases_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    source = json.loads(source_report_path.read_text(encoding="utf-8"))
    _validate_source_scope(manifest, cases, source)
    source_by_id = {str(case["case_id"]): case for case in source["cases"]}
    blind_cases = []
    for case in cases:
        system = source_by_id[str(case["case_id"])]
        blind_cases.append({
            "case_id": case["case_id"],
            "city": case["city"],
            "source_kind": case["source_kind"],
            "trip_days": case["trip_days"],
            "group_size": case["group_size"],
            "raw_itinerary": case["raw_itinerary"],
            "simulated_organizer_profile": case.get("simulated_organizer_profile") or {},
            "system_parser": system.get("parser") or {},
            "system_entity_resolution": system.get("entity_resolution") or {},
            "system_audit": system.get("audit") or {},
            "system_repair": system.get("repair") or {},
            "system_audit_elapsed_seconds": system.get("audit_elapsed_seconds"),
            "system_critical_categories": _critical_categories(system),
        })
    common_bindings = {
        "source_report_sha256": _file_sha256(source_report_path),
        "dataset_manifest_sha256": _file_sha256(manifest_path),
        "cases_sha256": _file_sha256(cases_path),
        "pipeline_code_sha256": source.get("pipeline_code_sha256"),
        "runner_code_sha256": source.get("runner_code_sha256"),
        "blind_cases_sha256": canonical_sha256(blind_cases),
    }
    roles = []
    for role in build_role_contracts()["roles"]:
        role_input = {
            "role_id": role["role_id"],
            "prompt_sha256": role["prompt_sha256"],
            "bindings": common_bindings,
            "cases": blind_cases,
        }
        roles.append({
            **role,
            "input_sha256": canonical_sha256(role_input),
            "artifact_contract": {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "execution_status": "COMPLETED",
                "evidence_type": EVIDENCE_TYPE,
                "calibration_lane": CALIBRATION_LANE,
                "evaluator": {
                    "role_id": role["role_id"],
                    "model": EXPECTED_MODEL,
                    "model_version": EXPECTED_MODEL,
                    "blind": True,
                    "independent": True,
                },
                "generated_at": "timezone-aware ISO-8601 model generation timestamp",
                "bindings": {
                    **common_bindings,
                    "prompt_sha256": role["prompt_sha256"],
                    "input_sha256": canonical_sha256(role_input),
                },
                "output_sha256": "canonical SHA-256 of cases",
                "cases": [{
                    "case_id": "one supplied case ID",
                    "expected_critical_categories": ["BLOCKER/HIGH reason_code values"],
                    "evidence_readback": {"system critical reason_code": True},
                    "repair_decision": "ACCEPT, REJECT, or SKIP",
                    "repair_rejection_reason": "required when REJECT; otherwise null",
                }],
            },
        })
    return {
        "schema_version": CONTRACT_VERSION,
        "evidence_type": EVIDENCE_TYPE,
        "calibration_lane": CALIBRATION_LANE,
        "generated_at": source.get("deterministic_reference_time"),
        "execution_status": "NOT_RUN",
        "human_validated": False,
        "public_claim_eligible": False,
        "bindings": common_bindings,
        "roles": roles,
        "cases": blind_cases,
    }


def _parse_generation_timestamp(value: Any, role_id: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{role_id} lacks generated_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{role_id} generated_at is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{role_id} generated_at must be timezone-aware")
    return value


def validate_role_artifact(payload: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise ValueError("proxy evaluator artifact schema is unsupported")
    if payload.get("execution_status") != "COMPLETED":
        raise ValueError("proxy evaluator artifact was not executed")
    if payload.get("evidence_type") != EVIDENCE_TYPE or payload.get("calibration_lane") != CALIBRATION_LANE:
        raise ValueError("proxy evaluator artifact has the wrong evidence boundary")
    forbidden = _contains_forbidden_human_field(payload)
    if forbidden:
        raise ValueError(f"proxy evaluator artifact contains forbidden human field: {forbidden}")
    evaluator = payload.get("evaluator") or {}
    role_id = str(evaluator.get("role_id") or "")
    role = next((item for item in bundle["roles"] if item["role_id"] == role_id), None)
    if role is None:
        raise ValueError(f"unknown proxy evaluator role: {role_id}")
    if (
        evaluator.get("model") != EXPECTED_MODEL
        or evaluator.get("model_version") != EXPECTED_MODEL
        or evaluator.get("blind") is not True
        or evaluator.get("independent") is not True
    ):
        raise ValueError(f"{role_id} model/blind/independence contract mismatch")
    _parse_generation_timestamp(payload.get("generated_at"), role_id)
    expected_bindings = {
        **bundle["bindings"],
        "prompt_sha256": role["prompt_sha256"],
        "input_sha256": role["input_sha256"],
    }
    if payload.get("bindings") != expected_bindings:
        raise ValueError(f"{role_id} prompt/model input bindings do not match the blind bundle")
    rows = list(payload.get("cases") or [])
    expected_ids = [str(case["case_id"]) for case in bundle["cases"]]
    row_ids = [str(row.get("case_id") or "") for row in rows]
    if len(rows) != len(expected_ids) or len(set(row_ids)) != len(row_ids) or set(row_ids) != set(expected_ids):
        raise ValueError(f"{role_id} must cover every blind case exactly once")
    system_by_id = {str(case["case_id"]): case for case in bundle["cases"]}
    for row in rows:
        case_id = str(row["case_id"])
        expected = row.get("expected_critical_categories")
        if not isinstance(expected, list) or any(not isinstance(item, str) or not item for item in expected):
            raise ValueError(f"{role_id}/{case_id} expected critical categories are invalid")
        if len(expected) != len(set(expected)):
            raise ValueError(f"{role_id}/{case_id} expected critical categories are duplicated")
        readback = row.get("evidence_readback")
        system_categories = set(system_by_id[case_id]["system_critical_categories"])
        if not isinstance(readback, dict) or set(readback) != system_categories:
            raise ValueError(f"{role_id}/{case_id} evidence readback does not cover system critical findings")
        if any(type(value) is not bool for value in readback.values()):
            raise ValueError(f"{role_id}/{case_id} evidence readback values must be boolean")
        decision = row.get("repair_decision")
        if decision not in {"ACCEPT", "REJECT", "SKIP"}:
            raise ValueError(f"{role_id}/{case_id} repair decision is invalid")
        rejection_reason = row.get("repair_rejection_reason")
        if decision == "REJECT" and (not isinstance(rejection_reason, str) or not rejection_reason):
            raise ValueError(f"{role_id}/{case_id} rejected repair lacks a reason")
        if decision != "REJECT" and rejection_reason not in {None, ""}:
            raise ValueError(f"{role_id}/{case_id} non-rejected repair has a rejection reason")
    if payload.get("output_sha256") != canonical_sha256(rows):
        raise ValueError(f"{role_id} output_sha256 does not match cases")
    return payload


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def evaluate_panel(bundle: dict[str, Any], artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    completed_roles: list[dict[str, Any]] = []
    validation_errors: list[str] = []
    for artifact in artifacts:
        try:
            completed_roles.append(validate_role_artifact(artifact, bundle))
        except ValueError as exc:
            validation_errors.append(str(exc))
    role_ids = [str(item["evaluator"]["role_id"]) for item in completed_roles]
    if len(role_ids) != len(set(role_ids)):
        validation_errors.append("proxy evaluator role IDs must be unique")
    missing_roles = sorted(set(ROLE_IDS) - set(role_ids))
    unexpected_roles = sorted(set(role_ids) - set(ROLE_IDS))

    role_rows = {
        str(item["evaluator"]["role_id"]): {
            str(row["case_id"]): row for row in item["cases"]
        }
        for item in completed_roles
    }
    system_by_id = {str(case["case_id"]): case for case in bundle["cases"]}
    unanimous = true_positive = predicted = expected_total = 0
    readback_true = readback_total = 0
    decisions: Counter[str] = Counter()
    rejection_reasons: Counter[str] = Counter()
    per_case = []
    if not missing_roles and not unexpected_roles and not validation_errors:
        for case_id, system in system_by_id.items():
            role_sets = [
                set(role_rows[role_id][case_id]["expected_critical_categories"])
                for role_id in ROLE_IDS
            ]
            unanimous += int(role_sets[0] == role_sets[1] == role_sets[2])
            votes: Counter[str] = Counter(category for values in role_sets for category in values)
            majority = {category for category, count in votes.items() if count >= 2}
            system_categories = set(system["system_critical_categories"])
            true_positive += len(majority & system_categories)
            predicted += len(system_categories)
            expected_total += len(majority)
            for role_id in ROLE_IDS:
                row = role_rows[role_id][case_id]
                readback_true += sum(bool(value) for value in row["evidence_readback"].values())
                readback_total += len(row["evidence_readback"])
                decision = row["repair_decision"]
                if decision != "SKIP":
                    decisions[decision] += 1
                if decision == "REJECT":
                    rejection_reasons[str(row["repair_rejection_reason"])] += 1
            per_case.append({
                "case_id": case_id,
                "majority_expected_critical_categories": sorted(majority),
                "system_critical_categories": sorted(system_categories),
                "unanimous": role_sets[0] == role_sets[1] == role_sets[2],
            })

    source_elapsed = [case.get("system_audit_elapsed_seconds") for case in bundle["cases"]]
    elapsed_valid = all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
        for value in source_elapsed
    )
    within_three_minutes = (
        _ratio(sum(float(value) <= 180 for value in source_elapsed), len(source_elapsed))
        if elapsed_valid else None
    )
    precision = _ratio(true_positive, predicted)
    recall = _ratio(true_positive, expected_total)
    agreement = _ratio(unanimous, len(bundle["cases"])) if not missing_roles and not validation_errors else None
    evidence_rate = _ratio(readback_true, readback_total)
    decision_total = decisions["ACCEPT"] + decisions["REJECT"]
    repair_adoption = _ratio(decisions["ACCEPT"], decision_total)
    gates = {
        "three_independent_proxy_roles_completed": (
            not missing_roles and not unexpected_roles and not validation_errors and len(role_ids) == 3
        ),
        "critical_precision_at_least_0_90": precision is not None and precision >= 0.90,
        "critical_recall_at_least_0_85": recall is not None and recall >= 0.85,
        "critical_unanimous_agreement_at_least_0_85": agreement is not None and agreement >= 0.85,
        "critical_evidence_readback_100_percent": evidence_rate == 1.0,
        "at_least_80_percent_audits_within_180_seconds": (
            within_three_minutes is not None and within_three_minutes >= 0.80
        ),
        "synthetic_repair_adoption_at_least_0_40": (
            repair_adoption is not None and repair_adoption >= 0.40
        ),
    }
    passed = all(gates.values())
    if passed:
        status = "M1_DEV_PROXY_PASSED"
    elif missing_roles and not artifacts:
        status = "PROXY_CONTRACT_READY"
    elif validation_errors:
        status = "BLOCKED_PROXY_EVIDENCE_INVALID"
    elif missing_roles:
        status = "BLOCKED_PROXY_EVALUATORS_INCOMPLETE"
    else:
        status = "M1_DEV_PROXY_FAILED"
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "evidence_type": EVIDENCE_TYPE,
        "calibration_lane": CALIBRATION_LANE,
        "status": status,
        "passed": passed,
        "allows_p5_p8_development": passed,
        "human_validated": False,
        "human_calibration_performed": False,
        "public_claim_eligible": False,
        "release_eligible": False,
        "claim_boundary": (
            "synthetic_proxy proves only the local M1-dev development gate; "
            "it is not human validation, market validation, production proof, or release approval"
        ),
        "bindings": bundle["bindings"],
        "roles": [{
            "role_id": role_id,
            "execution_status": "COMPLETED" if role_id in role_ids else "NOT_RUN",
            "model": EXPECTED_MODEL,
            "prompt_sha256": next(
                role["prompt_sha256"] for role in bundle["roles"] if role["role_id"] == role_id
            ),
            "input_sha256": next(
                role["input_sha256"] for role in bundle["roles"] if role["role_id"] == role_id
            ),
            "output_sha256": next((
                artifact["output_sha256"]
                for artifact in completed_roles
                if artifact["evaluator"]["role_id"] == role_id
            ), None),
            "generated_at": next((
                artifact["generated_at"]
                for artifact in completed_roles
                if artifact["evaluator"]["role_id"] == role_id
            ), None),
        } for role_id in ROLE_IDS],
        "validation_errors": validation_errors,
        "missing_roles": missing_roles,
        "metrics": {
            "critical_precision": precision,
            "critical_recall": recall,
            "critical_unanimous_agreement": agreement,
            "critical_evidence_readback_rate": evidence_rate,
            "audits_within_180_seconds_rate": within_three_minutes,
            "synthetic_repair_adoption_rate": repair_adoption,
            "synthetic_repair_decisions": dict(sorted(decisions.items())),
            "synthetic_repair_rejection_reasons": dict(sorted(rejection_reasons.items())),
            "original_and_controlled_errors_reported_separately": True,
        },
        "gates": gates,
        "cases": per_case,
    }


def run(
    source_report_path: Path,
    dataset_dir: Path,
    bundle_path: Path,
    report_path: Path,
    artifact_paths: list[Path],
) -> dict[str, Any]:
    bundle = export_blind_bundle(source_report_path, dataset_dir)
    _write_json(bundle_path, bundle)
    artifacts = [json.loads(path.read_text(encoding="utf-8")) for path in artifact_paths]
    report = evaluate_panel(bundle, artifacts)
    report["bundle_sha256"] = _file_sha256(bundle_path)
    _write_json(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", type=Path, default=DEFAULT_SOURCE_REPORT)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    args = parser.parse_args()
    report = run(
        args.source_report,
        args.dataset_dir,
        args.bundle,
        args.report,
        args.artifact,
    )
    print(json.dumps({
        "status": report["status"],
        "passed": report["passed"],
        "missing_roles": report["missing_roles"],
        "validation_errors": report["validation_errors"],
    }, ensure_ascii=False))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
