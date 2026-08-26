from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

import evals.trip_check_v1.p5.final_blind_scorer as blind_v1
import evals.trip_check_v1.p5.final_blind_scorer_v2 as blind_v2
import scripts.run_trip_check_p5_eval as v1_runner
import scripts.run_trip_check_p5_gate as v1_gate
import scripts.score_trip_check_p5_eval as v1_scorer
from evals.trip_check_v1.p5.active_contract import P5ContractNotReadyError
from evals.trip_check_v1.p5.contracts_v2 import P5TerminalOutputV2
from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest
from evals.trip_check_v1.p5.final_blind_scorer_v2 import P5BlindScoringErrorV2
from evals.trip_check_v1.p5.scorer_v2 import (
    P5V2ScoringError,
    score_case_v2,
    score_run_group_v2,
    semantic_output_hash_v2,
    variant_output_hashes_v2,
)
from tests.test_trip_check_p5_final_blind_scorer_v2 import (
    _fixture as _blind_fixture,
)
from tests.test_trip_check_p5_final_blind_scorer_v2 import _score as _blind_score
from tests.test_trip_check_p5_final_blind_scorer_v2 import _synchronize_commitments
from tests.test_trip_check_p5_scorer_v2 import (
    _case,
    _output,
    _spec,
    _write_run_group,
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )


def _rewrite_manifest(run_dir: Path, **updates: object) -> dict:
    path = run_dir / "run_group_manifest.json"
    manifest = _load(path)
    manifest.update(updates)
    manifest["manifest_hash"] = digest({key: value for key, value in manifest.items() if key != "manifest_hash"})
    _write(path, manifest)
    return manifest


def _replace_terminal_rows(run_dir: Path, rows: list[dict]) -> None:
    terminal_path = run_dir / "terminal_outputs.jsonl"
    _write_jsonl(terminal_path, rows)
    updates: dict[str, object] = {
        "terminal_outputs_file_sha256": hashlib.sha256(terminal_path.read_bytes()).hexdigest(),
        "terminal_outputs_content_sha256": digest(rows),
    }
    try:
        outputs = [P5TerminalOutputV2.model_validate(row) for row in rows]
    except ValueError:
        pass
    else:
        updates["variant_output_sha256"] = variant_output_hashes_v2(outputs)
    artifact_index_path = run_dir / "artifact_index.json"
    artifact_index = _load(artifact_index_path)
    terminal_entry = next(
        item for item in artifact_index["artifacts"] if item["path"] == terminal_path.name
    )
    terminal_entry["byte_size"] = terminal_path.stat().st_size
    terminal_entry["sha256"] = hashlib.sha256(terminal_path.read_bytes()).hexdigest()
    artifact_index["index_hash"] = digest(
        {key: value for key, value in artifact_index.items() if key != "index_hash"}
    )
    _write(artifact_index_path, artifact_index)
    _rewrite_manifest(run_dir, **updates)


def _score_run_group(fixture: tuple[Path, Path, Path, Path]) -> dict:
    run_dir, cases_path, materializations_path, dataset_path = fixture
    return score_run_group_v2(
        run_dir=run_dir,
        cases_path=cases_path,
        materializations_path=materializations_path,
        dataset_manifest_path=dataset_path,
        require_formal=False,
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("schema_version", "trip-check-p5-run-group-v1", "RUN_GROUP_MANIFEST_VERSION_INVALID"),
        ("status", "REJECT", "RUN_GROUP_CONTRACT_INVALID"),
        ("dirty_tree", True, "RUN_SPEC_BINDING_MISMATCH"),
        ("blind_labels_read", True, "RUN_GROUP_CONTRACT_INVALID"),
        ("external_api_calls", 1, "RUN_GROUP_CONTRACT_INVALID"),
        ("replay_executed", False, "RUN_GROUP_REPLAY_INVALID"),
    ],
)
def test_run_group_rejects_rehashed_contract_tamper(tmp_path: Path, field: str, value: object, reason: str) -> None:
    fixture = _write_run_group(tmp_path)
    _rewrite_manifest(fixture[0], **{field: value})

    with pytest.raises(P5V2ScoringError, match=reason):
        _score_run_group(fixture)


def test_run_group_rejects_unrehashed_manifest_and_extra_field(tmp_path: Path) -> None:
    fixture = _write_run_group(tmp_path)
    manifest_path = fixture[0] / "run_group_manifest.json"
    manifest = _load(manifest_path)
    manifest["terminal_count"] = 999
    _write(manifest_path, manifest)
    with pytest.raises(P5V2ScoringError, match="RUN_GROUP_MANIFEST_HASH_MISMATCH"):
        _score_run_group(fixture)

    manifest["attacker_note"] = "hidden detail"
    manifest["manifest_hash"] = digest({key: value for key, value in manifest.items() if key != "manifest_hash"})
    _write(manifest_path, manifest)
    with pytest.raises(P5V2ScoringError, match="RUN_GROUP_MANIFEST_FIELDS_INVALID"):
        _score_run_group(fixture)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extra"])
def test_run_group_rejects_missing_duplicate_or_extra_terminal_rows(tmp_path: Path, mutation: str) -> None:
    fixture = _write_run_group(tmp_path)
    terminal_path = fixture[0] / "terminal_outputs.jsonl"
    rows = [json.loads(line) for line in terminal_path.read_text(encoding="utf-8").splitlines()]
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(deepcopy(rows[0]))
    else:
        extra = deepcopy(rows[0])
        extra["case_id"] = "p5.dev.bj.injected"
        rows.append(extra)
    _replace_terminal_rows(fixture[0], rows)

    with pytest.raises(P5V2ScoringError, match="TERMINAL_OUTPUT_EXACT_KEY_SET_MISMATCH"):
        _score_run_group(fixture)


def test_run_group_rejects_terminal_schema_extra_and_stale_artifact_binding(tmp_path: Path) -> None:
    fixture = _write_run_group(tmp_path)
    terminal_path = fixture[0] / "terminal_outputs.jsonl"
    rows = [json.loads(line) for line in terminal_path.read_text(encoding="utf-8").splitlines()]
    rows[0]["oracle"] = {"leak": True}
    _replace_terminal_rows(fixture[0], rows)
    with pytest.raises(P5V2ScoringError, match="TERMINAL_OUTPUT_SCHEMA_INVALID"):
        _score_run_group(fixture)

    stale_root = tmp_path / "stale"
    stale_root.mkdir()
    fixture = _write_run_group(stale_root)
    terminal_path = fixture[0] / "terminal_outputs.jsonl"
    rows = [json.loads(line) for line in terminal_path.read_text(encoding="utf-8").splitlines()]
    rows[0]["provider_snapshot_hash"] = "0" * 64
    changed = P5TerminalOutputV2.model_validate(rows[0])
    semantic_hash = semantic_output_hash_v2(changed)
    rows[0]["semantic_output_hash"] = semantic_hash
    rows[0]["replay_hash"] = semantic_hash
    _replace_terminal_rows(fixture[0], rows)
    with pytest.raises(P5V2ScoringError, match="TERMINAL_ARTIFACT_BINDING_MISMATCH"):
        _score_run_group(fixture)


def test_run_group_rejects_path_escape_and_stale_case_materialization_bytes(tmp_path: Path) -> None:
    fixture = _write_run_group(tmp_path)
    run_dir, cases_path, materializations_path, _ = fixture
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes((run_dir / "terminal_outputs.jsonl").read_bytes())
    _rewrite_manifest(run_dir, terminal_outputs_path="../outside.jsonl")
    with pytest.raises(P5V2ScoringError, match="TERMINAL_OUTPUT_PATH_ESCAPE"):
        _score_run_group(fixture)

    case_tamper_root = tmp_path / "case-tamper"
    case_tamper_root.mkdir()
    fixture = _write_run_group(case_tamper_root)
    cases_path = fixture[1]
    cases_path.write_bytes(cases_path.read_bytes().replace(b"Beijing", b"Shanghai"))
    # The fixture is Chinese, so force a byte-level drift that remains parseable.
    cases_path.write_bytes(cases_path.read_bytes() + b"\n")
    with pytest.raises(P5V2ScoringError, match="RUN_GROUP_CASES_FILE_HASH_MISMATCH"):
        _score_run_group(fixture)

    materialization_tamper_root = tmp_path / "materialization-tamper"
    materialization_tamper_root.mkdir()
    fixture = _write_run_group(materialization_tamper_root)
    materializations_path = fixture[2]
    materializations_path.write_bytes(materializations_path.read_bytes() + b"\n")
    with pytest.raises(P5V2ScoringError, match="RUN_GROUP_MATERIALIZATIONS_FILE_HASH_MISMATCH"):
        _score_run_group(fixture)


def test_run_group_terminal_output_symlink_must_be_rejected(tmp_path: Path) -> None:
    fixture = _write_run_group(tmp_path)
    run_dir = fixture[0]
    link = run_dir / "terminal_outputs_link.jsonl"
    try:
        os.symlink(run_dir / "terminal_outputs.jsonl", link)
    except OSError:
        pytest.skip("this Windows host does not permit symlink creation")
    _rewrite_manifest(run_dir, terminal_outputs_path=link.name)

    with pytest.raises(P5V2ScoringError, match="TERMINAL_OUTPUT_SYMLINK_FORBIDDEN"):
        _score_run_group(fixture)


def _rebind_bundle_hash(fixture: dict, payload: dict) -> str:
    fixture["bundle"].write_bytes(canonical_bytes(payload) + b"\n")
    changed_hash = hashlib.sha256(fixture["bundle"].read_bytes()).hexdigest()
    seal = _load(fixture["seal"])
    seal["external_bundle_sha256"] = changed_hash
    _write(fixture["seal"], seal)
    _synchronize_commitments(fixture)
    return changed_hash


def test_blind_bundle_rejects_path_escape_symlink_and_conflicting_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _blind_fixture(tmp_path, monkeypatch)
    with pytest.raises(P5BlindScoringErrorV2, match="BLIND_BUNDLE_PATH_ESCAPE"):
        _blind_score(fixture, bundle_path=Path("relative-bundle.json"))
    escaped = fixture["bundle"].parent / ".." / fixture["bundle"].parent.name / fixture["bundle"].name
    with pytest.raises(P5BlindScoringErrorV2, match="BLIND_BUNDLE_PATH_ESCAPE"):
        _blind_score(fixture, bundle_path=escaped)
    with pytest.raises(P5BlindScoringErrorV2, match="BLIND_BUNDLE_SOURCE_REQUIRED"):
        _blind_score(fixture, bundle_bytes=fixture["bundle"].read_bytes())

    link = tmp_path / "bundle-link.json"
    try:
        os.symlink(fixture["bundle"], link)
    except OSError:
        pytest.skip("this Windows host does not permit symlink creation")
    with pytest.raises(P5BlindScoringErrorV2, match="BLIND_BUNDLE_SYMLINK_FORBIDDEN"):
        _blind_score(fixture, bundle_path=link)


def test_blind_bundle_parent_junction_must_be_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _blind_fixture(tmp_path, monkeypatch)
    junction = fixture["repo"] / "external-bundle-junction"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(fixture["bundle"].parent)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("this Windows host does not permit junction creation")
    attacked_path = junction / fixture["bundle"].name
    with pytest.raises(P5BlindScoringErrorV2, match="BLIND_BUNDLE_SYMLINK_FORBIDDEN"):
        _blind_score(fixture, bundle_path=attacked_path)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("bundle_extra", "BLIND_BUNDLE_EXTRA_OR_MISSING_FIELDS"),
        ("duplicate_label", "BLIND_BUNDLE_LABEL_DUPLICATE"),
        ("label_commitment", "BLIND_LABEL_COMMITMENT_MISMATCH"),
    ],
)
def test_blind_bundle_rejects_rehashed_schema_case_set_and_label_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    reason: str,
) -> None:
    fixture = _blind_fixture(tmp_path, monkeypatch)
    payload = _load(fixture["bundle"])
    if mutation == "bundle_extra":
        payload["case_scores"] = [{"case_id": "leak"}]
    elif mutation == "duplicate_label":
        payload["labels"][0]["case_id"] = payload["labels"][1]["case_id"]
    else:
        payload["labels"][0]["oracle"]["advice_required"] = not payload["labels"][0]["oracle"]["advice_required"]
    changed_hash = _rebind_bundle_hash(fixture, payload)

    with pytest.raises(P5BlindScoringErrorV2, match=reason):
        _blind_score(fixture, expected_bundle_sha256=changed_hash)


def test_blind_seal_inputs_materializations_template_and_schema_are_byte_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _blind_fixture(tmp_path, monkeypatch)
    seal = _load(fixture["seal"])
    seal["case_scores"] = []
    _write(fixture["seal"], seal)
    with pytest.raises(P5BlindScoringErrorV2, match="BLIND_SEAL_EXTRA_OR_MISSING_FIELDS"):
        _blind_score(fixture)

    inputs_root = tmp_path / "inputs"
    inputs_root.mkdir()
    fixture = _blind_fixture(inputs_root, monkeypatch)
    fixture["inputs"].write_bytes(fixture["inputs"].read_bytes() + b"\n")
    with pytest.raises(P5BlindScoringErrorV2, match="BLIND_INPUT_FILE_HASH_MISMATCH"):
        _blind_score(fixture)

    materializations_root = tmp_path / "materializations"
    materializations_root.mkdir()
    fixture = _blind_fixture(materializations_root, monkeypatch)
    fixture["materializations"].write_bytes(fixture["materializations"].read_bytes() + b"\n")
    with pytest.raises(P5BlindScoringErrorV2, match="BLIND_MATERIALIZATION_FILE_HASH_MISMATCH"):
        _blind_score(fixture)

    template_root = tmp_path / "template"
    template_root.mkdir()
    fixture = _blind_fixture(template_root, monkeypatch)
    fixture["template"].write_bytes(fixture["template"].read_bytes() + b"\n")
    with pytest.raises(P5BlindScoringErrorV2, match="BLIND_RUN_SPEC_TEMPLATE_HASH_MISMATCH"):
        _blind_score(fixture)

    schema_root = tmp_path / "schema"
    schema_root.mkdir()
    fixture = _blind_fixture(schema_root, monkeypatch)
    schema = fixture["repo"] / blind_v2.SCHEMA_CONTRACT_PATHS_V2[0]
    schema.write_bytes(schema.read_bytes() + b"\n")
    with pytest.raises(P5BlindScoringErrorV2, match="BLIND_SCHEMA_CONTRACT_MISMATCH"):
        _blind_score(fixture)


def test_candidate_outside_set_or_missing_place_route_receipts_must_fail() -> None:
    case = _case()
    output = _output(case, _spec(case, "core_b", "e" * 64))
    payload = output.model_dump(mode="json")
    payload["evaluation_projection"]["selected_place_ids"] = ["outside-frozen-candidate-set"]
    payload["evaluation_projection"]["candidate_receipt_coverage"] = 1.0
    assert not any(receipt.get("type") in {"place_receipt", "route_receipt"} for receipt in payload["receipts"])
    changed = P5TerminalOutputV2.model_validate(payload)
    semantic_hash = semantic_output_hash_v2(changed)
    changed = changed.model_copy(update={"semantic_output_hash": semantic_hash, "replay_hash": semantic_hash})

    score = score_case_v2(case, changed)
    assert score.candidate_receipt_coverage == "FAIL"
    assert score.task_success is False


def test_forged_concurrency_receipt_must_not_prove_real_execution() -> None:
    case = _case()
    output = _output(case, _spec(case, "core_b", "e" * 64))
    payload = output.model_dump(mode="json")
    payload["receipts"] = [
        {
            "schema_version": "trip-check-p5-apply-fault-receipt-v2",
            "status": "PASS",
            "fault_profile_id": "duplicate_apply",
            "semantic_projection": {
                "outcome_counts": {"APPLIED": 1, "IDEMPOTENT_REPLAY": 1},
                "all_invariants_passed": True,
            },
            "attacker_authored": True,
        }
    ]
    changed = P5TerminalOutputV2.model_validate(payload)
    semantic_hash = semantic_output_hash_v2(changed)
    changed = changed.model_copy(update={"semantic_output_hash": semantic_hash, "replay_hash": semantic_hash})

    score = score_case_v2(case, changed)
    assert score.concurrency_result == "FAIL"
    assert score.task_success is False


def test_unknown_finding_relabelled_pass_without_source_status_must_fail() -> None:
    case = _case()
    output = _output(case, _spec(case, "core_b", "e" * 64))
    payload = output.model_dump(mode="json")
    payload["findings"] = [{"reason_code": "TIME_CHAIN_CONFLICT", "status": "PASS"}]
    payload["evaluation_projection"]["unknown_preserved"] = True
    changed = P5TerminalOutputV2.model_validate(payload)
    semantic_hash = semantic_output_hash_v2(changed)
    changed = changed.model_copy(update={"semantic_output_hash": semantic_hash, "replay_hash": semantic_hash})

    score = score_case_v2(case, changed)
    assert score.unknown_preservation == "FAIL"
    assert score.task_success is False


def test_v1_formal_runner_must_remain_superseded_after_v2_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(v1_runner, "_git", lambda *args: "a" * 40 if args[-1] == "HEAD" else "")

    def proceeded(_lane: str) -> list[dict]:
        raise AssertionError("superseded v1 runner proceeded past its formal guard")

    monkeypatch.setattr(v1_runner, "_load_cases", proceeded)
    args = SimpleNamespace(require_formal=True, allow_dirty=False, lane="nonblind")
    with pytest.raises(P5ContractNotReadyError, match="P5_V1_FORMAL_CONTRACT_SUPERSEDED"):
        asyncio.run(v1_runner._execute(args))


def test_v1_formal_scorer_must_remain_superseded_after_v2_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def proceeded(**_kwargs: object) -> dict:
        raise AssertionError("superseded v1 scorer proceeded past its formal guard")

    monkeypatch.setattr(v1_scorer, "score_run_group", proceeded)
    monkeypatch.setattr(sys, "argv", ["score-v1", "--run-dir", str(tmp_path)])
    with pytest.raises(P5ContractNotReadyError, match="P5_V1_FORMAL_CONTRACT_SUPERSEDED"):
        v1_scorer.main()


def test_v1_blind_scorer_must_remain_superseded_after_v2_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(blind_v1.P5BlindScoringError, match="P5_V1_FORMAL_CONTRACT_SUPERSEDED"):
        blind_v1.score_external_blind_run_group(
            repo_root=tmp_path,
            run_dir=tmp_path / "missing-run",
            expected_bundle_sha256="0" * 64,
            bundle_bytes=b"{}",
            require_current_subject=True,
        )


def test_v1_gate_must_remain_superseded_after_v2_is_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def proceeded(**_kwargs: object) -> dict:
        raise AssertionError("superseded v1 Gate proceeded past its formal guard")

    monkeypatch.setattr(v1_gate, "build_p5_gate_manifest", proceeded)
    args = [
        "gate-v1",
        "--nonblind-run-dir",
        str(tmp_path),
        "--nonblind-score",
        str(tmp_path / "nonblind.json"),
        "--blind-run-dir",
        str(tmp_path),
        "--blind-score",
        str(tmp_path / "blind.json"),
        "--judge-panel",
        str(tmp_path / "judge.json"),
    ]
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(P5ContractNotReadyError, match="P5_V1_FORMAL_CONTRACT_SUPERSEDED"):
        v1_gate.main()
