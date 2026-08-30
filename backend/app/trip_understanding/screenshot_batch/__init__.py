"""Bounded ephemeral multipart staging for Trip Understanding screenshots."""

from app.trip_understanding.screenshot_batch.cleanup import cleanup_staged_batch
from app.trip_understanding.screenshot_batch.errors import (
    MultipartMalformedError,
    MultipartRequiredError,
    ScreenshotBatchError,
    ScreenshotBatchTooLargeError,
    ScreenshotCleanupError,
    ScreenshotCountError,
    ScreenshotEmptyFileError,
    ScreenshotFileTooLargeError,
    ScreenshotMagicMismatchError,
    ScreenshotPathSecurityError,
    ScreenshotStagingCancelledError,
    ScreenshotStagingTimeoutError,
    ScreenshotStorageError,
    ScreenshotStreamError,
    ScreenshotUnsupportedMediaTypeError,
)
from app.trip_understanding.screenshot_batch.models import (
    DEFAULT_SCREENSHOT_BATCH_LIMITS,
    CleanupAttempt,
    ScreenshotBatchLimits,
    StagedBatch,
    StagedScreenshot,
    UploadLimits,
)
from app.trip_understanding.screenshot_batch.staging import stage_screenshot_multipart

__all__ = [
    "DEFAULT_SCREENSHOT_BATCH_LIMITS",
    "CleanupAttempt",
    "MultipartMalformedError",
    "MultipartRequiredError",
    "ScreenshotBatchError",
    "ScreenshotBatchLimits",
    "ScreenshotBatchTooLargeError",
    "ScreenshotCleanupError",
    "ScreenshotCountError",
    "ScreenshotEmptyFileError",
    "ScreenshotFileTooLargeError",
    "ScreenshotMagicMismatchError",
    "ScreenshotPathSecurityError",
    "ScreenshotStagingCancelledError",
    "ScreenshotStagingTimeoutError",
    "ScreenshotStorageError",
    "ScreenshotStreamError",
    "ScreenshotUnsupportedMediaTypeError",
    "StagedBatch",
    "StagedScreenshot",
    "UploadLimits",
    "cleanup_staged_batch",
    "stage_screenshot_multipart",
]
