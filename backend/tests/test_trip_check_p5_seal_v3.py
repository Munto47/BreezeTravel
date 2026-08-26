from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.contracts_v3 import P5BlindSealV3
from evals.trip_check_v1.p5.data_contract import file_sha256
from evals.trip_check_v1.p5.data_contract_v3 import BLIND_SEAL_PATH_V3
from evals.trip_check_v1.p5.seal_v3 import (
    P5V3SealError,
    SealPathsV3,
    _preflight_git,
    _validate_external_truth_v2,
    build_blind_seal_v3,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
COMMIT = "a" * 40
HASHES = {
    "labels_canonical_sha256": "b" * 64,
    "external_bundle_sha256": "c" * 64,
    "review_receipt_sha256": "d" * 64,
    "source_v2_blind_seal_file_sha256": "e" * 64,
}


def test_blind_seal_binds_four_dataset_files_contracts_and_unchanged_v2_truth() -> None:
    paths = SealPathsV3.for_repo(REPO_ROOT)
    seal = build_blind_seal_v3(
        paths=paths,
        candidate_freeze_commit=COMMIT,
        candidate_manifest_hash="f" * 64,
        custody_commitments=HASHES,
    )

    assert P5BlindSealV3.model_validate(seal).model_dump(mode="json") == seal
    assert seal["case_count"] == 90
    assert seal["nonblind_cases_file_sha256"] == file_sha256(paths.nonblind_cases_path)
    assert seal["nonblind_materializations_file_sha256"] == file_sha256(
        paths.nonblind_materializations_path
    )
    assert seal["inputs_file_sha256"] == file_sha256(paths.blind_inputs_path)
    assert seal["materializations_file_sha256"] == file_sha256(
        paths.blind_materializations_path
    )
    assert seal["source_truth_contract"] == (
        "trip-check-p5-blind-label-bundle-v2-unchanged"
    )
    assert seal["scoring_payload_present"] is False
    assert seal["human_evidence"] is False
    assert "labels" not in seal
    assert "oracle" not in json.dumps(seal, sort_keys=True)


@pytest.mark.parametrize(
    ("head", "dirty", "upstream_head", "reason"),
    [
        ("9" * 40, "", COMMIT, "P5_V3_CANDIDATE_HEAD_NOT_EXACT"),
        (COMMIT, " M candidate", COMMIT, "P5_V3_DIRTY_TREE_FORBIDDEN"),
        (COMMIT, "", "8" * 40, "P5_V3_UPSTREAM_NOT_SYNCHRONIZED"),
    ],
)
def test_git_preflight_requires_exact_clean_pushed_candidate(
    head: str, dirty: str, upstream_head: str, reason: str
) -> None:
    paths = SealPathsV3.for_repo(REPO_ROOT)

    def fake_git(_root: Path, arguments: tuple[str, ...]) -> str:
        if arguments == ("rev-parse", "HEAD"):
            return head
        if arguments == ("status", "--porcelain"):
            return dirty
        if arguments[-1] == "@{upstream}":
            return "origin/codex/p5-v3-blind-seal"
        if arguments == ("rev-parse", "origin/codex/p5-v3-blind-seal"):
            return upstream_head
        raise AssertionError(arguments)

    with pytest.raises(P5V3SealError, match=reason):
        _preflight_git(
            paths=paths,
            candidate_freeze_commit=COMMIT,
            git_output=fake_git,
        )


def test_git_preflight_accepts_exact_clean_pushed_candidate() -> None:
    paths = SealPathsV3.for_repo(REPO_ROOT)

    def fake_git(_root: Path, arguments: tuple[str, ...]) -> str:
        if arguments == ("rev-parse", "HEAD"):
            return COMMIT
        if arguments == ("status", "--porcelain"):
            return ""
        if arguments[-1] == "@{upstream}":
            return "origin/codex/p5-v3-blind-seal"
        if arguments == ("rev-parse", "origin/codex/p5-v3-blind-seal"):
            return COMMIT
        raise AssertionError(arguments)

    _preflight_git(
        paths=paths,
        candidate_freeze_commit=COMMIT,
        git_output=fake_git,
    )


def test_external_truth_rebind_returns_only_commitments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = SealPathsV3.for_repo(REPO_ROOT)
    bundle = tmp_path / "bundle.json"
    review = tmp_path / "review.json"
    bundle.write_text("{}", encoding="utf-8")
    review.write_text("{}", encoding="utf-8")
    source_seal = json.loads(paths.source_v2_seal_path.read_text(encoding="utf-8"))
    blind_case_ids = tuple(
        sorted(
            row["case_id"]
            for row in (
                json.loads(line)
                for line in paths.blind_inputs_path.read_text(encoding="utf-8").splitlines()
                if line
            )
        )
    )

    monkeypatch.setattr(
        "evals.trip_check_v1.p5.seal_v3.validate_v2_source_anchor",
        lambda: {"candidate_freeze_commit": "1" * 40},
    )
    monkeypatch.setattr(
        "evals.trip_check_v1.p5.seal_v3.expected_blind_dataset_binding_v2",
        lambda _root: ({"case_count": 90}, blind_case_ids),
    )
    monkeypatch.setattr(
        "evals.trip_check_v1.p5.seal_v3.validate_external_blind_bundle_v2",
        lambda **_kwargs: {
            "bundle_byte_sha256": source_seal["external_bundle_sha256"],
            "bundle_canonical_sha256": "2" * 64,
            "labels_canonical_sha256": source_seal["labels_canonical_sha256"],
        },
    )
    monkeypatch.setattr(
        "evals.trip_check_v1.p5.seal_v3.validate_external_blind_review_receipt_v2",
        lambda **_kwargs: {
            "review_receipt_sha256": source_seal["review_receipt_sha256"],
            "bundle_byte_sha256": source_seal["external_bundle_sha256"],
            "labels_canonical_sha256": source_seal["labels_canonical_sha256"],
        },
    )

    result = _validate_external_truth_v2(
        paths=paths,
        external_bundle_path=bundle,
        external_review_receipt_path=review,
    )

    assert set(result) == {
        "labels_canonical_sha256",
        "external_bundle_sha256",
        "review_receipt_sha256",
        "source_v2_blind_seal_file_sha256",
    }
    assert result["source_v2_blind_seal_file_sha256"] == file_sha256(
        paths.source_v2_seal_path
    )


def test_checked_in_v3_seal_never_contains_scoring_payload() -> None:
    if not BLIND_SEAL_PATH_V3.exists():
        return
    seal = P5BlindSealV3.model_validate_json(
        BLIND_SEAL_PATH_V3.read_text(encoding="utf-8")
    )
    assert seal.scoring_payload_present is False
    assert seal.human_evidence is False
