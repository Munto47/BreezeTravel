from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError
from pydantic import ValidationError as PydanticValidationError

from evals.trip_check_v1.p5.blind_custody_v2 import (
    BLIND_INPUT_RELATIVE,
    BLIND_MATERIALIZATIONS_RELATIVE,
    BUNDLE_SCHEMA_RELATIVE,
    DATASET_MANIFEST_RELATIVE,
    REVIEW_SCHEMA_RELATIVE,
    RUBRIC_RELATIVE,
    RUN_SPEC_RELATIVE,
    BlindLabelBundleV2,
    build_blind_label_bundle_v2,
    derive_all_blind_labels_v2,
    review_blind_label_bundle_v2,
)
from evals.trip_check_v1.p5.data_contract import canonical_bytes, load_jsonl
from evals.trip_check_v1.p5.final_blind_scorer_v2 import SCHEMA_CONTRACT_PATHS_V2


SOURCE_ROOT = Path(__file__).resolve().parents[2]
SUBJECT_COMMIT = "5b79fc0c7ee8ab45f8145361aab8d715f4885878"


def _copy_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    relative_paths = {
        BLIND_INPUT_RELATIVE,
        BLIND_MATERIALIZATIONS_RELATIVE,
        DATASET_MANIFEST_RELATIVE,
        RUN_SPEC_RELATIVE,
        RUBRIC_RELATIVE,
        BUNDLE_SCHEMA_RELATIVE,
        REVIEW_SCHEMA_RELATIVE,
        *(Path(value) for value in SCHEMA_CONTRACT_PATHS_V2),
    }
    for relative in relative_paths:
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE_ROOT / relative, destination)
    return repo


def _build(tmp_path: Path) -> tuple[Path, Path, dict]:
    repo = _copy_repo(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    bundle_path = external / "blind.bundle.v2.json"
    result = build_blind_label_bundle_v2(repo_root=repo, external_output_path=bundle_path)
    return repo, bundle_path, result


def _review(repo: Path, bundle_path: Path, receipt_path: Path) -> dict:
    return review_blind_label_bundle_v2(
        repo_root=repo,
        external_bundle_path=bundle_path,
        external_receipt_path=receipt_path,
        candidate_subject_commit=SUBJECT_COMMIT,
    )


def test_custodian_derives_exact_frozen_coverage_and_reviewer_recomputes_it(tmp_path: Path) -> None:
    repo, bundle_path, result = _build(tmp_path)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert bundle_path.read_bytes() == canonical_bytes(bundle) + b"\n"
    assert result["case_count"] == 90
    assert len(bundle["labels"]) == 90
    assert len({item["case_id"] for item in bundle["labels"]}) == 90

    oracles = [item["oracle"] for item in bundle["labels"]]
    assert sum(item["unknown_must_be_preserved"] for item in oracles) == 18
    assert Counter(item["candidate_receipt_mode"] for item in oracles) == {
        "REQUIRED": 10,
        "FORBIDDEN": 20,
        "NOT_APPLICABLE": 60,
    }
    assert Counter(item["concurrency_expectation"] for item in oracles) == {
        "NONE": 70,
        "IDEMPOTENT_REPLAY": 10,
        "SINGLE_WINNER": 10,
    }

    review_result = _review(repo, bundle_path, tmp_path / "external" / "review.json")
    receipt = json.loads(Path(review_result["path"]).read_text(encoding="utf-8"))
    assert receipt["candidate_subject_commit"] == SUBJECT_COMMIT
    assert receipt["checks"] == {
        "binding_recomputed": True,
        "candidate_output_dependency_count": 0,
        "candidate_set_mode_counts": {
            "EMPTY": 10,
            "MISSING_RECEIPT": 10,
            "NOT_APPLICABLE": 60,
            "VALID": 10,
        },
        "case_count": 90,
        "case_set_exact": True,
        "concurrency_case_count": 20,
        "network_api_calls": 0,
        "oracle_exact": True,
        "privacy_findings_count": 0,
        "unknown_preserved_count": 18,
    }


def test_bundle_and_review_models_reject_additional_properties(tmp_path: Path) -> None:
    repo, bundle_path, _result = _build(tmp_path)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    with pytest.raises(PydanticValidationError):
        BlindLabelBundleV2.model_validate({**bundle, "candidate_outputs": []})
    label = dict(bundle["labels"][0])
    label["expected"] = "forbidden"
    with pytest.raises(PydanticValidationError):
        BlindLabelBundleV2.model_validate({**bundle, "labels": [label, *bundle["labels"][1:]]})

    receipt_path = tmp_path / "external" / "review.json"
    _review(repo, bundle_path, receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["extra"] = True
    schema = json.loads((repo / REVIEW_SCHEMA_RELATIVE).read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(receipt)


def test_reviewer_rejects_tampered_oracle_even_when_recanonicalized(tmp_path: Path) -> None:
    repo, bundle_path, _result = _build(tmp_path)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["labels"][0]["oracle"]["unknown_must_be_preserved"] = False
    attacked = tmp_path / "external" / "attacked.json"
    attacked.write_bytes(canonical_bytes(bundle) + b"\n")
    with pytest.raises(ValueError, match="independently derived"):
        _review(repo, attacked, tmp_path / "external" / "review.json")


@pytest.mark.parametrize("relative", [BLIND_INPUT_RELATIVE, BLIND_MATERIALIZATIONS_RELATIVE])
def test_stale_input_or_materialization_binding_fails_closed(tmp_path: Path, relative: Path) -> None:
    repo = _copy_repo(tmp_path)
    path = repo / relative
    rows = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[0])
    if relative == BLIND_INPUT_RELATIVE:
        payload["group_size"] = 5 if payload["group_size"] != 5 else 4
    else:
        payload["materialization_id"] += "-tampered"
    rows[0] = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    external = tmp_path / "external"
    external.mkdir()
    with pytest.raises(ValueError, match="stale blind artifact binding"):
        build_blind_label_bundle_v2(
            repo_root=repo,
            external_output_path=external / "bundle.json",
        )


def test_external_output_rejects_relative_path_repo_path_and_existing_file(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        build_blind_label_bundle_v2(repo_root=repo, external_output_path=Path("bundle.json"))
    with pytest.raises(ValueError, match="outside the repository"):
        build_blind_label_bundle_v2(
            repo_root=repo,
            external_output_path=repo / "bundle.json",
        )
    external = tmp_path / "external"
    external.mkdir()
    destination = external / "bundle.json"
    destination.write_text("occupied", encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        build_blind_label_bundle_v2(repo_root=repo, external_output_path=destination)


def test_external_output_rejects_symlink_parent(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    real = tmp_path / "real-external"
    real.mkdir()
    link = tmp_path / "external-link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit directory symlinks")
    with pytest.raises(ValueError, match="symlink or junction"):
        build_blind_label_bundle_v2(repo_root=repo, external_output_path=link / "bundle.json")


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-specific")
def test_external_output_rejects_junction_parent(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    real = tmp_path / "real-junction-target"
    real.mkdir()
    junction = tmp_path / "external-junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(real)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("host does not permit junction creation")
    try:
        with pytest.raises(ValueError, match="symlink or junction"):
            build_blind_label_bundle_v2(
                repo_root=repo,
                external_output_path=junction / "bundle.json",
            )
    finally:
        junction.rmdir()


def test_derivation_has_no_candidate_output_or_run_output_parameter(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    inputs = load_jsonl(repo / BLIND_INPUT_RELATIVE)
    materializations = load_jsonl(repo / BLIND_MATERIALIZATIONS_RELATIVE)
    labels = derive_all_blind_labels_v2(inputs, materializations)
    assert len(labels) == 90
    forbidden = {"candidate_outputs", "terminal_outputs", "run_dir", "run_manifest", "outputs_path"}
    for callable_value in (
        derive_all_blind_labels_v2,
        build_blind_label_bundle_v2,
        review_blind_label_bundle_v2,
    ):
        assert forbidden.isdisjoint(inspect.signature(callable_value).parameters)
