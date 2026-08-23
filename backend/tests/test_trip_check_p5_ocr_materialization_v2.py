from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.importing.errors import OcrProcessingError
from app.importing.screenshots import OcrBoundingBox, OcrTextLine, PaddleOcrEngine
from evals.trip_check_v1.p5.ocr_materialization_v2 import (
    materialize_ocr_input,
    validate_ocr_materialization,
)


class InspectingOcrEngine:
    name = "controlled-production-boundary"
    version = "1.0"

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.observed: list[tuple[str, bytes]] = []

    async def recognize(self, image_path: Path) -> list[OcrTextLine]:
        assert image_path.is_file()
        self.observed.append((image_path.suffix, image_path.read_bytes()[:12]))
        if self.fail:
            raise OcrProcessingError("controlled OCR failure")
        return [
            OcrTextLine(
                text="北京三日行程，故宫待确认",
                confidence=0.72,
                box=OcrBoundingBox(x_min=10, y_min=20, x_max=420, y_max=80),
                requires_confirmation=True,
            )
        ]


def _product_input(image_format: str, *, seed: int = 17) -> dict:
    source_text = "北京三日行程\n第一天 09:00 故宫博物院\n住宿地点待确认"
    return {
        "source_type": "SYNTHETIC_SCREENSHOT",
        "source_text": source_text,
        "render_spec": {
            "schema_version": "trip-check-p5-render-spec-v2",
            "format": image_format,
            "theme": "LIGHT",
            "layout": "CHAT",
            "width": 480,
            "height": 640,
            "seed": seed,
            "text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image_format", "suffix", "signature"),
    [
        ("PNG", ".png", b"\x89PNG\r\n\x1a\n"),
        ("JPEG", ".jpg", b"\xff\xd8\xff"),
        ("WEBP", ".webp", b"RIFF"),
    ],
)
async def test_materializes_deterministic_formats_through_production_boundary(
    tmp_path,
    image_format,
    suffix,
    signature,
):
    engine_a = InspectingOcrEngine()
    engine_b = InspectingOcrEngine()
    product_input = _product_input(image_format)

    first = await materialize_ocr_input(
        product_input,
        case_id=f"p5.{image_format.lower()}.01",
        work_root=tmp_path / "first",
        ocr_engine=engine_a,
    )
    second = await materialize_ocr_input(
        product_input,
        case_id=f"p5.{image_format.lower()}.01",
        work_root=tmp_path / "second",
        ocr_engine=engine_b,
    )

    assert first["status"] == "SUCCEEDED"
    assert engine_a.observed[0][0] == suffix
    assert engine_a.observed[0][1].startswith(signature)
    if image_format == "WEBP":
        assert engine_a.observed[0][1][8:12] == b"WEBP"
    assert first["hashes"]["image_sha256"] == second["hashes"]["image_sha256"]
    assert first["hashes"]["semantic_replay_sha256"] == second["hashes"]["semantic_replay_sha256"]
    assert first["semantic_replay_hash"] == first["hashes"]["semantic_replay_sha256"]
    assert first["render_receipt"]["render_id"] != second["render_receipt"]["render_id"]
    assert first["cleanup_receipt"]["original_removed"] is True
    assert first["cleanup_receipt"]["cleanup_status"] == "DELETED"
    assert not list((tmp_path / "first").glob("*"))
    json.dumps(first, ensure_ascii=False)
    assert validate_ocr_materialization(first, product_input=product_input) == first


@pytest.mark.asyncio
async def test_default_engine_is_the_production_paddle_boundary(monkeypatch, tmp_path):
    observed: dict[str, object] = {}

    async def controlled_recognize(self, image_path):
        observed["engine"] = self
        observed["path_existed"] = image_path.is_file()
        return [
            OcrTextLine(
                text="上海两日行程",
                confidence=0.99,
                box=OcrBoundingBox(x_min=1, y_min=1, x_max=200, y_max=50),
            )
        ]

    monkeypatch.setattr(PaddleOcrEngine, "recognize", controlled_recognize)
    result = await materialize_ocr_input(
        _product_input("PNG"),
        case_id="p5.png.paddle",
        work_root=tmp_path,
    )

    assert isinstance(observed["engine"], PaddleOcrEngine)
    assert observed["path_existed"] is True
    assert result["ocr_baseline_receipt"]["engine"] == "paddleocr"
    assert result["cleanup_receipt"]["original_removed"] is True


@pytest.mark.asyncio
async def test_ocr_failure_still_deletes_original_and_returns_strict_receipts(tmp_path):
    result = await materialize_ocr_input(
        _product_input("PNG"),
        case_id="p5.png.failure",
        work_root=tmp_path,
        ocr_engine=InspectingOcrEngine(fail=True),
    )

    assert result["status"] == "FAILED"
    assert result["error_category"] == "OcrProcessingError"
    assert result["ocr_baseline_receipt"] is None
    assert result["hashes"]["ocr_baseline_receipt_sha256"] is None
    assert result["cleanup_receipt"]["terminal_reason"] == "FAILED"
    assert result["cleanup_receipt"]["cleanup_status"] == "DELETED"
    assert result["cleanup_receipt"]["original_removed"] is True
    assert not list(tmp_path.glob("*"))


@pytest.mark.asyncio
async def test_low_confidence_confirmation_survives_receipt_and_semantic_projection(tmp_path):
    result = await materialize_ocr_input(
        _product_input("PNG"),
        case_id="p5.png.confirm",
        work_root=tmp_path,
        ocr_engine=InspectingOcrEngine(),
    )

    line = result["ocr_baseline_receipt"]["lines"][0]
    projected_line = result["semantic_projection"]["ocr"]["lines"][0]
    assert line["confidence"] == 0.72
    assert line["requires_confirmation"] is True
    assert projected_line["requires_confirmation"] is True

    tampered = {**result, "ocr_baseline_receipt": {**result["ocr_baseline_receipt"]}}
    tampered["ocr_baseline_receipt"]["lines"] = [{**line, "requires_confirmation": False}]
    with pytest.raises(ValueError, match="lost requires_confirmation"):
        validate_ocr_materialization(tampered)


@pytest.mark.asyncio
async def test_rejects_precomputed_ocr_text_before_writing_an_original(tmp_path):
    product_input = {**_product_input("PNG"), "ocr_text": "shortcut"}

    with pytest.raises(ValueError, match="precomputed ocr_text is forbidden"):
        await materialize_ocr_input(
            product_input,
            case_id="p5.png.shortcut",
            work_root=tmp_path,
            ocr_engine=InspectingOcrEngine(),
        )

    assert not list(tmp_path.iterdir())
