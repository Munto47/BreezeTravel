from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from app.trip_understanding.screenshot_batch import (
    DEFAULT_SCREENSHOT_BATCH_LIMITS,
    MultipartMalformedError,
    MultipartRequiredError,
    ScreenshotBatchLimits,
    ScreenshotBatchTooLargeError,
    ScreenshotCleanupError,
    ScreenshotCountError,
    ScreenshotEmptyFileError,
    ScreenshotFileTooLargeError,
    ScreenshotMagicMismatchError,
    ScreenshotPathSecurityError,
    ScreenshotStagingCancelledError,
    ScreenshotStagingTimeoutError,
    ScreenshotStreamError,
    ScreenshotUnsupportedMediaTypeError,
    cleanup_staged_batch,
    stage_screenshot_multipart,
)


MIB = 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
WEBP_MAGIC = b"RIFF\x04\x00\x00\x00WEBP"
RANDOM_LOCATOR = re.compile(r"[0-9a-f]{64}")


def _multipart(
    parts: list[tuple[str, bytes, str]],
    *,
    boundary: str = "g04-controlled-boundary",
    close: bool = True,
    field_name: str = "screenshots",
) -> tuple[str, bytes]:
    body = bytearray()
    for media_type, payload, filename in parts:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            (
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                f"Content-Type: {media_type}\r\n\r\n"
            ).encode()
        )
        body.extend(payload)
        body.extend(b"\r\n")
    if close:
        body.extend(f"--{boundary}--\r\n".encode())
    return f'multipart/form-data; boundary="{boundary}"', bytes(body)


async def _chunks(body: bytes, chunk_size: int = 64 * 1024):
    for offset in range(0, len(body), chunk_size):
        await asyncio.sleep(0)
        yield body[offset : offset + chunk_size]


async def _stream_then_raise(body: bytes, exc: BaseException):
    yield body
    raise exc


def _assert_temp_root_empty(root: Path) -> None:
    if root.exists():
        assert list(root.rglob("*")) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stages_three_formats_in_order_with_random_owner_only_paths(tmp_path: Path) -> None:
    temp_root = tmp_path / "staged"
    parts = [
        ("image/png", PNG_MAGIC + b"png-payload", "../../private-original.png"),
        ("image/jpeg", JPEG_MAGIC + b"jpeg-payload", "C:\\secrets\\photo.jpg"),
        ("image/webp", WEBP_MAGIC + b"webp-payload", "notes.webp"),
    ]
    content_type, body = _multipart(parts)

    batch = await stage_screenshot_multipart(content_type, _chunks(body, 7), None, temp_root)

    assert [item.part_index for item in batch.screenshots] == [0, 1, 2]
    assert [item.media_type for item in batch.screenshots] == [item[0] for item in parts]
    assert [item.path.read_bytes() for item in batch.screenshots] == [item[1] for item in parts]
    assert [item.sha256 for item in batch.screenshots] == [hashlib.sha256(item[1]).hexdigest() for item in parts]
    assert batch.total_file_bytes == sum(len(item[1]) for item in parts)
    assert batch.body_bytes == len(body)
    assert RANDOM_LOCATOR.fullmatch(batch.batch_locator)
    assert batch.directory.parent == temp_root.resolve()
    assert all(RANDOM_LOCATOR.fullmatch(item.locator) for item in batch.screenshots)
    assert all(item.path.parent == batch.directory for item in batch.screenshots)
    assert all(item.path.name == item.locator for item in batch.screenshots)
    assert "private-original" not in repr(batch)
    assert "photo.jpg" not in repr(batch)
    assert "notes.webp" not in repr(batch)
    if os.name != "nt":
        assert stat.S_IMODE(batch.directory.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(item.path.stat().st_mode) == 0o600 for item in batch.screenshots)

    attempts = cleanup_staged_batch(batch, "SUCCEEDED")
    assert len(attempts) == 1
    assert attempts[0].terminal_reason == "SUCCEEDED"
    assert attempts[0].succeeded is True
    assert not batch.directory.exists()
    _assert_temp_root_empty(temp_root)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 6])
async def test_accepts_one_and_six_ordered_screenshots(tmp_path: Path, count: int) -> None:
    parts = [("image/png", PNG_MAGIC + bytes([index]), f"{index}.png") for index in range(count)]
    content_type, body = _multipart(parts)

    batch = await stage_screenshot_multipart(content_type, _chunks(body), None, tmp_path / f"count-{count}")

    assert len(batch.screenshots) == count
    assert [item.path.read_bytes()[-1] for item in batch.screenshots] == list(range(count))
    cleanup_staged_batch(batch, "SUCCEEDED")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rejects_zero_and_seven_files_and_cleans_every_created_file(tmp_path: Path) -> None:
    zero_root = tmp_path / "zero"
    content_type, empty_body = _multipart([])
    with pytest.raises(ScreenshotCountError) as zero_error:
        await stage_screenshot_multipart(content_type, _chunks(empty_body), None, zero_root)
    assert zero_error.value.cleanup_attempts[-1].succeeded is True
    _assert_temp_root_empty(zero_root)

    seven_root = tmp_path / "seven"
    seven_parts = [("image/png", PNG_MAGIC + bytes([index]), f"{index}.png") for index in range(7)]
    content_type, seven_body = _multipart(seven_parts)
    with pytest.raises(ScreenshotCountError) as seven_error:
        await stage_screenshot_multipart(content_type, _chunks(seven_body, 11), None, seven_root)
    assert seven_error.value.cleanup_attempts[-1].succeeded is True
    _assert_temp_root_empty(seven_root)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enforces_exact_10_mib_file_boundary_and_cleans_overflow(tmp_path: Path) -> None:
    exact_payload = PNG_MAGIC + b"a" * (10 * MIB - len(PNG_MAGIC))
    content_type, exact_body = _multipart([("image/png", exact_payload, "exact.png")])
    batch = await stage_screenshot_multipart(content_type, _chunks(exact_body), None, tmp_path / "exact")
    assert batch.screenshots[0].byte_size == 10 * MIB
    cleanup_staged_batch(batch, "SUCCEEDED")

    overflow_root = tmp_path / "overflow"
    overflow_payload = exact_payload + b"x"
    content_type, overflow_body = _multipart([("image/png", overflow_payload, "overflow.png")])
    with pytest.raises(ScreenshotFileTooLargeError) as error:
        await stage_screenshot_multipart(content_type, _chunks(overflow_body, 8191), None, overflow_root)
    assert error.value.cleanup_attempts[-1].succeeded is True
    _assert_temp_root_empty(overflow_root)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enforces_61_mib_request_limit_before_parser_consumption(tmp_path: Path) -> None:
    assert DEFAULT_SCREENSHOT_BATCH_LIMITS.max_total_bytes == 61 * MIB
    content_type, _ = _multipart([])
    oversized_chunk = b"x" * (DEFAULT_SCREENSHOT_BATCH_LIMITS.max_total_bytes + 1)
    root = tmp_path / "total-overflow"

    with pytest.raises(ScreenshotBatchTooLargeError) as error:
        await stage_screenshot_multipart(content_type, _chunks(oversized_chunk, len(oversized_chunk)), None, root)

    assert error.value.cleanup_attempts[-1].succeeded is True
    _assert_temp_root_empty(root)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_total_body_bound_is_inclusive_and_checked_across_chunks(tmp_path: Path) -> None:
    content_type, body = _multipart([("image/png", PNG_MAGIC + b"payload", "one.png")])
    exact_limits = ScreenshotBatchLimits(max_total_bytes=len(body))
    batch = await stage_screenshot_multipart(content_type, _chunks(body, 1), exact_limits, tmp_path / "inclusive")
    cleanup_staged_batch(batch, "SUCCEEDED")

    root = tmp_path / "one-byte-over"
    too_small = ScreenshotBatchLimits(max_total_bytes=len(body) - 1)
    with pytest.raises(ScreenshotBatchTooLargeError):
        await stage_screenshot_multipart(content_type, _chunks(body, 1), too_small, root)
    _assert_temp_root_empty(root)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rejects_json_unsupported_mime_magic_mismatch_and_empty_file(tmp_path: Path) -> None:
    json_root = tmp_path / "json"
    with pytest.raises(MultipartRequiredError):
        await stage_screenshot_multipart("application/json", _chunks(b'{"screenshots": []}'), None, json_root)
    assert not json_root.exists()

    cases = [
        (
            ScreenshotUnsupportedMediaTypeError,
            [("image/gif", b"GIF89a", "fake.gif")],
            tmp_path / "unsupported",
        ),
        (
            ScreenshotMagicMismatchError,
            [("image/png", JPEG_MAGIC + b"not-png", "fake.png")],
            tmp_path / "magic",
        ),
        (
            ScreenshotEmptyFileError,
            [("image/png", b"", "empty.png")],
            tmp_path / "empty",
        ),
    ]
    for error_type, parts, root in cases:
        content_type, body = _multipart(parts)
        with pytest.raises(error_type) as error:
            await stage_screenshot_multipart(content_type, _chunks(body, 2), None, root)
        assert error.value.cleanup_attempts[-1].succeeded is True
        _assert_temp_root_empty(root)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_file_limit_is_enforced_when_overflow_arrives_one_byte_at_a_time(tmp_path: Path) -> None:
    limits = ScreenshotBatchLimits(max_file_bytes=16, max_total_bytes=1024)
    content_type, body = _multipart([("image/png", PNG_MAGIC + b"123456789", "chunked.png")])
    root = tmp_path / "chunked-overflow"

    with pytest.raises(ScreenshotFileTooLargeError) as error:
        await stage_screenshot_multipart(content_type, _chunks(body, 1), limits, root)

    assert error.value.cleanup_attempts[-1].succeeded is True
    _assert_temp_root_empty(root)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_and_exceptional_streams_delete_partial_files(tmp_path: Path) -> None:
    content_type, incomplete_body = _multipart(
        [("image/png", PNG_MAGIC + b"partial", "partial.png")],
        close=False,
    )
    malformed_root = tmp_path / "malformed"
    with pytest.raises(MultipartMalformedError) as malformed:
        await stage_screenshot_multipart(content_type, _chunks(incomplete_body), None, malformed_root)
    assert malformed.value.cleanup_attempts[-1].terminal_reason == "STAGING_FAILED"
    assert malformed.value.cleanup_attempts[-1].succeeded is True
    _assert_temp_root_empty(malformed_root)

    exception_root = tmp_path / "exception"
    with pytest.raises(ScreenshotStreamError) as stream_error:
        await stage_screenshot_multipart(
            content_type,
            _stream_then_raise(incomplete_body, RuntimeError("controlled stream failure")),
            None,
            exception_root,
        )
    assert stream_error.value.cleanup_attempts[-1].terminal_reason == "FAILED"
    assert stream_error.value.cleanup_attempts[-1].succeeded is True
    _assert_temp_root_empty(exception_root)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_and_deadline_are_typed_and_delete_partial_files(tmp_path: Path) -> None:
    content_type, incomplete_body = _multipart(
        [("image/png", PNG_MAGIC + b"partial", "partial.png")],
        close=False,
    )
    cancelled_root = tmp_path / "cancelled"
    with pytest.raises(ScreenshotStagingCancelledError) as cancelled:
        await stage_screenshot_multipart(
            content_type,
            _stream_then_raise(incomplete_body, asyncio.CancelledError()),
            None,
            cancelled_root,
        )
    assert cancelled.value.cleanup_attempts[-1].terminal_reason == "CANCELLED"
    assert cancelled.value.cleanup_attempts[-1].succeeded is True
    _assert_temp_root_empty(cancelled_root)

    timeout_root = tmp_path / "timeout"
    with pytest.raises(ScreenshotStagingTimeoutError) as timed_out:
        await stage_screenshot_multipart(
            content_type,
            _stream_then_raise(incomplete_body, TimeoutError("controlled deadline")),
            None,
            timeout_root,
        )
    assert timed_out.value.cleanup_attempts[-1].terminal_reason == "TIMED_OUT"
    assert timed_out.value.cleanup_attempts[-1].succeeded is True
    _assert_temp_root_empty(timeout_root)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_retries_and_returns_frozen_attempt_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_type, body = _multipart([("image/png", PNG_MAGIC + b"retry", "retry.png")])
    batch = await stage_screenshot_multipart(content_type, _chunks(body), None, tmp_path / "retry")
    original_unlink = Path.unlink
    calls = 0

    def fail_once(path: Path, missing_ok: bool = False) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("controlled first-attempt lock")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_once)
    attempts = cleanup_staged_batch(batch, "EXPIRED")

    assert len(attempts) == 2
    assert attempts[0].succeeded is False
    assert attempts[0].failed_locators == (batch.screenshots[0].locator,)
    assert "PERMISSIONERROR" in attempts[0].error_categories
    assert attempts[1].succeeded is True
    assert attempts[1].terminal_reason == "EXPIRED"
    with pytest.raises(FrozenInstanceError):
        attempts[0].succeeded = True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_failure_is_typed_blocking_result_and_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_type, body = _multipart([("image/png", PNG_MAGIC + b"locked", "locked.png")])
    batch = await stage_screenshot_multipart(content_type, _chunks(body), None, tmp_path / "locked")

    def always_fail(path: Path, missing_ok: bool = False) -> None:
        raise PermissionError("controlled persistent lock")

    with monkeypatch.context() as controlled:
        controlled.setattr(Path, "unlink", always_fail)
        with pytest.raises(ScreenshotCleanupError) as blocked:
            cleanup_staged_batch(batch, "TIMED_OUT")

    error = blocked.value
    assert error.batch == batch
    assert len(error.attempts) == 3
    assert all(not attempt.succeeded for attempt in error.attempts)
    assert batch.screenshots[0].path.exists()
    recovery = cleanup_staged_batch(error.batch, "TIMED_OUT")
    assert recovery[-1].succeeded is True
    _assert_temp_root_empty(batch.temp_root)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_staging_cleanup_failure_overrides_parse_success_and_blocks_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_type, body = _multipart([("image/png", JPEG_MAGIC + b"wrong", "wrong.png")])

    def always_fail(path: Path, missing_ok: bool = False) -> None:
        raise PermissionError("controlled persistent lock")

    with monkeypatch.context() as controlled:
        controlled.setattr(Path, "unlink", always_fail)
        with pytest.raises(ScreenshotCleanupError) as blocked:
            await stage_screenshot_multipart(content_type, _chunks(body), None, tmp_path / "privacy-blocked")

    error = blocked.value
    assert error.staging_error_code == ScreenshotMagicMismatchError.code
    assert error.batch is not None
    assert len(error.attempts) == 3
    assert all(not attempt.succeeded for attempt in error.attempts)
    recovery = cleanup_staged_batch(error.batch, "PRIVACY_BLOCKED")
    assert recovery[-1].succeeded is True
    _assert_temp_root_empty(error.batch.temp_root)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rejects_git_roots_path_escape_and_never_uses_original_filename(tmp_path: Path) -> None:
    content_type, body = _multipart(
        [("image/png", PNG_MAGIC + b"safe", "../../tracked-secret.png")]
    )
    repository_root = Path(__file__).resolve().parents[2]
    with pytest.raises(ScreenshotPathSecurityError):
        await stage_screenshot_multipart(content_type, _chunks(body), None, repository_root)

    batch = await stage_screenshot_multipart(content_type, _chunks(body), None, tmp_path / "outside-git")
    assert batch.screenshots[0].path.name == batch.screenshots[0].locator
    assert "tracked-secret" not in str(batch.directory)
    escaped_asset = replace(batch.screenshots[0], path=tmp_path / "escaped")
    escaped_batch = replace(batch, screenshots=(escaped_asset,))
    with pytest.raises(ScreenshotPathSecurityError):
        cleanup_staged_batch(escaped_batch, "CANCELLED")
    cleanup_staged_batch(batch, "CANCELLED")
    _assert_temp_root_empty(batch.temp_root)
