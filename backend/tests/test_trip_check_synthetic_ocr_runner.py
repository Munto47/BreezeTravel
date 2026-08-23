from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.importing.screenshots import OcrBoundingBox, OcrTextLine
from evals.trip_check_v1.synthetic_ocr_runner import _field_matches, _safe_cleanup, run_synthetic_ocr


FIXTURE = Path(__file__).parents[1] / "evals" / "fixtures" / "trip_check_ocr_synthetic_v1.json"


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
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    unmatched = [
        field["field_id"]
        for case in payload["cases"]
        for field in case["oracle"]["key_fields"]
        if not _field_matches(field, "\n".join(block["text"] for block in case["text_blocks"]))
    ]

    assert unmatched == []


@pytest.mark.asyncio
async def test_synthetic_ocr_runner_scores_fields_and_cleans_images(tmp_path):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    texts = {
        case["case_id"]: "\n".join(block["text"] for block in case["text_blocks"])
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
        visual_review_approved=True,
    )

    assert manifest["status"] == "PASS"
    assert manifest["evidence_class"] == "synthetic_stress"
    assert manifest["metrics"]["case_count"] == 12
    assert manifest["metrics"]["key_field_f1"] >= 0.95
    assert manifest["metrics"]["low_confidence_confirmation_recall"] == 1.0
    assert manifest["metrics"]["original_image_leak_hits"] == 0
    assert manifest["non_claims"]
    assert output.is_file()
    assert not list(work_root.glob("*"))


def test_safe_cleanup_refuses_root_or_outside(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ValueError):
        _safe_cleanup(root, root)
    with pytest.raises(ValueError):
        _safe_cleanup(tmp_path / "outside", root)
