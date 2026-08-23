from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy

import pytest

from evals.trip_check_v1.p5.adapters_v3 import (
    CoreAdapterV3,
    EvaluationCachingPaddleOcrEngineV3,
    LegacyAdapterV3,
    validate_materialization_v3,
)
from evals.trip_check_v1.p5.contracts_v3 import (
    P5CaseResultV3,
    P5CaseV3,
    P5VariantRunSpecV3,
    TerminalStatusV3,
)
from evals.trip_check_v1.p5.data_contract import digest, file_sha256, load_jsonl
from evals.trip_check_v1.p5.data_contract_v2 import JUDGE_RUBRIC_PATH_V2
from evals.trip_check_v1.p5.data_contract_v3 import (
    MANIFEST_PATH_V3,
    NONBLIND_MATERIALIZATIONS_PATH_V3,
    NONBLIND_PATH_V3,
    RUN_SPEC_TEMPLATE_PATH_V3,
    case_set_hash_v3,
    materialization_set_hash_v3,
)
from evals.trip_check_v1.p5.runner_v3 import (
    build_case_result_v3,
    build_failure_record_v3,
    execute_terminal_v3,
)
import scripts.run_trip_check_p5_v3_eval as run_v3


def _case_and_materialization(case_id: str) -> tuple[P5CaseV3, dict]:
    cases = {row["case_id"]: row for row in load_jsonl(NONBLIND_PATH_V3)}
    materializations = {
        row["case_id"]: row for row in load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V3)
    }
    return P5CaseV3.model_validate(cases[case_id]), materializations[case_id]


def _spec(
    case: P5CaseV3, materialization: dict, *, variant_id: str
) -> P5VariantRunSpecV3:
    template = json.loads(RUN_SPEC_TEMPLATE_PATH_V3.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_PATH_V3.read_text(encoding="utf-8"))
    adapter_version, repair_strategy = run_v3.ADAPTER_VERSIONS_V3[variant_id]
    return P5VariantRunSpecV3(
        subject_commit="a" * 40,
        dirty_tree=True,
        lane="nonblind",
        dataset_manifest_hash=manifest["manifest_hash"],
        case_set_hash=case_set_hash_v3(
            [case.model_dump(mode="json", exclude_none=True)]
        ),
        materialization_set_hash=materialization_set_hash_v3([materialization]),
        run_spec_template_hash=file_sha256(RUN_SPEC_TEMPLATE_PATH_V3),
        rubric_hash=file_sha256(JUDGE_RUBRIC_PATH_V2),
        renderer_version=template["renderer"]["version"],
        ocr_engine_version=template["historical_ocr_evidence"]["engine_version"],
        evidence_policy_version=template["evidence_policy_version"],
        fault_registry_version=template["fault_registry_version"],
        random_seed=template["random_seed"],
        budget=template["budget"],
        variant_id=variant_id,
        adapter_version=adapter_version,
        repair_strategy=repair_strategy,
    )


def _args(tmp_path, *, lane: str, active_contract: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        lane=lane,
        variants="legacy,core,solver",
        cases_file=None,
        materializations_file=None,
        dataset_manifest=None,
        run_spec_template=None,
        rubric=None,
        active_contract=active_contract,
        case_id=["p5.dev.bj.002"] if lane == "nonblind" else None,
        limit=None,
        replay=True,
        allow_dirty=True,
        require_formal=False,
        run_id="v3-runner-test",
        output_dir=str(tmp_path),
    )


def test_v3_terminal_uses_outer_validator_and_frozen_ocr_replay() -> None:
    case, materialization = _case_and_materialization("p5.dev.bj.002")
    engine = EvaluationCachingPaddleOcrEngineV3()
    engine.preload(materialization["ocr_baseline_receipt"])
    spec = _spec(case, materialization, variant_id="core_b")
    adapter = CoreAdapterV3(ocr_engine=engine)

    first = asyncio.run(
        execute_terminal_v3(
            case=case,
            materialization=materialization,
            run_spec=spec,
            adapter=adapter,
        )
    )
    replay = asyncio.run(
        execute_terminal_v3(
            case=case,
            materialization=materialization,
            run_spec=spec,
            adapter=adapter,
        )
    )

    assert first.replay_hash == replay.replay_hash
    assert first.schema_version == "trip-check-p5-terminal-output-v3"
    assert first.capability_outcomes["screenshot_execution"] == (
        "FROZEN_ACTUAL_OCR_RECEIPT_REPLAY"
    )
    adjacent = [row for row in first.receipts if row.get("type") == "ocr_replay_provenance"]
    assert len(adjacent) == 1
    assert adjacent[0]["receipt_match"] is True
    assert adjacent[0]["cleanup_status"] == "DELETED"
    assert engine.provenance() | {} == {
        **engine.provenance(),
        "lookup_count": 2,
        "hit_count": 2,
        "miss_count": 0,
        "fallback_count": 0,
        "fresh_prediction_count": 0,
        "receipt_match_count": 2,
        "cleanup_deleted_count": 2,
    }

    result = build_case_result_v3(run_id="unit", case=case, terminal=first)
    assert result.revision_lineage["source_materialization_hash"] == (
        materialization["materialization_hash"]
    )


def test_v3_ocr_cache_miss_becomes_invalid_evidence_failure() -> None:
    case, materialization = _case_and_materialization("p5.dev.bj.002")
    terminal = asyncio.run(
        execute_terminal_v3(
            case=case,
            materialization=materialization,
            run_spec=_spec(case, materialization, variant_id="core_b"),
            adapter=CoreAdapterV3(ocr_engine=EvaluationCachingPaddleOcrEngineV3()),
        )
    )

    assert terminal.terminal_status is TerminalStatusV3.ERROR
    assert terminal.error_category == "OcrProcessingError"
    failure = build_failure_record_v3(run_id="unit", lane="nonblind", terminal=terminal)
    assert failure is not None
    assert failure.failure_status == "INVALID_EVIDENCE"


def test_v3_low_confidence_confirmation_flags_survive_product_import() -> None:
    case, materialization = _case_and_materialization("p5.dev.bj.042")
    expected_lines = materialization["ocr_baseline_receipt"]["lines"]
    assert any(line["requires_confirmation"] is True for line in expected_lines)
    engine = EvaluationCachingPaddleOcrEngineV3()
    engine.preload(materialization["ocr_baseline_receipt"])

    terminal = asyncio.run(
        execute_terminal_v3(
            case=case,
            materialization=materialization,
            run_spec=_spec(case, materialization, variant_id="core_b"),
            adapter=CoreAdapterV3(ocr_engine=engine),
        )
    )

    receipt = next(row for row in terminal.receipts if row.get("type") == "ocr")
    assert receipt["lines"] == expected_lines
    assert any(line["requires_confirmation"] is True for line in receipt["lines"])


def test_v3_outer_tamper_fails_before_adapter_execution() -> None:
    case, materialization = _case_and_materialization("p5.pilot.bj.001")
    tampered = deepcopy(materialization)
    tampered["evidence_snapshot"]["snapshot"]["policy_version"] = "downgraded-v2"
    tampered["materialization_hash"] = digest(
        {key: value for key, value in tampered.items() if key != "materialization_hash"}
    )

    with pytest.raises(ValueError):
        validate_materialization_v3(case, tampered)


def test_legacy_v3_screenshot_never_accesses_ocr_cache() -> None:
    case, materialization = _case_and_materialization("p5.dev.bj.002")
    terminal = asyncio.run(
        execute_terminal_v3(
            case=case,
            materialization=materialization,
            run_spec=_spec(case, materialization, variant_id="legacy_a"),
            adapter=LegacyAdapterV3(),
        )
    )

    assert terminal.terminal_status is TerminalStatusV3.UNSUPPORTED_CAPABILITY
    assert terminal.capability_outcomes["screenshot_execution"] == "DENIED_LEGACY_BOUNDARY"
    assert not any(row.get("type") == "ocr_replay_provenance" for row in terminal.receipts)


def test_legacy_v3_text_converts_budget_across_frozen_contract_boundary() -> None:
    case, materialization = _case_and_materialization("p5.pilot.bj.001")
    terminal = asyncio.run(
        execute_terminal_v3(
            case=case,
            materialization=materialization,
            run_spec=_spec(case, materialization, variant_id="legacy_a"),
            adapter=LegacyAdapterV3(),
        )
    )

    assert terminal.terminal_status is not TerminalStatusV3.ERROR
    assert terminal.error_category is None
    assert terminal.evaluation_projection["import_status"] == "LEGACY_NATIVE_TEXT"
    assert any(row.get("type") == "legacy_isolation" for row in terminal.receipts)


def test_v3_nonblind_run_writes_three_strict_results_and_four_ocr_replays(
    tmp_path,
) -> None:
    manifest = asyncio.run(run_v3.execute_run(_args(tmp_path, lane="nonblind")))

    assert manifest["status"] == "PASS"
    assert manifest["formal_evidence"] is False
    assert manifest["terminal_count"] == 3
    assert manifest["expected_terminal_count"] == 3
    assert manifest["replay_match_count"] == 3
    assert manifest["blind_labels_read"] is False
    assert manifest["external_api_calls"] == 0
    provenance = manifest["ocr_replay_provenance"]
    assert provenance["lookup_count"] == 4
    assert provenance["hit_count"] == 4
    assert provenance["receipt_match_count"] == 4
    assert provenance["cleanup_deleted_count"] == 4
    assert provenance["miss_count"] == 0
    assert provenance["fallback_count"] == 0
    assert provenance["fresh_prediction_count"] == 0
    assert provenance["legacy_cache_access_count"] == 0

    rows = [
        P5CaseResultV3.model_validate(json.loads(line))
        for line in (tmp_path / "v3-runner-test" / "case_results.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert {row.terminal_output.variant_id for row in rows} == {
        "legacy_a",
        "core_b",
        "solver_c",
    }


def test_v3_blind_lane_fails_before_loading_any_blind_row(
    tmp_path, monkeypatch
) -> None:
    active_path = tmp_path / "active_contract.json"
    active_path.write_text(
        json.dumps(
            {
                "schema_version": "trip-check-p5-active-contract-v1",
                "active_contract": "trip-check-p5-v2",
                "formal_evidence_status": "READY",
            }
        ),
        encoding="utf-8",
    )

    def forbidden_load(_path):
        raise AssertionError("blind row access must occur after the active v3 boundary")

    monkeypatch.setattr(run_v3, "load_jsonl", forbidden_load)
    with pytest.raises(RuntimeError, match="P5_V3_FORMAL_CONTRACT_NOT_READY"):
        asyncio.run(
            run_v3.execute_run(
                _args(
                    tmp_path,
                    lane="frozen_blind",
                    active_contract=str(active_path),
                )
            )
        )
