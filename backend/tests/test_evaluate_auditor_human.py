from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from scripts.evaluate_auditor_human import evaluate


BOUNDARY = (
    "Only a real organizer may add source text and human findings. Synthetic, model-generated, "
    "Judge, or agent labels must not increment human counts."
)


def _manifest(**updates: object) -> dict:
    value = {
        "schema_version": "auditor-human-v1",
        "status": "awaiting_human_collection",
        "scope": {
            "cities": ["北京", "上海", "杭州"],
            "trip_days": {"min": 2, "max": 5},
            "group_size": {"min": 2, "max": 5},
        },
        "required_real_itineraries_for_p0": 10,
        "required_real_itineraries_for_m1": 30,
        "required_real_organizers_for_m1": {"min": 15, "max": 20},
        "collected_real_itineraries": 0,
        "human_labeled_itineraries": 0,
        "real_organizers": 0,
        "case_schema": "case.schema.json",
        "prediction_schema": "prediction.schema.json",
        "cases": [],
        "evidence_boundary": BOUNDARY,
    }
    value.update(updates)
    return value


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_human_bundle(
    root: Path,
    *,
    organizer_count: int = 15,
    human_check_available: bool = True,
    split_origins: bool = False,
    prediction_update: dict | None = None,
) -> Path:
    entries = []
    for index in range(30):
        case_id = f"bundle-{index:02d}"
        human_finding_id = f"finding-{index:02d}"
        case_path = Path("cases") / f"{case_id}.json"
        prediction_path = Path("predictions") / f"{case_id}.json"
        _write(root / case_path, {
            "case_id": case_id,
            "source_document_id": f"source-{index:02d}",
            "city": ["北京", "上海", "杭州"][index % 3],
            "trip_days": 3,
            "group_size": 3,
            "raw_itinerary": "真实行程占位文本",
            "source_kind": "REAL_AI_ITINERARY",
            "organizer_id_hash": hashlib.sha256(
                f"organizer-{index % organizer_count}".encode()
            ).hexdigest(),
            "consent_recorded": True,
            "human_findings": [{
                "finding_id": human_finding_id,
                "category": "ROUTE",
                "severity": "HIGH",
                "description": "真人确认的关键问题",
                "is_original_error": not split_origins or index % 2 == 0,
            }],
        })
        prediction = {
            "case_id": case_id,
            "audit_duration_seconds": 60,
            "extraction_counts": {
                field: {"tp": 1, "fp": 0, "fn": 0}
                for field in ("date", "time", "place", "fixed_commitment")
            },
            "auto_matches": {"accepted": 1, "correct": 1},
            "fixed_commitments": {"expected": 1, "recalled": 1},
            "silent_mismatches": 0,
            "findings": [{
                "prediction_id": f"prediction-{index:02d}",
                "severity": "HIGH",
                "human_verdict": "CORRECT",
                "matched_human_finding_ids": [human_finding_id],
                "evidence_readable": True,
            }],
            "critical_human_check": (
                {
                    "status": "AVAILABLE",
                    "review_protocol": "independent_critical_finding_check_v1",
                    "checked": 1,
                    "correct": 1,
                }
                if human_check_available
                else {"status": "UNAVAILABLE", "reason": "independent review not run"}
            ),
            "repair": {"offered": False, "accepted": False, "rejection_reason": None},
        }
        prediction.update(prediction_update or {})
        _write(root / prediction_path, prediction)
        entries.append({
            "case_id": case_id,
            "case_path": str(case_path).replace("\\", "/"),
            "prediction_path": str(prediction_path).replace("\\", "/"),
        })
    manifest_path = root / "manifest.json"
    _write(manifest_path, _manifest(
        status="ready_for_evaluation",
        collected_real_itineraries=30,
        human_labeled_itineraries=30,
        real_organizers=organizer_count,
        cases=entries,
    ))
    return manifest_path


def test_empty_manifest_is_blocked_instead_of_reporting_zero_quality(tmp_path):
    manifest = tmp_path / "manifest.json"
    _write(manifest, _manifest())

    result = evaluate(manifest)

    assert result["status"] == "BLOCKED_HUMAN_DATA"
    assert result["gates_passed"] is False
    assert "30 real itineraries" in result["reason"]


def test_simulated_dataset_manifest_cannot_enter_human_m1_lane():
    simulated_manifest = (
        Path(__file__).resolve().parents[1]
        / "eval_data"
        / "auditor_simulated"
        / "manifest.json"
    )

    result = evaluate(simulated_manifest)

    assert result["status"] == "BLOCKED_HUMAN_DATA"
    assert result["gates_passed"] is False
    assert result["human_labeled_itineraries"] == 0
    assert result["real_organizers"] == 0
    assert result["reason"] == "manifest lacks the exact human-only evidence boundary"


def test_complete_human_bundle_computes_m1_gates_without_judge(tmp_path):
    entries = []
    for index in range(30):
        case_id = f"human-{index:02d}"
        case_path = Path("cases") / f"{case_id}.json"
        prediction_path = Path("predictions") / f"{case_id}.json"
        human_finding_id = f"human-finding-{index:02d}"
        _write(tmp_path / case_path, {
            "case_id": case_id,
            "source_document_id": f"source-{index:02d}",
            "city": ["北京", "上海", "杭州"][index % 3],
            "trip_days": 3,
            "group_size": 3,
            "raw_itinerary": "真实行程占位文本",
            "source_kind": "REAL_AI_ITINERARY",
            "organizer_id_hash": hashlib.sha256(f"organizer-{index % 15}".encode()).hexdigest(),
            "consent_recorded": True,
            "human_findings": [{
                "finding_id": human_finding_id,
                "category": "ROUTE",
                "severity": "HIGH",
                "description": "真人确认的关键问题",
                "is_original_error": True,
            }],
        })
        _write(tmp_path / prediction_path, {
            "case_id": case_id,
            "audit_duration_seconds": 40 + index,
            "extraction_counts": {
                field: {"tp": 3, "fp": 0, "fn": 0}
                for field in ("date", "time", "place", "fixed_commitment")
            },
            "auto_matches": {"accepted": 2, "correct": 2},
            "fixed_commitments": {"expected": 1, "recalled": 1},
            "silent_mismatches": 0,
            "findings": [{
                "prediction_id": f"prediction-{index:02d}",
                "severity": "HIGH",
                "human_verdict": "CORRECT",
                "matched_human_finding_ids": [human_finding_id],
                "evidence_readable": True,
            }],
            "critical_human_check": {
                "status": "AVAILABLE",
                "review_protocol": "independent_critical_finding_check_v1",
                "checked": 1,
                "correct": 1,
            },
            "repair": {
                "offered": True,
                "accepted": index < 12,
                "rejection_reason": None if index < 12 else "偏移时间过大",
            },
        })
        entries.append({
            "case_id": case_id,
            "case_path": str(case_path).replace("\\", "/"),
            "prediction_path": str(prediction_path).replace("\\", "/"),
        })
    manifest = tmp_path / "manifest.json"
    _write(manifest, _manifest(
        status="ready_for_evaluation",
        collected_real_itineraries=30,
        human_labeled_itineraries=30,
        real_organizers=15,
        cases=entries,
    ))

    result = evaluate(manifest)

    assert result["status"] == "M1_PASSED"
    assert result["gates_passed"] is True
    assert result["metrics"]["critical_precision"] == 1.0
    assert result["metrics"]["critical_recall"] == 1.0
    assert result["metrics"]["critical_evidence_readback_rate"] == 1.0
    assert result["metrics"]["repair_adoption_rate"] == 0.4
    assert result["metrics"]["repair_rejection_reasons"] == {"偏移时间过大": 18}
    assert result["evidence_boundary"] == "human labels only; no LLM-as-Judge"
    assert result["derived_unique_source_documents"] == 30
    assert result["derived_unique_organizers"] == 15


def test_manifest_cannot_inflate_human_counts_without_case_files(tmp_path):
    manifest = tmp_path / "manifest.json"
    _write(manifest, _manifest(
        collected_real_itineraries=30,
        human_labeled_itineraries=30,
        real_organizers=15,
    ))

    result = evaluate(manifest)

    assert result["status"] == "BLOCKED_HUMAN_DATA"
    assert result["reason"] == "fewer than 30 real itineraries have human labels"


def test_dataset_paths_cannot_escape_manifest_directory(tmp_path):
    manifest = tmp_path / "manifest.json"
    entries = [{
        "case_id": f"escape-{index}",
        "case_path": "../case.json" if index == 0 else f"cases/{index}.json",
        "prediction_path": "../prediction.json" if index == 0 else f"predictions/{index}.json",
    } for index in range(30)]
    _write(manifest, _manifest(
        status="ready_for_evaluation",
        collected_real_itineraries=30,
        human_labeled_itineraries=30,
        real_organizers=15,
        cases=entries,
    ))

    try:
        evaluate(manifest)
    except ValueError as exc:
        assert "escapes manifest directory" in str(exc)
    else:
        raise AssertionError("path traversal must be rejected")


def test_manifest_cannot_lower_fixed_m1_sample_thresholds(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    _write(manifest, _manifest(
        required_real_itineraries_for_m1=1,
        required_real_organizers_for_m1={"min": 1, "max": 1},
    ))

    with pytest.raises(ValueError, match="manifest schema validation failed"):
        evaluate(manifest)


def test_manifest_rejects_more_than_twenty_organizers(tmp_path: Path):
    manifest = _write_human_bundle(tmp_path, organizer_count=21)

    with pytest.raises(ValueError, match="manifest schema validation failed"):
        evaluate(manifest)


def test_prediction_schema_rejects_negative_metric_counts(tmp_path: Path):
    manifest = _write_human_bundle(
        tmp_path,
        prediction_update={
            "extraction_counts": {
                field: {"tp": -1, "fp": 0, "fn": 0}
                for field in ("date", "time", "place", "fixed_commitment")
            }
        },
    )

    with pytest.raises(ValueError, match="prediction .* schema validation failed"):
        evaluate(manifest)


def test_independent_human_check_unavailable_fails_closed(tmp_path: Path):
    manifest = _write_human_bundle(tmp_path, human_check_available=False)

    result = evaluate(manifest)

    assert result["status"] == "M1_FAILED"
    assert result["gates"]["critical_human_check_accuracy_at_least_0_85"] is False
    assert result["metrics"]["critical_human_check_status"] == "UNAVAILABLE"
    assert result["metrics"]["critical_human_check_accuracy"] is None


def test_original_and_controlled_injected_critical_errors_are_reported_separately(
    tmp_path: Path,
):
    manifest = _write_human_bundle(tmp_path, split_origins=True)

    result = evaluate(manifest)

    assert result["metrics"]["critical_counts_by_origin"] == {
        "original": {"human_expected": 15, "matched": 15},
        "controlled_injected": {"human_expected": 15, "matched": 15},
    }
    assert result["metrics"]["critical_recall_by_origin"] == {
        "original": 1.0,
        "controlled_injected": 1.0,
    }
