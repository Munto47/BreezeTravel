from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from evals.trip_check_v1.p5.contracts_v2 import P5OracleV2
from evals.trip_check_v1.p5.contracts_v3 import (
    GateStatusV3,
    P5ArtifactIndexV3,
    P5CaseV3,
    P5CaseResultV3,
    P5FailureRecordV3,
    P5GateManifestV3,
    P5MaterializationBindingV3,
    P5OcrSourceBindingV3,
    P5TerminalOutputV3,
    P5VariantRunSpecV3,
    TerminalStatusV3,
    VARIANT_IDS_V3,
)
from evals.trip_check_v1.p5.data_contract import digest


def _artifact(artifact_id: str, schema_version: str, marker: str) -> dict[str, str]:
    return {
        "artifact_id": artifact_id,
        "schema_version": schema_version,
        "content_sha256": marker * 64,
    }


def _oracle() -> P5OracleV2:
    return P5OracleV2(
        task_success_required=True,
        requires_user_resolution=False,
        required_reason_codes=["TIME_CHAIN_CONFLICT"],
        wrong_city_or_poi_max=0,
        max_new_blocker_high_unknown=0,
        unknown_must_be_preserved=True,
        advice_required=True,
        specific_place_allowed=True,
        candidate_receipt_mode="REQUIRED",
        expected_strategy_outcome="FEASIBLE",
        concurrency_expectation="NONE",
        ocr_required=True,
    )


def _ocr_source(*, blind: bool = False) -> dict[str, str]:
    return {
        "schema_version": "trip-check-p5-v3-ocr-source-binding-v1",
        "source_dataset_id": "trip-check-p5-360-v2",
        "source_manifest_hash": "1" * 64,
        "source_manifest_file_sha256": "2" * 64,
        "source_blind_seal_file_sha256": "3" * 64,
        "source_active_contract_sha256": "4" * 64,
        "source_active_contract_file_sha256": "5" * 64,
        "source_candidate_freeze_commit": "6" * 40,
        "source_path": (
            "evals/trip_check_v1/p5/frozen_blind.v2.materializations.jsonl"
            if blind
            else "evals/trip_check_v1/p5/materializations_nonblind_v2.jsonl"
        ),
        "source_file_sha256": "7" * 64,
        "source_materialization_hash": "8" * 64,
        "historical_render_receipt_sha256": "9" * 64,
        "historical_ocr_receipt_sha256": "a" * 64,
        "historical_cleanup_receipt_sha256": "b" * 64,
    }


def _materialization(*, screenshot: bool, blind: bool = False) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "trip-check-p5-materialization-binding-v3",
        "materialization_id": "materialization-p5-v3-case",
        "materialization_sha256": "c" * 64,
        "source_payload": _artifact("source-p5-v3-case", "trip-check-p5-source-payload-v3", "d"),
        "provider_snapshot": _artifact(
            "trip-check-p5-controlled-snapshot-v3",
            "trip-check-p5-provider-snapshot-v3",
            "e",
        ),
        "evidence_snapshot": _artifact(
            "snapshot-p5-v3-case", "trip-check-p5-evidence-snapshot-v3", "f"
        ),
        "candidate_sets": [],
        "fault_script": _artifact("fault-p5-v3-case", "trip-check-p5-fault-artifact-v3", "0"),
    }
    if screenshot:
        value.update(
            {
                "render_receipt": _artifact(
                    "render-p5-v3-case", "trip-check-p5-render-receipt-v2", "9"
                ),
                "ocr_baseline_receipt": _artifact(
                    "ocr-p5-v3-case", "trip-check-p5-ocr-baseline-receipt-v2", "a"
                ),
                "cleanup_receipt": _artifact(
                    "cleanup-p5-v3-case", "trip-check-p5-cleanup-receipt-v2", "b"
                ),
                "ocr_source_binding": _ocr_source(blind=blind),
            }
        )
    return value


def _case_payload(
    *, input_kind: str = "TEXT", split: str = "dev"
) -> dict[str, object]:
    screenshot = input_kind == "SYNTHETIC_SCREENSHOT"
    blind = split == "frozen_blind"
    oracle = None if blind else _oracle()
    payload: dict[str, object] = {
        "schema_version": "trip-check-p5-eval-case-v3",
        "case_id": "p5.blind.bj.001" if blind else "p5.dev.bj.001",
        "split": split,
        "city": "北京",
        "trip_days": 2,
        "group_size": 2,
        "input_kind": input_kind,
        "difficulty": "HARD",
        "coverage_tags": ["route", "unknown", "semantic_closure"],
        "product_input": (
            {
                "source_type": "SYNTHETIC_SCREENSHOT",
                "source_text": "北京2日，第1天故宫。",
                "render_spec": {"schema_version": "trip-check-p5-render-spec-v2"},
            }
            if screenshot
            else {"source_type": "MANUAL_TEXT", "raw_text": "北京2日，第1天故宫。"}
        ),
        "normalized_input_sha256": "1" * 64,
        "materialization": _materialization(screenshot=screenshot, blind=blind),
        "runner_control": {"fault_profile_id": "none"},
        "lineage": {"content_family_id": "family-1"},
        "source_ref": {"case_id": "source-1"},
        "provenance": {"reviewed_by": "NOT_RUN", "contains_human_data": False},
    }
    if oracle is not None:
        payload["oracle"] = oracle.model_dump(mode="json")
        payload["oracle_sha256"] = digest(oracle.model_dump(mode="json"))
    payload["case_hash"] = digest(payload)
    return payload


def _run_spec_payload() -> dict[str, object]:
    return {
        "schema_version": "trip-check-p5-variant-run-spec-v3",
        "subject_commit": "1" * 40,
        "dirty_tree": False,
        "lane": "nonblind",
        "dataset_manifest_hash": "2" * 64,
        "case_set_hash": "3" * 64,
        "materialization_set_hash": "4" * 64,
        "run_spec_template_hash": "5" * 64,
        "rubric_hash": "6" * 64,
        "renderer_version": "2.0.0",
        "ocr_engine_version": "3.7.0",
        "evidence_policy_version": "trip-check-p5-controlled-evidence-v3",
        "fault_registry_version": "trip-check-p5-fault-registry-v3",
        "random_seed": 20260824,
        "budget": {
            "max_cost_usd": 0,
            "max_provider_queries": 0,
            "max_retries": 1,
            "max_tokens": 0,
            "timeout_seconds": 30,
        },
        "replay_hash_policy": "p5-semantic-projection-v3",
        "variant_id": "core_b",
        "adapter_version": "core-b-v3",
        "repair_strategy": "bounded_repair_v1",
    }


def _terminal_payload() -> dict[str, object]:
    return {
        "schema_version": "trip-check-p5-terminal-output-v3",
        "case_id": "p5.dev.bj.001",
        "split": "dev",
        "city": "北京",
        "input_kind": "TEXT",
        "input_hash": "1" * 64,
        "materialization_hash": "2" * 64,
        "render_receipt_hash": None,
        "ocr_receipt_hash": None,
        "provider_snapshot_hash": "3" * 64,
        "evidence_snapshot_hash": "4" * 64,
        "candidate_set_hashes": [],
        "fault_script_hash": "5" * 64,
        "run_spec_hash": "6" * 64,
        "variant_id": "core_b",
        "adapter_version": "core-b-v3",
        "repair_strategy": "bounded_repair_v1",
        "terminal_status": "SUCCEEDED",
        "capability_outcomes": {"external_api_calls": "0"},
        "native_output": {},
        "evaluation_projection": {},
        "findings": [],
        "advice": [],
        "postcheck": None,
        "receipts": [],
        "latency_ms": 1.0,
        "token_count": 0,
        "cost_usd": 0,
        "error_category": None,
        "raw_artifact_hash": "7" * 64,
        "semantic_output_hash": "8" * 64,
        "replay_hash": "8" * 64,
    }


def test_v3_case_reuses_v2_oracle_shape_without_relabeling_truth() -> None:
    case = P5CaseV3.model_validate(_case_payload())

    assert isinstance(case.oracle, P5OracleV2)
    assert case.oracle is not None
    assert case.oracle.schema_version == "trip-check-p5-oracle-v2"
    assert case.model_dump(mode="json")["oracle"]["schema_version"] == "trip-check-p5-oracle-v2"


def test_v3_blind_case_is_label_free_and_hash_bound() -> None:
    payload = _case_payload(split="frozen_blind")
    case = P5CaseV3.model_validate(payload)
    assert case.oracle is None
    assert case.oracle_sha256 is None

    leaked = {**payload, "oracle": _oracle().model_dump(mode="json")}
    leaked["oracle_sha256"] = digest(leaked["oracle"])
    leaked["case_hash"] = digest({key: value for key, value in leaked.items() if key != "case_hash"})
    with pytest.raises(ValidationError, match="frozen blind cases cannot contain oracle"):
        P5CaseV3.model_validate(leaked)


def test_v3_nonblind_requires_exact_oracle_and_case_hashes() -> None:
    missing_oracle = _case_payload()
    missing_oracle.pop("oracle")
    missing_oracle.pop("oracle_sha256")
    missing_oracle["case_hash"] = digest(
        {key: value for key, value in missing_oracle.items() if key != "case_hash"}
    )
    with pytest.raises(ValidationError, match="hash-bound v2 oracle"):
        P5CaseV3.model_validate(missing_oracle)

    stale_oracle = _case_payload()
    stale_oracle["oracle_sha256"] = "0" * 64
    stale_oracle["case_hash"] = digest(
        {key: value for key, value in stale_oracle.items() if key != "case_hash"}
    )
    with pytest.raises(ValidationError, match="oracle_sha256"):
        P5CaseV3.model_validate(stale_oracle)

    stale_case = _case_payload()
    stale_case["product_input"] = {"source_type": "MANUAL_TEXT", "raw_text": "tampered"}
    with pytest.raises(ValidationError, match="case_hash"):
        P5CaseV3.model_validate(stale_case)


def test_v3_screenshot_binds_complete_historical_v2_receipt_provenance() -> None:
    case = P5CaseV3.model_validate(_case_payload(input_kind="SYNTHETIC_SCREENSHOT"))
    binding = case.materialization
    assert binding.render_receipt is not None
    assert binding.render_receipt.schema_version == "trip-check-p5-render-receipt-v2"
    assert binding.ocr_baseline_receipt is not None
    assert binding.ocr_baseline_receipt.schema_version == "trip-check-p5-ocr-baseline-receipt-v2"
    assert binding.cleanup_receipt is not None
    assert binding.cleanup_receipt.schema_version == "trip-check-p5-cleanup-receipt-v2"
    assert binding.ocr_source_binding is not None
    assert binding.ocr_source_binding.source_dataset_id == "trip-check-p5-360-v2"


@pytest.mark.parametrize(
    "missing",
    ["render_receipt", "ocr_baseline_receipt", "cleanup_receipt", "ocr_source_binding"],
)
def test_v3_screenshot_rejects_partial_receipt_bindings(missing: str) -> None:
    payload = _materialization(screenshot=True)
    payload.pop(missing)
    with pytest.raises(ValidationError, match="all present or all absent"):
        P5MaterializationBindingV3.model_validate(payload)


def test_v3_screenshot_rejects_relabeling_or_hash_drift() -> None:
    relabeled = _materialization(screenshot=True)
    relabeled["ocr_baseline_receipt"] = {
        **relabeled["ocr_baseline_receipt"],  # type: ignore[arg-type]
        "schema_version": "trip-check-p5-ocr-baseline-receipt-v3",
    }
    with pytest.raises(ValidationError, match="historical v2 receipt schemas"):
        P5MaterializationBindingV3.model_validate(relabeled)

    drifted = _materialization(screenshot=True)
    source = dict(drifted["ocr_source_binding"])  # type: ignore[arg-type]
    source["historical_ocr_receipt_sha256"] = "f" * 64
    drifted["ocr_source_binding"] = source
    with pytest.raises(ValidationError, match="sealed v2 source binding"):
        P5MaterializationBindingV3.model_validate(drifted)


def test_v3_text_case_cannot_carry_screenshot_provenance() -> None:
    payload = _case_payload()
    payload["materialization"] = _materialization(screenshot=True)
    payload["case_hash"] = digest({key: value for key, value in payload.items() if key != "case_hash"})
    with pytest.raises(ValidationError, match="text cases cannot bind"):
        P5CaseV3.model_validate(payload)


def test_v3_run_spec_is_strict_and_hash_stable() -> None:
    payload = _run_spec_payload()
    spec = P5VariantRunSpecV3.model_validate(payload)
    assert VARIANT_IDS_V3 == ("legacy_a", "core_b", "solver_c")
    assert spec.run_spec_hash == digest(spec.model_dump(mode="json"))

    v2_policy = {**payload, "replay_hash_policy": "p5-semantic-projection-v2"}
    with pytest.raises(ValidationError):
        P5VariantRunSpecV3.model_validate(v2_policy)

    extra_budget = deepcopy(payload)
    extra_budget["budget"]["hidden_retries"] = 1  # type: ignore[index]
    with pytest.raises(ValidationError, match="extra_forbidden"):
        P5VariantRunSpecV3.model_validate(extra_budget)


def test_v3_terminal_contract_rejects_v2_schema_extra_and_negative_usage() -> None:
    terminal = P5TerminalOutputV3.model_validate(_terminal_payload())
    assert terminal.terminal_status is TerminalStatusV3.SUCCEEDED

    for update in (
        {"schema_version": "trip-check-p5-terminal-output-v2"},
        {"unexpected": True},
        {"latency_ms": -1},
        {"token_count": -1},
        {"cost_usd": -0.01},
    ):
        with pytest.raises(ValidationError):
            P5TerminalOutputV3.model_validate({**_terminal_payload(), **update})


def test_v3_case_result_binds_terminal_and_revision_lineage() -> None:
    payload = {
        "schema_version": "trip-check-p5-case-result-v3",
        "case_result_id": "result-p5.dev.bj.001-core-b",
        "run_id": "run-nonblind-core-b",
        "terminal_output": P5TerminalOutputV3.model_validate(_terminal_payload()).model_dump(
            mode="json"
        ),
        "revision_lineage": {
            "input_revision": 1,
            "adopted_revision": 2,
            "postcheck_revision": 2,
        },
    }
    payload["case_result_hash"] = digest(payload)

    result = P5CaseResultV3.model_validate(payload)
    assert result.terminal_output.terminal_status is TerminalStatusV3.SUCCEEDED

    tampered = deepcopy(payload)
    tampered["revision_lineage"]["postcheck_revision"] = 3  # type: ignore[index]
    with pytest.raises(ValidationError, match="case_result_hash"):
        P5CaseResultV3.model_validate(tampered)


def test_v3_failure_record_binds_first_attempt_and_retry_policy() -> None:
    payload = {
        "schema_version": "trip-check-p5-failure-record-v3",
        "run_id": "run-nonblind-core-b",
        "case_id": "p5.dev.bj.001",
        "lane": "nonblind",
        "variant_id": "core_b",
        "failure_status": "REJECT",
        "failure_category": "REPLAY_HASH_MISMATCH",
        "terminal_status": "ERROR",
        "first_attempt_receipt_hash": "1" * 64,
        "reproduction_command": "python scripts/run_trip_check_p5_v3_eval.py --case-id p5.dev.bj.001",
        "retry_allowed": False,
        "retry_count": 0,
    }
    payload["failure_record_hash"] = digest(payload)
    assert P5FailureRecordV3.model_validate(payload).failure_status == "REJECT"

    retried = {**payload, "retry_count": 1}
    retried["failure_record_hash"] = digest(
        {key: value for key, value in retried.items() if key != "failure_record_hash"}
    )
    with pytest.raises(ValidationError, match="retry_count"):
        P5FailureRecordV3.model_validate(retried)


def test_v3_artifact_index_requires_unique_relative_hash_bound_entries() -> None:
    entry = {
        "path": "evals/trip_check_v1/p5/results/run.jsonl",
        "byte_size": 123,
        "sha256": "2" * 64,
        "generated_by": "run_trip_check_p5_v3_eval.py",
        "generated_at": "2026-08-24T05:30:00Z",
    }
    payload = {
        "schema_version": "trip-check-p5-artifact-index-v3",
        "subject_commit": "3" * 40,
        "dirty_tree": False,
        "entries": [entry],
    }
    payload["artifact_index_hash"] = digest(payload)
    assert P5ArtifactIndexV3.model_validate(payload).entries[0].byte_size == 123

    duplicate = {**payload, "entries": [entry, entry]}
    duplicate["artifact_index_hash"] = digest(
        {key: value for key, value in duplicate.items() if key != "artifact_index_hash"}
    )
    with pytest.raises(ValidationError, match="paths must be unique"):
        P5ArtifactIndexV3.model_validate(duplicate)


def test_v3_gate_manifest_pass_requires_all_gates_and_nonrelease_boundary() -> None:
    gate = {
        "gate_id": "P5-R1-NONBLIND",
        "status": "PASS",
        "hard_thresholds": {"terminal_rows": 810, "replay_match_rate": 1.0},
        "evidence_boundary": {"controlled_fixture": "PASS", "human_evidence": "NOT_RUN"},
        "artifact_hashes": ["4" * 64],
        "notes": [],
    }
    payload = {
        "schema_version": "trip-check-p5-gate-manifest-v3",
        "subject_commit": "5" * 40,
        "dirty_tree": False,
        "status": "PASS",
        "gates": [gate],
        "artifact_index_hash": "6" * 64,
        "dataset_manifest_hash": "7" * 64,
        "human_calibration_performed": False,
        "human_evidence": "NOT_RUN",
        "production_release": "NOT_RUN",
        "main_merge": "NOT_RUN",
    }
    payload["gate_manifest_hash"] = digest(payload)
    assert P5GateManifestV3.model_validate(payload).status is GateStatusV3.PASS

    contradictory = deepcopy(payload)
    contradictory["gates"][0]["status"] = "NOT_RUN"  # type: ignore[index]
    contradictory["gate_manifest_hash"] = digest(
        {key: value for key, value in contradictory.items() if key != "gate_manifest_hash"}
    )
    with pytest.raises(ValidationError, match="evidence boundary"):
        P5GateManifestV3.model_validate(contradictory)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (P5OcrSourceBindingV3, _ocr_source()),
        (P5MaterializationBindingV3, _materialization(screenshot=False)),
        (P5CaseV3, _case_payload()),
        (P5VariantRunSpecV3, _run_spec_payload()),
        (P5TerminalOutputV3, _terminal_payload()),
    ],
)
def test_all_v3_contract_envelopes_forbid_extra_fields(model: type, payload: dict) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate({**payload, "unexpected": True})
