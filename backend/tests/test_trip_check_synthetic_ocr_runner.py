from __future__ import annotations

from pathlib import Path

import pytest

from app.importing.screenshots import OcrBoundingBox, OcrTextLine
from evals.trip_check_v1 import synthetic_ocr_runner
from evals.trip_check_v1.synthetic_ocr_runner import (
    _field_confirmation_required,
    _field_matches,
    _expected_visible_text,
    _load_spec,
    _safe_cleanup,
    run_synthetic_ocr,
)


FIXTURE = Path(__file__).parents[1] / "evals" / "fixtures" / "trip_check_ocr_synthetic_v2.json"


class FixtureOcrEngine:
    name = "fixture-render-readback"
    version = "v1"

    def __init__(self, texts: dict[str, str]):
        self.texts = texts

    async def recognize(self, image_path: Path) -> list[OcrTextLine]:
        case_id = image_path.stem
        return [
            OcrTextLine(
                text=self.texts[case_id],
                confidence=0.72 if case_id.endswith("03") or case_id.endswith("04") else 0.99,
                box=OcrBoundingBox(x_min=1, y_min=1, x_max=1000, y_max=2000),
                requires_confirmation=case_id.endswith("03") or case_id.endswith("04"),
            )
        ]


def test_frozen_oracle_fields_are_recoverable_from_source_text():
    payload, _ = _load_spec(FIXTURE)

    unmatched = [
        field["field_id"]
        for case in payload["cases"]
        for field in case["oracle"]["key_fields"]
        if not _field_matches(field, "\n".join(block["text"] for block in case["text_blocks"]))
    ]

    assert unmatched == []


@pytest.mark.asyncio
async def test_synthetic_ocr_runner_scores_fields_and_cleans_images(tmp_path):
    payload, _ = _load_spec(FIXTURE)
    texts = {
        case["case_id"]: _expected_visible_text(case)
        for case in payload["cases"]
    }
    work_root = tmp_path / "work"
    output = tmp_path / "manifest.json"

    manifest = await run_synthetic_ocr(
        spec_path=FIXTURE,
        output=output,
        work_root=work_root,
        subject_commit="a" * 40,
        engine=FixtureOcrEngine(texts),
    )

    assert manifest["status"] == "PASS"
    assert manifest["evidence_class"] == "synthetic_stress"
    assert manifest["metrics"]["case_count"] == 12
    assert manifest["metrics"]["key_field_f1"] >= 0.95
    assert manifest["metrics"]["ocr_text_micro_precision"] == 1.0
    assert manifest["metrics"]["ocr_text_micro_recall"] == 1.0
    assert manifest["metrics"]["low_confidence_confirmation_recall"] == 1.0
    assert manifest["metrics"]["original_image_leak_hits"] == 0
    assert manifest["schema_version"] == "trip-check-p3-synthetic-ocr-manifest-v2"
    assert len(manifest["render_set_sha256"]) == 64
    assert manifest["render_integrity"]["status"] == "PASS"
    assert manifest["render_integrity"]["review_type"] == "deterministic_automated"
    assert manifest["cleanup_receipt"] == {
        "status": "DELETED",
        "reason": "terminal_ocr_run",
        "run_dir_removed": True,
    }
    assert manifest["non_claims"]
    assert output.is_file()
    assert not list(work_root.glob("*"))


@pytest.mark.asyncio
async def test_synthetic_ocr_runner_marks_cleanup_failure_privacy_blocked(monkeypatch, tmp_path):
    payload, _ = _load_spec(FIXTURE)
    texts = {
        case["case_id"]: _expected_visible_text(case)
        for case in payload["cases"]
    }
    work_root = tmp_path / "work"

    def fail_cleanup(*_args):
        raise PermissionError("controlled cleanup failure")

    monkeypatch.setattr(synthetic_ocr_runner, "_safe_cleanup", fail_cleanup)
    manifest = await run_synthetic_ocr(
        spec_path=FIXTURE,
        output=tmp_path / "manifest.json",
        work_root=work_root,
        subject_commit="b" * 40,
        engine=FixtureOcrEngine(texts),
    )

    assert manifest["status"] == "PRIVACY_BLOCKED"
    assert manifest["cleanup_receipt"]["status"] == "CLEANUP_FAILED"
    assert manifest["cleanup_receipt"]["error_category"] == "PermissionError"
    assert manifest["metrics"]["original_image_leak_hits"] == 12


def test_safe_cleanup_refuses_root_or_outside(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ValueError):
        _safe_cleanup(root, root)
    with pytest.raises(ValueError):
        _safe_cleanup(tmp_path / "outside", root)


def test_confirmation_is_scoped_to_the_field_source_line():
    lines = [
        OcrTextLine(
            text="09:00故宫博物院",
            confidence=0.99,
            box=OcrBoundingBox(x_min=1, y_min=1, x_max=10, y_max=10),
            requires_confirmation=False,
        ),
        OcrTextLine(
            text="酒店具体店名待确认",
            confidence=0.70,
            box=OcrBoundingBox(x_min=1, y_min=200, x_max=10, y_max=210),
            requires_confirmation=True,
        ),
    ]

    assert _field_confirmation_required(
        {"type": "time", "value": "09:00"},
        lines,
    ) is False


def test_confirmation_includes_a_wrapped_neighbor_line_in_the_same_visual_block():
    lines = [
        OcrTextLine(
            text="返程究竟是15:10",
            confidence=0.99,
            box=OcrBoundingBox(x_min=10, y_min=10, x_max=500, y_max=50),
            requires_confirmation=False,
        ),
        OcrTextLine(
            text="还是15:40，请确认",
            confidence=0.99,
            box=OcrBoundingBox(x_min=10, y_min=55, x_max=500, y_max=95),
            requires_confirmation=False,
        ),
    ]

    assert _field_confirmation_required(
        {"type": "time", "value": ["15:10", "15:40"]},
        lines,
    ) is True


def test_confirmation_uses_the_rendered_source_block_without_leaking_to_other_blocks():
    lines = [
        OcrTextLine(
            text="住宿写作湖滨附近连锁酒店",
            confidence=0.99,
            box=OcrBoundingBox(x_min=20, y_min=20, x_max=480, y_max=60),
            requires_confirmation=False,
        ),
        OcrTextLine(
            text="品牌尚未确认",
            confidence=0.99,
            box=OcrBoundingBox(x_min=20, y_min=140, x_max=480, y_max=180),
            requires_confirmation=False,
        ),
        OcrTextLine(
            text="另一个气泡待确认",
            confidence=0.70,
            box=OcrBoundingBox(x_min=20, y_min=500, x_max=480, y_max=540),
            requires_confirmation=True,
        ),
    ]

    assert _field_confirmation_required(
        {"type": "hotel", "value": "湖滨附近连锁酒店"},
        lines,
        source_blocks=[
            {"block_id": "b01", "text": "住宿写作湖滨附近连锁酒店，品牌尚未确认。"},
            {"block_id": "b02", "text": "另一个气泡待确认。"},
        ],
        rendered_block_boxes=[
            {"block_id": "b01", "x_min": 0, "y_min": 0, "x_max": 500, "y_max": 200},
            {"block_id": "b02", "x_min": 0, "y_min": 450, "x_max": 500, "y_max": 560},
        ],
    ) is True
