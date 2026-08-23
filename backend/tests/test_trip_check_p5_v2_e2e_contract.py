from __future__ import annotations

import asyncio
import ast
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.active_contract import (
    require_v2_formal_ready,
)
from evals.trip_check_v1.p5.adapters_v2 import validate_materialization_v2
from evals.trip_check_v1.p5.contracts_v2 import P5CaseV2
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.data_contract_v2 import (
    case_set_hash,
    materialization_set_hash,
)
from evals.trip_check_v1.p5.gate_v2 import build_p5_gate_manifest_v2
from evals.trip_check_v1.p5.judge_v2 import (
    aggregate_judge_rounds_v2,
    export_judge_bundles_v2,
)
from evals.trip_check_v1.p5.ocr_materialization_v2 import materialize_ocr_input
from scripts import run_trip_check_p5_v2_eval as run_cli
from tests.p5_v2_e2e_helpers import (
    BLIND_CASES_PATH,
    BLIND_MATERIALIZATIONS_PATH,
    DATASET_MANIFEST_PATH,
    NONBLIND_CASES_PATH,
    NONBLIND_MATERIALIZATIONS_PATH,
    P5_ROOT,
    REPO_ROOT,
    file_sha256,
    load_json,
    load_jsonl,
    materializations_by_case,
)


EXPECTED_SPLITS = {"pilot": 18, "dev": 180, "regression": 72, "frozen_blind": 90}
EXPECTED_CITIES = {"北京": 120, "上海": 120, "杭州": 120}
EXPECTED_SCREENSHOTS = {"dev": 90, "regression": 36, "frozen_blind": 45}


def _run_module(module: str, *args: str, cwd: Path = REPO_ROOT / "backend") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def test_dataset_360_exact_byte_and_canonical_readback() -> None:
    manifest = load_json(DATASET_MANIFEST_PATH)
    assert manifest["manifest_hash"] == digest(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    )
    paths = {
        "nonblind_cases": NONBLIND_CASES_PATH,
        "blind_cases": BLIND_CASES_PATH,
        "nonblind_materializations": NONBLIND_MATERIALIZATIONS_PATH,
        "blind_materializations": BLIND_MATERIALIZATIONS_PATH,
    }
    loaded = {key: load_jsonl(path) for key, path in paths.items()}
    for key, path in paths.items():
        binding = manifest["files"][key]
        assert binding["row_count"] == len(loaded[key])
        assert binding["file_sha256"] == file_sha256(path)
        assert binding["content_sha256"] == digest(loaded[key])

    cases = [*loaded["nonblind_cases"], *loaded["blind_cases"]]
    materializations = [
        *loaded["nonblind_materializations"],
        *loaded["blind_materializations"],
    ]
    assert len(cases) == len(materializations) == 360
    assert Counter(row["split"] for row in cases) == EXPECTED_SPLITS
    assert Counter(row["city"] for row in cases) == EXPECTED_CITIES
    assert Counter(row["split"] for row in cases if row["input_kind"] == "SYNTHETIC_SCREENSHOT") == EXPECTED_SCREENSHOTS
    assert manifest["counts"] == {
        "total": 360,
        "by_split": EXPECTED_SPLITS,
        "by_city": EXPECTED_CITIES,
        "screenshots_by_split": EXPECTED_SCREENSHOTS,
    }

    validated = [P5CaseV2.model_validate(row) for row in cases]
    assert len({case.case_id for case in validated}) == 360
    assert {row["case_id"] for row in materializations} == {case.case_id for case in validated}
    assert all(
        row["case_hash"] == digest({key: value for key, value in row.items() if key != "case_hash"}) for row in cases
    )
    assert all(
        row["materialization_hash"]
        == digest({key: value for key, value in row.items() if key != "materialization_hash"})
        for row in materializations
    )
    for lane, lane_cases, lane_materializations in (
        ("nonblind", loaded["nonblind_cases"], loaded["nonblind_materializations"]),
        ("frozen_blind", loaded["blind_cases"], loaded["blind_materializations"]),
    ):
        binding = manifest["lanes"][lane]
        assert binding["case_count"] == len(lane_cases)
        assert binding["materialization_count"] == len(lane_materializations)
        assert binding["case_set_hash"] == case_set_hash(lane_cases)
        assert binding["materialization_set_hash"] == materialization_set_hash(lane_materializations)


def test_exact_abc_terminal_cardinality_is_810_plus_270() -> None:
    manifest = load_json(DATASET_MANIFEST_PATH)
    variant_count = 3
    nonblind = manifest["lanes"]["nonblind"]["case_count"] * variant_count
    blind = manifest["lanes"]["frozen_blind"]["case_count"] * variant_count
    assert (nonblind, blind, nonblind + blind) == (810, 270, 1080)


def test_formal_dataset_validator_passes_in_fresh_detached_worktree(tmp_path: Path) -> None:
    detached = tmp_path / "candidate-head"
    assert detached.parent.resolve() == tmp_path.resolve()
    added = subprocess.run(
        ["git", "worktree", "add", "--detach", str(detached), "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert added.returncode == 0, added.stderr
    try:
        completed = _run_module(
            "scripts.validate_trip_check_p5_dataset_v2",
            cwd=detached / "backend",
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout
        report = json.loads(completed.stdout)
        assert report["schema_version"] == "trip-check-p5-dataset-validation-v2"
        assert report["status"] == "PASS"
        assert report["formal"] is True
        assert report["errors"] == []
        assert report["counts"]["total"] == 360
        assert report["manifest_hash"] == load_json(DATASET_MANIFEST_PATH)["manifest_hash"]
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=detached,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        assert status.stdout == ""
    finally:
        removed = subprocess.run(
            ["git", "worktree", "remove", "--force", str(detached)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
        assert removed.returncode == 0, removed.stderr


@pytest.mark.parametrize(
    ("lane", "path"),
    (
        ("nonblind", NONBLIND_MATERIALIZATIONS_PATH),
        ("frozen_blind", BLIND_MATERIALIZATIONS_PATH),
    ),
)
def test_runner_full_lane_materialization_set_hash_matches_dataset(lane: str, path: Path) -> None:
    manifest = load_json(DATASET_MANIFEST_PATH)
    rows = load_jsonl(path)
    assert run_cli._materialization_set_hash(rows) == manifest["lanes"][lane]["materialization_set_hash"]


def test_all_171_screenshot_receipts_are_bound_and_privacy_closed() -> None:
    cases = [
        *load_jsonl(NONBLIND_CASES_PATH),
        *load_jsonl(BLIND_CASES_PATH),
    ]
    case_by_id = {row["case_id"]: P5CaseV2.model_validate(row) for row in cases}
    rows = materializations_by_case()
    screenshot_ids = {row["case_id"] for row in cases if row["input_kind"] == "SYNTHETIC_SCREENSHOT"}
    assert len(screenshot_ids) == 171
    engines = Counter()
    for case_id in screenshot_ids:
        row = rows[case_id]
        render = row["render_receipt"]
        ocr = row["ocr_baseline_receipt"]
        cleanup = [
            receipt
            for receipt in row["receipts"]
            if receipt.get("schema_version") == "trip-check-p5-cleanup-receipt-v2"
        ]
        assert render["schema_version"] == "trip-check-p5-render-receipt-v2"
        assert ocr["schema_version"] == "trip-check-p5-ocr-baseline-receipt-v2"
        assert render["image_sha256"] == ocr["asset_hash"]
        assert len(cleanup) == 1
        assert cleanup[0]["asset_hash"] == render["image_sha256"]
        assert cleanup[0]["cleanup_status"] == "DELETED"
        assert cleanup[0]["original_removed"] is True
        assert digest(render) == case_by_id[case_id].materialization.render_receipt.content_sha256
        assert digest(ocr) == case_by_id[case_id].materialization.ocr_baseline_receipt.content_sha256
        assert not {"image_bytes", "image_path", "storage_locator", "source_path"}.intersection(row)
        engines[ocr["engine"]] += 1

    manifest = load_json(DATASET_MANIFEST_PATH)
    if manifest["evidence_boundary"]["actual_ocr"] == "PASS":
        assert "p5-development-ocr" not in engines
    else:
        assert manifest["evidence_boundary"]["actual_ocr"] == "NOT_RUN"
        assert engines == {"p5-development-ocr": 171}


def test_screenshot_materialization_passes_adapter_readback() -> None:
    cases = [
        *load_jsonl(NONBLIND_CASES_PATH),
        *load_jsonl(BLIND_CASES_PATH),
    ]
    case_by_id = {row["case_id"]: P5CaseV2.model_validate(row) for row in cases}
    rows = materializations_by_case()
    samples: dict[str, str] = {}
    for row in cases:
        if row["input_kind"] == "SYNTHETIC_SCREENSHOT":
            samples.setdefault(row["city"], row["case_id"])
    assert set(samples) == {"北京", "上海", "杭州"}
    for case_id in samples.values():
        validate_materialization_v2(case_by_id[case_id], rows[case_id])


@pytest.mark.external
def test_actual_ocr_production_interface_small_sample(tmp_path: Path) -> None:
    if os.environ.get("P5_V2_ACTUAL_OCR_SAMPLE") != "1":
        pytest.skip("set P5_V2_ACTUAL_OCR_SAMPLE=1 with RUN_EXTERNAL_TESTS=1 for actual OCR")
    rows = load_jsonl(NONBLIND_CASES_PATH)
    samples: dict[str, dict] = {}
    for row in rows:
        if row["input_kind"] == "SYNTHETIC_SCREENSHOT":
            samples.setdefault(row["city"], row)
    assert set(samples) == {"北京", "上海", "杭州"}
    for city, row in samples.items():
        receipt = asyncio.run(
            materialize_ocr_input(
                row["product_input"],
                case_id=f"p5.actual-ocr.{city}",
                work_root=tmp_path / city,
            )
        )
        assert receipt["status"] == "SUCCEEDED"
        assert receipt["ocr_baseline_receipt"]["engine"] != "p5-development-ocr"
        assert receipt["cleanup_receipt"]["cleanup_status"] == "DELETED"
        assert receipt["cleanup_receipt"]["original_removed"] is True


def test_pilot_has_18_isolated_representatives_across_all_cities() -> None:
    rows = [row for row in load_jsonl(NONBLIND_CASES_PATH) if row["split"] == "pilot"]
    assert len(rows) == 18
    assert Counter(row["city"] for row in rows) == {"北京": 6, "上海": 6, "杭州": 6}
    assert all(row["oracle"] is not None and row["oracle_sha256"] for row in rows)


def test_blind_inputs_are_label_free_and_lineage_disjoint() -> None:
    blind = load_jsonl(BLIND_CASES_PATH)
    assert len(blind) == 90
    assert all(row["split"] == "frozen_blind" for row in blind)
    assert all("oracle" not in row and "oracle_sha256" not in row for row in blind)
    assert all(row["provenance"]["contains_human_data"] is False for row in blind)

    all_rows = [*load_jsonl(NONBLIND_CASES_PATH), *blind]
    for field in ("content_family_id", "source_family_id", "mutation_ancestry_id"):
        by_split: dict[str, set[str]] = defaultdict(set)
        for row in all_rows:
            by_split[row["split"]].add(row["lineage"][field])
        splits = sorted(by_split)
        assert all(
            by_split[left].isdisjoint(by_split[right])
            for index, left in enumerate(splits)
            for right in splits[index + 1 :]
        )


def test_formal_execution_is_ready_only_after_active_contract_and_seal() -> None:
    active = require_v2_formal_ready(P5_ROOT / "active_contract.json")
    assert active["formal_evidence_status"] == "READY"
    assert (P5_ROOT / "sealed" / "frozen_blind.v2.seal.json").is_file()


def test_sealed_state_is_exact_and_hash_only() -> None:
    active = load_json(P5_ROOT / "active_contract.json")
    manifest = load_json(DATASET_MANIFEST_PATH)
    seal_path = P5_ROOT / "sealed" / "frozen_blind.v2.seal.json"
    seal = load_json(seal_path)
    commitment = manifest["sealing_commitment"]
    assert active["active_contract"] == "trip-check-p5-v2"
    assert active["formal_evidence_status"] == "READY"
    assert active["candidate_freeze_commit"] == commitment["candidate_freeze_commit"]
    assert active["dataset_manifest_hash"] == manifest["manifest_hash"]
    assert active["blind_seal_v2_sha256"] == file_sha256(seal_path)
    assert commitment["blind_seal_v2_sha256"] == file_sha256(seal_path)
    assert commitment["status"] == "SEALED"
    assert seal["scoring_payload_present"] is False
    assert seal["label_storage"] == "external_bundle_only"
    assert "labels" not in seal and "oracle" not in seal


@pytest.mark.parametrize(
    ("module", "required_options"),
    (
        (
            "scripts.validate_trip_check_p5_external_custody_v2",
            (
                "--repo-root",
                "--external-bundle",
                "--external-review-receipt",
                "--labels-canonical-sha256",
                "--candidate-subject-commit",
            ),
        ),
        (
            "scripts.seal_trip_check_p5_blind_v2",
            (
                "--labels-canonical-sha256",
                "--external-bundle-sha256",
                "--review-receipt-sha256",
                "--candidate-freeze-commit",
            ),
        ),
        (
            "scripts.export_trip_check_p5_v2_judge",
            ("--run-dir", "--output-dir"),
        ),
        (
            "scripts.aggregate_trip_check_p5_v2_judges",
            ("--mapping", "--mapping-sha256", "--round", "--output"),
        ),
        (
            "scripts.run_trip_check_p5_v2_gate",
            (
                "--nonblind-run-dir",
                "--nonblind-score",
                "--blind-run-dir",
                "--blind-score",
                "--judge-panel",
                "--formal-validation-receipt",
            ),
        ),
    ),
)
def test_p5_v2_formal_cli_help_is_readable(module: str, required_options: tuple[str, ...]) -> None:
    completed = _run_module(module, "--help")
    assert completed.returncode == 0, completed.stderr
    assert all(option in completed.stdout for option in required_options)


@pytest.mark.parametrize(
    ("module", "args", "expected_error"),
    (
        (
            "scripts.export_trip_check_p5_v2_judge",
            ("--run-dir", "missing", "--output-dir", "missing"),
            "BLIND_JUDGE_EXPORT_INSIDE_REPOSITORY",
        ),
        (
            "scripts.aggregate_trip_check_p5_v2_judges",
            (
                "--mapping",
                "missing",
                "--mapping-sha256",
                "0" * 64,
                "--round",
                "missing",
                "--output",
                "missing",
            ),
            "BLIND_JUDGE_MAPPING_INSIDE_REPOSITORY",
        ),
        (
            "scripts.run_trip_check_p5_v2_gate",
            (
                "--nonblind-run-dir",
                "missing",
                "--nonblind-score",
                "missing",
                "--blind-run-dir",
                "missing",
                "--blind-score",
                "missing",
                "--judge-panel",
                "missing",
                "--formal-validation-receipt",
                "missing",
            ),
            "RUN_GROUP_MANIFEST_INVALID",
        ),
    ),
)
def test_sealed_v2_judge_and_gate_clis_fail_closed_on_invalid_artifacts(
    module: str,
    args: tuple[str, ...],
    expected_error: str,
) -> None:
    completed = _run_module(module, *args)
    assert completed.returncode != 0
    assert expected_error in completed.stderr


@pytest.mark.parametrize(
    ("module", "args"),
    (
        (
            "scripts.run_trip_check_p5_eval",
            ("--lane", "nonblind", "--require-formal", "--output-dir", "missing"),
        ),
        ("scripts.score_trip_check_p5_eval", ("--run-dir", "missing")),
        (
            "scripts.run_trip_check_p5_gate",
            (
                "--nonblind-run-dir",
                "missing",
                "--nonblind-score",
                "missing",
                "--blind-run-dir",
                "missing",
                "--blind-score",
                "missing",
                "--judge-panel",
                "missing",
            ),
        ),
    ),
)
def test_v1_formal_clis_reject_superseded_contract_before_artifact_read(
    module: str,
    args: tuple[str, ...],
) -> None:
    completed = _run_module(module, *args)
    assert completed.returncode != 0
    assert "P5_V1_FORMAL_CONTRACT_SUPERSEDED" in completed.stderr
    assert "FileNotFoundError" not in completed.stderr


def test_frozen_blind_output_must_be_outside_repository(tmp_path: Path) -> None:
    args = run_cli.parser().parse_args(
        [
            "--lane",
            "frozen-blind",
            "--limit",
            "1",
            "--allow-dirty",
            "--output-dir",
            str(REPO_ROOT / "forbidden-blind-output"),
        ]
    )
    with pytest.raises(RuntimeError, match="outside the repository"):
        asyncio.run(run_cli.execute_run(args))


def test_judge_and_gate_interfaces_are_v2_not_v1_aliases() -> None:
    assert export_judge_bundles_v2.__module__ == "evals.trip_check_v1.p5.judge_v2"
    assert aggregate_judge_rounds_v2.__module__ == "evals.trip_check_v1.p5.judge_v2"
    assert build_p5_gate_manifest_v2.__module__ == "evals.trip_check_v1.p5.gate_v2"


def test_tracked_repository_contains_no_blind_oracle_answer_path() -> None:
    """The external custodian may consume labels; tracked code may not derive them."""

    forbidden = (
        "_REASON_BY_FAULT",
        "_STRATEGY_BY_FAULT",
        "_CANDIDATE_MODE_TO_ORACLE",
        "_CANDIDATE_BY_FAULT",
        "_CONCURRENCY_BY_FAULT",
        "derive_blind_oracle_v2",
        "derive_all_blind_labels_v2",
        "build_blind_label_bundle_v2",
        "review_blind_label_bundle_v2",
    )
    tracked = subprocess.run(
        [
            "git",
            "ls-files",
            "backend/evals/trip_check_v1/p5/*.py",
            "backend/scripts/*trip_check_p5*v2.py",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    ).stdout.splitlines()
    findings: list[str] = []
    fault_profiles = {
        "advice_completeness",
        "empty_candidate_set",
        "candidate_receipt_missing",
        "route_conflict",
        "duplicate_apply",
        "concurrent_apply",
        "solver_unsat",
        "solver_timeout",
        "solver_fallback",
    }
    oracle_markers = {
        "required_reason_codes",
        "expected_strategy_outcome",
        "candidate_receipt_mode",
        "concurrency_expectation",
        "unknown_must_be_preserved",
        "specific_place_allowed",
    }

    def string_literals(node: ast.AST) -> set[str]:
        return {
            child.value
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }

    for relative in tracked:
        path = REPO_ROOT / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        findings.extend(f"{relative}:{symbol}" for symbol in forbidden if symbol in text)
        tree = ast.parse(text, filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                literals = string_literals(node)
                fault_keys = literals.intersection(fault_profiles)
                if len(fault_keys) >= 2 and (
                    literals.intersection(oracle_markers)
                    or any(value.startswith("P4_") for value in literals)
                    or literals.intersection(
                        {
                            "FEASIBLE",
                            "UNSAT",
                            "TIMEOUT",
                            "FALLBACK",
                            "REQUIRED",
                            "FORBIDDEN",
                            "IDEMPOTENT_REPLAY",
                            "SINGLE_WINNER",
                        }
                    )
                ):
                    findings.append(f"{relative}:{node.lineno}:fault-to-oracle-map")
                blind_case_keys = {
                    value for value in literals if re.fullmatch(r"p5\.blind\.(?:bj|sh|hz)\.\d{3}", value)
                }
                if blind_case_keys and literals.intersection(oracle_markers):
                    findings.append(f"{relative}:{node.lineno}:per-case-reversible-oracle-map")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lowered = node.name.lower()
                producer = lowered.startswith(("derive", "generate", "create", "build", "recompute", "review"))
                answer_artifact = "blind" in lowered and any(
                    marker in lowered for marker in ("oracle", "label", "bundle")
                )
                if producer and answer_artifact:
                    findings.append(f"{relative}:{node.lineno}:blind-answer-producer:{node.name}")
                    parameters = {argument.arg.lower() for argument in node.args.args}
                    if any(
                        marker in parameter
                        for parameter in parameters
                        for marker in ("candidate_output", "terminal_output", "run_dir", "run_manifest")
                    ):
                        findings.append(f"{relative}:{node.lineno}:candidate-output-dependent-answer")
    assert findings == [], "P5_BLIND_ORACLE_ANSWER_PATH_IN_REPOSITORY:\n" + "\n".join(findings)
