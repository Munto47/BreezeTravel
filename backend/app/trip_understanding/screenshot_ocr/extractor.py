from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Protocol, Sequence

from pydantic import ValidationError

from .errors import (
    ScreenshotOcrAllFailedError,
    ScreenshotOcrError,
    ScreenshotOcrPartialError,
    ScreenshotOcrTimeoutError,
)
from .models import (
    RawOcrLine,
    SemanticSpanV1,
    ScreenshotImageResultV1,
    ScreenshotOcrEngineBindingV1,
    ScreenshotOcrImageErrorV1,
    ScreenshotOcrRunSpecV1,
    ScreenshotSourceDocumentV1,
    ScreenshotSourceLineV1,
    StagedScreenshotAsset,
    coerce_raw_lines,
)
from .ordering import sort_reading_order


class ScreenshotOcrEngine(Protocol):
    name: str
    version: str

    @property
    def binding(self) -> ScreenshotOcrEngineBindingV1: ...

    async def recognize(self, image_path: Path) -> Sequence[RawOcrLine]: ...


def _binding(engine: Any, run_spec: ScreenshotOcrRunSpecV1) -> ScreenshotOcrEngineBindingV1:
    candidate = getattr(engine, "binding", None)
    if callable(candidate):
        candidate = candidate()
    if isinstance(candidate, ScreenshotOcrEngineBindingV1):
        return candidate.with_low_confidence_threshold(run_spec.low_confidence_threshold)
    if candidate is not None:
        parsed = ScreenshotOcrEngineBindingV1.model_validate(candidate)
        return parsed.with_low_confidence_threshold(run_spec.low_confidence_threshold)
    return ScreenshotOcrEngineBindingV1.create(
        engine=str(getattr(engine, "name", type(engine).__name__)),
        engine_version=str(getattr(engine, "version", "unversioned")),
        configuration=dict(getattr(engine, "configuration", {})),
        low_confidence_threshold=run_spec.low_confidence_threshold,
    )


async def _recognize(engine: ScreenshotOcrEngine, path: Path) -> tuple[RawOcrLine, ...]:
    value = engine.recognize(path)
    if inspect.isawaitable(value):
        value = await value
    return coerce_raw_lines(value)


def _failed_image(
    *,
    asset: StagedScreenshotAsset,
    image_index: int,
    status: str,
) -> ScreenshotImageResultV1:
    if status == "TIMED_OUT":
        error = ScreenshotOcrImageErrorV1(
            code="OCR_TIMEOUT",
            kind="TIMEOUT",
            retryable=True,
        )
    elif status == "NO_TEXT":
        error = ScreenshotOcrImageErrorV1(
            code="OCR_TEXT_NOT_FOUND",
            kind="PARTIAL",
            retryable=False,
        )
    else:
        error = ScreenshotOcrImageErrorV1(
            code="OCR_FAILED",
            kind="PARTIAL",
            retryable=True,
        )
    return ScreenshotImageResultV1(
        image_index=image_index,
        content_hash=asset.content_hash,
        status=status,
        line_count=0,
        error=error,
    )


async def extract_screenshot_document(
    assets: Sequence[StagedScreenshotAsset],
    engine: ScreenshotOcrEngine,
    run_spec: ScreenshotOcrRunSpecV1,
) -> ScreenshotSourceDocumentV1:
    """Extract upload-ordered semantic text while isolating per-image failures."""

    if not isinstance(run_spec, ScreenshotOcrRunSpecV1):
        run_spec = ScreenshotOcrRunSpecV1.model_validate(run_spec)
    staged_assets = tuple(
        asset if isinstance(asset, StagedScreenshotAsset) else StagedScreenshotAsset.model_validate(asset)
        for asset in assets
    )
    if not 1 <= len(staged_assets) <= 6:
        raise ValueError("screenshot OCR requires 1 to 6 staged assets")

    loop = asyncio.get_running_loop()
    batch_deadline = loop.time() + run_spec.batch_timeout_seconds
    image_results: list[ScreenshotImageResultV1] = []
    successful_lines: list[tuple[int, tuple[RawOcrLine, ...]]] = []

    for image_index, asset in enumerate(staged_assets):
        remaining_batch = batch_deadline - loop.time()
        if remaining_batch <= 0:
            image_results.extend(
                _failed_image(asset=pending, image_index=pending_index, status="TIMED_OUT")
                for pending_index, pending in enumerate(
                    staged_assets[image_index:],
                    start=image_index,
                )
            )
            break
        timeout = min(run_spec.per_image_timeout_seconds, remaining_batch)
        try:
            recognized = await asyncio.wait_for(_recognize(engine, asset.path), timeout=timeout)
            ordered = sort_reading_order(recognized)
        except TimeoutError:
            image_results.append(
                _failed_image(asset=asset, image_index=image_index, status="TIMED_OUT")
            )
            continue
        except (ScreenshotOcrError, ValidationError, TypeError, ValueError):
            image_results.append(
                _failed_image(asset=asset, image_index=image_index, status="FAILED")
            )
            continue
        except Exception:
            image_results.append(
                _failed_image(asset=asset, image_index=image_index, status="FAILED")
            )
            continue

        if not ordered:
            image_results.append(
                _failed_image(asset=asset, image_index=image_index, status="NO_TEXT")
            )
            continue
        successful_lines.append((image_index, ordered))
        image_results.append(
            ScreenshotImageResultV1(
                image_index=image_index,
                content_hash=asset.content_hash,
                status="SUCCEEDED",
                line_count=len(ordered),
            )
        )

    if not successful_lines:
        results = tuple(image_results)
        if any(result.status == "TIMED_OUT" for result in results):
            raise ScreenshotOcrTimeoutError(
                "screenshot OCR produced no text before its deadline",
                image_results=results,
            )
        raise ScreenshotOcrAllFailedError(
            "screenshot OCR produced no text for any image",
            image_results=results,
        )

    semantic_parts: list[str] = []
    source_lines: list[ScreenshotSourceLineV1] = []
    cursor = 0
    reading_index = 0
    for image_index, lines in successful_lines:
        for line in lines:
            if semantic_parts:
                semantic_parts.append("\n")
                cursor += 1
            start = cursor
            semantic_parts.append(line.text)
            cursor += len(line.text)
            source_lines.append(
                ScreenshotSourceLineV1(
                    image_index=image_index,
                    reading_index=reading_index,
                    text=line.text,
                    confidence=line.confidence,
                    bbox=line.bbox,
                    semantic_span=SemanticSpanV1(start=start, end=cursor),
                    requires_confirmation=(
                        line.confidence < run_spec.low_confidence_threshold
                    ),
                )
            )
            reading_index += 1

    images = tuple(sorted(image_results, key=lambda image: image.image_index))
    return ScreenshotSourceDocumentV1.create(
        semantic_text="".join(semantic_parts),
        partial=any(image.status != "SUCCEEDED" for image in images),
        images=images,
        lines=tuple(source_lines),
        engine_binding=_binding(engine, run_spec),
    )


def require_complete_document(
    document: ScreenshotSourceDocumentV1,
) -> ScreenshotSourceDocumentV1:
    if document.partial:
        raise ScreenshotOcrPartialError(document)
    return document
