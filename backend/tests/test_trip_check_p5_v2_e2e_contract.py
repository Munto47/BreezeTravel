from __future__ import annotations

import asyncio
import os
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.active_contract import (
    P5ContractNotReadyError,
    require_v2_formal_ready,
)
from evals.trip_check_v1.p5.adapters_v2 import validate_materialization_v2
from evals.trip_check_v1.p5.contracts_v2 import P5CaseV2
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.data_contract_v2 import (
    case_set_hash,
    materialization_set_hash,
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


def test_formal_execution_fails_closed_before_active_contract_and_seal() -> None:
    with pytest.raises(P5ContractNotReadyError, match="P5_V2_FORMAL_CONTRACT_NOT_READY"):
        require_v2_formal_ready(P5_ROOT / "active_contract.json")
    assert not (P5_ROOT / "sealed" / "frozen_blind.v2.seal.json").exists()


@pytest.mark.xfail(strict=True, reason="formal v2 active contract and blind seal are not ready")
def test_formal_active_contract_and_seal_readback_are_ready() -> None:
    active = require_v2_formal_ready(P5_ROOT / "active_contract.json")
    seal_path = P5_ROOT / "sealed" / "frozen_blind.v2.seal.json"
    assert seal_path.is_file()
    assert active["blind_seal_v2_sha256"] == file_sha256(seal_path)


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


def test_judge_v2_readback_interface_is_explicitly_unavailable() -> None:
    from evals.trip_check_v1.p5 import judge

    if not hasattr(judge, "export_judge_bundles_v2"):
        pytest.skip("P5 v2 Judge interface is not integrated; v1 Judge cannot prove v2")
    pytest.fail("P5 v2 Judge appeared; replace this placeholder with its readback contract")


def test_gate_v2_readback_interface_is_explicitly_unavailable() -> None:
    from evals.trip_check_v1.p5 import gate

    if not hasattr(gate, "build_p5_gate_manifest_v2"):
        pytest.skip("P5 v2 Gate interface is not integrated; v1 Gate cannot prove v2")
    pytest.fail("P5 v2 Gate appeared; replace this placeholder with its readback contract")
