from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


DOCUMENT_VERSION = "screenshot-source-document-v1"
HASH_PATTERN = r"^[0-9a-f]{64}$"

OcrPoint = tuple[float, float]
OcrQuadrilateral = tuple[OcrPoint, OcrPoint, OcrPoint, OcrPoint]


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_quadrilateral(bbox: OcrQuadrilateral) -> OcrQuadrilateral:
    for point in bbox:
        if len(point) != 2 or not all(math.isfinite(float(value)) for value in point):
            raise ValueError("OCR bbox points must contain two finite coordinates")
    area_twice = abs(
        sum(
            bbox[index][0] * bbox[(index + 1) % 4][1]
            - bbox[(index + 1) % 4][0] * bbox[index][1]
            for index in range(4)
        )
    )
    if area_twice <= 0:
        raise ValueError("OCR bbox must have positive area")
    return bbox


class StagedScreenshotAsset(BaseModel):
    """A temporary screenshot reference; paths never enter the source document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: Path
    content_hash: str = Field(pattern=HASH_PATTERN)


class ScreenshotOcrRunSpecV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_concurrency: Literal[1] = 1
    per_image_timeout_seconds: Annotated[float, Field(gt=0)] = 15.0
    batch_timeout_seconds: Annotated[float, Field(gt=0)] = 45.0
    low_confidence_threshold: Annotated[float, Field(ge=0, le=1)] = 0.85


ScreenshotOcrRunSpec = ScreenshotOcrRunSpecV1


class RawOcrLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0, le=1)
    bbox: OcrQuadrilateral

    @model_validator(mode="after")
    def validate_content(self) -> "RawOcrLine":
        normalized = self.text.strip()
        if not normalized:
            raise ValueError("OCR line text must not be blank")
        if normalized != self.text:
            object.__setattr__(self, "text", normalized)
        object.__setattr__(self, "bbox", _validate_quadrilateral(self.bbox))
        return self


class SemanticSpanV1(BaseModel):
    """A half-open span measured in Python/Unicode code points."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    unit: Literal["UNICODE_CODE_POINT"] = "UNICODE_CODE_POINT"
    end_exclusive: Literal[True] = True

    @model_validator(mode="after")
    def validate_order(self) -> "SemanticSpanV1":
        if self.end < self.start:
            raise ValueError("semantic span end must not precede start")
        return self


class ScreenshotSourceLineV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    image_index: int = Field(ge=0)
    reading_index: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0, le=1)
    bbox: OcrQuadrilateral
    semantic_span: SemanticSpanV1
    requires_confirmation: bool

    @model_validator(mode="after")
    def validate_bbox(self) -> "ScreenshotSourceLineV1":
        object.__setattr__(self, "bbox", _validate_quadrilateral(self.bbox))
        return self


class ScreenshotOcrImageErrorV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: Literal["OCR_FAILED", "OCR_TIMEOUT", "OCR_TEXT_NOT_FOUND"]
    kind: Literal["PARTIAL", "TIMEOUT"]
    retryable: bool


class ScreenshotImageResultV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    image_index: int = Field(ge=0)
    content_hash: str = Field(pattern=HASH_PATTERN)
    status: Literal["SUCCEEDED", "FAILED", "TIMED_OUT", "NO_TEXT"]
    line_count: int = Field(ge=0)
    error: ScreenshotOcrImageErrorV1 | None = None

    @model_validator(mode="after")
    def validate_status(self) -> "ScreenshotImageResultV1":
        if self.status == "SUCCEEDED":
            if self.line_count < 1 or self.error is not None:
                raise ValueError("successful OCR image must contain lines and no error")
        elif self.line_count != 0 or self.error is None:
            raise ValueError("unsuccessful OCR image must contain an error and no lines")
        return self


class ScreenshotOcrEngineBindingV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str = Field(min_length=1, max_length=100)
    engine_version: str = Field(min_length=1, max_length=100)
    runtime: str | None = Field(default=None, max_length=100)
    runtime_version: str | None = Field(default=None, max_length=100)
    configuration: dict[str, JsonValue] = Field(default_factory=dict)
    low_confidence_threshold: float = Field(ge=0, le=1)
    config_hash: str = Field(pattern=HASH_PATTERN)

    @classmethod
    def create(
        cls,
        *,
        engine: str,
        engine_version: str,
        configuration: dict[str, JsonValue] | None = None,
        low_confidence_threshold: float = 0.85,
        runtime: str | None = None,
        runtime_version: str | None = None,
    ) -> "ScreenshotOcrEngineBindingV1":
        canonical = {
            "engine": engine,
            "engine_version": engine_version,
            "runtime": runtime,
            "runtime_version": runtime_version,
            "configuration": configuration or {},
            "low_confidence_threshold": low_confidence_threshold,
        }
        return cls(**canonical, config_hash=_canonical_sha256(canonical))

    def with_low_confidence_threshold(self, value: float) -> "ScreenshotOcrEngineBindingV1":
        return self.create(
            engine=self.engine,
            engine_version=self.engine_version,
            runtime=self.runtime,
            runtime_version=self.runtime_version,
            configuration=self.configuration,
            low_confidence_threshold=value,
        )

    @model_validator(mode="after")
    def validate_hash(self) -> "ScreenshotOcrEngineBindingV1":
        canonical = self.model_dump(mode="json", exclude={"config_hash"})
        if self.config_hash != _canonical_sha256(canonical):
            raise ValueError("OCR engine config hash does not match its canonical binding")
        return self


class ScreenshotSourceDocumentV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["screenshot-source-document-v1"] = DOCUMENT_VERSION
    semantic_text: str = Field(min_length=1)
    partial: bool
    images: tuple[ScreenshotImageResultV1, ...] = Field(min_length=1, max_length=6)
    lines: tuple[ScreenshotSourceLineV1, ...] = Field(min_length=1)
    engine_binding: ScreenshotOcrEngineBindingV1
    document_hash: str = Field(pattern=HASH_PATTERN)

    @classmethod
    def create(
        cls,
        *,
        semantic_text: str,
        partial: bool,
        images: tuple[ScreenshotImageResultV1, ...],
        lines: tuple[ScreenshotSourceLineV1, ...],
        engine_binding: ScreenshotOcrEngineBindingV1,
    ) -> "ScreenshotSourceDocumentV1":
        canonical = {
            "version": DOCUMENT_VERSION,
            "semantic_text": semantic_text,
            "partial": partial,
            "images": [image.model_dump(mode="json") for image in images],
            "lines": [line.model_dump(mode="json") for line in lines],
            "engine_binding": engine_binding.model_dump(mode="json"),
        }
        return cls(**canonical, document_hash=_canonical_sha256(canonical))

    @model_validator(mode="after")
    def validate_document(self) -> "ScreenshotSourceDocumentV1":
        expected_image_indices = list(range(len(self.images)))
        if [image.image_index for image in self.images] != expected_image_indices:
            raise ValueError("screenshot image indices must be contiguous and upload ordered")
        if [line.reading_index for line in self.lines] != list(range(len(self.lines))):
            raise ValueError("OCR reading indices must be contiguous")
        successful_indices = {
            image.image_index for image in self.images if image.status == "SUCCEEDED"
        }
        if any(line.image_index not in successful_indices for line in self.lines):
            raise ValueError("OCR lines must belong to successful images")
        for line in self.lines:
            if self.semantic_text[line.semantic_span.start : line.semantic_span.end] != line.text:
                raise ValueError("OCR semantic span does not select its line text")
        expected_partial = any(image.status != "SUCCEEDED" for image in self.images)
        if self.partial != expected_partial:
            raise ValueError("partial flag must match image outcomes")
        canonical = self.model_dump(mode="json", exclude={"document_hash"})
        if self.document_hash != _canonical_sha256(canonical):
            raise ValueError("document hash does not match canonical OCR content")
        return self


def coerce_raw_lines(value: Any) -> tuple[RawOcrLine, ...]:
    if value is None:
        return ()
    if isinstance(value, RawOcrLine):
        return (value,)
    return tuple(
        item if isinstance(item, RawOcrLine) else RawOcrLine.model_validate(item)
        for item in value
    )
