from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import (
    PaddleOcrOutputError,
    PaddleOcrRuntimeUnavailableError,
    ScreenshotOcrError,
)
from .models import OcrQuadrilateral, RawOcrLine, ScreenshotOcrEngineBindingV1


PADDLEOCR_VERSION = "3.7.0"
PADDLEPADDLE_VERSION = "3.3.1"
DEFAULT_PADDLE_OPTIONS: dict[str, Any] = {
    "lang": "ch",
    "use_doc_orientation_classify": False,
    "use_doc_unwarping": False,
    "use_textline_orientation": False,
    "enable_mkldnn": False,
}


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise PaddleOcrOutputError("PaddleOCR returned invalid JSON") from exc
    return value


def _payload(value: Any) -> Any:
    candidate = getattr(value, "json", value)
    if callable(candidate):
        candidate = candidate()
    candidate = _json_value(candidate)
    if isinstance(candidate, Mapping) and "res" in candidate:
        candidate = _json_value(candidate["res"])
    return candidate


def _is_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _point(value: Any) -> tuple[float, float]:
    if isinstance(value, (str, bytes, Mapping)):
        raise PaddleOcrOutputError("PaddleOCR polygon point must contain x and y")
    try:
        coordinates = list(value)
    except TypeError as exc:
        raise PaddleOcrOutputError(
            "PaddleOCR polygon point must contain x and y"
        ) from exc
    if len(coordinates) != 2:
        raise PaddleOcrOutputError("PaddleOCR polygon point must contain x and y")
    if not _is_number(coordinates[0]) or not _is_number(coordinates[1]):
        raise PaddleOcrOutputError("PaddleOCR polygon point coordinates must be finite")
    x, y = float(coordinates[0]), float(coordinates[1])
    return (0.0 if x == 0 else x, 0.0 if y == 0 else y)


def _quadrilateral(value: Any) -> OcrQuadrilateral:
    if isinstance(value, (str, bytes, Mapping)):
        raise PaddleOcrOutputError("PaddleOCR bbox must be a sequence")
    try:
        coordinates = list(value)
    except TypeError as exc:
        raise PaddleOcrOutputError("PaddleOCR bbox must be a sequence") from exc
    if len(coordinates) == 4 and all(_is_number(item) for item in coordinates):
        x_min, y_min, x_max, y_max = (float(item) for item in coordinates)
        if x_max <= x_min or y_max <= y_min:
            raise PaddleOcrOutputError("PaddleOCR rectangle must have positive area")
        return (
            (x_min, y_min),
            (x_max, y_min),
            (x_max, y_max),
            (x_min, y_max),
        )
    if len(coordinates) != 4:
        raise PaddleOcrOutputError("PaddleOCR bbox must contain four points")
    points = tuple(_point(item) for item in coordinates)
    return (points[0], points[1], points[2], points[3])


def _raw_line(text: Any, score: Any, bbox: Any) -> RawOcrLine | None:
    normalized = str(text).strip()
    if not normalized:
        return None
    if not _is_number(score):
        raise PaddleOcrOutputError("PaddleOCR confidence must be finite")
    confidence = float(score)
    if not 0 <= confidence <= 1:
        raise PaddleOcrOutputError("PaddleOCR confidence must be between zero and one")
    try:
        return RawOcrLine(text=normalized, confidence=confidence, bbox=_quadrilateral(bbox))
    except ValueError as exc:
        raise PaddleOcrOutputError("PaddleOCR line geometry is invalid") from exc


def _mapping_lines(value: Mapping[str, Any]) -> list[RawOcrLine]:
    if {"text", "confidence", "bbox"}.issubset(value):
        line = _raw_line(value["text"], value["confidence"], value["bbox"])
        return [] if line is None else [line]

    texts = value.get("rec_texts")
    scores = value.get("rec_scores")
    boxes = value.get("rec_polys")
    if boxes is None:
        boxes = value.get("dt_polys")
    if boxes is None:
        boxes = value.get("rec_boxes")
    if texts is None and scores is None and boxes is None:
        return []
    if texts is None or scores is None or boxes is None:
        raise PaddleOcrOutputError("PaddleOCR result fields are incomplete")
    texts, scores, boxes = list(texts), list(scores), list(boxes)
    if not (len(texts) == len(scores) == len(boxes)):
        raise PaddleOcrOutputError("PaddleOCR result fields have inconsistent lengths")
    result: list[RawOcrLine] = []
    for text, score, bbox in zip(texts, scores, boxes, strict=True):
        line = _raw_line(text, score, bbox)
        if line is not None:
            result.append(line)
    return result


def _is_legacy_line(value: Any) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) == 2
        and isinstance(value[1], Sequence)
        and not isinstance(value[1], (str, bytes))
        and len(value[1]) == 2
        and isinstance(value[1][0], str)
        and _is_number(value[1][1])
    )


def _legacy_lines(value: Any) -> list[RawOcrLine] | None:
    if _is_legacy_line(value):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    if value and all(_is_legacy_line(item) for item in value):
        pages = [value]
    elif value and all(
        isinstance(page, Sequence)
        and not isinstance(page, (str, bytes))
        and all(_is_legacy_line(item) for item in page)
        for page in value
    ):
        pages = value
    else:
        return None
    result: list[RawOcrLine] = []
    for page in pages:
        for bbox, recognition in page:
            line = _raw_line(recognition[0], recognition[1], bbox)
            if line is not None:
                result.append(line)
    return result


def normalize_paddle_output(value: Any) -> tuple[RawOcrLine, ...]:
    legacy = _legacy_lines(value)
    if legacy is not None:
        return tuple(legacy)

    values = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else [value]
    result: list[RawOcrLine] = []
    for item in values:
        candidate = _payload(item)
        nested_legacy = _legacy_lines(candidate)
        if nested_legacy is not None:
            result.extend(nested_legacy)
        elif isinstance(candidate, Mapping):
            result.extend(_mapping_lines(candidate))
        elif candidate not in (None, []):
            raise PaddleOcrOutputError("PaddleOCR returned an unsupported result shape")
    return tuple(result)


class PaddleOcrAdapter:
    """Lazy PaddleOCR 3.7 adapter with a compatibility output reader."""

    name = "paddleocr"
    version = PADDLEOCR_VERSION

    def __init__(
        self,
        *,
        pipeline_factory: Callable[..., Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> None:
        self._pipeline_factory = pipeline_factory
        self._options = {**DEFAULT_PADDLE_OPTIONS, **dict(options or {})}
        self._pipeline: Any | None = None

    @property
    def binding(self) -> ScreenshotOcrEngineBindingV1:
        return ScreenshotOcrEngineBindingV1.create(
            engine=self.name,
            engine_version=self.version,
            runtime="paddlepaddle",
            runtime_version=PADDLEPADDLE_VERSION,
            configuration=self._options,
        )

    def _load_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        factory = self._pipeline_factory
        if factory is None:
            try:
                from paddleocr import PaddleOCR
            except ImportError as exc:
                raise PaddleOcrRuntimeUnavailableError(
                    "PaddleOCR 3.7.0 runtime is not installed"
                ) from exc
            factory = PaddleOCR
        try:
            self._pipeline = factory(**self._options)
        except ScreenshotOcrError:
            raise
        except Exception as exc:
            raise PaddleOcrRuntimeUnavailableError(
                "PaddleOCR 3.7.0 runtime could not be initialized"
            ) from exc
        return self._pipeline

    def _predict(self, image_path: Path) -> Any:
        pipeline = self._load_pipeline()
        if callable(getattr(pipeline, "predict", None)):
            return pipeline.predict(str(image_path))
        if callable(getattr(pipeline, "ocr", None)):
            return pipeline.ocr(str(image_path), cls=False)
        raise PaddleOcrRuntimeUnavailableError("PaddleOCR pipeline has no prediction method")

    async def recognize(self, image_path: Path) -> tuple[RawOcrLine, ...]:
        try:
            value = await asyncio.to_thread(self._predict, image_path)
            return normalize_paddle_output(value)
        except (PaddleOcrOutputError, PaddleOcrRuntimeUnavailableError):
            raise
        except Exception as exc:
            raise PaddleOcrOutputError("PaddleOCR prediction failed") from exc


PaddleOcrEngine = PaddleOcrAdapter
