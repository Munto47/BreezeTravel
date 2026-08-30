from __future__ import annotations

from typing import Any


class ScreenshotOcrError(RuntimeError):
    """Base error for the isolated screenshot OCR boundary."""

    code = "SCREENSHOT_OCR_ERROR"

    def __init__(self, message: str, *, image_results: tuple[Any, ...] = ()) -> None:
        super().__init__(message)
        self.image_results = image_results


class PaddleOcrRuntimeUnavailableError(ScreenshotOcrError):
    code = "PADDLE_OCR_RUNTIME_UNAVAILABLE"


class PaddleOcrOutputError(ScreenshotOcrError):
    code = "PADDLE_OCR_OUTPUT_INVALID"


class ScreenshotOcrAllFailedError(ScreenshotOcrError):
    code = "SCREENSHOT_OCR_ALL_FAILED"


class ScreenshotOcrTimeoutError(ScreenshotOcrAllFailedError):
    code = "SCREENSHOT_OCR_TIMEOUT"


class ScreenshotOcrPartialError(ScreenshotOcrError):
    """Raised only when a caller explicitly requires an all-success document."""

    code = "SCREENSHOT_OCR_PARTIAL"

    def __init__(self, document: Any) -> None:
        super().__init__(
            "one or more screenshot images did not produce OCR text",
            image_results=tuple(document.images),
        )
        self.document = document
