"""Eval-only deterministic screenshot rendering and production OCR materialization.

The module deliberately does not accept precomputed OCR text. Synthetic screenshots
cross the same image validation, Paddle OCR interface, temporary-asset path, and
cleanup boundary used by :class:`ScreenshotImportService`.
"""

from __future__ import annotations

import hashlib
import io
import os
import random
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from app.importing.errors import OcrProcessingError
from app.importing.screenshots import (
    InMemoryScreenshotAssetRepository,
    OcrEngine,
    PaddleOcrEngine,
    ScreenshotImportService,
    ScreenshotOcrReceipt,
    ScreenshotUpload,
    TemporaryAssetRecord,
    validate_screenshot_batch,
)
from evals.trip_check_v1.p5.data_contract import digest


RENDERER_NAME = "pillow-p5-synthetic-screenshot"
RENDERER_VERSION = "2.0.0"
MATERIALIZATION_SCHEMA_VERSION = "trip-check-p5-ocr-materialization-v2"
RENDER_RECEIPT_SCHEMA_VERSION = "trip-check-p5-render-receipt-v2"
OCR_RECEIPT_SCHEMA_VERSION = "trip-check-p5-ocr-baseline-receipt-v2"
CLEANUP_RECEIPT_SCHEMA_VERSION = "trip-check-p5-cleanup-receipt-v2"
SEMANTIC_REPLAY_POLICY = "p5-ocr-semantic-projection-v2"
CONFIRMATION_THRESHOLD = 0.85

_FORMAT_FACTS = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "WEBP": ("image/webp", ".webp"),
}
_THEMES = {
    "LIGHT": {
        "background": "#F3F5F8",
        "surface": "#FFFFFF",
        "primary": "#2356A8",
        "text": "#17212B",
        "muted": "#667085",
        "line": "#D0D5DD",
    },
    "DARK": {
        "background": "#14181F",
        "surface": "#222936",
        "primary": "#8BB8FF",
        "text": "#F3F5F8",
        "muted": "#B4BBC6",
        "line": "#465064",
    },
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _font_path(explicit: Path | None = None) -> Path:
    candidates = (
        explicit,
        Path(os.environ["P5_OCR_FONT_PATH"]) if os.getenv("P5_OCR_FONT_PATH") else None,
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    )
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("a Chinese font is required; set P5_OCR_FONT_PATH")


def _validate_product_input(product_input: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    if "ocr_text" in product_input:
        raise ValueError("precomputed ocr_text is forbidden for screenshot materialization")
    if product_input.get("source_type") != "SYNTHETIC_SCREENSHOT":
        raise ValueError("OCR materialization only accepts SYNTHETIC_SCREENSHOT input")
    source_text = product_input.get("source_text")
    if not isinstance(source_text, str) or not source_text.strip():
        raise ValueError("source_text must be a non-empty string")
    render_spec = product_input.get("render_spec")
    if not isinstance(render_spec, Mapping):
        raise ValueError("render_spec must be an object")
    spec = dict(render_spec)
    required = {
        "schema_version",
        "format",
        "theme",
        "layout",
        "width",
        "height",
        "seed",
        "text_sha256",
    }
    if set(spec) != required:
        raise ValueError(f"render_spec fields must be exactly {sorted(required)}")
    if spec["schema_version"] != "trip-check-p5-render-spec-v2":
        raise ValueError("unsupported render_spec schema_version")
    if spec["format"] not in _FORMAT_FACTS:
        raise ValueError("render format must be PNG, JPEG, or WEBP")
    if spec["theme"] not in _THEMES:
        raise ValueError("render theme must be LIGHT or DARK")
    if spec["layout"] not in {"CHAT", "MEMO", "GUIDE"}:
        raise ValueError("render layout must be CHAT, MEMO, or GUIDE")
    if not isinstance(spec["width"], int) or not isinstance(spec["height"], int):
        raise ValueError("render dimensions must be integers")
    if not 240 <= spec["width"] <= 4096 or not 320 <= spec["height"] <= 4096:
        raise ValueError("render dimensions are outside the eval-safe range")
    if not isinstance(spec["seed"], int):
        raise ValueError("render seed must be an integer")
    source_text_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    if spec["text_sha256"] != source_text_sha256:
        raise ValueError("render_spec text_sha256 does not bind source_text")
    return source_text, spec


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for character in paragraph:
            candidate = current + character
            if current and draw.textlength(candidate, font=font) > width:
                lines.append(current)
                current = character
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines


def _render_image(
    source_text: str,
    render_spec: Mapping[str, Any],
    *,
    font_path: Path,
) -> tuple[bytes, dict[str, Any]]:
    width = int(render_spec["width"])
    height = int(render_spec["height"])
    palette = _THEMES[str(render_spec["theme"])]
    layout = str(render_spec["layout"])
    rng = random.Random(int(render_spec["seed"]))
    image = Image.new("RGB", (width, height), palette["background"])
    draw = ImageDraw.Draw(image)
    margin = max(16, width // 18)
    header_height = max(48, height // 14)
    font_size = max(15, min(34, width // 30))
    title_font = ImageFont.truetype(str(font_path), max(font_size + 4, int(font_size * 1.18)))
    body_font = ImageFont.truetype(str(font_path), font_size)
    small_font = ImageFont.truetype(str(font_path), max(12, font_size - 4))

    draw.rectangle((0, 0, width, header_height), fill=palette["surface"])
    titles = {"CHAT": "行程群聊", "MEMO": "行程备忘", "GUIDE": "行程说明"}
    draw.text((margin, max(8, (header_height - font_size) // 2)), titles[layout], font=title_font, fill=palette["text"])
    draw.line((0, header_height, width, header_height), fill=palette["line"], width=max(1, width // 540))

    content_top = header_height + margin
    content_width = width - 2 * margin
    line_height = max(font_size + 8, int(font_size * 1.5))
    wrapped = _wrap_text(draw, source_text, body_font, max(80, content_width - 2 * margin))
    max_lines = max(1, (height - content_top - 2 * margin) // line_height)
    if len(wrapped) > max_lines:
        raise ValueError("source_text does not fit render dimensions without truncation")

    if layout == "CHAT":
        bubble_left = margin + rng.randint(0, max(1, margin // 3))
        bubble_right = width - margin
        bubble_bottom = content_top + len(wrapped) * line_height + 2 * margin
        draw.rounded_rectangle(
            (bubble_left, content_top, bubble_right, bubble_bottom),
            radius=max(10, width // 45),
            fill=palette["surface"],
            outline=palette["line"],
        )
        text_x = bubble_left + margin
        text_y = content_top + margin
    elif layout == "MEMO":
        draw.rounded_rectangle(
            (margin, content_top, width - margin, height - margin),
            radius=max(8, width // 60),
            fill=palette["surface"],
            outline=palette["line"],
        )
        text_x = 2 * margin
        text_y = content_top + margin
        for y in range(text_y + line_height, height - margin, line_height):
            draw.line((text_x, y, width - 2 * margin, y), fill=palette["line"], width=1)
    else:
        accent_width = max(5, width // 90)
        draw.rectangle((margin, content_top, margin + accent_width, height - margin), fill=palette["primary"])
        text_x = margin * 2
        text_y = content_top

    for index, line in enumerate(wrapped):
        draw.text((text_x, text_y + index * line_height), line, font=body_font, fill=palette["text"])
    footer = f"#{int(render_spec['seed']) % 10000:04d}"
    draw.text((width - margin - draw.textlength(footer, font=small_font), height - margin), footer, font=small_font, fill=palette["muted"])

    output = io.BytesIO()
    image_format = str(render_spec["format"])
    if image_format == "PNG":
        image.save(output, format="PNG", compress_level=9, optimize=False)
    elif image_format == "JPEG":
        image.save(output, format="JPEG", quality=90, subsampling=0, optimize=False, progressive=False)
    else:
        image.save(output, format="WEBP", quality=90, method=6, lossless=False, exact=True)
    payload = output.getvalue()
    return payload, {
        "line_count": len(wrapped),
        "font_sha256": hashlib.sha256(font_path.read_bytes()).hexdigest(),
        "font_size": font_size,
    }


def _semantic_projection(materialization: Mapping[str, Any]) -> dict[str, Any]:
    render_receipt = materialization["render_receipt"]
    ocr_receipt = materialization.get("ocr_baseline_receipt")
    cleanup_receipt = materialization["cleanup_receipt"]
    projection: dict[str, Any] = {
        "policy": SEMANTIC_REPLAY_POLICY,
        "case_id": materialization["case_id"],
        "status": materialization["status"],
        "source_text_sha256": materialization["source_payload"]["source_text_sha256"],
        "render": {
            key: value
            for key, value in render_receipt.items()
            if key not in {"render_id", "rendered_at"}
        },
        "ocr": None,
        "cleanup": {
            key: value
            for key, value in cleanup_receipt.items()
            if key not in {"receipt_id", "asset_id", "cleanup_attempted_at"}
        },
        "error_category": materialization.get("error_category"),
    }
    if ocr_receipt is not None:
        projection["ocr"] = {
            key: value
            for key, value in ocr_receipt.items()
            if key not in {"asset_id", "observed_at"}
        }
    return projection


def _receipt_hashes(materialization: Mapping[str, Any], semantic_projection: Mapping[str, Any]) -> dict[str, Any]:
    ocr_receipt = materialization.get("ocr_baseline_receipt")
    return {
        "image_sha256": materialization["render_receipt"]["image_sha256"],
        "render_receipt_sha256": digest(materialization["render_receipt"]),
        "ocr_baseline_receipt_sha256": digest(ocr_receipt) if ocr_receipt is not None else None,
        "cleanup_receipt_sha256": digest(materialization["cleanup_receipt"]),
        "semantic_replay_sha256": digest(semantic_projection),
    }


async def materialize_ocr_input(
    product_input: Mapping[str, Any],
    *,
    case_id: str,
    work_root: Path,
    ocr_engine: OcrEngine | None = None,
    font_path: Path | None = None,
    now_factory: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Render and OCR one P5 synthetic screenshot through production boundaries.

    OCR failures are returned as a machine-readable ``FAILED`` artifact after
    cleanup. A cleanup failure always wins and returns ``PRIVACY_BLOCKED``.
    Invalid contracts fail before an image is materialized.
    """

    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("case_id must be a non-empty string")
    source_text, render_spec = _validate_product_input(product_input)
    selected_font = _font_path(font_path)
    image_bytes, render_facts = _render_image(source_text, render_spec, font_path=selected_font)
    media_type, _ = _FORMAT_FACTS[str(render_spec["format"])]
    upload = ScreenshotUpload(media_type=media_type, content=image_bytes)
    validate_screenshot_batch([upload])

    root = Path(work_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    asset_repository = InMemoryScreenshotAssetRepository()
    engine = ocr_engine or PaddleOcrEngine(confirmation_threshold=CONFIRMATION_THRESHOLD)
    # Eval-only: no import/workspace persistence is invoked. The production
    # service owns the safe path, OCR engine interface, and cleanup service.
    service = ScreenshotImportService(
        import_repository=None,  # type: ignore[arg-type]
        itinerary_repository=None,  # type: ignore[arg-type]
        trip_brief_repository=None,  # type: ignore[arg-type]
        entity_resolver=None,
        command_repository=None,  # type: ignore[arg-type]
        asset_repository=asset_repository,
        ocr_engine=engine,
        temp_root=root,
    )
    asset_id = str(uuid4())
    asset_hash = hashlib.sha256(image_bytes).hexdigest()
    path = service._safe_asset_path(asset_id, media_type)
    observed_at = now_factory()
    if observed_at.tzinfo is None:
        raise ValueError("now_factory must return a timezone-aware datetime")
    render_receipt = {
        "schema_version": RENDER_RECEIPT_SCHEMA_VERSION,
        "render_id": str(uuid4()),
        "case_id": case_id,
        "renderer": {"name": RENDERER_NAME, "version": RENDERER_VERSION},
        "rendered_at": observed_at.isoformat(),
        "render_spec_sha256": digest(render_spec),
        "source_text_sha256": render_spec["text_sha256"],
        "image_sha256": asset_hash,
        "media_type": media_type,
        "format": render_spec["format"],
        "width": render_spec["width"],
        "height": render_spec["height"],
        "byte_size": len(image_bytes),
        **render_facts,
    }
    asset = TemporaryAssetRecord(
        asset_id=asset_id,
        workspace_id=f"p5-eval:{case_id}",
        content_hash=asset_hash,
        media_type=media_type,
        byte_size=len(image_bytes),
        storage_locator=str(path),
        expires_at=observed_at + timedelta(minutes=15),
        created_at=observed_at,
    )
    await asset_repository.create_assets([asset])

    ocr_artifact: dict[str, Any] | None = None
    processing_error: Exception | None = None
    try:
        path.write_bytes(image_bytes)
        del image_bytes
        await asset_repository.mark_processing(asset_id)
        lines = await service.ocr_engine.recognize(path)
        production_receipt = ScreenshotOcrReceipt(
            asset_id=asset_id,
            asset_hash=asset_hash,
            media_type=media_type,
            byte_size=asset.byte_size,
            engine=service.ocr_engine.name,
            engine_version=service.ocr_engine.version,
            observed_at=now_factory(),
            lines=lines,
        ).model_dump(mode="json")
        ocr_artifact = {
            "schema_version": OCR_RECEIPT_SCHEMA_VERSION,
            **production_receipt,
        }
        if not lines:
            raise OcrProcessingError("screenshot OCR produced no text")
    except Exception as exc:  # the failure is materialization evidence
        processing_error = exc
    finally:
        cleanup = await service.cleanup_service.cleanup(
            asset,
            terminal_reason="FAILED" if processing_error is not None else "SUCCEEDED",
        )

    cleanup_artifact = {
        "schema_version": CLEANUP_RECEIPT_SCHEMA_VERSION,
        **cleanup.model_dump(mode="json"),
        "original_removed": not path.exists(),
    }
    cleanup_succeeded = cleanup.cleanup_status == "DELETED" and not path.exists()
    status = "PRIVACY_BLOCKED" if not cleanup_succeeded else "FAILED" if processing_error else "SUCCEEDED"
    materialization: dict[str, Any] = {
        "schema_version": MATERIALIZATION_SCHEMA_VERSION,
        "case_id": case_id,
        "status": status,
        "source_payload": {
            "source_type": "SYNTHETIC_SCREENSHOT",
            "source_text_sha256": render_spec["text_sha256"],
            "render_spec": render_spec,
        },
        "render_receipt": render_receipt,
        "ocr_baseline_receipt": ocr_artifact,
        "cleanup_receipt": cleanup_artifact,
        "error_category": type(processing_error).__name__ if processing_error is not None else None,
    }
    semantic_projection = _semantic_projection(materialization)
    materialization["semantic_projection"] = semantic_projection
    materialization["hashes"] = _receipt_hashes(materialization, semantic_projection)
    materialization["semantic_replay_hash"] = materialization["hashes"]["semantic_replay_sha256"]
    return validate_ocr_materialization(materialization, product_input=product_input)


def validate_ocr_materialization(
    materialization: Mapping[str, Any],
    *,
    product_input: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed on receipt, privacy, confidence, and hash inconsistencies."""

    value = dict(materialization)
    required = {
        "schema_version",
        "case_id",
        "status",
        "source_payload",
        "render_receipt",
        "ocr_baseline_receipt",
        "cleanup_receipt",
        "error_category",
        "semantic_projection",
        "hashes",
        "semantic_replay_hash",
    }
    if set(value) != required:
        raise ValueError(f"OCR materialization fields must be exactly {sorted(required)}")
    if value["schema_version"] != MATERIALIZATION_SCHEMA_VERSION:
        raise ValueError("unsupported OCR materialization schema_version")
    if value["status"] not in {"SUCCEEDED", "FAILED", "PRIVACY_BLOCKED"}:
        raise ValueError("invalid OCR materialization status")
    source_payload = value["source_payload"]
    if set(source_payload) != {"source_type", "source_text_sha256", "render_spec"}:
        raise ValueError("source payload contains unexpected fields")
    if source_payload.get("source_type") != "SYNTHETIC_SCREENSHOT":
        raise ValueError("invalid OCR source type")
    if product_input is not None:
        source_text, render_spec = _validate_product_input(product_input)
        if source_payload["source_text_sha256"] != hashlib.sha256(source_text.encode("utf-8")).hexdigest():
            raise ValueError("source payload is not bound to product input")
        if source_payload["render_spec"] != render_spec:
            raise ValueError("source payload render_spec mismatch")
    render_receipt = value["render_receipt"]
    cleanup_receipt = value["cleanup_receipt"]
    expected_render_fields = {
        "schema_version",
        "render_id",
        "case_id",
        "renderer",
        "rendered_at",
        "render_spec_sha256",
        "source_text_sha256",
        "image_sha256",
        "media_type",
        "format",
        "width",
        "height",
        "byte_size",
        "line_count",
        "font_sha256",
        "font_size",
    }
    if set(render_receipt) != expected_render_fields:
        raise ValueError("render receipt contains unexpected fields")
    expected_cleanup_fields = {
        "schema_version",
        "receipt_id",
        "asset_id",
        "terminal_reason",
        "cleanup_status",
        "asset_hash",
        "cleanup_attempted_at",
        "cleanup_error_category",
        "original_removed",
    }
    if set(cleanup_receipt) != expected_cleanup_fields:
        raise ValueError("cleanup receipt contains unexpected fields")
    if render_receipt.get("schema_version") != RENDER_RECEIPT_SCHEMA_VERSION:
        raise ValueError("invalid render receipt schema_version")
    if cleanup_receipt.get("schema_version") != CLEANUP_RECEIPT_SCHEMA_VERSION:
        raise ValueError("invalid cleanup receipt schema_version")
    if render_receipt.get("image_sha256") != cleanup_receipt.get("asset_hash"):
        raise ValueError("cleanup receipt does not bind the rendered image")
    if render_receipt.get("case_id") != value["case_id"]:
        raise ValueError("render receipt case_id mismatch")
    if render_receipt.get("source_text_sha256") != source_payload.get("source_text_sha256"):
        raise ValueError("render receipt source text binding mismatch")
    render_spec = source_payload["render_spec"]
    if render_receipt.get("render_spec_sha256") != digest(render_spec):
        raise ValueError("render receipt render_spec binding mismatch")
    media_type, _ = _FORMAT_FACTS.get(render_spec.get("format"), (None, None))
    if (
        render_receipt.get("format") != render_spec.get("format")
        or render_receipt.get("media_type") != media_type
        or render_receipt.get("width") != render_spec.get("width")
        or render_receipt.get("height") != render_spec.get("height")
    ):
        raise ValueError("render receipt does not match render_spec")
    if cleanup_receipt.get("cleanup_status") != "DELETED" or cleanup_receipt.get("original_removed") is not True:
        if value["status"] != "PRIVACY_BLOCKED":
            raise ValueError("cleanup failure must fail closed as PRIVACY_BLOCKED")
    elif value["status"] == "PRIVACY_BLOCKED":
        raise ValueError("PRIVACY_BLOCKED requires a cleanup failure")
    if value["status"] == "SUCCEEDED":
        if value["error_category"] is not None or cleanup_receipt.get("terminal_reason") != "SUCCEEDED":
            raise ValueError("successful materialization contains failure evidence")
    elif value["status"] == "FAILED":
        if not value["error_category"] or cleanup_receipt.get("terminal_reason") != "FAILED":
            raise ValueError("failed materialization lacks failure evidence")
    ocr_receipt = value.get("ocr_baseline_receipt")
    if value["status"] == "SUCCEEDED" and ocr_receipt is None:
        raise ValueError("successful materialization requires an OCR receipt")
    if ocr_receipt is not None:
        expected_ocr_fields = {
            "schema_version",
            "asset_id",
            "asset_hash",
            "media_type",
            "byte_size",
            "engine",
            "engine_version",
            "observed_at",
            "lines",
        }
        if set(ocr_receipt) != expected_ocr_fields:
            raise ValueError("OCR receipt contains unexpected fields")
        if ocr_receipt.get("schema_version") != OCR_RECEIPT_SCHEMA_VERSION:
            raise ValueError("invalid OCR receipt schema_version")
        if ocr_receipt.get("asset_hash") != render_receipt.get("image_sha256"):
            raise ValueError("OCR receipt does not bind the rendered image")
        if ocr_receipt.get("asset_id") != cleanup_receipt.get("asset_id"):
            raise ValueError("OCR and cleanup receipt asset IDs differ")
        if (
            ocr_receipt.get("media_type") != render_receipt.get("media_type")
            or ocr_receipt.get("byte_size") != render_receipt.get("byte_size")
        ):
            raise ValueError("OCR receipt does not match rendered image facts")
        for line in ocr_receipt.get("lines", []):
            if float(line["confidence"]) < CONFIRMATION_THRESHOLD and not line.get("requires_confirmation"):
                raise ValueError("low-confidence OCR line lost requires_confirmation")
    forbidden_keys = {"image_bytes", "image_path", "storage_locator", "source_path", "ocr_text"}
    if forbidden_keys.intersection(value):
        raise ValueError("materialization leaks a forbidden original-image or OCR shortcut field")
    expected_projection = _semantic_projection(value)
    if value["semantic_projection"] != expected_projection:
        raise ValueError("semantic projection does not match receipts")
    expected_hashes = _receipt_hashes(value, expected_projection)
    if value["hashes"] != expected_hashes:
        raise ValueError("OCR materialization receipt hashes do not match")
    if value["semantic_replay_hash"] != expected_hashes["semantic_replay_sha256"]:
        raise ValueError("top-level semantic replay hash does not match semantic projection")
    return value


__all__ = [
    "CONFIRMATION_THRESHOLD",
    "materialize_ocr_input",
    "validate_ocr_materialization",
]
