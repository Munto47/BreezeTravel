from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.trip_understanding.models import (
    ScreenshotBatchAssetInput,
    ScreenshotBatchClaimInput,
)
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from app.trip_understanding.screenshot_batch.models import (
    CleanupAttempt,
    LocalRecoveryBatchEvidence,
    LocalRecoveryIssue,
    LocalScreenshotRecoveryReport,
)


@pytest.mark.asyncio
async def test_private_data_maintenance_uses_one_oldest_first_budget() -> None:
    repository = InMemoryTripUnderstandingRepository()
    now = datetime.now(timezone.utc)
    repository.screenshot_batches[("owner", "ref-hash")] = {
        "expires_at": now - timedelta(days=1),
        "source_document_json": "encrypted-private-document",
        "status": "READY",
    }
    repository.sources["old-source"] = object()  # type: ignore[assignment]
    repository.source_expiries["old-source"] = now - timedelta(days=31)

    first = await repository.purge_expired_private_data(now=now, limit=1)
    second = await repository.purge_expired_private_data(now=now, limit=1)

    assert first == {"sources_purged": 1, "batches_purged": 0}
    assert second == {"sources_purged": 0, "batches_purged": 1}
    assert repository.screenshot_batches[("owner", "ref-hash")][
        "source_document_json"
    ] is None
    assert [
        receipt["terminal_reason"]
        for receipt in repository.screenshot_cleanup_receipts
    ] == ["SOURCE_TTL_EXPIRED", "BATCH_TTL_EXPIRED"]


@pytest.mark.asyncio
async def test_account_deletion_physically_purges_unconsumed_screenshot_document() -> None:
    repository = InMemoryTripUnderstandingRepository()
    now = datetime.now(timezone.utc)
    repository.screenshot_batches[("owner", "ref-hash")] = {
        "expires_at": now + timedelta(minutes=15),
        "source_document_json": "encrypted-private-document",
        "status": "READY",
        "consumed_understanding_id": None,
    }

    outcome = await repository.delete_account_travel_data(
        user_id="owner",
        idempotency_key="delete-private-data",
        request_hash="f" * 64,
        now=now,
    )

    row = repository.screenshot_batches[("owner", "ref-hash")]
    assert outcome.view.status == "COMPLETED"
    assert row["source_document_json"] is None
    assert row["status"] == "EXPIRED"
    assert row["last_error_category"] == "ACCOUNT_DATA_DELETED"
    assert repository.screenshot_cleanup_receipts[-1]["terminal_reason"] == (
        "ACCOUNT_DATA_DELETED"
    )


@pytest.mark.asyncio
async def test_account_deletion_requires_confirmed_original_cleanup_before_completion() -> None:
    repository = InMemoryTripUnderstandingRepository()
    now = datetime.now(timezone.utc)
    locator = "a" * 64
    claim = ScreenshotBatchClaimInput(
        batch_ref="S" * 43,
        owner_user_id="owner",
        idempotency_key="upload-before-delete",
        request_hash="f" * 64,
        expires_at=now + timedelta(minutes=15),
        assets=(
            ScreenshotBatchAssetInput(
                upload_position=0,
                content_hash="b" * 64,
                media_type="image/png",
                byte_size=100,
                storage_locator=locator,
                ocr_status="PENDING",
            ),
        ),
    )
    assert await repository.claim_screenshot_batch(claim, now=now) is None

    retry = await repository.delete_account_travel_data(
        user_id="owner",
        idempotency_key="delete-while-pixels-remain",
        request_hash="e" * 64,
        now=now,
    )

    row = repository.screenshot_batches[
        ("owner", hashlib.sha256(("S" * 43).encode("utf-8")).hexdigest())
    ]
    assert retry.view.status == "RETRY_REQUIRED"
    assert retry.view.next_action == "RETRY"
    assert row["status"] == "PROCESSING"
    assert row["asset_cleanup_statuses"] == {0: "PENDING"}

    report = LocalScreenshotRecoveryReport(
        batches=(
            LocalRecoveryBatchEvidence(
                batch_locator="c" * 64,
                asset_locators=(locator,),
                attempts=(
                    CleanupAttempt(
                        attempt_number=1,
                        terminal_reason="CRASH_RECOVERY",
                        attempted_at=now,
                        deleted_locators=(locator,),
                        already_absent_locators=(),
                        failed_locators=(),
                        remaining_locators=(),
                        error_categories=(),
                        directory_removed=True,
                        succeeded=True,
                    ),
                ),
            ),
        ),
        issues=(),
        skipped_fresh_directories=0,
    )
    await repository.reconcile_local_screenshot_recovery(report, now=now)

    completed = await repository.delete_account_travel_data(
        user_id="owner",
        idempotency_key="delete-after-pixels-removed",
        request_hash="e" * 64,
        now=now,
    )
    assert completed.view.status == "COMPLETED"
    assert row["status"] == "EXPIRED"


@pytest.mark.asyncio
async def test_crash_recovery_evidence_is_reconciled_to_the_claimed_asset() -> None:
    repository = InMemoryTripUnderstandingRepository()
    now = datetime.now(timezone.utc)
    locator = "a" * 64
    claim = ScreenshotBatchClaimInput(
        batch_ref="S" * 43,
        owner_user_id="owner",
        idempotency_key="crashed-upload",
        request_hash="f" * 64,
        expires_at=now + timedelta(minutes=15),
        assets=(
            ScreenshotBatchAssetInput(
                upload_position=0,
                content_hash="b" * 64,
                media_type="image/png",
                byte_size=100,
                storage_locator=locator,
                ocr_status="PENDING",
            ),
        ),
    )
    assert await repository.claim_screenshot_batch(claim, now=now) is None
    report = LocalScreenshotRecoveryReport(
        batches=(
            LocalRecoveryBatchEvidence(
                batch_locator="c" * 64,
                asset_locators=(locator,),
                attempts=(
                    CleanupAttempt(
                        attempt_number=1,
                        terminal_reason="CRASH_RECOVERY",
                        attempted_at=now,
                        deleted_locators=(locator,),
                        already_absent_locators=(),
                        failed_locators=(),
                        remaining_locators=(),
                        error_categories=(),
                        directory_removed=True,
                        succeeded=True,
                    ),
                ),
            ),
        ),
        issues=(),
        skipped_fresh_directories=0,
    )

    result = await repository.reconcile_local_screenshot_recovery(report, now=now)

    row = next(iter(repository.screenshot_batches.values()))
    assert result == {
        "matched_assets": 1,
        "receipts_recorded": 1,
        "orphan_receipts": 0,
        "batches_finalized": 1,
        "unmatched_assets": 0,
        "local_issues": 0,
    }
    assert row["asset_cleanup_statuses"] == {0: "CLEANED"}
    assert row["status"] == "FAILED"
    assert row["last_error_category"] == "CRASH_RECOVERED_NO_RESULT"
    assert repository.screenshot_cleanup_receipts[-1]["terminal_reason"] == (
        "CRASH_RECOVERY"
    )


@pytest.mark.asyncio
async def test_preclaim_crash_recovery_records_hashed_orphan_evidence_once() -> None:
    repository = InMemoryTripUnderstandingRepository()
    now = datetime.now(timezone.utc)
    batch_locator = "d" * 64
    asset_locator = "e" * 64
    report = LocalScreenshotRecoveryReport(
        batches=(
            LocalRecoveryBatchEvidence(
                batch_locator=batch_locator,
                asset_locators=(asset_locator,),
                attempts=(
                    CleanupAttempt(
                        attempt_number=1,
                        terminal_reason="CRASH_RECOVERY",
                        attempted_at=now,
                        deleted_locators=(asset_locator,),
                        already_absent_locators=(),
                        failed_locators=(),
                        remaining_locators=(),
                        error_categories=(),
                        directory_removed=True,
                        succeeded=True,
                    ),
                ),
            ),
        ),
        issues=(
            LocalRecoveryIssue(
                batch_locator="f" * 64,
                category="UNSAFE_ASSET_PATH",
                observed_at=now,
            ),
        ),
        skipped_fresh_directories=0,
    )

    first = await repository.reconcile_local_screenshot_recovery(report, now=now)
    second = await repository.reconcile_local_screenshot_recovery(report, now=now)

    assert first == {
        "matched_assets": 0,
        "receipts_recorded": 2,
        "orphan_receipts": 2,
        "batches_finalized": 0,
        "unmatched_assets": 1,
        "local_issues": 1,
    }
    assert second["receipts_recorded"] == 0
    assert second["orphan_receipts"] == 0
    assert len(repository.screenshot_cleanup_receipts) == 2
    serialized = repr(repository.screenshot_cleanup_receipts)
    assert batch_locator not in serialized
    assert asset_locator not in serialized
    assert all(
        len(receipt["recovery_event_hash"]) == 64
        for receipt in repository.screenshot_cleanup_receipts
    )


@pytest.mark.asyncio
async def test_directory_cleanup_failure_forces_claimed_batch_privacy_blocked() -> None:
    repository = InMemoryTripUnderstandingRepository()
    now = datetime.now(timezone.utc)
    locator = "a" * 64
    claim = ScreenshotBatchClaimInput(
        batch_ref="S" * 43,
        owner_user_id="owner",
        idempotency_key="directory-failed-upload",
        request_hash="f" * 64,
        expires_at=now + timedelta(minutes=15),
        assets=(
            ScreenshotBatchAssetInput(
                upload_position=0,
                content_hash="b" * 64,
                media_type="image/png",
                byte_size=100,
                storage_locator=locator,
                ocr_status="PENDING",
            ),
        ),
    )
    assert await repository.claim_screenshot_batch(claim, now=now) is None
    report = LocalScreenshotRecoveryReport(
        batches=(
            LocalRecoveryBatchEvidence(
                batch_locator="c" * 64,
                asset_locators=(locator,),
                attempts=(
                    CleanupAttempt(
                        attempt_number=3,
                        terminal_reason="CRASH_RECOVERY",
                        attempted_at=now,
                        deleted_locators=(),
                        already_absent_locators=(locator,),
                        failed_locators=(),
                        remaining_locators=(),
                        error_categories=("DIRECTORY_PERMISSIONERROR",),
                        directory_removed=False,
                        succeeded=False,
                    ),
                ),
            ),
        ),
        issues=(),
        skipped_fresh_directories=0,
    )

    result = await repository.reconcile_local_screenshot_recovery(report, now=now)

    row = next(iter(repository.screenshot_batches.values()))
    assert result["batches_finalized"] == 1
    assert row["asset_cleanup_statuses"] == {0: "CLEANED"}
    assert row["status"] == "PRIVACY_BLOCKED"
    assert row["last_error_category"] == "SCREENSHOT_CLEANUP_FAILED"
    assert repository.screenshot_cleanup_receipts[-1]["cleanup_status"] == (
        "ALREADY_ABSENT"
    )


@pytest.mark.asyncio
async def test_expired_processing_batch_without_cleanup_confirmation_is_blocked() -> None:
    repository = InMemoryTripUnderstandingRepository()
    now = datetime.now(timezone.utc)
    repository.screenshot_batches[("owner", "ref-hash")] = {
        "batch_id": "batch-processing",
        "expires_at": now - timedelta(seconds=1),
        "source_document_json": None,
        "source_document_hash": None,
        "semantic_text_hash": None,
        "status": "PROCESSING",
        "asset_cleanup_statuses": {0: "PENDING"},
    }

    outcome = await repository.purge_expired_private_data(now=now, limit=1)

    row = repository.screenshot_batches[("owner", "ref-hash")]
    assert outcome == {"sources_purged": 0, "batches_purged": 1}
    assert row["status"] == "PRIVACY_BLOCKED"
    assert row["document_purged_at"] == now
    assert row["last_error_category"] == (
        "SCREENSHOT_CLEANUP_NOT_CONFIRMED_AT_TTL"
    )
    receipt = repository.screenshot_cleanup_receipts[-1]
    assert receipt["terminal_reason"] == "BATCH_TTL_EXPIRED"
    assert receipt["cleanup_status"] == "DELETE_FAILED"
    assert receipt["error_category"] == (
        "SCREENSHOT_CLEANUP_NOT_CONFIRMED_AT_TTL"
    )
