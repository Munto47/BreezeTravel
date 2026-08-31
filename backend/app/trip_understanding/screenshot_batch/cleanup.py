from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from app.trip_understanding.screenshot_batch.errors import ScreenshotCleanupError, ScreenshotPathSecurityError
from app.trip_understanding.screenshot_batch.models import CleanupAttempt, StagedBatch


_RANDOM_LOCATOR = re.compile(r"[0-9a-f]{64,}")
_MAX_CLEANUP_ATTEMPTS = 3


def _exists_without_following_symlinks(path: os.PathLike[str]) -> bool:
    return os.path.lexists(path)


def _validate_batch_paths(batch: StagedBatch) -> None:
    if not _RANDOM_LOCATOR.fullmatch(batch.batch_locator):
        raise ScreenshotPathSecurityError("batch locator is not a valid generated locator")
    if not batch.temp_root.is_absolute() or not batch.directory.is_absolute():
        raise ScreenshotPathSecurityError("staged screenshot paths must be absolute")
    if batch.directory.parent != batch.temp_root or batch.directory.name != batch.batch_locator:
        raise ScreenshotPathSecurityError("staged batch directory escaped its temporary root")
    if batch.directory.is_symlink():
        raise ScreenshotPathSecurityError("staged batch directory cannot be a symbolic link")
    for screenshot in batch.screenshots:
        if not _RANDOM_LOCATOR.fullmatch(screenshot.locator):
            raise ScreenshotPathSecurityError("screenshot locator is not a valid generated locator")
        if screenshot.path.parent != batch.directory or screenshot.path.name != screenshot.locator:
            raise ScreenshotPathSecurityError("staged screenshot path escaped its batch directory")


def _error_category(exc: OSError) -> str:
    return type(exc).__name__.upper()


def cleanup_staged_batch(
    batch: StagedBatch,
    terminal_reason: str,
    attempts: int = _MAX_CLEANUP_ATTEMPTS,
) -> tuple[CleanupAttempt, ...]:
    """Delete all pixels and the random batch directory, retrying at most three times.

    Successful calls return every immutable attempt. A final failure raises a typed
    error carrying the same attempt records so the integrator can persist a privacy
    blocking result instead of returning a consumable reference.
    """

    if not 1 <= attempts <= _MAX_CLEANUP_ATTEMPTS:
        raise ValueError("attempts must be between 1 and 3")
    if not isinstance(terminal_reason, str) or not terminal_reason.strip():
        raise ValueError("terminal_reason must be a non-empty string")
    _validate_batch_paths(batch)

    records: list[CleanupAttempt] = []
    for attempt_number in range(1, attempts + 1):
        deleted: list[str] = []
        already_absent: list[str] = []
        failed: list[str] = []
        errors: list[str] = []

        for screenshot in batch.screenshots:
            if not _exists_without_following_symlinks(screenshot.path):
                already_absent.append(screenshot.locator)
                continue
            try:
                screenshot.path.unlink(missing_ok=True)
            except OSError as exc:
                failed.append(screenshot.locator)
                errors.append(_error_category(exc))
            else:
                if _exists_without_following_symlinks(screenshot.path):
                    failed.append(screenshot.locator)
                    errors.append("DELETE_NOT_CONFIRMED")
                else:
                    deleted.append(screenshot.locator)

        directory_removed = not _exists_without_following_symlinks(batch.directory)
        if not directory_removed:
            try:
                batch.directory.rmdir()
            except OSError as exc:
                errors.append(f"DIRECTORY_{_error_category(exc)}")
            directory_removed = not _exists_without_following_symlinks(batch.directory)
            if not directory_removed and not errors:
                errors.append("DIRECTORY_DELETE_NOT_CONFIRMED")

        remaining = tuple(
            screenshot.locator
            for screenshot in batch.screenshots
            if _exists_without_following_symlinks(screenshot.path)
        )
        succeeded = not remaining and directory_removed
        record = CleanupAttempt(
            attempt_number=attempt_number,
            terminal_reason=terminal_reason,
            attempted_at=datetime.now(timezone.utc),
            deleted_locators=tuple(deleted),
            already_absent_locators=tuple(already_absent),
            failed_locators=tuple(failed),
            remaining_locators=remaining,
            error_categories=tuple(errors),
            directory_removed=directory_removed,
            succeeded=succeeded,
        )
        records.append(record)
        if succeeded:
            return tuple(records)

    raise ScreenshotCleanupError(
        "staged screenshot cleanup could not be confirmed",
        attempts=records,
        batch=batch,
    )
