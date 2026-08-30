from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ScreenshotBatchLimits:
    """Hard bounds applied while receiving a multipart screenshot batch."""

    min_files: int = 1
    max_files: int = 6
    max_file_bytes: int = 10 * MIB
    max_total_bytes: int = 61 * MIB
    max_header_bytes: int = 16 * 1024
    max_boundary_bytes: int = 200

    def __post_init__(self) -> None:
        if self.min_files < 1:
            raise ValueError("min_files must be at least 1")
        if self.max_files < self.min_files:
            raise ValueError("max_files must be greater than or equal to min_files")
        if self.max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        if self.max_total_bytes < 1:
            raise ValueError("max_total_bytes must be positive")
        if self.max_header_bytes < 1:
            raise ValueError("max_header_bytes must be positive")
        if self.max_boundary_bytes < 1:
            raise ValueError("max_boundary_bytes must be positive")


DEFAULT_SCREENSHOT_BATCH_LIMITS = ScreenshotBatchLimits()


@dataclass(frozen=True, slots=True)
class StagedScreenshot:
    """Metadata for one staged screenshot; no client filename is retained."""

    part_index: int
    media_type: str
    byte_size: int
    sha256: str
    locator: str
    path: Path

    @property
    def position(self) -> int:
        return self.part_index

    @property
    def content_hash(self) -> str:
        return self.sha256

    @property
    def storage_locator(self) -> str:
        return str(self.path)


@dataclass(frozen=True, slots=True)
class StagedBatch:
    """An ordered batch of files isolated below one random owner-only directory."""

    batch_locator: str
    temp_root: Path
    directory: Path
    screenshots: tuple[StagedScreenshot, ...]
    body_bytes: int
    total_file_bytes: int

    @property
    def assets(self) -> tuple[StagedScreenshot, ...]:
        return self.screenshots


@dataclass(frozen=True, slots=True)
class CleanupAttempt:
    """Immutable evidence for one bounded filesystem cleanup attempt."""

    attempt_number: int
    terminal_reason: str
    attempted_at: datetime
    deleted_locators: tuple[str, ...]
    already_absent_locators: tuple[str, ...]
    failed_locators: tuple[str, ...]
    remaining_locators: tuple[str, ...]
    error_categories: tuple[str, ...]
    directory_removed: bool
    succeeded: bool


@dataclass(frozen=True, slots=True)
class LocalRecoveryIssue:
    """A privacy-safe reason why one random local batch was not recovered."""

    batch_locator: str
    category: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class LocalRecoveryBatchEvidence:
    """Locator-bound crash cleanup evidence ready for later database reconciliation."""

    batch_locator: str
    asset_locators: tuple[str, ...]
    attempts: tuple[CleanupAttempt, ...]

    @property
    def succeeded(self) -> bool:
        return bool(self.attempts and self.attempts[-1].succeeded)


@dataclass(frozen=True, slots=True)
class LocalScreenshotRecoveryReport:
    """Bounded local recovery output; it intentionally contains no pixels or source text."""

    batches: tuple[LocalRecoveryBatchEvidence, ...]
    issues: tuple[LocalRecoveryIssue, ...]
    skipped_fresh_directories: int

    @property
    def directories_removed(self) -> int:
        return sum(item.succeeded for item in self.batches)

    @property
    def files_removed(self) -> int:
        return sum(
            len(attempt.deleted_locators)
            for item in self.batches
            for attempt in item.attempts
        )

    @property
    def failures(self) -> int:
        return len(self.issues) + sum(not item.succeeded for item in self.batches)

    def summary(self) -> dict[str, int]:
        return {
            "directories_removed": self.directories_removed,
            "files_removed": self.files_removed,
            "failures": self.failures,
        }


# The prompt calls this input simply ``limits``. Keep a concise public alias for integrators.
UploadLimits = ScreenshotBatchLimits
