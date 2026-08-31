from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import stat as stat_module
from collections.abc import AsyncIterable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError as PythonMultipartParseError
from python_multipart.multipart import parse_options_header

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
)
from app.trip_understanding.screenshot_batch.security import (
    require_node_local,
    secure_owner_only,
)


_SUPPORTED_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_LOCATOR_BYTES = 32
_LOCATOR_CREATION_ATTEMPTS = 16
_WINDOWS_REPARSE_POINT = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def stat_is_link_or_reparse(observed: os.stat_result) -> bool:
    """Recognize Unix links and every Windows reparse-point path."""

    return stat_module.S_ISLNK(observed.st_mode) or bool(
        getattr(observed, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


def validate_no_link_or_reparse_components(path: Path) -> Path:
    """Return a lexical absolute path after lstat-checking every existing component.

    This check intentionally runs before ``Path.resolve``.  In particular, a
    Windows junction is a directory according to ``is_dir()`` and is not always
    reported by ``is_symlink()``, but its reparse-point attribute is visible to
    ``lstat``.
    """

    try:
        absolute = Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError) as exc:
        raise ScreenshotPathSecurityError(
            "screenshot temporary path could not be inspected"
        ) from exc

    current = Path(absolute.anchor)
    try:
        anchor_stat = current.lstat()
    except OSError as exc:
        raise ScreenshotPathSecurityError(
            "screenshot temporary path anchor could not be inspected"
        ) from exc
    if stat_is_link_or_reparse(anchor_stat) or not stat_module.S_ISDIR(
        anchor_stat.st_mode
    ):
        raise ScreenshotPathSecurityError(
            "screenshot temporary path anchor is unsafe"
        )
    components = absolute.parts[1:] if absolute.anchor else absolute.parts
    missing_parent = False
    for component in components:
        current /= component
        try:
            observed = current.lstat()
        except FileNotFoundError:
            missing_parent = True
            continue
        except OSError as exc:
            raise ScreenshotPathSecurityError(
                "screenshot temporary path could not be inspected"
            ) from exc
        if missing_parent:
            raise ScreenshotPathSecurityError(
                "screenshot temporary path changed while it was inspected"
            )
        if stat_is_link_or_reparse(observed):
            raise ScreenshotPathSecurityError(
                "screenshot temporary path cannot contain links or reparse points"
            )
        if current != absolute and not stat_module.S_ISDIR(observed.st_mode):
            raise ScreenshotPathSecurityError(
                "screenshot temporary path parent must be a directory"
            )
    return absolute


def secure_and_validate_open_regular_file(path: Path, descriptor: int) -> os.stat_result:
    """Bind an open owner-only regular file to the same non-link path inode."""

    try:
        path_before = path.lstat()
        descriptor_before = os.fstat(descriptor)
    except OSError as exc:
        raise ScreenshotPathSecurityError(
            "staged screenshot file identity could not be verified"
        ) from exc
    if (
        stat_is_link_or_reparse(path_before)
        or not stat_module.S_ISREG(path_before.st_mode)
        or not stat_module.S_ISREG(descriptor_before.st_mode)
        or getattr(path_before, "st_nlink", 1) != 1
        or getattr(descriptor_before, "st_nlink", 1) != 1
        or not os.path.samestat(path_before, descriptor_before)
    ):
        raise ScreenshotPathSecurityError(
            "staged screenshot file is not a unique regular file"
        )
    if hasattr(os, "geteuid") and descriptor_before.st_uid != os.geteuid():
        raise ScreenshotPathSecurityError(
            "staged screenshot file owner is not the process user"
        )

    secure_owner_only(path, is_directory=False)
    try:
        path_after = path.lstat()
        descriptor_after = os.fstat(descriptor)
    except OSError as exc:
        raise ScreenshotPathSecurityError(
            "staged screenshot file identity could not be verified"
        ) from exc
    if (
        stat_is_link_or_reparse(path_after)
        or not stat_module.S_ISREG(path_after.st_mode)
        or not stat_module.S_ISREG(descriptor_after.st_mode)
        or getattr(path_after, "st_nlink", 1) != 1
        or getattr(descriptor_after, "st_nlink", 1) != 1
        or not os.path.samestat(path_after, descriptor_after)
        or not os.path.samestat(descriptor_before, descriptor_after)
    ):
        raise ScreenshotPathSecurityError(
            "staged screenshot file changed while it was secured"
        )
    if os.name != "nt" and stat_module.S_IMODE(descriptor_after.st_mode) != 0o600:
        raise ScreenshotPathSecurityError(
            "staged screenshot file permissions are not owner-only"
        )
    if hasattr(os, "geteuid") and descriptor_after.st_uid != os.geteuid():
        raise ScreenshotPathSecurityError(
            "staged screenshot file owner is not the process user"
        )
    return descriptor_after


def _is_inside_git_checkout(path: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


def _prepare_temp_root(temp_root: str | os.PathLike[str]) -> Path:
    root = Path(temp_root).expanduser()
    require_node_local(root)
    root = validate_no_link_or_reparse_components(root)
    root = root.resolve(strict=False)
    require_node_local(root)
    if _is_inside_git_checkout(root):
        raise ScreenshotPathSecurityError("temporary screenshot root cannot be inside a Git checkout")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ScreenshotStorageError("temporary screenshot root could not be created") from exc
    root = validate_no_link_or_reparse_components(root)
    root = root.resolve(strict=True)
    try:
        root_before = root.lstat()
    except OSError as exc:
        raise ScreenshotPathSecurityError(
            "temporary screenshot root could not be verified"
        ) from exc
    if stat_is_link_or_reparse(root_before) or not stat_module.S_ISDIR(root_before.st_mode):
        raise ScreenshotPathSecurityError("temporary screenshot root must resolve to a directory")
    secure_owner_only(root, is_directory=True)
    try:
        root_after = root.lstat()
    except OSError as exc:
        raise ScreenshotPathSecurityError(
            "temporary screenshot root could not be verified"
        ) from exc
    if (
        stat_is_link_or_reparse(root_after)
        or not stat_module.S_ISDIR(root_after.st_mode)
        or not os.path.samestat(root_before, root_after)
    ):
        raise ScreenshotPathSecurityError(
            "temporary screenshot root changed while it was secured"
        )
    return root


def _create_batch_directory(root: Path) -> tuple[str, Path]:
    for _ in range(_LOCATOR_CREATION_ATTEMPTS):
        locator = secrets.token_hex(_LOCATOR_BYTES)
        directory = root / locator
        try:
            os.mkdir(directory, mode=0o700)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ScreenshotStorageError("staged screenshot directory could not be created") from exc
        directory_before: os.stat_result | None = None
        try:
            validate_no_link_or_reparse_components(directory)
            directory_before = directory.lstat()
            if not stat_module.S_ISDIR(directory_before.st_mode):
                raise ScreenshotPathSecurityError(
                    "staged screenshot directory is not a directory"
                )
            secure_owner_only(directory, is_directory=True)
            directory_after = directory.lstat()
            if (
                stat_is_link_or_reparse(directory_after)
                or not stat_module.S_ISDIR(directory_after.st_mode)
                or not os.path.samestat(directory_before, directory_after)
            ):
                raise ScreenshotPathSecurityError(
                    "staged screenshot directory changed while it was secured"
                )
        except BaseException:
            try:
                if (
                    directory_before is not None
                    and not stat_is_link_or_reparse(directory.lstat())
                    and os.path.samestat(directory_before, directory.lstat())
                ):
                    directory.rmdir()
            except OSError:
                pass
            raise
        return locator, directory
    raise ScreenshotStorageError("a unique staged screenshot directory could not be allocated")


def _reserve_random_asset(directory: Path) -> tuple[str, Path, int]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOINHERIT", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _ in range(_LOCATOR_CREATION_ATTEMPTS):
        locator = secrets.token_hex(_LOCATOR_BYTES)
        path = directory / locator
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ScreenshotStorageError("staged screenshot file could not be created") from exc
        return locator, path, descriptor
    raise ScreenshotStorageError("a unique staged screenshot locator could not be allocated")


def _magic_matches(media_type: str, prefix: bytes) -> bool:
    if media_type == "image/png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return prefix.startswith(b"\xff\xd8\xff")
    return len(prefix) >= 12 and prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"


def _required_magic_bytes(media_type: str) -> int:
    return {"image/png": 8, "image/jpeg": 3, "image/webp": 12}[media_type]


@dataclass(slots=True)
class _PartState:
    part_index: int = -1
    header_name: bytearray = field(default_factory=bytearray)
    header_value: bytearray = field(default_factory=bytearray)
    headers: dict[bytes, bytes] = field(default_factory=dict)
    header_bytes: int = 0
    media_type: str = ""
    locator: str = ""
    path: Path | None = None
    handle: BinaryIO | None = None
    byte_size: int = 0
    hasher: Any = field(default_factory=hashlib.sha256)
    magic_prefix: bytearray = field(default_factory=bytearray)
    completed: bool = False

    def close(self, *, suppress_errors: bool = False) -> None:
        if self.handle is not None and not self.handle.closed:
            try:
                self.handle.close()
            except OSError:
                if not suppress_errors:
                    raise


class _MultipartStager:
    def __init__(self, limits: ScreenshotBatchLimits, temp_root: Path) -> None:
        self.limits = limits
        self.temp_root = temp_root
        self.batch_locator, self.directory = _create_batch_directory(temp_root)
        self.parts: list[_PartState] = []
        self.current: _PartState | None = None
        self.ended = False

    def callbacks(self) -> dict[str, Any]:
        return {
            "on_part_begin": self.on_part_begin,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_end": self.on_end,
        }

    def _require_current(self) -> _PartState:
        if self.current is None:
            raise MultipartMalformedError("multipart callback order is invalid")
        return self.current

    def _add_header_bytes(self, part: _PartState, count: int) -> None:
        part.header_bytes += count
        if part.header_bytes > self.limits.max_header_bytes:
            raise MultipartMalformedError("multipart part headers exceed the configured bound")

    def on_part_begin(self) -> None:
        if self.current is not None:
            raise MultipartMalformedError("multipart part began before the prior part ended")
        self.current = _PartState()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        part = self._require_current()
        value = data[start:end]
        self._add_header_bytes(part, len(value))
        part.header_name.extend(value)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        part = self._require_current()
        value = data[start:end]
        self._add_header_bytes(part, len(value))
        part.header_value.extend(value)

    def on_header_end(self) -> None:
        part = self._require_current()
        name = bytes(part.header_name).strip().lower()
        value = bytes(part.header_value).strip()
        if not name or name in part.headers:
            raise MultipartMalformedError("multipart part contains an invalid or duplicate header")
        part.headers[name] = value
        part.header_name.clear()
        part.header_value.clear()

    def on_headers_finished(self) -> None:
        part = self._require_current()
        raw_disposition = part.headers.get(b"content-disposition")
        if raw_disposition is None:
            raise MultipartMalformedError("multipart screenshot part is missing Content-Disposition")
        try:
            disposition, options = parse_options_header(raw_disposition)
        except (TypeError, ValueError) as exc:
            raise MultipartMalformedError("multipart Content-Disposition is malformed") from exc
        if disposition.lower() != b"form-data" or options.get(b"name") != b"screenshots":
            raise MultipartMalformedError("multipart accepts only repeated screenshots fields")
        if b"filename" not in options:
            raise MultipartMalformedError("multipart screenshots field must be a file part")

        part_index = len(self.parts)
        if part_index >= self.limits.max_files:
            raise ScreenshotCountError(
                f"screenshot batch must contain at most {self.limits.max_files} files"
            )
        raw_media_type = part.headers.get(b"content-type")
        if raw_media_type is None:
            raise ScreenshotUnsupportedMediaTypeError("screenshot file part is missing Content-Type")
        try:
            parsed_media_type, _ = parse_options_header(raw_media_type)
            media_type = parsed_media_type.decode("ascii").lower()
        except (UnicodeDecodeError, TypeError, ValueError) as exc:
            raise ScreenshotUnsupportedMediaTypeError("screenshot Content-Type is invalid") from exc
        if media_type not in _SUPPORTED_MEDIA_TYPES:
            raise ScreenshotUnsupportedMediaTypeError("screenshots must be PNG, JPEG or WebP")

        locator, path, descriptor = _reserve_random_asset(self.directory)
        part.part_index = part_index
        part.media_type = media_type
        part.locator = locator
        part.path = path
        self.parts.append(part)
        try:
            secure_and_validate_open_regular_file(path, descriptor)
            part.handle = os.fdopen(descriptor, "wb", buffering=0)
        except BaseException as exc:
            os.close(descriptor)
            if isinstance(exc, ScreenshotBatchError):
                raise
            raise ScreenshotStorageError("staged screenshot file could not be opened") from exc

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        part = self._require_current()
        if part.handle is None:
            raise MultipartMalformedError("multipart supplied data before screenshot headers completed")
        payload = data[start:end]
        next_size = part.byte_size + len(payload)
        if next_size > self.limits.max_file_bytes:
            raise ScreenshotFileTooLargeError(
                f"each screenshot must contain at most {self.limits.max_file_bytes} bytes"
            )
        required_prefix = _required_magic_bytes(part.media_type)
        if len(part.magic_prefix) < required_prefix:
            remaining = required_prefix - len(part.magic_prefix)
            part.magic_prefix.extend(payload[:remaining])
            if len(part.magic_prefix) == required_prefix and not _magic_matches(
                part.media_type, bytes(part.magic_prefix)
            ):
                raise ScreenshotMagicMismatchError("declared screenshot media type does not match its bytes")
        try:
            written = part.handle.write(payload)
        except OSError as exc:
            raise ScreenshotStorageError("staged screenshot bytes could not be written") from exc
        if written != len(payload):
            raise ScreenshotStorageError("staged screenshot write was incomplete")
        part.hasher.update(payload)
        part.byte_size = next_size

    def on_part_end(self) -> None:
        part = self._require_current()
        part.close()
        if part.handle is None:
            raise MultipartMalformedError("multipart screenshot part did not contain a file")
        if part.byte_size == 0:
            raise ScreenshotEmptyFileError("screenshots cannot be empty")
        if not _magic_matches(part.media_type, bytes(part.magic_prefix)):
            raise ScreenshotMagicMismatchError("declared screenshot media type does not match its bytes")
        part.completed = True
        self.current = None

    def on_end(self) -> None:
        if self.current is not None:
            raise MultipartMalformedError("multipart ended before its current part completed")
        self.ended = True

    def close_all(self, *, suppress_errors: bool = False) -> None:
        for part in self.parts:
            part.close(suppress_errors=suppress_errors)

    def snapshot(self, *, body_bytes: int, require_complete: bool) -> StagedBatch:
        self.close_all(suppress_errors=not require_complete)
        if require_complete:
            if not self.ended or any(not part.completed for part in self.parts):
                raise MultipartMalformedError("multipart body is incomplete")
            if not self.limits.min_files <= len(self.parts) <= self.limits.max_files:
                raise ScreenshotCountError(
                    f"screenshot batch must contain {self.limits.min_files} to {self.limits.max_files} files"
                )
        screenshots = tuple(
            StagedScreenshot(
                part_index=part.part_index,
                media_type=part.media_type,
                byte_size=part.byte_size,
                sha256=part.hasher.hexdigest(),
                locator=part.locator,
                path=part.path,
            )
            for part in self.parts
            if part.path is not None
        )
        return StagedBatch(
            batch_locator=self.batch_locator,
            temp_root=self.temp_root,
            directory=self.directory,
            screenshots=screenshots,
            body_bytes=body_bytes,
            total_file_bytes=sum(item.byte_size for item in screenshots),
        )


def _parse_boundary(content_type: str, limits: ScreenshotBatchLimits) -> bytes:
    if not isinstance(content_type, str):
        raise MultipartRequiredError("content_type must be multipart/form-data")
    try:
        media_type, options = parse_options_header(content_type.encode("latin-1"))
    except (UnicodeEncodeError, TypeError, ValueError) as exc:
        raise MultipartRequiredError("content_type must be multipart/form-data") from exc
    if media_type.lower() != b"multipart/form-data":
        raise MultipartRequiredError("content_type must be multipart/form-data")
    boundary = options.get(b"boundary")
    if (
        not boundary
        or len(boundary) > limits.max_boundary_bytes
        or any(value in boundary for value in (b"\r", b"\n", b"\x00"))
    ):
        raise MultipartMalformedError("multipart boundary is missing or invalid")
    return boundary


def _terminal_reason(exc: BaseException) -> str:
    if isinstance(exc, asyncio.CancelledError):
        return "CANCELLED"
    if isinstance(exc, TimeoutError):
        return "TIMED_OUT"
    if isinstance(exc, ScreenshotBatchError):
        return "STAGING_FAILED"
    return "FAILED"


def _translate_error(exc: BaseException) -> BaseException:
    if isinstance(exc, (ScreenshotBatchError, ScreenshotStagingCancelledError, ScreenshotStagingTimeoutError)):
        return exc
    if isinstance(exc, asyncio.CancelledError):
        return ScreenshotStagingCancelledError("screenshot staging was cancelled")
    if isinstance(exc, TimeoutError):
        return ScreenshotStagingTimeoutError("screenshot staging timed out")
    if isinstance(exc, PythonMultipartParseError):
        return MultipartMalformedError("multipart body is malformed")
    if isinstance(exc, OSError):
        return ScreenshotStorageError("screenshot staging encountered a storage error")
    if isinstance(exc, Exception):
        return ScreenshotStreamError("screenshot body stream failed")
    return exc


def _attach_staging_evidence(
    error: BaseException,
    batch: StagedBatch,
    attempts: Sequence[CleanupAttempt],
) -> None:
    attach = getattr(error, "attach_staging_evidence", None)
    if callable(attach):
        attach(batch, attempts)
        return
    setattr(error, "batch", batch)
    setattr(error, "cleanup_attempts", tuple(attempts))


async def stage_screenshot_multipart(
    content_type: str,
    chunks: AsyncIterable[bytes],
    limits: ScreenshotBatchLimits | None,
    temp_root: str | os.PathLike[str],
    cleanup_started: Callable[[StagedBatch, str], None] | None = None,
    cleanup_finished: Callable[[StagedBatch, tuple[CleanupAttempt, ...]], None]
    | None = None,
) -> StagedBatch:
    """Receive and stage an ordered multipart screenshot batch within fixed bounds."""

    effective_limits = limits or DEFAULT_SCREENSHOT_BATCH_LIMITS
    if not isinstance(effective_limits, ScreenshotBatchLimits):
        raise TypeError("limits must be ScreenshotBatchLimits or None")
    boundary = _parse_boundary(content_type, effective_limits)
    root = _prepare_temp_root(temp_root)
    stager = _MultipartStager(effective_limits, root)
    parser = MultipartParser(boundary, stager.callbacks())
    body_bytes = 0

    try:
        async for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise MultipartMalformedError("multipart body chunks must be bytes")
            body_bytes += len(chunk)
            if body_bytes > effective_limits.max_total_bytes:
                raise ScreenshotBatchTooLargeError(
                    f"multipart screenshot batch must contain at most {effective_limits.max_total_bytes} bytes"
                )
            if chunk:
                parser.write(chunk)
        parser.finalize()
        if not stager.ended:
            raise MultipartMalformedError("multipart body is incomplete")
        return stager.snapshot(body_bytes=body_bytes, require_complete=True)
    except BaseException as exc:
        stager.close_all(suppress_errors=True)
        partial_batch = stager.snapshot(body_bytes=body_bytes, require_complete=False)
        translated = _translate_error(exc)
        terminal_reason = _terminal_reason(exc)
        if cleanup_started is not None:
            try:
                cleanup_started(partial_batch, terminal_reason)
            except BaseException as journal_error:
                attempt = CleanupAttempt(
                    attempt_number=1,
                    terminal_reason=terminal_reason,
                    attempted_at=datetime.now(timezone.utc),
                    deleted_locators=(),
                    already_absent_locators=(),
                    failed_locators=tuple(
                        item.locator for item in partial_batch.screenshots
                    ),
                    remaining_locators=tuple(
                        item.locator for item in partial_batch.screenshots
                    ),
                    error_categories=("CLEANUP_JOURNAL_UNAVAILABLE",),
                    directory_removed=False,
                    succeeded=False,
                )
                raise ScreenshotCleanupError(
                    "cleanup journal could not be written",
                    attempts=(attempt,),
                    staging_error_code=getattr(
                        translated, "code", type(translated).__name__
                    ),
                    batch=partial_batch,
                ) from journal_error
        try:
            cleanup_attempts = cleanup_staged_batch(partial_batch, terminal_reason)
        except ScreenshotCleanupError as cleanup_error:
            cleanup_error.staging_error_code = getattr(translated, "code", type(translated).__name__)
            if cleanup_finished is not None:
                try:
                    cleanup_finished(partial_batch, cleanup_error.attempts)
                except BaseException as journal_error:
                    _attach_staging_evidence(
                        journal_error, partial_batch, cleanup_error.attempts
                    )
                    raise journal_error from cleanup_error
            raise cleanup_error from exc
        if cleanup_finished is not None:
            try:
                cleanup_finished(partial_batch, cleanup_attempts)
            except BaseException as journal_error:
                _attach_staging_evidence(journal_error, partial_batch, cleanup_attempts)
                raise journal_error from exc
        _attach_staging_evidence(translated, partial_batch, cleanup_attempts)
        if translated is exc:
            raise
        raise translated from exc
