from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.adapters_v3 import (
    CoreAdapterV3,
    EvaluationCachingPaddleOcrEngineV3,
)
from evals.trip_check_v1.p5.contracts_v3 import P5CaseV3, P5TerminalOutputV3
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
from evals.trip_check_v1.p5.scorer_v3 import (
    P5V3ScoringError,
    score_case_v3,
    score_run_group_v3,
    semantic_output_hash_v3,
    validate_run_group_v3,
)
import evals.trip_check_v1.p5.scorer_v3 as scorer_v3
import scripts.run_trip_check_p5_v3_eval as run_v3


def _case_and_materialization(case_id: str) -> tuple[P5CaseV3, dict]:
    cases = {row["case_id"]: row for row in load_jsonl(NONBLIND_PATH_V3)}
    materializations = {
        row["case_id"]: row for row in load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V3)
    }
    return P5CaseV3.model_validate(cases[case_id]), materializations[case_id]


def _spec(case: P5CaseV3, materialization: dict):
    template = json.loads(RUN_SPEC_TEMPLATE_PATH_V3.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_PATH_V3.read_text(encoding="utf-8"))
    adapter_version, repair_strategy = run_v3.ADAPTER_VERSIONS_V3["core_b"]
    return run_v3.P5VariantRunSpecV3(
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
        variant_id="core_b",
        adapter_version=adapter_version,
        repair_strategy=repair_strategy,
    )


def _execute_core(case_id: str) -> tuple[P5CaseV3, dict, P5TerminalOutputV3]:
    case, materialization = _case_and_materialization(case_id)
    engine = EvaluationCachingPaddleOcrEngineV3()
    if materialization.get("ocr_baseline_receipt") is not None:
        engine.preload(materialization["ocr_baseline_receipt"])
    terminal = asyncio.run(
        run_v3.execute_terminal_v3(
            case=case,
            materialization=materialization,
            run_spec=_spec(case, materialization),
            adapter=CoreAdapterV3(ocr_engine=engine),
        )
    )
    return case, materialization, terminal


def _rehash_terminal(terminal: P5TerminalOutputV3, **changes) -> P5TerminalOutputV3:
    payload = terminal.model_dump(mode="json")
    payload.update(changes)
    payload["semantic_output_hash"] = "0" * 64
    payload["replay_hash"] = "0" * 64
    provisional = P5TerminalOutputV3.model_validate(payload)
    semantic_hash = semantic_output_hash_v3(provisional)
    payload["semantic_output_hash"] = semantic_hash
    payload["replay_hash"] = semantic_hash
    return P5TerminalOutputV3.model_validate(payload)


def _runner_args(output_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        lane="nonblind",
        variants="legacy,core,solver",
        cases_file=None,
        materializations_file=None,
        dataset_manifest=None,
        run_spec_template=None,
        rubric=None,
        active_contract=None,
        case_id=["p5.dev.bj.002"],
        limit=None,
        replay=True,
        allow_dirty=True,
        require_formal=False,
        run_id="v3-scorer-test-run",
        output_dir=str(output_dir),
    )


@pytest.fixture(scope="module")
def development_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("p5-v3-scorer-run")
    asyncio.run(run_v3.execute_run(_runner_args(output)))
    return output / "v3-scorer-test-run"


@pytest.fixture(scope="module")
def validated_dataset():
    return scorer_v3._validate_dataset_v3(require_formal=False)


def _copy_run(source: Path, tmp_path: Path) -> Path:
    target = tmp_path / "run"
    shutil.copytree(source, target)
    return target


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _rehash_manifest(run_dir: Path, manifest: dict) -> None:
    manifest["manifest_hash"] = digest(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    )
    _write_json(run_dir / "run_group_manifest.json", manifest)


def _refresh_index_and_manifest(run_dir: Path, artifact_name: str) -> None:
    manifest = json.loads((run_dir / "run_group_manifest.json").read_text(encoding="utf-8"))
    artifact_path = run_dir / artifact_name
    if artifact_name == "case_results.jsonl":
        rows = [json.loads(line) for line in artifact_path.read_text(encoding="utf-8").splitlines()]
        manifest["case_results_file_sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        manifest["case_results_content_sha256"] = digest(rows)
    else:
        rows = [json.loads(line) for line in artifact_path.read_text(encoding="utf-8").splitlines() if line]
        manifest["failure_records_file_sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        manifest["failure_records_content_sha256"] = digest(rows)
        manifest["failure_record_count"] = len(rows)
    index = json.loads((run_dir / "artifact_index.json").read_text(encoding="utf-8"))
    entry = next(item for item in index["entries"] if item["path"] == artifact_name)
    entry["byte_size"] = artifact_path.stat().st_size
    entry["sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    index["artifact_index_hash"] = digest(
        {key: value for key, value in index.items() if key != "artifact_index_hash"}
    )
    _write_json(run_dir / "artifact_index.json", index)
    manifest["artifact_index_hash"] = index["artifact_index_hash"]
    _rehash_manifest(run_dir, manifest)


def test_v3_postcheck_schema_is_native_not_v2() -> None:
    case, materialization, terminal = _execute_core("p5.pilot.bj.001")
    score = score_case_v3(case, terminal, materialization=materialization)
    assert terminal.postcheck["schema_version"] == "trip-check-p5-postcheck-projection-v3"
    assert score.repair_postcheck == "PASS"

    stale_postcheck = deepcopy(terminal.postcheck)
    stale_postcheck["schema_version"] = "trip-check-p5-postcheck-projection-v2"
    changed = _rehash_terminal(terminal, postcheck=stale_postcheck)
    assert score_case_v3(case, changed, materialization=materialization).repair_postcheck == "FAIL"


def test_user_resolution_requires_terminal_candidate_receipts() -> None:
    case, materialization, terminal = _execute_core("p5.pilot.bj.002")
    assert terminal.evaluation_projection["selected_place_ids"] == []
    score = score_case_v3(case, terminal, materialization=materialization)
    assert score.candidate_receipt_coverage == "FAIL"

    projection = deepcopy(terminal.evaluation_projection)
    projection["candidate_receipt_coverage"] = 1.0
    fixed = _rehash_terminal(
        terminal,
        evaluation_projection=projection,
        receipts=materialization["receipts"],
    )
    fixed_score = score_case_v3(case, fixed, materialization=materialization)
    assert fixed_score.candidate_receipt_coverage == "PASS"
    assert "CANDIDATE_RECEIPT_VIOLATION" not in fixed_score.deterministic_failure_codes


def test_development_run_is_strictly_validated_but_cannot_pass_gate(
    development_run: Path,
) -> None:
    report = score_run_group_v3(run_dir=development_run, require_formal=False)
    assert report["status"] == "REJECT"
    assert report["formal_validation_performed"] is False
    assert report["evidence_boundary"]["blind_labels_read"] is False
    assert report["evidence_boundary"]["controlled_snapshot"] == "DIAGNOSTIC_ONLY"


def test_scorer_never_loads_frozen_blind_rows(
    development_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = scorer_v3._load_jsonl

    def guarded(path: Path, reason: str):
        assert "frozen_blind" not in path.name
        return original(path, reason)

    monkeypatch.setattr(scorer_v3, "_load_jsonl", guarded)
    validate_run_group_v3(run_dir=development_run, require_formal=False)


def test_manifest_extra_field_and_blind_lane_fail_before_scoring(
    development_run: Path, tmp_path: Path
) -> None:
    for field, value, reason in (
        ("unexpected", True, "RUN_GROUP_MANIFEST_FIELDS_INVALID"),
        ("lane", "frozen_blind", "RUN_GROUP_CONTRACT_INVALID"),
    ):
        run_dir = _copy_run(development_run, tmp_path / field)
        manifest = json.loads((run_dir / "run_group_manifest.json").read_text(encoding="utf-8"))
        manifest[field] = value
        _rehash_manifest(run_dir, manifest)
        with pytest.raises(P5V3ScoringError, match=reason):
            validate_run_group_v3(run_dir=run_dir, require_formal=False)


def test_rehashed_semantic_tamper_is_rejected(
    development_run: Path,
    validated_dataset,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _copy_run(development_run, tmp_path)
    path = run_dir / "case_results.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["terminal_output"]["semantic_output_hash"] = "f" * 64
    payload = {key: value for key, value in rows[0].items() if key != "case_result_hash"}
    rows[0]["case_result_hash"] = digest(payload)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )
    _refresh_index_and_manifest(run_dir, "case_results.jsonl")
    monkeypatch.setattr(scorer_v3, "_validate_dataset_v3", lambda **_: validated_dataset)
    with pytest.raises(P5V3ScoringError, match="TERMINAL_CASE_OR_HASH_BINDING_MISMATCH"):
        validate_run_group_v3(run_dir=run_dir, require_formal=False)


def test_rehashed_revision_lineage_tamper_is_rejected(
    development_run: Path,
    validated_dataset,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _copy_run(development_run, tmp_path)
    path = run_dir / "case_results.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["revision_lineage"]["resulting_revision"] = 999
    payload = {key: value for key, value in rows[0].items() if key != "case_result_hash"}
    rows[0]["case_result_hash"] = digest(payload)
    path.write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for row in rows
        )
        + "\n",
        encoding="utf-8",
    )
    _refresh_index_and_manifest(run_dir, "case_results.jsonl")
    monkeypatch.setattr(scorer_v3, "_validate_dataset_v3", lambda **_: validated_dataset)
    with pytest.raises(
        P5V3ScoringError, match="CASE_RESULT_RUN_OR_LINEAGE_BINDING_MISMATCH"
    ):
        validate_run_group_v3(run_dir=run_dir, require_formal=False)


def test_missing_failure_record_and_ocr_count_drift_are_rejected(
    development_run: Path,
    validated_dataset,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scorer_v3, "_validate_dataset_v3", lambda **_: validated_dataset)
    failure_run = _copy_run(development_run, tmp_path / "failure")
    failure_path = failure_run / "failure_records.jsonl"
    rows = failure_path.read_text(encoding="utf-8").splitlines()
    failure_path.write_text("\n".join(rows[1:]) + ("\n" if len(rows) > 1 else ""), encoding="utf-8")
    _refresh_index_and_manifest(failure_run, "failure_records.jsonl")
    with pytest.raises(P5V3ScoringError, match="FAILURE_RECORD_EXACT_BINDING_INVALID"):
        validate_run_group_v3(run_dir=failure_run, require_formal=False)

    ocr_run = _copy_run(development_run, tmp_path / "ocr")
    manifest = json.loads((ocr_run / "run_group_manifest.json").read_text(encoding="utf-8"))
    manifest["ocr_replay_provenance"]["lookup_count"] += 1
    _rehash_manifest(ocr_run, manifest)
    with pytest.raises(P5V3ScoringError, match="RUN_OCR_REPLAY_COUNTS_INVALID"):
        validate_run_group_v3(run_dir=ocr_run, require_formal=False)


def test_artifact_path_escape_and_runspec_whitelist_drift_are_rejected(
    development_run: Path,
    validated_dataset,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scorer_v3, "_validate_dataset_v3", lambda **_: validated_dataset)
    path_run = _copy_run(development_run, tmp_path / "path")
    manifest = json.loads((path_run / "run_group_manifest.json").read_text(encoding="utf-8"))
    manifest["case_results_path"] = "../case_results.jsonl"
    _rehash_manifest(path_run, manifest)
    with pytest.raises(P5V3ScoringError, match="RUN_ARTIFACT_PATH_INVALID"):
        validate_run_group_v3(run_dir=path_run, require_formal=False)

    spec_run = _copy_run(development_run, tmp_path / "spec")
    manifest = json.loads((spec_run / "run_group_manifest.json").read_text(encoding="utf-8"))
    manifest["run_specs"]["core_b"]["random_seed"] += 1
    _rehash_manifest(spec_run, manifest)
    with pytest.raises(P5V3ScoringError, match="RUN_SPEC_SCHEMA_OR_WHITELIST_INVALID"):
        validate_run_group_v3(run_dir=spec_run, require_formal=False)


def test_artifact_index_generated_by_is_revalidated(
    development_run: Path,
    validated_dataset,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scorer_v3, "_validate_dataset_v3", lambda **_: validated_dataset)
    run_dir = _copy_run(development_run, tmp_path)
    index_path = run_dir / "artifact_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["entries"][0]["generated_by"] = "untrusted.writer"
    index["artifact_index_hash"] = digest(
        {key: value for key, value in index.items() if key != "artifact_index_hash"}
    )
    _write_json(index_path, index)
    manifest = json.loads((run_dir / "run_group_manifest.json").read_text(encoding="utf-8"))
    manifest["artifact_index_hash"] = index["artifact_index_hash"]
    _rehash_manifest(run_dir, manifest)
    with pytest.raises(P5V3ScoringError, match="RUN_ARTIFACT_INDEX_ENTRY_INVALID"):
        validate_run_group_v3(run_dir=run_dir, require_formal=False)
