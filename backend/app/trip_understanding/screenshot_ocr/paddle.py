from __future__ import annotations

import asyncio
import atexit
import glob
import json
import math
import multiprocessing
import os
import sys
import threading
import types
from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version as distribution_version
from multiprocessing.connection import Connection
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
PADDLE_WORKER_STARTUP_TIMEOUT_SECONDS = 30.0
PADDLE_WORKER_POLL_SECONDS = 0.1
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


def _predict_with_pipeline(pipeline: Any, image_path: str) -> Any:
    if callable(getattr(pipeline, "predict", None)):
        return pipeline.predict(image_path)
    if callable(getattr(pipeline, "ocr", None)):
        return pipeline.ocr(image_path, cls=False)
    raise PaddleOcrRuntimeUnavailableError("PaddleOCR pipeline has no prediction method")


def _configure_isolated_paddle_imports() -> None:
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    for dll_directory in glob.glob(
        str(Path(sys.prefix) / "Lib" / "site-packages" / "nvidia" / "*" / "bin")
    ):
        try:
            os.add_dll_directory(dll_directory)
        except (AttributeError, FileNotFoundError, OSError):
            continue
    if "modelscope" not in sys.modules:
        stub = types.ModuleType("modelscope")

        def _network_disabled(*_args: Any, **_kwargs: Any) -> str:
            raise RuntimeError("remote model downloads are disabled")

        stub.snapshot_download = _network_disabled  # type: ignore[attr-defined]
        sys.modules["modelscope"] = stub


def _paddle_worker_main(options: dict[str, Any], connection: Connection) -> None:
    try:
        _configure_isolated_paddle_imports()
        import paddle
        from paddleocr import PaddleOCR

        try:
            paddleocr_version = distribution_version("paddleocr")
        except PackageNotFoundError:
            paddleocr_version = "NOT_INSTALLED"
        paddlepaddle_version = str(paddle.__version__)
        if (
            paddleocr_version != PADDLEOCR_VERSION
            or paddlepaddle_version != PADDLEPADDLE_VERSION
        ):
            raise RuntimeError("unexpected Paddle OCR runtime version")
        pipeline = PaddleOCR(**options)
        connection.send(
            {
                "kind": "READY",
                "engine_version": paddleocr_version,
                "runtime_version": paddlepaddle_version,
            }
        )
    except BaseException as exc:
        try:
            connection.send(
                {
                    "kind": "INIT_ERROR",
                    "error_type": type(exc).__name__,
                }
            )
        finally:
            connection.close()
        return

    try:
        while True:
            message = connection.recv()
            if message.get("kind") == "STOP":
                return
            request_id = str(message["request_id"])
            try:
                raw = _predict_with_pipeline(pipeline, str(message["image_path"]))
                lines = normalize_paddle_output(raw)
                connection.send(
                    {
                        "kind": "RESULT",
                        "request_id": request_id,
                        "lines": [line.model_dump(mode="json") for line in lines],
                    }
                )
            except BaseException as exc:
                connection.send(
                    {
                        "kind": "PREDICTION_ERROR",
                        "request_id": request_id,
                        "error_type": type(exc).__name__,
                    }
                )
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        connection.close()


class _PaddleProcessWorker:
    def __init__(self, options: Mapping[str, Any]) -> None:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        self._connection = parent
        self._process = context.Process(
            target=_paddle_worker_main,
            args=(dict(options), child),
            name="breezetravel-paddle-ocr",
            daemon=True,
        )
        self._ready = False
        self._closed = False
        self.engine_version: str | None = None
        self.runtime_version: str | None = None
        self._process.start()
        child.close()

    @property
    def pid(self) -> int | None:
        return self._process.pid

    def _receive(self, *, timeout_seconds: float | None) -> Mapping[str, Any]:
        remaining = timeout_seconds
        while remaining is None or remaining > 0:
            poll_for = (
                PADDLE_WORKER_POLL_SECONDS
                if remaining is None
                else min(PADDLE_WORKER_POLL_SECONDS, remaining)
            )
            if self._connection.poll(poll_for):
                value = self._connection.recv()
                if not isinstance(value, Mapping):
                    raise PaddleOcrOutputError("PaddleOCR worker returned invalid data")
                return value
            if not self._process.is_alive():
                raise PaddleOcrRuntimeUnavailableError(
                    "PaddleOCR worker exited before returning a result"
                )
            if remaining is not None:
                remaining -= poll_for
        raise PaddleOcrRuntimeUnavailableError("PaddleOCR worker did not become ready")

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        message = self._receive(timeout_seconds=PADDLE_WORKER_STARTUP_TIMEOUT_SECONDS)
        if message.get("kind") != "READY":
            raise PaddleOcrRuntimeUnavailableError(
                "PaddleOCR 3.7.0 runtime could not be initialized"
            )
        self.engine_version = str(message.get("engine_version") or "")
        self.runtime_version = str(message.get("runtime_version") or "")
        if (
            self.engine_version != PADDLEOCR_VERSION
            or self.runtime_version != PADDLEPADDLE_VERSION
        ):
            self.abort()
            raise PaddleOcrRuntimeUnavailableError(
                "PaddleOCR runtime version does not match the frozen G04 binding"
            )
        self._ready = True

    def recognize(self, image_path: Path) -> tuple[RawOcrLine, ...]:
        if self._closed:
            raise PaddleOcrRuntimeUnavailableError("PaddleOCR worker is closed")
        self._ensure_ready()
        request_id = os.urandom(16).hex()
        try:
            self._connection.send(
                {
                    "kind": "PREDICT",
                    "request_id": request_id,
                    "image_path": str(image_path),
                }
            )
            message = self._receive(timeout_seconds=None)
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise PaddleOcrRuntimeUnavailableError(
                "PaddleOCR worker communication failed"
            ) from exc
        if message.get("request_id") != request_id:
            raise PaddleOcrOutputError("PaddleOCR worker response binding mismatch")
        if message.get("kind") != "RESULT":
            raise PaddleOcrOutputError("PaddleOCR prediction failed")
        try:
            return tuple(RawOcrLine.model_validate(item) for item in message["lines"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PaddleOcrOutputError("PaddleOCR worker returned invalid lines") from exc

    def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=5.0)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=5.0)
        self._connection.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.is_alive():
                self._connection.send({"kind": "STOP"})
                self._process.join(timeout=5.0)
        except (BrokenPipeError, EOFError, OSError):
            pass
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=5.0)
        self._connection.close()


_GLOBAL_PADDLE_SERIAL_LOCK = threading.Lock()


class PaddleOcrAdapter:
    """Lazy PaddleOCR 3.7 adapter with a compatibility output reader."""

    name = "paddleocr"
    version = PADDLEOCR_VERSION

    def __init__(
        self,
        *,
        pipeline_factory: Callable[..., Any] | None = None,
        options: Mapping[str, Any] | None = None,
        process_worker_factory: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self._pipeline_factory = pipeline_factory
        self._options = {**DEFAULT_PADDLE_OPTIONS, **dict(options or {})}
        self._pipeline: Any | None = None
        self._process_worker_factory = process_worker_factory or _PaddleProcessWorker
        self._process_worker: Any | None = None
        self._recognition_lock = asyncio.Lock()
        self._worker_state_lock = threading.Lock()
        self._observed_engine_version: str | None = None
        self._observed_runtime_version: str | None = None
        atexit.register(self.close)

    @property
    def binding(self) -> ScreenshotOcrEngineBindingV1:
        return ScreenshotOcrEngineBindingV1.create(
            engine=self.name,
            engine_version=self._observed_engine_version or self.version,
            runtime="paddlepaddle",
            runtime_version=(
                self._observed_runtime_version or PADDLEPADDLE_VERSION
            ),
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
        return _predict_with_pipeline(pipeline, str(image_path))

    def _recognize_isolated(self, image_path: Path) -> tuple[RawOcrLine, ...]:
        with _GLOBAL_PADDLE_SERIAL_LOCK:
            with self._worker_state_lock:
                worker = self._process_worker
                if worker is None:
                    worker = self._process_worker_factory(self._options)
                    self._process_worker = worker
            try:
                result = worker.recognize(image_path)
                observed_engine = getattr(worker, "engine_version", None)
                observed_runtime = getattr(worker, "runtime_version", None)
                if observed_engine is not None:
                    self._observed_engine_version = str(observed_engine)
                if observed_runtime is not None:
                    self._observed_runtime_version = str(observed_runtime)
                return result
            except PaddleOcrRuntimeUnavailableError:
                with self._worker_state_lock:
                    if self._process_worker is worker:
                        self._process_worker = None
                worker.abort()
                raise

    def _abort_isolated_worker(self) -> None:
        with self._worker_state_lock:
            worker = self._process_worker
            self._process_worker = None
        if worker is not None:
            worker.abort()

    def close(self) -> None:
        with self._worker_state_lock:
            worker = self._process_worker
            self._process_worker = None
        if worker is not None:
            worker.close()

    async def recognize(self, image_path: Path) -> tuple[RawOcrLine, ...]:
        try:
            if self._pipeline_factory is not None:
                value = await asyncio.to_thread(self._predict, image_path)
                return normalize_paddle_output(value)
            async with self._recognition_lock:
                operation = asyncio.create_task(
                    asyncio.to_thread(self._recognize_isolated, image_path)
                )
                try:
                    # Shield the thread-backed operation so cancellation does not
                    # merely detach a still-running OCR call.  The cancellation
                    # branch kills and joins the isolated process before the API
                    # is allowed to finish its terminal cleanup.
                    return await asyncio.shield(operation)
                except asyncio.CancelledError:
                    await asyncio.to_thread(self._abort_isolated_worker)
                    try:
                        await operation
                    except (ScreenshotOcrError, asyncio.CancelledError):
                        pass
                    except Exception:
                        pass
                    raise
        except (PaddleOcrOutputError, PaddleOcrRuntimeUnavailableError):
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise PaddleOcrOutputError("PaddleOCR prediction failed") from exc


PaddleOcrEngine = PaddleOcrAdapter
