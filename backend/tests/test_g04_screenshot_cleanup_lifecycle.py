from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.api import screenshot_batches_v3
from app.config import clear_settings_cache
from app.trip_understanding.screenshot_batch import (
    CleanupAttempt,
    LocalRecoveryBatchEvidence,
    LocalScreenshotRecoveryReport,
    ScreenshotBatchError,
    ScreenshotPathSecurityError,
    ScreenshotStagingCancelledError,
    StagedBatch,
    StagedScreenshot,
    cleanup_staged_batch,
    stage_screenshot_multipart,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"g04-private-pixels"


@pytest.fixture(autouse=True)
def _reset_settings_cache_after_test():
    """Do not leak a deleted tmp_path into later full-suite app startups."""

    clear_settings_cache()
    yield
    clear_settings_cache()


def _create_directory_link(target: Path, link: Path) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            pytest.skip("Windows directory junctions are unavailable")
        assert not link.is_symlink()
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")


def _remove_directory_link(link: Path) -> None:
    if os.name == "nt":
        link.rmdir()
    else:
        link.unlink()


def _write_pending_cleanup_journal_in_process(
    root_value: str,
    batch_locator: str,
    asset_locator: str,
    ready,
    release,
) -> None:
    root = Path(root_value)
    batch = StagedBatch(
        batch_locator=batch_locator,
        temp_root=root,
        directory=root / batch_locator,
        screenshots=(
            StagedScreenshot(
                part_index=0,
                media_type="image/png",
                byte_size=len(PNG),
                sha256="c" * 64,
                locator=asset_locator,
                path=root / batch_locator / asset_locator,
            ),
        ),
        body_bytes=len(PNG),
        total_file_bytes=len(PNG),
    )
    ready.put(True)
    if not release.wait(20):
        raise RuntimeError("cross-process cleanup journal test did not start")
    screenshot_batches_v3.begin_local_screenshot_cleanup(batch, "SUCCEEDED")


def _multipart(payload: bytes) -> tuple[str, bytes]:
    boundary = "g04-cleanup-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="screenshots"; filename="private.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body


async def _chunks(payload: bytes):
    midpoint = max(1, len(payload) // 2)
    yield payload[:midpoint]
    yield payload[midpoint:]


def _evidence_batch(tmp_path: Path) -> tuple[StagedBatch, CleanupAttempt]:
    batch_locator = "a" * 64
    asset_locator = "b" * 64
    screenshot = StagedScreenshot(
        part_index=0,
        media_type="image/png",
        byte_size=len(PNG),
        sha256="c" * 64,
        locator=asset_locator,
        path=tmp_path / batch_locator / asset_locator,
    )
    batch = StagedBatch(
        batch_locator=batch_locator,
        temp_root=tmp_path,
        directory=tmp_path / batch_locator,
        screenshots=(screenshot,),
        body_bytes=len(PNG),
        total_file_bytes=len(PNG),
    )
    attempt = CleanupAttempt(
        attempt_number=1,
        terminal_reason="TIMED_OUT",
        attempted_at=datetime.now(timezone.utc),
        deleted_locators=(asset_locator,),
        already_absent_locators=(),
        failed_locators=(),
        remaining_locators=(),
        error_categories=(),
        directory_removed=True,
        succeeded=True,
    )
    return batch, attempt


@pytest.mark.asyncio
async def test_wait_for_timeout_keeps_inner_partial_batch_and_cleanup_attempts(
    tmp_path: Path,
) -> None:
    batch, attempt = _evidence_batch(tmp_path)

    async def cancelled_stager() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError as exc:
            raise ScreenshotStagingCancelledError(
                "controlled cancellation",
                batch=batch,
                cleanup_attempts=(attempt,),
            ) from exc

    with pytest.raises(TimeoutError) as captured:
        await asyncio.wait_for(cancelled_stager(), timeout=0.001)

    observed_batch, observed_attempts = screenshot_batches_v3._exception_evidence(
        captured.value
    )
    assert observed_batch == batch
    assert observed_attempts == (attempt,)
    assert screenshot_batches_v3._exception_code(captured.value) == (
        "SCREENSHOT_STAGING_TIMED_OUT"
    )


@pytest.mark.asyncio
async def test_all_staging_errors_carry_partial_batch_and_ordered_cleanup_evidence(
    tmp_path: Path,
) -> None:
    content_type, body = _multipart(b"not-a-png")

    with pytest.raises(Exception) as captured:
        await stage_screenshot_multipart(content_type, _chunks(body), None, tmp_path)

    error = captured.value
    assert isinstance(error.batch, StagedBatch)
    assert error.batch.screenshots[0].part_index == 0
    assert tuple(attempt.attempt_number for attempt in error.cleanup_attempts) == (1,)
    assert error.cleanup_attempts[-1].succeeded is True
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_staging_cancellation_writes_cleanup_evidence_before_propagating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(tmp_path))
    clear_settings_cache()
    content_type, body = _multipart(PNG)
    started = asyncio.Event()

    async def interrupted_chunks():
        yield body[:-20]
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(
        stage_screenshot_multipart(
            content_type,
            interrupted_chunks(),
            None,
            tmp_path,
            screenshot_batches_v3.begin_local_screenshot_cleanup,
            screenshot_batches_v3.finish_local_screenshot_cleanup,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(ScreenshotStagingCancelledError) as captured:
        await task

    assert captured.value.cleanup_attempts[-1].terminal_reason == "CANCELLED"
    assert captured.value.cleanup_attempts[-1].succeeded is True
    assert not captured.value.batch.directory.exists()
    ledger = screenshot_batches_v3._load_recovery_ledger(
        screenshot_batches_v3._recovery_ledger_path(tmp_path.resolve())
    )
    assert ledger.batches[0].attempts == captured.value.cleanup_attempts
    assert screenshot_batches_v3.acknowledge_local_screenshot_recovery(ledger)


@pytest.mark.asyncio
async def test_permission_hardening_failure_fails_closed_before_pixels_are_staged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.trip_understanding.screenshot_batch import staging

    calls = 0

    def fail_file_acl(path: Path, *, is_directory: bool) -> None:
        nonlocal calls
        calls += 1
        if calls >= 3 and not is_directory:
            raise ScreenshotPathSecurityError("controlled ACL verification failure")

    monkeypatch.setattr(staging, "secure_owner_only", fail_file_acl)
    content_type, body = _multipart(PNG)
    with pytest.raises(ScreenshotPathSecurityError) as captured:
        await stage_screenshot_multipart(content_type, _chunks(body), None, tmp_path)

    assert captured.value.batch.screenshots[0].locator
    assert captured.value.cleanup_attempts[-1].succeeded is True
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL boundary")
@pytest.mark.asyncio
async def test_windows_owner_only_acl_is_applied_and_verified_for_live_staging(
    tmp_path: Path,
) -> None:
    content_type, body = _multipart(PNG)
    batch = await stage_screenshot_multipart(content_type, _chunks(body), None, tmp_path)

    # stage_screenshot_multipart only returns after the Windows DACL has been
    # protected and re-read as exactly one full-control ACE for the process SID.
    assert batch.directory.exists()
    assert batch.screenshots[0].path.exists()
    attempts = cleanup_staged_batch(batch, "SUCCEEDED")
    assert attempts[-1].succeeded is True


@pytest.mark.skipif(os.name != "nt", reason="Windows node-local boundary")
def test_windows_network_temp_root_is_rejected_before_recovery_io() -> None:
    with pytest.raises(ScreenshotPathSecurityError, match="node-local"):
        screenshot_batches_v3._resolved_local_temp_root(
            Path(r"\\g04-unreachable-node\private-share\screenshots")
        )


@pytest.mark.asyncio
async def test_staging_rejects_link_or_reparse_in_unresolved_root_chain(
    tmp_path: Path,
) -> None:
    target = tmp_path / "actual-parent"
    target.mkdir()
    linked_parent = tmp_path / "linked-parent"
    _create_directory_link(target, linked_parent)
    content_type, body = _multipart(PNG)
    try:
        with pytest.raises(ScreenshotPathSecurityError, match="reparse|links"):
            await stage_screenshot_multipart(
                content_type,
                _chunks(body),
                None,
                linked_parent / "pixel-root",
            )
        assert not (target / "pixel-root").exists()
    finally:
        _remove_directory_link(linked_parent)


def test_recovery_rejects_link_or_reparse_root_before_lock_or_ledger_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "actual-root"
    target.mkdir()
    linked_root = tmp_path / "linked-root"
    _create_directory_link(target, linked_root)
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(linked_root))
    clear_settings_cache()
    try:
        with pytest.raises(ScreenshotPathSecurityError, match="reparse|links"):
            screenshot_batches_v3.recover_orphaned_local_screenshot_files(
                minimum_age_seconds=0
            )
        assert not tuple(tmp_path.glob(".breezetravel-g04-recovery-*"))
    finally:
        _remove_directory_link(linked_root)


@pytest.mark.parametrize("kind", ["lock", "ledger"])
def test_recovery_never_follows_predictable_journal_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    root = tmp_path / "node-root"
    root.mkdir()
    resolved_root = root.resolve()
    unsafe_path = (
        screenshot_batches_v3._recovery_lock_path(resolved_root)
        if kind == "lock"
        else screenshot_batches_v3._recovery_ledger_path(resolved_root)
    )
    victim = tmp_path / f"{kind}-victim.txt"
    victim.write_bytes(b"must-not-change")
    try:
        unsafe_path.symlink_to(victim)
    except (NotImplementedError, OSError):
        pytest.skip("file symlinks are unavailable")
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(root))
    clear_settings_cache()

    with pytest.raises(ScreenshotPathSecurityError, match="reparse|links"):
        screenshot_batches_v3.recover_orphaned_local_screenshot_files(
            minimum_age_seconds=0
        )

    assert victim.read_bytes() == b"must-not-change"


def test_recovery_rejects_hardlinked_predictable_lock_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "node-root"
    root.mkdir()
    lock_path = screenshot_batches_v3._recovery_lock_path(root.resolve())
    victim = tmp_path / "lock-hardlink-victim.txt"
    victim.write_bytes(b"must-not-change")
    try:
        os.link(victim, lock_path)
    except OSError:
        pytest.skip("hard links are unavailable")
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(root))
    clear_settings_cache()

    with pytest.raises(ScreenshotPathSecurityError, match="unique regular file"):
        screenshot_batches_v3.recover_orphaned_local_screenshot_files(
            minimum_age_seconds=0
        )

    assert victim.read_bytes() == b"must-not-change"


def test_recovery_does_not_traverse_reparse_batch_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "node-root"
    root.mkdir()
    outside = tmp_path / "outside-batch"
    outside.mkdir()
    victim = outside / ("a" * 64)
    victim.write_bytes(b"must-not-change")
    batch_link = root / ("d" * 64)
    _create_directory_link(outside, batch_link)
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(root))
    clear_settings_cache()
    try:
        report = screenshot_batches_v3.recover_orphaned_local_screenshot_files(
            minimum_age_seconds=0
        )
        assert report.issues[-1].category == "UNSAFE_BATCH_PATH"
        assert victim.read_bytes() == b"must-not-change"
    finally:
        _remove_directory_link(batch_link)


def test_recovery_does_not_follow_reparse_asset_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "node-root"
    batch_dir = root / ("d" * 64)
    batch_dir.mkdir(parents=True)
    victim = tmp_path / "outside-private-pixels"
    victim.write_bytes(b"must-not-change")
    asset_link = batch_dir / ("a" * 64)
    try:
        asset_link.symlink_to(victim)
    except (NotImplementedError, OSError):
        pytest.skip("file symlinks are unavailable")
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(root))
    clear_settings_cache()

    report = screenshot_batches_v3.recover_orphaned_local_screenshot_files(
        minimum_age_seconds=0
    )

    assert report.issues[-1].category == "UNSAFE_ASSET_PATH"
    assert victim.read_bytes() == b"must-not-change"
    assert asset_link.is_symlink()


def test_orphan_recovery_returns_ordered_locator_attempts_for_db_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "node-root"
    batch_dir = root / ("d" * 64)
    batch_dir.mkdir(parents=True)
    first = batch_dir / ("a" * 64)
    second = batch_dir / ("b" * 64)
    first.write_bytes(PNG)
    second.write_bytes(PNG)
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(root))
    clear_settings_cache()

    report = screenshot_batches_v3.recover_orphaned_local_screenshot_files(
        minimum_age_seconds=0
    )

    assert report.summary() == {
        "directories_removed": 1,
        "files_removed": 2,
        "failures": 0,
    }
    assert report.batches[0].asset_locators == ("a" * 64, "b" * 64)
    assert report.batches[0].attempts[-1].deleted_locators == (
        "a" * 64,
        "b" * 64,
    )
    assert report.batches[0].attempts[-1].terminal_reason == "CRASH_RECOVERY"
    assert not batch_dir.exists()


def test_orphan_recovery_preserves_three_failed_attempts_for_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "node-root"
    batch_dir = root / ("d" * 64)
    batch_dir.mkdir(parents=True)
    asset = batch_dir / ("a" * 64)
    asset.write_bytes(PNG)
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(root))
    clear_settings_cache()
    original_unlink = Path.unlink

    def block_asset(path: Path, *args, **kwargs):
        if path == asset:
            raise PermissionError("controlled persistent lock")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", block_asset)
    report = screenshot_batches_v3.recover_orphaned_local_screenshot_files(
        minimum_age_seconds=0
    )

    assert report.failures == 1
    assert len(report.batches[0].attempts) == 3
    assert tuple(item.attempt_number for item in report.batches[0].attempts) == (1, 2, 3)
    assert report.batches[0].attempts[-1].remaining_locators == ("a" * 64,)
    assert asset.exists()


def test_orphan_recovery_write_ahead_ledger_survives_process_death_and_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "node-root"
    batch_dir = root / ("d" * 64)
    batch_dir.mkdir(parents=True)
    asset = batch_dir / ("a" * 64)
    asset.write_bytes(PNG)
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(root))
    clear_settings_cache()
    real_cleanup = screenshot_batches_v3.cleanup_staged_batch

    def process_death_before_first_unlink(*args, **kwargs):
        del args, kwargs
        raise KeyboardInterrupt("controlled process death")

    monkeypatch.setattr(
        screenshot_batches_v3,
        "cleanup_staged_batch",
        process_death_before_first_unlink,
    )
    with pytest.raises(KeyboardInterrupt, match="controlled process death"):
        screenshot_batches_v3.recover_orphaned_local_screenshot_files(
            minimum_age_seconds=0
        )

    ledger_files = tuple(tmp_path.glob(".breezetravel-g04-recovery-*.json"))
    assert len(ledger_files) == 1
    assert asset.exists()

    monkeypatch.setattr(
        screenshot_batches_v3,
        "cleanup_staged_batch",
        real_cleanup,
    )
    recovered = screenshot_batches_v3.recover_orphaned_local_screenshot_files(
        minimum_age_seconds=0
    )
    assert recovered.batches[0].attempts[-1].succeeded is True
    assert not batch_dir.exists()

    replayed_after_restart = (
        screenshot_batches_v3.recover_orphaned_local_screenshot_files(
            minimum_age_seconds=0
        )
    )
    assert replayed_after_restart == recovered
    assert screenshot_batches_v3.acknowledge_local_screenshot_recovery(
        replayed_after_restart
    )
    assert not ledger_files[0].exists()
    assert screenshot_batches_v3.recover_orphaned_local_screenshot_files(
        minimum_age_seconds=0
    ) == type(recovered)(batches=(), issues=(), skipped_fresh_directories=0)


def test_partial_delete_process_death_preserves_all_locators_and_marks_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "node-root"
    batch_dir = root / ("d" * 64)
    batch_dir.mkdir(parents=True)
    first = batch_dir / ("a" * 64)
    second = batch_dir / ("b" * 64)
    first.write_bytes(PNG)
    second.write_bytes(PNG)
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(root))
    clear_settings_cache()
    real_cleanup = screenshot_batches_v3.cleanup_staged_batch

    def process_death_after_first_unlink(batch: StagedBatch, terminal_reason: str):
        del terminal_reason
        batch.screenshots[0].path.unlink()
        raise KeyboardInterrupt("controlled partial-delete process death")

    monkeypatch.setattr(
        screenshot_batches_v3,
        "cleanup_staged_batch",
        process_death_after_first_unlink,
    )
    with pytest.raises(KeyboardInterrupt, match="partial-delete"):
        screenshot_batches_v3.recover_orphaned_local_screenshot_files(
            minimum_age_seconds=0
        )
    assert not first.exists()
    assert second.exists()

    monkeypatch.setattr(
        screenshot_batches_v3,
        "cleanup_staged_batch",
        real_cleanup,
    )
    recovered = screenshot_batches_v3.recover_orphaned_local_screenshot_files(
        minimum_age_seconds=0
    )
    evidence = recovered.batches[0]
    assert evidence.asset_locators == ("a" * 64, "b" * 64)
    assert evidence.attempts[-1].already_absent_locators == ("a" * 64,)
    assert evidence.attempts[-1].deleted_locators == ("b" * 64,)
    assert evidence.attempts[-1].succeeded is True


def test_pending_grace_event_cannot_be_reconciled_or_acknowledged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "node-root"
    batch_dir = root / ("d" * 64)
    batch_dir.mkdir(parents=True)
    asset = batch_dir / ("a" * 64)
    asset.write_bytes(PNG)
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(root))
    clear_settings_cache()
    real_cleanup = screenshot_batches_v3.cleanup_staged_batch

    def process_death_before_cleanup(*args, **kwargs):
        del args, kwargs
        raise KeyboardInterrupt("controlled pending event")

    monkeypatch.setattr(
        screenshot_batches_v3,
        "cleanup_staged_batch",
        process_death_before_cleanup,
    )
    with pytest.raises(KeyboardInterrupt, match="pending event"):
        screenshot_batches_v3.recover_orphaned_local_screenshot_files(
            minimum_age_seconds=0
        )

    pending = screenshot_batches_v3.recover_orphaned_local_screenshot_files(
        minimum_age_seconds=86_400
    )
    assert pending.batches[0].attempts == ()
    assert pending.skipped_fresh_directories == 1
    reconcilable = screenshot_batches_v3.reconcilable_local_screenshot_recovery(
        pending
    )
    assert reconcilable.batches == ()
    assert not screenshot_batches_v3.acknowledge_local_screenshot_recovery(pending)
    assert tuple(tmp_path.glob(".breezetravel-g04-recovery-*.json"))

    monkeypatch.setattr(
        screenshot_batches_v3,
        "cleanup_staged_batch",
        real_cleanup,
    )
    completed = screenshot_batches_v3.recover_orphaned_local_screenshot_files(
        minimum_age_seconds=0
    )
    assert completed.batches[0].attempts[-1].succeeded is True
    assert screenshot_batches_v3.acknowledge_local_screenshot_recovery(completed)


def test_completed_cleanup_is_not_reconciled_until_db_write_grace_elapses(
    tmp_path: Path,
) -> None:
    batch, attempt = _evidence_batch(tmp_path)
    report = LocalScreenshotRecoveryReport(
        batches=(
            LocalRecoveryBatchEvidence(
                batch_locator=batch.batch_locator,
                asset_locators=(batch.screenshots[0].locator,),
                attempts=(attempt,),
            ),
        ),
        issues=(),
        skipped_fresh_directories=0,
    )

    still_live = screenshot_batches_v3.reconcilable_local_screenshot_recovery(
        report,
        minimum_age_seconds=60,
        now=attempt.attempted_at + timedelta(seconds=59),
    )
    matured = screenshot_batches_v3.reconcilable_local_screenshot_recovery(
        report,
        minimum_age_seconds=60,
        now=attempt.attempted_at + timedelta(seconds=60),
    )

    assert still_live.batches == ()
    assert matured.batches == report.batches


@pytest.mark.asyncio
async def test_normal_cleanup_uses_locator_only_write_ahead_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "node-root"
    batch_dir = root / ("d" * 64)
    batch_dir.mkdir(parents=True)
    asset_locator = "a" * 64
    asset_path = batch_dir / asset_locator
    asset_path.write_bytes(PNG)
    batch = StagedBatch(
        batch_locator="d" * 64,
        temp_root=root,
        directory=batch_dir,
        screenshots=(
            StagedScreenshot(
                part_index=0,
                media_type="image/png",
                byte_size=len(PNG),
                sha256="c" * 64,
                locator=asset_locator,
                path=asset_path,
            ),
        ),
        body_bytes=len(PNG),
        total_file_bytes=len(PNG),
    )
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(root))
    clear_settings_cache()

    attempts, cleanup_error = await screenshot_batches_v3._cleanup_batch(
        batch, "SUCCEEDED"
    )
    assert cleanup_error is None
    assert attempts[-1].succeeded is True
    ledger = next(tmp_path.glob(".breezetravel-g04-recovery-*.json"))
    serialized = ledger.read_text(encoding="utf-8")
    assert str(root) not in serialized
    assert "g04-private-pixels" not in serialized
    report = screenshot_batches_v3.recover_orphaned_local_screenshot_files(
        minimum_age_seconds=0
    )
    assert report.batches[0].asset_locators == (asset_locator,)
    assert report.batches[0].attempts == attempts
    assert screenshot_batches_v3.acknowledge_local_screenshot_recovery(report)


@pytest.mark.asyncio
async def test_cleanup_journal_failure_leaves_pixels_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch, _ = _evidence_batch(tmp_path)
    batch.directory.mkdir()
    batch.screenshots[0].path.write_bytes(PNG)

    def fail_write_ahead(*args, **kwargs):
        del args, kwargs
        raise ScreenshotBatchError("controlled journal failure")

    monkeypatch.setattr(
        screenshot_batches_v3,
        "begin_local_screenshot_cleanup",
        fail_write_ahead,
    )
    attempts, cleanup_error = await screenshot_batches_v3._cleanup_batch(
        batch, "FAILED"
    )

    assert cleanup_error is not None
    assert attempts[-1].error_categories == ("CLEANUP_JOURNAL_UNAVAILABLE",)
    assert attempts[-1].succeeded is False
    assert batch.screenshots[0].path.exists()
    assert batch.directory.exists()


def test_cleanup_journal_cross_process_lock_preserves_concurrent_writers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "node-root"
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Event()
    processes = [
        context.Process(
            target=_write_pending_cleanup_journal_in_process,
            args=(
                str(root),
                batch_locator,
                asset_locator,
                ready,
                release,
            ),
        )
        for batch_locator, asset_locator in (
            ("a" * 64, "c" * 64),
            ("b" * 64, "d" * 64),
        )
    ]
    for process in processes:
        process.start()
    assert ready.get(timeout=20) is True
    assert ready.get(timeout=20) is True
    release.set()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    ledger_path = screenshot_batches_v3._recovery_ledger_path(root.resolve())
    report = screenshot_batches_v3._load_recovery_ledger(ledger_path)
    assert tuple(item.batch_locator for item in report.batches) == (
        "a" * 64,
        "b" * 64,
    )
    assert all(item.attempts == () for item in report.batches)


def test_recovery_oserror_is_sanitized_without_local_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "private-node-root"
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(root))
    clear_settings_cache()

    lock_path = screenshot_batches_v3._recovery_lock_path(root.resolve())
    real_open = os.open

    def fail_lock_open(path, *args, **kwargs):
        if Path(path) == lock_path:
            raise PermissionError(str(root))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_lock_open)
    with pytest.raises(ScreenshotBatchError) as captured:
        screenshot_batches_v3.recover_orphaned_local_screenshot_files(
            minimum_age_seconds=0
        )
    assert str(root) not in str(captured.value)
    assert captured.value.__cause__ is None


def test_orphan_recovery_rejects_malformed_ledger_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "node-root"
    batch_dir = root / ("d" * 64)
    batch_dir.mkdir(parents=True)
    (batch_dir / ("a" * 64)).write_bytes(PNG)
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(root))
    clear_settings_cache()

    def process_death_before_first_unlink(*args, **kwargs):
        del args, kwargs
        raise KeyboardInterrupt("controlled process death")

    monkeypatch.setattr(
        screenshot_batches_v3,
        "cleanup_staged_batch",
        process_death_before_first_unlink,
    )
    with pytest.raises(KeyboardInterrupt, match="controlled process death"):
        screenshot_batches_v3.recover_orphaned_local_screenshot_files(
            minimum_age_seconds=0
        )

    ledger = next(tmp_path.glob(".breezetravel-g04-recovery-*.json"))
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    payload["issues"] = ["silently-skipped-entry"]
    ledger.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ScreenshotBatchError, match="ledger is invalid"):
        screenshot_batches_v3.recover_orphaned_local_screenshot_files(
            minimum_age_seconds=0
        )
