from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.trip_understanding.screenshot_batch.models import CleanupAttempt, StagedBatch


class ScreenshotBatchError(Exception):
    code = "SCREENSHOT_BATCH_ERROR"

    def __init__(
        self,
        message: str,
        *,
        cleanup_attempts: Sequence[CleanupAttempt] = (),
        batch: StagedBatch | None = None,
    ) -> None:
        super().__init__(message)
        self.cleanup_attempts = tuple(cleanup_attempts)
        self.batch = batch

    def attach_cleanup_attempts(self, attempts: Sequence[CleanupAttempt]) -> None:
        self.cleanup_attempts = tuple(attempts)

    def attach_staging_evidence(
        self,
        batch: StagedBatch,
        attempts: Sequence[CleanupAttempt],
    ) -> None:
        self.batch = batch
        self.cleanup_attempts = tuple(attempts)


class MultipartRequiredError(ScreenshotBatchError):
    code = "MULTIPART_REQUIRED"


class MultipartMalformedError(ScreenshotBatchError):
    code = "INVALID_MULTIPART"


class ScreenshotCountError(ScreenshotBatchError):
    code = "SCREENSHOT_COUNT_INVALID"


class ScreenshotFileTooLargeError(ScreenshotBatchError):
    code = "SCREENSHOT_FILE_TOO_LARGE"


class ScreenshotBatchTooLargeError(ScreenshotBatchError):
    code = "SCREENSHOT_BATCH_TOO_LARGE"


class ScreenshotUnsupportedMediaTypeError(ScreenshotBatchError):
    code = "SCREENSHOT_MEDIA_TYPE_UNSUPPORTED"


class ScreenshotMagicMismatchError(ScreenshotBatchError):
    code = "SCREENSHOT_MIME_MAGIC_MISMATCH"


class ScreenshotEmptyFileError(ScreenshotBatchError):
    code = "SCREENSHOT_EMPTY_FILE"


class ScreenshotPathSecurityError(ScreenshotBatchError):
    code = "SCREENSHOT_PATH_SECURITY_VIOLATION"


class ScreenshotStorageError(ScreenshotBatchError):
    code = "SCREENSHOT_STORAGE_ERROR"


class ScreenshotStreamError(ScreenshotBatchError):
    code = "SCREENSHOT_STREAM_ERROR"


class ScreenshotCleanupError(ScreenshotBatchError):
    code = "SCREENSHOT_CLEANUP_FAILED"

    def __init__(
        self,
        message: str,
        *,
        attempts: Sequence[CleanupAttempt],
        staging_error_code: str | None = None,
        batch: StagedBatch | None = None,
    ) -> None:
        super().__init__(message, cleanup_attempts=attempts, batch=batch)
        self.attempts = tuple(attempts)
        self.staging_error_code = staging_error_code
        self.batch = batch


class ScreenshotStagingCancelledError(asyncio.CancelledError):
    code = "SCREENSHOT_STAGING_CANCELLED"

    def __init__(
        self,
        message: str,
        *,
        cleanup_attempts: Sequence[CleanupAttempt] = (),
        batch: StagedBatch | None = None,
    ) -> None:
        super().__init__(message)
        self.cleanup_attempts = tuple(cleanup_attempts)
        self.batch = batch

    def attach_cleanup_attempts(self, attempts: Sequence[CleanupAttempt]) -> None:
        self.cleanup_attempts = tuple(attempts)

    def attach_staging_evidence(
        self,
        batch: StagedBatch,
        attempts: Sequence[CleanupAttempt],
    ) -> None:
        self.batch = batch
        self.cleanup_attempts = tuple(attempts)


class ScreenshotStagingTimeoutError(TimeoutError):
    code = "SCREENSHOT_STAGING_TIMED_OUT"

    def __init__(
        self,
        message: str,
        *,
        cleanup_attempts: Sequence[CleanupAttempt] = (),
        batch: StagedBatch | None = None,
    ) -> None:
        super().__init__(message)
        self.cleanup_attempts = tuple(cleanup_attempts)
        self.batch = batch

    def attach_cleanup_attempts(self, attempts: Sequence[CleanupAttempt]) -> None:
        self.cleanup_attempts = tuple(attempts)

    def attach_staging_evidence(
        self,
        batch: StagedBatch,
        attempts: Sequence[CleanupAttempt],
    ) -> None:
        self.batch = batch
        self.cleanup_attempts = tuple(attempts)
