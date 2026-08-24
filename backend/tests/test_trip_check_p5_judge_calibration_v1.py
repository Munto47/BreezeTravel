from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.judge_calibration_v1 import (
    P5JudgeCalibrationErrorV1,
    aggregate_judge_calibration_rounds_v1,
    export_judge_calibration_bundles_v1,
    validate_judge_calibration_panel_v1,
)
from evals.trip_check_v1.p5.formal_receipts_v5 import RepoBindingV5


SOURCE_P5 = Path(__file__).parents[1] / "evals" / "trip_check_v1" / "p5"
SUBJECT = "a" * 40
UPSTREAM_REF = "origin/codex/p5-calibration-test"


def _repo_binding(*, dirty_tree: bool = False) -> RepoBindingV5:
    return RepoBindingV5(
        subject_commit=SUBJECT,
        upstream_ref=UPSTREAM_REF,
        upstream_commit=SUBJECT,
        dirty_tree=dirty_tree,
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _copy_json(source: Path, target: Path) -> None:
    _write_json(target, json.loads(source.read_text(encoding="utf-8")))


def _export(tmp_path: Path) -> tuple[Path, dict, list[Path]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    rubric = repo / "rubric.json"
    protocol = repo / "protocol.json"
    calibration = repo / "calibration.json"
    _copy_json(SOURCE_P5 / "judge_rubric_v2.json", rubric)
    _copy_json(SOURCE_P5 / "judge_protocol_v1.json", protocol)
    _copy_json(SOURCE_P5 / "judge_calibration_v1.json", calibration)
    round_dirs = [tmp_path / f"round-{index}" for index in range(1, 4)]
    custody = tmp_path / "custody"
    receipt = export_judge_calibration_bundles_v1(
        repo_root=repo,
        round_output_dirs=round_dirs,
        custody_output_dir=custody,
        rubric_path=rubric,
        protocol_path=protocol,
        calibration_set_path=calibration,
        repo_binding=_repo_binding(),
    )
    return repo, receipt, round_dirs


def _round_paths(
    tmp_path: Path,
    receipt: dict,
    round_dirs: list[Path],
    *,
    mutate_round: int | None = None,
) -> list[Path]:
    key = json.loads(
        (tmp_path / "custody" / "judge_calibration_key.v1.json").read_text(
            encoding="utf-8"
        )
    )
    expected = {item["calibration_item_id"]: item for item in key["expected"]}
    paths = []
    for round_index, bundle_receipt in enumerate(receipt["bundle_receipts"], 1):
        bundle = json.loads(
            (
                round_dirs[round_index - 1]
                / f"judge_calibration_round_{round_index}.v1.json"
            ).read_text(encoding="utf-8")
        )
        scores = []
        for item in bundle["items"]:
            target = expected[item["calibration_item_id"]]
            score = {
                "calibration_item_id": item["calibration_item_id"],
                "clarity": target["clarity"],
                "actionability": target["actionability"],
                "evidence_boundary_expression": target[
                    "evidence_boundary_expression"
                ],
                "derived_verdict": target["derived_verdict"],
            }
            if mutate_round == round_index:
                score.update(
                    {
                        "clarity": 0,
                        "actionability": 0,
                        "evidence_boundary_expression": 0,
                        "derived_verdict": "NEEDS_REVISION",
                    }
                )
            scores.append(score)
        report = {
            "schema_version": "trip-check-p5-judge-calibration-round-v1",
            "round_index": round_index,
            "evaluator_id": f"evaluator-{round_index}",
            "agent_task_id": f"task-{round_index}",
            "agent_id": f"agent-{round_index}",
            "context_id": f"context-{round_index}",
            "model_id": "gpt-test",
            "started_at": f"2026-08-24T00:0{round_index}:00Z",
            "ended_at": f"2026-08-24T00:0{round_index}:30Z",
            "bundle_sha256": bundle_receipt["sha256"],
            **{
                field: bundle_receipt[field]
                for field in (
                    "subject_commit",
                    "upstream_ref",
                    "upstream_commit",
                    "dirty_tree",
                    "source_rubric_sha256",
                    "judge_input_rubric_sha256",
                    "source_protocol_sha256",
                    "judge_input_protocol_sha256",
                    "calibration_set_sha256",
                    "calibration_input_content_sha256",
                )
            },
            "api_usage_count": 0,
            "tool_usage_count": 0,
            "automated_proxy_judge": True,
            "human_calibration_performed": False,
            "expected_scores_observed": False,
            "peer_round_output_observed": False,
            "scores": scores,
        }
        path = tmp_path / f"result-{round_index}" / "round.json"
        _write_json(path, report)
        paths.append(path)
    return paths


def test_calibration_export_and_three_round_panel_pass(tmp_path: Path) -> None:
    repo, receipt, round_dirs = _export(tmp_path)
    assert receipt["status"] == "EXPORTED"
    assert receipt["expected_scores_exported_to_judges"] is False
    for round_index, round_dir in enumerate(round_dirs, 1):
        bundle = json.loads(
            (
                round_dir / f"judge_calibration_round_{round_index}.v1.json"
            ).read_text(encoding="utf-8")
        )
        assert len(bundle["items"]) == 10
        assert all("expected" not in item for item in bundle["items"])
    key = tmp_path / "custody" / "judge_calibration_key.v1.json"
    panel = aggregate_judge_calibration_rounds_v1(
        repo_root=repo,
        key_path=key,
        key_sha256=hashlib.sha256(key.read_bytes()).hexdigest(),
        round_paths=_round_paths(tmp_path, receipt, round_dirs),
    )
    assert panel["status"] == "PASS"
    assert panel["verdict_agreement_rate"] == 1.0
    assert panel["expected_verdict_match_rate"] == 1.0
    panel_path = tmp_path / "panel" / "calibration.json"
    _write_json(panel_path, panel)
    validated = validate_judge_calibration_panel_v1(
        repo_root=repo,
        panel_path=panel_path,
        rubric_path=repo / "rubric.json",
        protocol_path=repo / "protocol.json",
    )
    assert validated == panel


def test_calibration_panel_blocks_uncalibrated_round(tmp_path: Path) -> None:
    repo, receipt, round_dirs = _export(tmp_path)
    key = tmp_path / "custody" / "judge_calibration_key.v1.json"
    panel = aggregate_judge_calibration_rounds_v1(
        repo_root=repo,
        key_path=key,
        key_sha256=hashlib.sha256(key.read_bytes()).hexdigest(),
        round_paths=_round_paths(
            tmp_path, receipt, round_dirs, mutate_round=1
        ),
    )
    assert panel["status"] == "BLOCKED"


def test_calibration_rejects_expected_score_observation(tmp_path: Path) -> None:
    repo, receipt, round_dirs = _export(tmp_path)
    paths = _round_paths(tmp_path, receipt, round_dirs)
    first = json.loads(paths[0].read_text(encoding="utf-8"))
    first["expected_scores_observed"] = True
    _write_json(paths[0], first)
    key = tmp_path / "custody" / "judge_calibration_key.v1.json"
    with pytest.raises(
        P5JudgeCalibrationErrorV1, match="JUDGE_CALIBRATION_ROUND_INVALID"
    ):
        aggregate_judge_calibration_rounds_v1(
            repo_root=repo,
            key_path=key,
            key_sha256=hashlib.sha256(key.read_bytes()).hexdigest(),
            round_paths=paths,
        )


@pytest.mark.parametrize(
    "repo_binding",
    (
        _repo_binding(dirty_tree=True),
        RepoBindingV5(
            subject_commit=SUBJECT,
            upstream_ref=UPSTREAM_REF,
            upstream_commit="b" * 40,
            dirty_tree=False,
        ),
    ),
)
def test_calibration_export_rejects_unfrozen_subject(
    tmp_path: Path, repo_binding: RepoBindingV5
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    rubric = repo / "rubric.json"
    protocol = repo / "protocol.json"
    calibration = repo / "calibration.json"
    _copy_json(SOURCE_P5 / "judge_rubric_v2.json", rubric)
    _copy_json(SOURCE_P5 / "judge_protocol_v1.json", protocol)
    _copy_json(SOURCE_P5 / "judge_calibration_v1.json", calibration)
    with pytest.raises(
        P5JudgeCalibrationErrorV1,
        match="JUDGE_CALIBRATION_SUBJECT_NOT_CLEAN_PUSHED_UPSTREAM",
    ):
        export_judge_calibration_bundles_v1(
            repo_root=repo,
            round_output_dirs=[
                tmp_path / f"round-{index}" for index in range(1, 4)
            ],
            custody_output_dir=tmp_path / "custody",
            rubric_path=rubric,
            protocol_path=protocol,
            calibration_set_path=calibration,
            repo_binding=repo_binding,
        )
