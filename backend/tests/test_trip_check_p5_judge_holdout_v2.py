from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.formal_receipts_v5 import RepoBindingV5
from evals.trip_check_v1.p5.judge_holdout_v2 import (
    P5JudgeHoldoutErrorV2,
    aggregate_judge_holdout_rounds_v2,
    export_judge_holdout_bundles_v2,
    validate_judge_holdout_panel_v2,
)


SOURCE_P5 = Path(__file__).parents[1] / "evals" / "trip_check_v1" / "p5"
SUBJECT = "a" * 40
UPSTREAM_REF = "origin/codex/p5-judge-v2-test"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _package() -> dict[str, object]:
    items = []
    for index in range(30):
        actionability = index // 6
        scores = {
            "clarity": 2 if actionability >= 2 else 1,
            "actionability": actionability,
            "evidence_boundary_expression": 2 if actionability >= 2 else 1,
        }
        items.append(
            {
                "holdout_item_id": f"holdout-{index + 1:03d}",
                "public_input": f"Synthetic nonblind scenario {index + 1}",
                "candidate_expression": {
                    "terminal_status": "ADVICE_READY",
                    "requires_user_resolution": actionability < 2,
                    "advice": [] if actionability == 0 else [{"action": "confirm target"}],
                },
                "evidence_summary": {"fact_authority": "DETERMINISTIC_SCORER_ONLY"},
                "expected": {
                    **scores,
                    "derived_verdict": (
                        "PASS" if min(scores.values()) >= 2 else "NEEDS_REVISION"
                    ),
                },
            }
        )
    return {
        "schema_version": "trip-check-p5-judge-holdout-package-v2",
        "evidence_class": "sealed_nonblind_synthetic_holdout",
        "source_lane": "NONBLIND_SYNTHETIC_HOLDOUT",
        "item_count": 30,
        "blind_source_used": False,
        "human_evidence": False,
        "human_calibration_performed": False,
        "items": items,
    }


def _public_expected(package: dict[str, object]) -> tuple[list[dict], list[dict]]:
    public = []
    expected = []
    for item in package["items"]:
        public.append(
            {
                "holdout_item_id": item["holdout_item_id"],
                "public_input": item["public_input"],
                "candidate_expression": item["candidate_expression"],
                "evidence_summary": item["evidence_summary"],
            }
        )
        expected.append({"holdout_item_id": item["holdout_item_id"], **item["expected"]})
    return public, expected


def _export(tmp_path: Path) -> tuple[Path, Path, Path, dict, list[Path]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    rubric = repo / "rubric.json"
    protocol = repo / "protocol.json"
    rubric.write_bytes((SOURCE_P5 / "judge_rubric_v2.json").read_bytes())
    protocol.write_bytes((SOURCE_P5 / "judge_protocol_v2.json").read_bytes())
    package = _package()
    package_path = tmp_path / "sealed" / "package.json"
    _write_json(package_path, package)
    public, expected = _public_expected(package)
    commitment = {
        "schema_version": "trip-check-p5-judge-holdout-commitment-v2",
        "status": "SEALED",
        "item_count": 30,
        "source_lane": "NONBLIND_SYNTHETIC_HOLDOUT",
        "blind_source_used": False,
        "package_sha256": _sha256(package_path),
        "public_items_content_sha256": digest(public),
        "expected_items_content_sha256": digest(expected),
        "custodian_receipt_sha256": "b" * 64,
        "review_receipt_sha256": "c" * 64,
    }
    commitment_path = repo / "commitment.json"
    _write_json(commitment_path, commitment)
    round_dirs = [tmp_path / f"bundle-{index}" for index in range(1, 4)]
    receipt = export_judge_holdout_bundles_v2(
        repo_root=repo,
        round_output_dirs=round_dirs,
        custody_output_dir=tmp_path / "custody",
        rubric_path=rubric,
        protocol_path=protocol,
        commitment_path=commitment_path,
        package_path=package_path,
        repo_binding=RepoBindingV5(SUBJECT, UPSTREAM_REF, SUBJECT, False),
    )
    return repo, commitment_path, package_path, receipt, round_dirs


def _round_paths(
    tmp_path: Path,
    receipt: dict,
    *,
    mutate_round: int | None = None,
    substitute_model_round: int | None = None,
) -> list[Path]:
    key = json.loads(
        (tmp_path / "custody" / "judge_holdout_key.v2.json").read_text(
            encoding="utf-8"
        )
    )
    expected = {row["holdout_item_id"]: row for row in key["expected"]}
    paths = []
    for round_index, bundle_receipt in enumerate(receipt["bundle_receipts"], 1):
        scores = []
        for target in expected.values():
            score = dict(target)
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
        binding_fields = {
            key: bundle_receipt[key]
            for key in (
                "subject_commit",
                "upstream_ref",
                "upstream_commit",
                "dirty_tree",
                "source_rubric_sha256",
                "judge_input_rubric_sha256",
                "source_protocol_sha256",
                "judge_input_protocol_sha256",
                "holdout_commitment_sha256",
                "holdout_package_sha256",
                "holdout_public_content_sha256",
                "holdout_expected_content_sha256",
            )
        }
        report = {
            "schema_version": "trip-check-p5-judge-holdout-round-v2",
            "round_index": round_index,
            "evaluator_profile_id": bundle_receipt["evaluator_profile_id"],
            "reasoning_effort": bundle_receipt["reasoning_effort"],
            "evaluator_id": f"evaluator-{round_index}",
            "agent_task_id": f"task-{round_index}",
            "agent_id": f"agent-{round_index}",
            "context_id": f"context-{round_index}",
            "model_id": (
                "substituted-model"
                if substitute_model_round == round_index
                else bundle_receipt["model_id"]
            ),
            "started_at": f"2026-08-24T00:0{round_index}:00Z",
            "ended_at": f"2026-08-24T00:0{round_index}:30Z",
            "bundle_sha256": bundle_receipt["sha256"],
            **binding_fields,
            "api_usage_count": 0,
            "tool_usage_count": 0,
            "automated_proxy_judge": True,
            "human_calibration_performed": False,
            "expected_scores_observed": False,
            "peer_round_output_observed": False,
            "scores": scores,
        }
        path = tmp_path / "results" / f"round-{round_index}.json"
        _write_json(path, report)
        paths.append(path)
    return paths


def test_holdout_export_aggregate_and_validate_pass(tmp_path: Path) -> None:
    repo, commitment, _, receipt, _ = _export(tmp_path)
    round_paths = _round_paths(tmp_path, receipt)
    key = tmp_path / "custody" / "judge_holdout_key.v2.json"
    panel = aggregate_judge_holdout_rounds_v2(
        repo_root=repo,
        key_path=key,
        key_sha256=_sha256(key),
        round_paths=round_paths,
    )
    assert panel["status"] == "PASS"
    assert all(
        metric["exact_score_match_rate"]["actionability"] == 1.0
        for metric in panel["slot_metrics"]
    )
    panel_path = tmp_path / "panel" / "panel.json"
    _write_json(panel_path, panel)
    assert (
        validate_judge_holdout_panel_v2(
            repo_root=repo,
            panel_path=panel_path,
            rubric_path=repo / "rubric.json",
            protocol_path=repo / "protocol.json",
            commitment_path=commitment,
        )["report_hash"]
        == panel["report_hash"]
    )


def test_holdout_rejects_package_inside_repo(tmp_path: Path) -> None:
    repo, commitment, package, _, _ = _export(tmp_path)
    tracked_package = repo / "package.json"
    tracked_package.write_bytes(package.read_bytes())
    with pytest.raises(P5JudgeHoldoutErrorV2, match="JUDGE_HOLDOUT_CUSTODY_PATH_INVALID"):
        export_judge_holdout_bundles_v2(
            repo_root=repo,
            round_output_dirs=[tmp_path / f"new-{index}" for index in range(3)],
            custody_output_dir=tmp_path / "new-custody",
            rubric_path=repo / "rubric.json",
            protocol_path=repo / "protocol.json",
            commitment_path=commitment,
            package_path=tracked_package,
            repo_binding=RepoBindingV5(SUBJECT, UPSTREAM_REF, SUBJECT, False),
        )


def test_holdout_rejects_model_substitution(tmp_path: Path) -> None:
    repo, _, _, receipt, _ = _export(tmp_path)
    key = tmp_path / "custody" / "judge_holdout_key.v2.json"
    with pytest.raises(P5JudgeHoldoutErrorV2, match="JUDGE_HOLDOUT_ROUND_INVALID"):
        aggregate_judge_holdout_rounds_v2(
            repo_root=repo,
            key_path=key,
            key_sha256=_sha256(key),
            round_paths=_round_paths(tmp_path, receipt, substitute_model_round=2),
        )


def test_holdout_quality_failure_blocks_panel(tmp_path: Path) -> None:
    repo, _, _, receipt, _ = _export(tmp_path)
    key = tmp_path / "custody" / "judge_holdout_key.v2.json"
    panel = aggregate_judge_holdout_rounds_v2(
        repo_root=repo,
        key_path=key,
        key_sha256=_sha256(key),
        round_paths=_round_paths(tmp_path, receipt, mutate_round=2),
    )
    assert panel["status"] == "BLOCKED"


def test_holdout_rejects_blind_fragment(tmp_path: Path) -> None:
    package = _package()
    package["items"][0]["public_input"] = "p5.blind.synthetic"
    repo = tmp_path / "repo"
    repo.mkdir()
    rubric = repo / "rubric.json"
    protocol = repo / "protocol.json"
    rubric.write_bytes((SOURCE_P5 / "judge_rubric_v2.json").read_bytes())
    protocol.write_bytes((SOURCE_P5 / "judge_protocol_v2.json").read_bytes())
    package_path = tmp_path / "sealed" / "package.json"
    _write_json(package_path, package)
    public, expected = _public_expected(package)
    commitment = {
        "schema_version": "trip-check-p5-judge-holdout-commitment-v2",
        "status": "SEALED",
        "item_count": 30,
        "source_lane": "NONBLIND_SYNTHETIC_HOLDOUT",
        "blind_source_used": False,
        "package_sha256": _sha256(package_path),
        "public_items_content_sha256": digest(public),
        "expected_items_content_sha256": digest(expected),
        "custodian_receipt_sha256": "b" * 64,
        "review_receipt_sha256": "c" * 64,
    }
    commitment_path = repo / "commitment.json"
    _write_json(commitment_path, commitment)
    with pytest.raises(P5JudgeHoldoutErrorV2, match="JUDGE_HOLDOUT_BOUNDARY_INVALID"):
        export_judge_holdout_bundles_v2(
            repo_root=repo,
            round_output_dirs=[tmp_path / f"round-{index}" for index in range(3)],
            custody_output_dir=tmp_path / "custody",
            rubric_path=rubric,
            protocol_path=protocol,
            commitment_path=commitment_path,
            package_path=package_path,
            repo_binding=RepoBindingV5(SUBJECT, UPSTREAM_REF, SUBJECT, False),
        )
