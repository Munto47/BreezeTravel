from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evals.trip_text_cards_v1.contracts import RuntimeGateEvidence
from evals.trip_text_cards_v1.validator import validate_dataset


def _check(name: str, observed: object, requirement: str, passed: bool) -> dict[str, object]:
    return {
        "name": name,
        "observed": observed,
        "requirement": requirement,
        "passed": passed,
    }


def assess_semantic_score(score: dict[str, Any], *, split: str) -> dict[str, Any]:
    auto = score["auto_match"]
    executable = score["executable_mentions"]
    day = score["day_assignment"]
    deep = score["deep_city_auto_match"]
    confirmation = score["human_confirmation_count"]
    other_city_confirmation = score["other_city_confirmation_required_count"]
    public = score["public_projection"]
    checks = [
        _check("scoring_coverage", score["scoring_coverage"], "= 1.0", score["scoring_coverage"] == 1.0),
        _check("evidence_span_validity", score["evidence_span_validity"], "= 1.0", score["evidence_span_validity"] == 1.0),
        _check("eligibility_rule_consistency", score["eligibility_rule_consistency"], "= 1.0", score["eligibility_rule_consistency"] == 1.0),
        _check("forbidden_content_as_place", score["forbidden_content_as_place_count"], "= 0", score["forbidden_content_as_place_count"] == 0),
        _check("severe_wrong_auto_matches", score["severe_wrong_auto_match_count"], "= 0", score["severe_wrong_auto_match_count"] == 0),
        _check("auto_match_precision", auto["precision"], ">= 0.99", auto["precision"] >= 0.99),
        _check(
            "auto_match_denominator",
            auto["denominator"],
            ">= 50 for validation/frozen_blind",
            split == "dev" or auto["denominator"] >= 50,
        ),
        _check("executable_precision", executable["precision"], ">= 0.98", executable["precision"] >= 0.98),
        _check("executable_recall", executable["recall"], ">= 0.95", executable["recall"] >= 0.95),
        _check("day_assignment_f1", day["f1"], ">= 0.97", day["f1"] >= 0.97),
        _check("role_macro_f1", score["role_macro_f1"], ">= 0.94", score["role_macro_f1"] >= 0.94),
        _check("deep_city_auto_match_coverage", deep["coverage"], ">= 0.80", deep["coverage"] >= 0.80),
        _check(
            "confirmation_population",
            confirmation.get("population"),
            "= DEEP_CITY",
            confirmation.get("population") == "DEEP_CITY",
        ),
        _check("confirmation_median", confirmation["median"], "<= 1", confirmation["median"] is not None and confirmation["median"] <= 1),
        _check("confirmation_p90", confirmation["p90"], "<= 3", confirmation["p90"] is not None and confirmation["p90"] <= 3),
        _check(
            "other_city_population",
            other_city_confirmation.get("population"),
            "= OTHER_CITY",
            other_city_confirmation.get("population") == "OTHER_CITY",
        ),
        _check(
            "other_city_auto_matches",
            other_city_confirmation.get("auto_match_count"),
            "= 0",
            other_city_confirmation.get("auto_match_count") == 0,
        ),
        _check(
            "other_city_pending_burden_report",
            {
                "case_count": other_city_confirmation.get("case_count"),
                "gold_executable_count": other_city_confirmation.get(
                    "gold_executable_count"
                ),
                "total": other_city_confirmation.get("total"),
            },
            "case_count > 0 and total = gold executable count",
            other_city_confirmation.get("case_count", 0) > 0
            and other_city_confirmation.get("total")
            == other_city_confirmation.get("gold_executable_count"),
        ),
        _check("public_forbidden_keys", public["forbidden_key_hits"], "= 0", public["forbidden_key_hits"] == 0),
        _check("public_source_leaks", public["full_source_leak_hits"], "= 0", public["full_source_leak_hits"] == 0),
    ]
    return {
        "split": split,
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
    }


def assess_runtime(evidence: RuntimeGateEvidence) -> dict[str, Any]:
    checks = [
        _check("qwen_exact_binding", evidence.qwen_binding_status, "EXACT_ACCOUNT_BINDING_CONFIRMED", evidence.qwen_binding_status == "EXACT_ACCOUNT_BINDING_CONFIRMED"),
        _check("amap_persistence_permission", evidence.amap_persistence_status, "WRITTEN_PERMISSION_CONFIRMED", evidence.amap_persistence_status == "WRITTEN_PERMISSION_CONFIRMED"),
        _check("first_progress", evidence.first_progress_max_ms, "<= 500 ms", evidence.first_progress_max_ms <= 500),
        _check("cards_ready_p95", evidence.cards_ready_p95_ms, "<= 8000 ms", evidence.cards_ready_p95_ms <= 8000),
        _check("provider_failure_partial_result", evidence.partial_result_on_provider_failure, "true", evidence.partial_result_on_provider_failure),
        _check("browser_scenarios", evidence.browser_scenarios_pass, "true", evidence.browser_scenarios_pass),
        _check("job_restart_lease_sse", evidence.job_restart_lease_sse_pass, "true", evidence.job_restart_lease_sse_pass),
        _check("duplicate_event_side_effects", evidence.duplicate_event_side_effects, "= 0", evidence.duplicate_event_side_effects == 0),
        _check("full_unauthorized_accesses", evidence.full_unauthorized_accesses, "= 0", evidence.full_unauthorized_accesses == 0),
        _check("demo_unauthorized_accesses", evidence.demo_unauthorized_accesses, "= 0", evidence.demo_unauthorized_accesses == 0),
        _check("ttl_claim_delete_readback", evidence.ttl_claim_delete_readback_pass, "true", evidence.ttl_claim_delete_readback_pass),
        _check("privacy_leak_hits", evidence.privacy_leak_hits, "= 0", evidence.privacy_leak_hits == 0),
        _check("budget_boundaries", evidence.budget_boundaries_pass, "true", evidence.budget_boundaries_pass),
        _check("map_edit_provider_calls", evidence.map_edit_provider_calls, "= 0", evidence.map_edit_provider_calls == 0),
        _check("map_logical_duplicate_calls", evidence.map_logical_duplicate_calls, "= 0", evidence.map_logical_duplicate_calls == 0),
        _check("map_late_pointer_overwrites", evidence.map_late_pointer_overwrites, "= 0", evidence.map_late_pointer_overwrites == 0),
        _check("map_fixture_trip_count", evidence.map_fixture_trip_count, ">= 30", evidence.map_fixture_trip_count >= 30),
        _check("map_fixture_edge_count", evidence.map_fixture_edge_count, ">= 120", evidence.map_fixture_edge_count >= 120),
        _check("map_fixture_usable_coverage", evidence.map_fixture_usable_coverage, "= 1.0", evidence.map_fixture_usable_coverage == 1.0),
        _check("map_fixture_snapshot_p95", evidence.map_fixture_snapshot_p95_ms, "<= 15000 ms", evidence.map_fixture_snapshot_p95_ms <= 15000),
        _check("map_live_usable_coverage", evidence.map_live_usable_coverage, ">= 0.95", evidence.map_live_usable_coverage >= 0.95),
        _check("map_live_snapshot_p95", evidence.map_live_snapshot_p95_ms, "<= 20000 ms", evidence.map_live_snapshot_p95_ms <= 20000),
    ]
    return {"passed": all(item["passed"] for item in checks), "checks": checks}


def run_gate(
    *,
    validation_score: dict[str, Any],
    frozen_blind_score: dict[str, Any],
    runtime_evidence: RuntimeGateEvidence,
) -> dict[str, Any]:
    validation = assess_semantic_score(validation_score, split="validation")
    frozen_blind = assess_semantic_score(frozen_blind_score, split="frozen_blind")
    runtime = assess_runtime(runtime_evidence)
    passed = validation["passed"] and frozen_blind["passed"] and runtime["passed"]
    return {
        "schema_version": "g01-text-card-gate-receipt-v1",
        "gate": "PASS" if passed else "FAIL",
        "candidate_commit": runtime_evidence.candidate_commit,
        "validation": validation,
        "frozen_blind": frozen_blind,
        "runtime": runtime,
        "human_annotation_requirement": "DUAL_HUMAN_ADJUDICATED",
        "blind_custody_requirement": "EXTERNAL_CUSTODIAN_ONE_SHOT",
    }


def readiness_receipt(data_root: Path, provider_readback_path: Path) -> dict[str, Any]:
    dataset = validate_dataset(data_root)
    provider_payload = json.loads(provider_readback_path.read_text(encoding="utf-8"))["provider_readback"]
    blockers = [
        "DEV_VALIDATION_DUAL_HUMAN_ANNOTATION_AND_ADJUDICATION",
        "QWEN_ACCOUNT_REGION_WORKSPACE_EXACT_MODEL_AND_PRICE_BINDING",
        "AMAP_WRITTEN_PERSISTENCE_PERMISSION",
        "FROZEN_BLIND_EXTERNAL_CUSTODIAN_AND_ONE_SHOT_RUN_AFTER_CANDIDATE_FREEZE",
        "FULL_TEXT_CARD_GATE_RUNTIME_MATRIX",
    ]
    return {
        "schema_version": "g01-text-card-gate-readiness-v1",
        "gate": "HITL_PENDING",
        "dataset": dataset,
        "provider_readback": provider_payload,
        "blocking_requirements": blockers,
        "automated_gate_pass_claim": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("eval_data/trip_text_cards_v1"))
    parser.add_argument(
        "--provider-readback",
        type=Path,
        default=Path("../docs/governance/g01_s0_asset_disposition.json"),
    )
    parser.add_argument("--require-pass", action="store_true")
    args = parser.parse_args()
    receipt = readiness_receipt(args.data_root.resolve(strict=True), args.provider_readback.resolve(strict=True))
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 2 if args.require_pass and receipt["gate"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
