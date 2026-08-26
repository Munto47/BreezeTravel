from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.trip_check_v1.p5.final_blind_scorer_v4 import (
    P5BlindScoringErrorV4,
    _default_case_scorer,
    canonical_labels_hash_v4,
    score_isolated_blind_v4,
)
from evals.trip_check_v1.p5.runner_v4 import BlindDatasetPathsV4, VARIANT_IDS_V4


class _Reader:
    role = "blind_custodian"

    def __init__(self, payload: bytes, manifest_hash: str) -> None:
        self.payload = payload
        self.run_group_manifest_hash = manifest_hash
        self.bundle_sha256 = hashlib.sha256(payload).hexdigest()
        self.read_count = 0

    def read_committed_bundle(self, *, expected_sha256: str, repo_root: Path) -> bytes:
        del repo_root
        assert expected_sha256 == self.bundle_sha256
        self.read_count += 1
        return self.payload


def _score(case, output, oracle, materialization):
    del case, oracle, materialization
    return {
        "variant_id": output["variant_id"],
        "task_success": True,
        "score": 100.0,
        "deterministic_pass": True,
        "wrong_city_or_poi_count": 0,
        "missing_reason_codes": [],
        "unknown_preservation": "PASS",
        "candidate_receipt_coverage": "PASS",
        "concurrency_result": "PASS",
        "repair_postcheck": "PASS",
        "replay_hash_match": True,
        "nonpass_finding_count": 1,
        "covered_nonpass_finding_count": 1,
        "unsupported_claim_count": 0,
        "usage_measurement": "PASS",
        "token_count": 0,
        "cost_usd": 0.0,
    }


def _fixture(
    tmp_path: Path,
    *,
    candidate_mode: str = "VALID",
    oracle_overrides: dict | None = None,
) -> tuple[dict, _Reader, BlindDatasetPathsV4, callable]:
    cases = [
        {
            "case_id": f"blind-{index:03d}",
            "runner_control": {"candidate_set_mode": candidate_mode},
        }
        for index in range(90)
    ]
    outputs = [
        {
            "case_id": case["case_id"],
            "variant_id": variant_id,
            "latency_ms": 1.0,
            "terminal_status": "SUCCEEDED",
        }
        for variant_id in VARIANT_IDS_V4
        for case in cases
    ]
    blocked = candidate_mode in {"EMPTY", "MISSING_RECEIPT"}
    oracle = {
        "requires_user_resolution": blocked,
        "candidate_receipt_mode": (
            "REQUIRED"
            if candidate_mode == "VALID"
            else "NOT_APPLICABLE"
            if candidate_mode == "NOT_APPLICABLE"
            else "FORBIDDEN"
        ),
        "specific_place_allowed": not blocked,
        **(oracle_overrides or {}),
    }
    labels = [
        {
            "schema_version": "trip-check-p5-blind-label-v2",
            "case_id": case["case_id"],
            "oracle": oracle,
        }
        for case in reversed(cases)
    ]
    bundle = {
        "schema_version": "trip-check-p5-blind-label-bundle-v2",
        "evidence_class": "controlled_blind_oracle",
        "human_evidence": False,
        "dataset_binding": {"source": "sealed-v2"},
        "labels": labels,
    }
    payload = json.dumps(bundle, sort_keys=True).encode()
    manifest = {
        "manifest_hash": "a" * 64,
        "subject_commit": "b" * 40,
        "dataset_manifest_hash": "c" * 64,
        "terminal_outputs_file_sha256": "d" * 64,
        "terminal_outputs_content_sha256": "e" * 64,
        "artifact_index_hash": "f" * 64,
        "blind_seal_sha256": "1" * 64,
        "run_spec_template_sha256": "2" * 64,
    }
    reader = _Reader(payload, manifest["manifest_hash"])
    seal = tmp_path / "seal.json"
    seal.write_text(
        json.dumps(
            {
                "external_bundle_sha256": reader.bundle_sha256,
                "labels_canonical_sha256": canonical_labels_hash_v4(labels),
            }
        ),
        encoding="utf-8",
    )
    paths = BlindDatasetPathsV4(
        inputs=tmp_path / "unused-inputs",
        materializations=tmp_path / "unused-materializations",
        manifest=tmp_path / "unused-manifest",
        seal=seal,
        run_spec_template=tmp_path / "unused-template",
        rubric=tmp_path / "unused-rubric",
        active_contract=tmp_path / "unused-active",
    )

    def validator(**kwargs):
        del kwargs
        return manifest, cases, outputs, {
            case["case_id"]: {
                "source_payload": {
                    "entity_resolutions": [{"outcome": "AUTO_RESOLVED"}]
                }
            }
            for case in cases
        }

    return manifest, reader, paths, validator


def test_v4_scorer_returns_only_variant_aggregates_without_label_side_channels(
    tmp_path: Path,
) -> None:
    manifest, reader, paths, validator = _fixture(tmp_path)
    report = score_isolated_blind_v4(
        repo_root=tmp_path / "repo",
        run_dir=tmp_path / "run",
        expected_bundle_sha256=reader.bundle_sha256,
        custodian_reader=reader,
        dataset_paths=paths,
        run_validator=validator,
        oracle_validator=lambda value: value,
        case_scorer=_score,
    )

    assert report["status"] == "PASS"
    assert report["bindings"]["run_group_manifest_hash"] == manifest["manifest_hash"]
    assert report["case_count"] == 90
    assert report["terminal_count"] == 270
    assert report["replay_readback_count"] == 270
    assert set(report["variant_metrics"]) == set(VARIANT_IDS_V4)
    assert all(metrics["case_count"] == 90 for metrics in report["variant_metrics"].values())
    serialized = json.dumps(report, sort_keys=True)
    assert reader.read_count == 1
    assert all(case_id not in serialized for case_id in ("blind-000", "blind-089"))
    assert all(token not in serialized for token in ('"labels"', '"oracle"', '"case_id"', '"buckets"'))


def test_v4_scorer_rejects_non_custodian_before_bundle_read(tmp_path: Path) -> None:
    _manifest, reader, paths, validator = _fixture(tmp_path)
    reader.role = "developer"  # type: ignore[misc]

    with pytest.raises(P5BlindScoringErrorV4, match="CUSTODIAN_AUTHORIZATION_BINDING_MISMATCH"):
        score_isolated_blind_v4(
            repo_root=tmp_path / "repo",
            run_dir=tmp_path / "run",
            expected_bundle_sha256=reader.bundle_sha256,
            custodian_reader=reader,
            dataset_paths=paths,
            run_validator=validator,
            oracle_validator=lambda value: value,
            case_scorer=_score,
        )

    assert reader.read_count == 0


@pytest.mark.parametrize(
    "candidate_mode",
    ["VALID", "EMPTY", "MISSING_RECEIPT", "NOT_APPLICABLE"],
)
def test_v4_scorer_accepts_each_compatible_oracle_payload_mode(
    tmp_path: Path,
    candidate_mode: str,
) -> None:
    _manifest, reader, paths, validator = _fixture(
        tmp_path,
        candidate_mode=candidate_mode,
    )

    report = score_isolated_blind_v4(
        repo_root=tmp_path / "repo",
        run_dir=tmp_path / "run",
        expected_bundle_sha256=reader.bundle_sha256,
        custodian_reader=reader,
        dataset_paths=paths,
        run_validator=validator,
        oracle_validator=lambda value: value,
        case_scorer=_score,
    )

    assert report["status"] == "PASS"
    assert reader.read_count == 1


@pytest.mark.parametrize(
    ("candidate_mode", "oracle_overrides"),
    [
        ("VALID", {"requires_user_resolution": True}),
        ("VALID", {"candidate_receipt_mode": "FORBIDDEN"}),
        ("VALID", {"specific_place_allowed": False}),
        ("EMPTY", {"candidate_receipt_mode": "REQUIRED"}),
        ("EMPTY", {"specific_place_allowed": True}),
        ("MISSING_RECEIPT", {"candidate_receipt_mode": "REQUIRED"}),
        ("MISSING_RECEIPT", {"specific_place_allowed": True}),
        ("NOT_APPLICABLE", {"candidate_receipt_mode": "FORBIDDEN"}),
        ("NOT_APPLICABLE", {"specific_place_allowed": False}),
    ],
)
def test_v4_scorer_rejects_semantic_mismatch_before_case_scorer_without_disclosure(
    tmp_path: Path,
    candidate_mode: str,
    oracle_overrides: dict,
) -> None:
    _manifest, reader, paths, validator = _fixture(
        tmp_path,
        candidate_mode=candidate_mode,
        oracle_overrides=oracle_overrides,
    )
    scorer_calls = 0

    def forbidden_scorer(*args, **kwargs):
        nonlocal scorer_calls
        del args, kwargs
        scorer_calls += 1
        return _score

    with pytest.raises(P5BlindScoringErrorV4) as caught:
        score_isolated_blind_v4(
            repo_root=tmp_path / "repo",
            run_dir=tmp_path / "run",
            expected_bundle_sha256=reader.bundle_sha256,
            custodian_reader=reader,
            dataset_paths=paths,
            run_validator=validator,
            oracle_validator=lambda value: value,
            case_scorer=forbidden_scorer,
        )

    assert caught.value.reason_code == "BLIND_ORACLE_PAYLOAD_SEMANTIC_MISMATCH"
    assert str(caught.value) == "BLIND_ORACLE_PAYLOAD_SEMANTIC_MISMATCH"
    assert reader.read_count == 1
    assert scorer_calls == 0


def test_v4_cli_emits_only_invalid_evidence_for_semantic_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from scripts import score_trip_check_p5_v4_blind as cli

    monkeypatch.setattr(
        cli.ExternalCustodianBundleReaderV4,
        "from_external_files",
        classmethod(lambda cls, **kwargs: object()),
    )

    def reject_semantics(**kwargs):
        del kwargs
        raise P5BlindScoringErrorV4("BLIND_ORACLE_PAYLOAD_SEMANTIC_MISMATCH")

    monkeypatch.setattr(cli, "score_isolated_blind_v4", reject_semantics)

    exit_code = cli.main(
        [
            "--run-dir",
            str(tmp_path / "run"),
            "--bundle",
            str(tmp_path / "bundle.json"),
            "--bundle-sha256",
            "a" * 64,
            "--custodian-authorization",
            str(tmp_path / "authorization.json"),
            "--output",
            str(tmp_path / "score.json"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "schema_version": "trip-check-p5-isolated-blind-score-error-v4",
        "status": "INVALID_EVIDENCE",
        "reason_code": "BLIND_ORACLE_PAYLOAD_SEMANTIC_MISMATCH",
        "human_evidence": False,
    }


def test_v4_default_case_scorer_reuses_v3_deterministic_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.trip_check_v1.p5 import nonblind_scorer_v4, scorer_v3

    projected = object()
    expected = object()
    oracle = object()
    materialization = {"receipt": "bound"}
    source_case = SimpleNamespace(case_id="blind-case")
    source_output = object()

    monkeypatch.setattr(
        nonblind_scorer_v4,
        "_project_terminal_to_v3",
        lambda value: projected if value is source_output else None,
    )

    def fake_score(case, output, *, materialization):
        assert case.case_id == "blind-case"
        assert case.oracle is oracle
        assert output is projected
        assert materialization == {"receipt": "bound"}
        return expected

    monkeypatch.setattr(scorer_v3, "score_case_v3", fake_score)

    assert (
        _default_case_scorer(
            source_case,
            source_output,
            oracle,
            materialization,
        )
        is expected
    )
