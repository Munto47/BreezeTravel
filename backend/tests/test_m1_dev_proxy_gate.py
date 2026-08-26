from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from scripts.auditor_proxy_contract import (
    EVIDENCE_TYPE,
    EXPECTED_MODEL,
    ROLE_IDS,
    build_role_contracts,
    canonical_sha256,
)
from scripts.generate_auditor_simulated import build_cases, write_dataset
from scripts.run_m1_dev_proxy_gate import (
    ARTIFACT_SCHEMA_VERSION,
    evaluate_panel,
    export_blind_bundle,
    validate_role_artifact,
)


def _source_report(path: Path, cases: list[dict]) -> Path:
    rows = []
    for case in cases:
        findings = [
            {
                "finding_id": finding["finding_id"],
                "rule_id": finding["expected_rule_id"],
                "reason_code": finding["reason_code"],
                "status": finding["expected_status"],
                "severity": finding["severity"],
                "message": finding["description"],
                "evidence_fact_ids": [f"fact:{finding['finding_id']}"],
                "confirmation_action": "review supplied synthetic evidence",
            }
            for finding in case["simulated_findings"]
        ]
        rows.append({
            "case_id": case["case_id"],
            "parser": {"stop_count": 1, "errors": []},
            "entity_resolution": {"AUTO_MATCHED": 1},
            "audit": {
                "findings": findings,
                "evidence_facts": [{
                    "fact_id": finding["evidence_fact_ids"][0],
                    "response_hash": "a" * 64,
                } for finding in findings],
            },
            "repair": {"attempted": bool(case["injected_errors"]), "proposed": 1},
            "audit_elapsed_seconds": 0.01,
        })
    payload = {
        "schema_version": "2.0",
        "evidence_boundary": EVIDENCE_TYPE,
        "evidence_type": EVIDENCE_TYPE,
        "calibration_lane": "M1-dev",
        "human_labels": False,
        "human_validated": False,
        "pipeline_code_sha256": "b" * 64,
        "runner_code_sha256": "c" * 64,
        "deterministic_reference_time": "2026-08-20T08:00:00+00:00",
        "cases": rows,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _bundle(tmp_path: Path) -> dict:
    dataset = tmp_path / "dataset"
    write_dataset(dataset)
    cases = build_cases()
    source = _source_report(tmp_path / "source.json", cases)
    return export_blind_bundle(source, dataset)


def _artifact(bundle: dict, role_id: str) -> dict:
    role = next(item for item in bundle["roles"] if item["role_id"] == role_id)
    rows = []
    for index, case in enumerate(bundle["cases"]):
        rows.append({
            "case_id": case["case_id"],
            "expected_critical_categories": case["system_critical_categories"],
            "evidence_readback": {
                category: True for category in case["system_critical_categories"]
            },
            "repair_decision": "ACCEPT" if index % 2 == 0 else "REJECT",
            "repair_rejection_reason": None if index % 2 == 0 else "EDIT_COST_TOO_HIGH",
        })
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "execution_status": "COMPLETED",
        "evidence_type": EVIDENCE_TYPE,
        "calibration_lane": "M1-dev",
        "evaluator": {
            "role_id": role_id,
            "model": EXPECTED_MODEL,
            "model_version": EXPECTED_MODEL,
            "blind": True,
            "independent": True,
        },
        "generated_at": "2026-08-20T09:00:00+00:00",
        "bindings": {
            **bundle["bindings"],
            "prompt_sha256": role["prompt_sha256"],
            "input_sha256": role["input_sha256"],
        },
        "output_sha256": canonical_sha256(rows),
        "cases": rows,
    }


def test_role_contracts_are_deterministic_not_run_and_hash_bound() -> None:
    first = build_role_contracts()
    second = build_role_contracts()

    assert first == second
    assert first["evidence_type"] == EVIDENCE_TYPE
    assert first["execution_status"] == "NOT_RUN"
    assert first["human_validated"] is False
    assert [role["role_id"] for role in first["roles"]] == list(ROLE_IDS)
    for role in first["roles"]:
        assert role["execution_status"] == "NOT_RUN"
        assert role["model"] == EXPECTED_MODEL
        assert role["input_sha256"] is None
        assert role["output_sha256"] is None
        assert re.fullmatch(r"[0-9a-f]{64}", role["prompt_sha256"])
        assert role["prompt_sha256"] == canonical_sha256(role["prompt"])


def test_blind_bundle_excludes_frozen_labels_and_keeps_roles_not_run(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)

    assert bundle["execution_status"] == "NOT_RUN"
    assert bundle["human_validated"] is False
    assert len(bundle["cases"]) == 150
    serialized = json.dumps(bundle["cases"], ensure_ascii=False)
    assert "simulated_findings" not in serialized
    assert "injected_errors" not in serialized
    assert "original_errors" not in serialized
    assert all(role["execution_status"] == "NOT_RUN" for role in bundle["roles"])
    assert all(re.fullmatch(r"[0-9a-f]{64}", role["input_sha256"]) for role in bundle["roles"])


def test_zero_artifacts_is_contract_ready_but_never_a_gate_pass(tmp_path: Path) -> None:
    result = evaluate_panel(_bundle(tmp_path), [])

    assert result["status"] == "PROXY_CONTRACT_READY"
    assert result["passed"] is False
    assert result["allows_p5_p8_development"] is False
    assert result["human_validated"] is False
    assert result["missing_roles"] == list(ROLE_IDS)
    assert result["gates"]["three_independent_proxy_roles_completed"] is False


def test_partial_panel_fails_closed(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    result = evaluate_panel(bundle, [_artifact(bundle, ROLE_IDS[0])])

    assert result["status"] == "BLOCKED_PROXY_EVALUATORS_INCOMPLETE"
    assert result["passed"] is False
    assert result["missing_roles"] == list(ROLE_IDS[1:])


def test_invalid_artifact_is_reported_as_invalid_not_as_completed(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    artifact = _artifact(bundle, ROLE_IDS[0])
    artifact["bindings"]["input_sha256"] = "0" * 64

    result = evaluate_panel(bundle, [artifact])

    assert result["status"] == "BLOCKED_PROXY_EVIDENCE_INVALID"
    assert result["passed"] is False
    assert result["validation_errors"]
    assert result["roles"][0]["execution_status"] == "NOT_RUN"


def test_three_hash_bound_role_artifacts_can_satisfy_only_m1_dev(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    artifacts = [_artifact(bundle, role_id) for role_id in ROLE_IDS]
    result = evaluate_panel(bundle, artifacts)

    assert result["status"] == "M1_DEV_PROXY_PASSED"
    assert result["passed"] is True
    assert result["human_validated"] is False
    assert result["human_calibration_performed"] is False
    assert result["public_claim_eligible"] is False
    assert result["release_eligible"] is False
    assert result["metrics"]["critical_precision"] == 1.0
    assert result["metrics"]["critical_recall"] == 1.0
    assert result["metrics"]["critical_unanimous_agreement"] == 1.0
    assert result["metrics"]["critical_evidence_readback_rate"] == 1.0
    assert result["metrics"]["synthetic_repair_adoption_rate"] == 0.5
    assert all(result["gates"].values())


@pytest.mark.parametrize("mutation", ["human", "model", "input", "output"])
def test_role_artifact_rejects_boundary_or_hash_tampering(tmp_path: Path, mutation: str) -> None:
    bundle = _bundle(tmp_path)
    artifact = _artifact(bundle, ROLE_IDS[0])
    if mutation == "human":
        artifact["cases"][0]["human_label"] = "PASS"
    elif mutation == "model":
        artifact["evaluator"]["model_version"] = "unversioned"
    elif mutation == "input":
        artifact["bindings"]["input_sha256"] = "0" * 64
    else:
        artifact["cases"][0]["repair_decision"] = "SKIP"

    with pytest.raises(ValueError):
        validate_role_artifact(copy.deepcopy(artifact), bundle)
