from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.run_auditor_simulation import (
    EVIDENCE_BOUNDARY,
    PIPELINE_CODE_FILES,
    PIPELINE_CODE_ROOTS,
    REFERENCE_TIME,
    _pipeline_code_binding,
    load_dataset,
    run,
)


def _write_dataset(root: Path, cases: list[dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    normalized_cases = [
        {
            "evidence_boundary": EVIDENCE_BOUNDARY,
            "m1_eligible": False,
            **case,
        }
        for case in cases
    ]
    cases_bytes = (
        "\n".join(json.dumps(case, ensure_ascii=False) for case in normalized_cases) + "\n"
    ).encode("utf-8")
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": "1.0",
        "evidence_boundary": EVIDENCE_BOUNDARY,
        "human_labels": False,
        "case_count": len(cases),
        "cases_file": "cases.jsonl",
        "cases_sha256": hashlib.sha256(cases_bytes).hexdigest(),
    }, ensure_ascii=False), encoding="utf-8")
    (root / "cases.jsonl").write_bytes(cases_bytes)


def _case(case_id: str, raw: str, findings: list[dict] | None = None) -> dict:
    return {
        "case_id": case_id,
        "source_document_id": f"doc-{case_id}",
        "split": "test",
        "city": "北京",
        "trip_days": 2,
        "group_size": 3,
        "source_kind": "SIMULATED_CONTROLLED_MUTATION",
        "raw_itinerary": raw,
        "simulated_organizer_profile": {"pace": "relaxed"},
        "simulated_findings": findings or [],
    }


def test_dataset_rejects_human_or_unlabelled_evidence(tmp_path: Path):
    _write_dataset(tmp_path, [_case("a", "第一天：09:00-11:00 故宫")])
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["human_labels"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="isolated from human evidence"):
        load_dataset(tmp_path)


def test_dataset_rejects_tampered_cases_file(tmp_path: Path):
    _write_dataset(tmp_path, [_case("a", "第一天：09:00-11:00 故宫\n第二天：北京酒店")])
    with (tmp_path / "cases.jsonl").open("ab") as stream:
        stream.write(b" ")

    with pytest.raises(ValueError, match="sha256"):
        load_dataset(tmp_path)


def test_pipeline_code_binding_is_deterministic_and_well_formed():
    first_hash, first_count = _pipeline_code_binding()
    second_hash, second_count = _pipeline_code_binding()

    assert first_hash == second_hash
    assert first_count == second_count > 0
    assert re.fullmatch(r"[0-9a-f]{64}", first_hash)


def test_pipeline_code_binding_includes_explicit_task_spec_dependency(tmp_path: Path):
    for root in PIPELINE_CODE_ROOTS:
        (tmp_path / root).mkdir(parents=True, exist_ok=True)
    task_spec = tmp_path / PIPELINE_CODE_FILES[0]
    task_spec.parent.mkdir(parents=True, exist_ok=True)
    task_spec.write_text("VERSION = 1\n", encoding="utf-8")

    first_hash, first_count = _pipeline_code_binding(tmp_path)
    task_spec.write_text("VERSION = 2\n", encoding="utf-8")
    second_hash, second_count = _pipeline_code_binding(tmp_path)

    assert first_count == second_count == 1
    assert first_hash != second_hash


def test_dataset_rejects_manifest_count_mismatch(tmp_path: Path):
    _write_dataset(tmp_path, [_case("a", "第一天：09:00-11:00 故宫\n第二天：北京酒店")])
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["case_count"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="case count"):
        load_dataset(tmp_path)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"evidence_boundary": "human_calibration"}, "synthetic evidence boundary"),
        ({"m1_eligible": True}, "ineligible for M1"),
        ({"human_label": {"accepted": True}}, "forbidden human evidence field"),
        ({"simulated_organizer_profile": {"is_real_human": True}}, "real-human provenance"),
    ],
)
def test_dataset_rejects_case_boundary_or_human_provenance(
    tmp_path: Path,
    update: dict,
    message: str,
):
    case = {**_case("a", "第一天：09:00-11:00 故宫\n第二天：北京酒店"), **update}
    _write_dataset(tmp_path, [case])

    with pytest.raises(ValueError, match=message):
        load_dataset(tmp_path)


@pytest.mark.asyncio
async def test_runner_uses_real_pipeline_and_keeps_synthetic_boundary(tmp_path: Path):
    dataset = tmp_path / "dataset"
    output = tmp_path / "result.json"
    _write_dataset(dataset, [
        _case(
            "overlap",
            "第一天：09:00-11:00 故宫 → 10:30-12:00 景山公园 → 18:00-19:00 北京饭店\n"
            "第二天：09:00-11:00 颐和园 → 20:00-21:00 北京酒店",
            [{"category": "time_overlap", "is_original_error": False, "injected_by_simulation": True}],
        ),
    ])

    started_at = datetime.now(timezone.utc)
    report = await run(dataset, output)
    finished_at = datetime.now(timezone.utc)

    assert output.is_file()
    assert report["evidence_boundary"] == EVIDENCE_BOUNDARY
    assert report["human_labels"] is False
    assert report["public_claim_eligible"] is False
    assert report["cases_sha256"] == hashlib.sha256((dataset / "cases.jsonl").read_bytes()).hexdigest()
    assert report["manifest_sha256"] == hashlib.sha256((dataset / "manifest.json").read_bytes()).hexdigest()
    assert report["runner_version"]
    assert len(report["runner_code_sha256"]) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", report["pipeline_code_sha256"])
    assert report["pipeline_code_scope"]["roots"] == list(PIPELINE_CODE_ROOTS)
    assert report["pipeline_code_scope"]["files"] == list(PIPELINE_CODE_FILES)
    assert report["pipeline_code_scope"]["python_file_count"] > 0
    generated_at = datetime.fromisoformat(report["generated_at"])
    assert started_at <= generated_at <= finished_at
    assert report["deterministic_reference_time"] == REFERENCE_TIME.isoformat()
    case = report["cases"][0]
    assert case["parser"]["stop_count"] == 5
    assert case["entity_resolution"]["AUTO_MATCHED"] == 5
    assert "time_chain" in case["audit"]["risk_categories"]
    assert case["expected_injected_error_categories"] == ["time_chain"]
    assert case["repair"]["attempted"] is True
    assert case["repair"]["proposed"] >= 1


@pytest.mark.asyncio
async def test_report_has_city_kind_risk_and_error_lists(tmp_path: Path):
    dataset = tmp_path / "dataset"
    _write_dataset(dataset, [
        _case("clean", "第一天：09:00-11:00 故宫\n第二天：09:00-11:00 北京酒店"),
        {
            **_case("missing", "第一天：09:00-11:00 未知地点\n第二天：09:00-11:00 北京酒店", [{"category": "entity_not_found"}]),
            "simulated_repair_decisions": [
                {"decision": "rejected", "rejection_reason": "change_too_large"},
                {"decision": "accepted"},
                {"decision": "skip", "reason_code": "nothing_to_repair"},
            ],
        },
    ])

    report = await run(dataset, tmp_path / "result.json")
    summary = report["summary"]

    assert summary["case_count"] == 2
    assert summary["by_city"]["北京"]["cases"] == 2
    assert summary["by_source_kind"]["SIMULATED_CONTROLLED_MUTATION"]["cases"] == 2
    assert isinstance(summary["detected_risk_categories"], dict)
    assert isinstance(summary["original_errors"]["uncaptured"], list)
    assert isinstance(summary["explicit_false_positives"], list)
    assert isinstance(summary["additional_unlabelled_diagnostics"], list)
    assert set(summary["diagnostics_by_stage"]) == {"parser", "resolution", "audit"}
    assert summary["diagnostic_only"] is True
    assert summary["quality_gate"] is False
    simulated = summary["simulated_repair_decisions_not_human"]
    assert simulated["simulated_acceptance_rate"] == 0.5
    assert simulated["simulated_rejection_reasons"] == {"change_too_large": 1}
    assert simulated["simulated_skipped_count"] == 1
    assert simulated["simulated_skip_reasons"] == {"nothing_to_repair": 1}
    assert simulated["eligible_for_m1_human_repair_adoption"] is False


@pytest.mark.asyncio
async def test_injected_errors_are_scored_by_paired_difference_not_baseline_unknowns(tmp_path: Path):
    dataset = tmp_path / "dataset"
    original = {
        **_case("original", "第一天：09:00-11:00 故宫\n第二天：09:00-11:00 北京酒店"),
        "source_document_id": "paired-doc",
        "source_kind": "SIMULATED_AI_ITINERARY",
    }
    mutation = {
        **_case(
            "mutation",
            "第一天：09:00-11:00 故宫 → 10:30-12:00 景山公园\n第二天：09:00-11:00 北京酒店",
            [{"category": "time_overlap", "injected_by_simulation": True}],
        ),
        "source_document_id": "paired-doc",
    }
    boundary = {
        **_case("boundary", "第一天：下午 待确认地点\n第二天：北京酒店"),
        "source_document_id": "boundary-doc",
        "source_kind": "SIMULATED_BOUNDARY",
        "simulated_findings": [{"category": "input_ambiguous", "injected_by_simulation": True}],
    }
    _write_dataset(dataset, [original, mutation, boundary])

    report = await run(dataset, tmp_path / "result.json")
    paired = report["summary"]["injected_errors_paired_difference"]

    assert paired["pair_count"] == 1
    assert paired["pairs"][0]["captured_injected_error_categories"] == ["time_chain"]
    # Baseline evidence UNKNOWNs are not classified as false positives.
    assert "opening_hours" not in paired["pairs"][0]["explicit_false_positive_risk_categories"]
    assert report["summary"]["boundary_confirmation"]["requires_confirmation_count"] == 1


@pytest.mark.asyncio
async def test_parse_failure_is_scored_at_parser_stage_without_audit(tmp_path: Path):
    dataset = tmp_path / "dataset"
    case = {
        **_case(
            "parse-failed",
            "说明：仅有模糊需求，地点和时间都未提供。",
            [{
                "reason_code": "IMPORT_PARSE_FAILED",
                "expected_rule_id": "import.parser",
                "provenance": "original_error",
            }],
        ),
        "source_kind": "SIMULATED_BOUNDARY",
    }
    _write_dataset(dataset, [case])

    report = await run(dataset, tmp_path / "result.json")
    result = report["cases"][0]

    assert result["audit"]["executed"] is False
    assert result["audit"]["skip_reason"] == "IMPORT_PARSE_FAILED"
    assert result["diagnostics_by_stage"]["parser"]["captured_original"] == [
        "import_parse_failed"
    ]
    assert result["original_error_categories_uncaptured"] == []


@pytest.mark.asyncio
async def test_environment_unknown_is_not_additional_but_stays_visible(tmp_path: Path):
    dataset = tmp_path / "dataset"
    case = _case(
        "honest-unknown",
        "第一天：09:00-11:00 故宫 → 12:15-13:15 北京模拟餐厅1 → "
        "14:00-16:00 景山公园 → 18:30-19:30 北京模拟餐厅2 → 21:00-22:00 北京模拟酒店1\n"
        "第二天：09:00-11:00 颐和园 → 12:15-13:15 北京模拟餐厅3 → "
        "14:00-16:00 天坛公园 → 18:30-19:30 北京模拟餐厅4 → 21:00-22:00 北京模拟酒店2",
        [
            {"reason_code": "OPENING_HOURS_MISSING", "provenance": "environment_unknown"},
            {"reason_code": "WEATHER_DATA_MISSING", "provenance": "environment_unknown"},
        ],
    )
    _write_dataset(dataset, [case])

    report = await run(dataset, tmp_path / "result.json")
    result = report["cases"][0]

    assert result["additional_unlabelled_diagnostics"] == []
    assert result["honest_unknown_categories"] == ["opening_hours", "weather_exposure"]
    assert report["summary"]["honest_unknown"]["case_count"] == 1
