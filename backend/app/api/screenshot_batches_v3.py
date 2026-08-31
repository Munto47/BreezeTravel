from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import stat as stat_module
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.config import get_settings
from app.trip_understanding.errors import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from app.trip_understanding.models import (
    ScreenshotBatchAcceptedView,
    ScreenshotBatchAssetInput,
    ScreenshotBatchClaimInput,
    ScreenshotBatchFailurePersistenceInput,
    ScreenshotBatchPersistenceInput,
    ScreenshotCleanupPersistenceInput,
    ScreenshotCleanupReceiptInput,
)
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.repository import (
    PostgresTripUnderstandingRepository,
    TripUnderstandingRepository,
)
from app.trip_understanding.screenshot_batch import (
    MultipartRequiredError,
    ScreenshotBatchError,
    ScreenshotBatchTooLargeError,
    ScreenshotCleanupError,
    ScreenshotFileTooLargeError,
    ScreenshotStagingCancelledError,
    ScreenshotStagingTimeoutError,
    StagedBatch,
    StagedScreenshot,
    cleanup_staged_batch,
    stage_screenshot_multipart,
)
from app.trip_understanding.screenshot_batch.models import (
    CleanupAttempt,
    LocalRecoveryBatchEvidence,
    LocalRecoveryIssue,
    LocalScreenshotRecoveryReport,
)
from app.trip_understanding.screenshot_batch.security import (
    require_node_local,
    secure_owner_only,
)
from app.trip_understanding.screenshot_batch.staging import (
    secure_and_validate_open_regular_file,
    stat_is_link_or_reparse,
    validate_no_link_or_reparse_components,
)
from app.trip_understanding.screenshot_ocr import (
    PaddleOcrAdapter,
    RawOcrLine,
    ScreenshotOcrAllFailedError,
    ScreenshotOcrEngineBindingV1,
    ScreenshotOcrRunSpecV1,
    ScreenshotOcrTimeoutError,
    StagedScreenshotAsset,
    extract_screenshot_document,
)
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.utils.auth import get_current_user


router = APIRouter(prefix="/v3/screenshot-batches")


def get_screenshot_batch_repository() -> TripUnderstandingRepository:
    return PostgresTripUnderstandingRepository()


RepositoryDep = Annotated[
    TripUnderstandingRepository,
    Depends(get_screenshot_batch_repository),
]
CurrentUserDep = Annotated[str, Depends(get_current_user)]


class _FixtureScreenshotOcrEngine:
    """Explicit CI fixture. It is never selected by the production default."""

    name = "g04-ci-fixture"
    version = "1"

    @property
    def binding(self) -> ScreenshotOcrEngineBindingV1:
        return ScreenshotOcrEngineBindingV1.create(
            engine=self.name,
            engine_version=self.version,
            configuration={"evidence_tier": "AUTOMATED_FIXTURE"},
        )

    async def recognize(self, image_path: Path) -> tuple[RawOcrLine, ...]:
        del image_path
        return (
            RawOcrLine(
                text="北京三日行程",
                confidence=0.99,
                bbox=((20, 20), (500, 20), (500, 90), (20, 90)),
            ),
            RawOcrLine(
                text="Day 1 故宫博物院 景山公园",
                confidence=0.99,
                bbox=((20, 120), (900, 120), (900, 190), (20, 190)),
            ),
            RawOcrLine(
                text="Day 2 天坛公园 前门大街",
                confidence=0.99,
                bbox=((20, 220), (900, 220), (900, 290), (20, 290)),
            ),
            RawOcrLine(
                text="Day 3 颐和园 圆明园",
                confidence=0.99,
                bbox=((20, 320), (900, 320), (900, 390), (20, 390)),
            ),
        )


@lru_cache(maxsize=8)
def _configured_ocr_engine(
    mode: str,
    detection_model: str = "PP-OCRv5_mobile_det",
    recognition_model: str = "PP-OCRv5_mobile_rec",
    detection_model_dir: str = "",
    recognition_model_dir: str = "",
    device: str = "",
):
    if mode == "fixture":
        return _FixtureScreenshotOcrEngine()
    options = {
        "lang": None,
        "text_detection_model_name": detection_model,
        "text_recognition_model_name": recognition_model,
        "enable_mkldnn": False,
    }
    if device.strip():
        options["device"] = device.strip()
    if detection_model_dir.strip():
        options["text_detection_model_dir"] = detection_model_dir.strip()
    if recognition_model_dir.strip():
        options["text_recognition_model_dir"] = recognition_model_dir.strip()
    return PaddleOcrAdapter(options=options)


def get_screenshot_ocr_engine():
    settings = get_settings()
    return _configured_ocr_engine(
        settings.screenshot_ocr_mode,
        settings.screenshot_ocr_detection_model,
        settings.screenshot_ocr_recognition_model,
        settings.screenshot_ocr_detection_model_dir,
        settings.screenshot_ocr_recognition_model_dir,
        settings.screenshot_ocr_device,
    )


OcrEngineDep = Annotated[Any, Depends(get_screenshot_ocr_engine)]


def _require_idempotency_key(raw: str | None) -> str:
    if raw is None or not raw.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "请重新选择截图"},
        )
    value = raw.strip()
    if len(value) > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_IDEMPOTENCY_KEY", "message": "请求标识无效，请重试"},
        )
    return value


def _batch_ref(owner_user_id: str, idempotency_key: str) -> str:
    """Derive a 32-byte pseudorandom ref while persisting only its SHA-256.

    Stable derivation is required so an idempotent replay can return the original
    opaque reference without storing recoverable reference bytes in PostgreSQL.
    """

    settings = get_settings()
    secret = (
        settings.trip_understanding_source_encryption_key
        or settings.trip_understanding_cookie_signing_key
        or settings.jwt_secret_key
    )
    digest = hmac.new(
        secret.encode("utf-8"),
        f"g04-screenshot-batch\0{owner_user_id}\0{idempotency_key}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _temp_root() -> Path:
    configured = get_settings().screenshot_batch_temp_root.strip()
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "breezetravel-g04-screenshots"


_LOCAL_BATCH_DIRECTORY = re.compile(r"^[0-9a-f]{64}$")
_LOCAL_RECOVERY_LEDGER_VERSION = 1
_LOCAL_RECOVERY_LEDGER_MAX_BYTES = 1_048_576
_LOCAL_RECOVERY_THREAD_LOCK = threading.RLock()


def _recovery_ledger_path(root: Path) -> Path:
    """Keep the no-pixel write-ahead ledger outside the raw-image directory."""

    root_binding = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:24]
    return root.parent / f".breezetravel-g04-recovery-{root_binding}.json"


def _recovery_lock_path(root: Path) -> Path:
    root_binding = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:24]
    return root.parent / f".breezetravel-g04-recovery-{root_binding}.lock"


def _resolved_local_temp_root(path: Path) -> Path:
    try:
        expanded = path.expanduser()
        require_node_local(expanded)
        expanded = validate_no_link_or_reparse_components(expanded)
        resolved = expanded.resolve(strict=False)
        require_node_local(resolved)
        validate_no_link_or_reparse_components(resolved)
        return resolved
    except ScreenshotBatchError:
        raise
    except OSError:
        raise ScreenshotBatchError("local screenshot cleanup journal I/O failed") from None


def _recovery_file_open_flags(flags: int) -> int:
    return (
        flags
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOINHERIT", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_existing_recovery_file(path: Path, flags: int) -> tuple[int, os.stat_result]:
    """Open an existing journal file without accepting a path substitution."""

    validate_no_link_or_reparse_components(path)
    try:
        path_before = path.lstat()
    except FileNotFoundError:
        raise
    except OSError:
        raise ScreenshotBatchError(
            "local screenshot cleanup journal I/O failed"
        ) from None
    if stat_is_link_or_reparse(path_before) or not stat_module.S_ISREG(
        path_before.st_mode
    ):
        raise ScreenshotBatchError("local screenshot recovery file is unsafe")
    try:
        descriptor = os.open(path, _recovery_file_open_flags(flags))
    except FileNotFoundError:
        raise
    except OSError:
        raise ScreenshotBatchError(
            "local screenshot cleanup journal I/O failed"
        ) from None
    try:
        descriptor_stat = secure_and_validate_open_regular_file(path, descriptor)
        if not os.path.samestat(path_before, descriptor_stat):
            raise ScreenshotBatchError(
                "local screenshot recovery file changed while it was opened"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, descriptor_stat


def _open_or_create_recovery_lock(path: Path) -> int:
    """Create once with O_EXCL, or securely bind an existing lock inode."""

    validate_no_link_or_reparse_components(path.parent)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ScreenshotBatchError(
            "local screenshot cleanup journal I/O failed"
        ) from None
    validate_no_link_or_reparse_components(path.parent)
    create_flags = _recovery_file_open_flags(
        os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL
    )
    try:
        descriptor = os.open(path, create_flags, 0o600)
    except FileExistsError:
        descriptor, _ = _open_existing_recovery_file(
            path, os.O_RDWR | os.O_APPEND
        )
        return descriptor
    except OSError:
        raise ScreenshotBatchError(
            "local screenshot cleanup journal I/O failed"
        ) from None
    try:
        secure_and_validate_open_regular_file(path, descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _verify_or_absent_recovery_file(path: Path) -> None:
    try:
        descriptor, _ = _open_existing_recovery_file(path, os.O_RDONLY)
    except FileNotFoundError:
        return
    else:
        os.close(descriptor)


def _unlink_verified_recovery_file(path: Path) -> None:
    descriptor, descriptor_stat = _open_existing_recovery_file(path, os.O_RDONLY)
    os.close(descriptor)
    try:
        path_stat = path.lstat()
        if (
            stat_is_link_or_reparse(path_stat)
            or not os.path.samestat(descriptor_stat, path_stat)
        ):
            raise ScreenshotBatchError(
                "local screenshot recovery file changed before deletion"
            )
        path.unlink()
    except ScreenshotBatchError:
        raise
    except OSError:
        raise ScreenshotBatchError(
            "local screenshot cleanup journal I/O failed"
        ) from None


@contextmanager
def _local_recovery_lock(root: Path):
    """Serialize journal read/modify/write across workers on this node."""

    if os.name == "nt" and str(root).startswith("\\\\"):
        raise ScreenshotBatchError("screenshot temporary root must be node-local")
    lock_path = _recovery_lock_path(root)
    try:
        with _LOCAL_RECOVERY_THREAD_LOCK:
            descriptor = _open_or_create_recovery_lock(lock_path)
            with os.fdopen(descriptor, "a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                    os.fsync(handle.fileno())
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except ScreenshotBatchError:
        raise
    except OSError:
        raise ScreenshotBatchError("local screenshot cleanup journal I/O failed") from None


def _recovery_report_payload(report: LocalScreenshotRecoveryReport) -> dict[str, Any]:
    return {
        "version": _LOCAL_RECOVERY_LEDGER_VERSION,
        "batches": [
            {
                "batch_locator": batch.batch_locator,
                "asset_locators": list(batch.asset_locators),
                "attempts": [
                    {
                        "attempt_number": attempt.attempt_number,
                        "terminal_reason": attempt.terminal_reason,
                        "attempted_at": attempt.attempted_at.isoformat(),
                        "deleted_locators": list(attempt.deleted_locators),
                        "already_absent_locators": list(
                            attempt.already_absent_locators
                        ),
                        "failed_locators": list(attempt.failed_locators),
                        "remaining_locators": list(attempt.remaining_locators),
                        "error_categories": list(attempt.error_categories),
                        "directory_removed": attempt.directory_removed,
                        "succeeded": attempt.succeeded,
                    }
                    for attempt in batch.attempts
                ],
            }
            for batch in report.batches
        ],
        "issues": [
            {
                "batch_locator": issue.batch_locator,
                "category": issue.category,
                "observed_at": issue.observed_at.isoformat(),
            }
            for issue in report.issues
        ],
        "skipped_fresh_directories": report.skipped_fresh_directories,
    }


def _parse_ledger_timestamp(raw: object) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("recovery ledger timestamp is invalid")
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        raise ValueError("recovery ledger timestamp must include a timezone")
    return value


def _parse_ledger_locator(raw: object, *, allow_marker: bool = False) -> str:
    if not isinstance(raw, str):
        raise ValueError("recovery ledger locator is invalid")
    if allow_marker and raw in {"ROOT", "LEDGER"}:
        return raw
    if not _LOCAL_BATCH_DIRECTORY.fullmatch(raw):
        raise ValueError("recovery ledger locator is invalid")
    return raw


def _parse_ledger_list(raw: object, *, field: str) -> list[object]:
    if not isinstance(raw, list):
        raise ValueError(f"recovery ledger {field} is invalid")
    return raw


def _parse_ledger_code(raw: object, *, field: str) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > 128:
        raise ValueError(f"recovery ledger {field} is invalid")
    if not re.fullmatch(r"[A-Z0-9_]+", raw):
        raise ValueError(f"recovery ledger {field} is invalid")
    return raw


def _parse_ledger_bool(raw: object, *, field: str) -> bool:
    if type(raw) is not bool:
        raise ValueError(f"recovery ledger {field} is invalid")
    return raw


def _load_recovery_ledger(path: Path) -> LocalScreenshotRecoveryReport:
    try:
        try:
            descriptor, descriptor_stat = _open_existing_recovery_file(
                path, os.O_RDONLY
            )
        except FileNotFoundError:
            return LocalScreenshotRecoveryReport(
                batches=(), issues=(), skipped_fresh_directories=0
            )
        if descriptor_stat.st_size > _LOCAL_RECOVERY_LEDGER_MAX_BYTES:
            os.close(descriptor)
            raise ScreenshotBatchError("local screenshot recovery ledger is too large")
        with os.fdopen(descriptor, "rb") as handle:
            serialized = handle.read(_LOCAL_RECOVERY_LEDGER_MAX_BYTES + 1)
        if len(serialized) > _LOCAL_RECOVERY_LEDGER_MAX_BYTES:
            raise ScreenshotBatchError("local screenshot recovery ledger is too large")
        payload = json.loads(serialized.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("recovery ledger version is invalid")
        batches = []
        for raw_batch in _parse_ledger_list(
            payload.get("batches", []), field="batches"
        ):
            if not isinstance(raw_batch, dict):
                raise ValueError("recovery ledger batch is invalid")
            attempts = []
            for raw_attempt in _parse_ledger_list(
                raw_batch.get("attempts", []), field="attempts"
            ):
                if not isinstance(raw_attempt, dict):
                    raise ValueError("recovery ledger attempt is invalid")
                attempt_number = raw_attempt["attempt_number"]
                if type(attempt_number) is not int or not 1 <= attempt_number <= 3:
                    raise ValueError("recovery ledger attempt number is invalid")
                attempts.append(
                    CleanupAttempt(
                        attempt_number=attempt_number,
                        terminal_reason=_parse_ledger_code(
                            raw_attempt["terminal_reason"], field="terminal reason"
                        ),
                        attempted_at=_parse_ledger_timestamp(
                            raw_attempt["attempted_at"]
                        ),
                        deleted_locators=tuple(
                            _parse_ledger_locator(item)
                            for item in _parse_ledger_list(
                                raw_attempt["deleted_locators"],
                                field="deleted locators",
                            )
                        ),
                        already_absent_locators=tuple(
                            _parse_ledger_locator(item)
                            for item in _parse_ledger_list(
                                raw_attempt["already_absent_locators"],
                                field="already absent locators",
                            )
                        ),
                        failed_locators=tuple(
                            _parse_ledger_locator(item)
                            for item in _parse_ledger_list(
                                raw_attempt["failed_locators"],
                                field="failed locators",
                            )
                        ),
                        remaining_locators=tuple(
                            _parse_ledger_locator(item)
                            for item in _parse_ledger_list(
                                raw_attempt["remaining_locators"],
                                field="remaining locators",
                            )
                        ),
                        error_categories=tuple(
                            _parse_ledger_code(item, field="error category")
                            for item in _parse_ledger_list(
                                raw_attempt["error_categories"],
                                field="error categories",
                            )
                        ),
                        directory_removed=_parse_ledger_bool(
                            raw_attempt["directory_removed"],
                            field="directory removed",
                        ),
                        succeeded=_parse_ledger_bool(
                            raw_attempt["succeeded"], field="succeeded"
                        ),
                    )
                )
            batches.append(
                LocalRecoveryBatchEvidence(
                    batch_locator=_parse_ledger_locator(
                        raw_batch["batch_locator"]
                    ),
                    asset_locators=tuple(
                        _parse_ledger_locator(item)
                        for item in _parse_ledger_list(
                            raw_batch["asset_locators"], field="asset locators"
                        )
                    ),
                    attempts=tuple(attempts),
                )
            )
        issues_list = []
        for raw_issue in _parse_ledger_list(
            payload.get("issues", []), field="issues"
        ):
            if not isinstance(raw_issue, dict):
                raise ValueError("recovery ledger issue is invalid")
            issues_list.append(
                LocalRecoveryIssue(
                    batch_locator=_parse_ledger_locator(
                        raw_issue["batch_locator"], allow_marker=True
                    ),
                    category=_parse_ledger_code(
                        raw_issue["category"], field="issue category"
                    ),
                    observed_at=_parse_ledger_timestamp(raw_issue["observed_at"]),
                )
            )
        issues = tuple(issues_list)
        skipped = payload.get("skipped_fresh_directories", 0)
        if type(skipped) is not int or skipped < 0:
            raise ValueError("recovery ledger skipped count is invalid")
    except OSError:
        raise ScreenshotBatchError("local screenshot cleanup journal I/O failed") from None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ScreenshotBatchError("local screenshot recovery ledger is invalid") from exc
    return LocalScreenshotRecoveryReport(
        batches=tuple(batches),
        issues=issues,
        skipped_fresh_directories=skipped,
    )


def _write_recovery_ledger(
    path: Path,
    report: LocalScreenshotRecoveryReport,
) -> None:
    payload = json.dumps(
        _recovery_report_payload(report),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > _LOCAL_RECOVERY_LEDGER_MAX_BYTES:
        raise ScreenshotBatchError("local screenshot recovery ledger is too large")
    try:
        validate_no_link_or_reparse_components(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        validate_no_link_or_reparse_components(path.parent)
        _verify_or_absent_recovery_file(path)
        descriptor, raw_temp_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
    except OSError:
        raise ScreenshotBatchError("local screenshot cleanup journal I/O failed") from None
    temp_path = Path(raw_temp_path)
    try:
        secure_and_validate_open_regular_file(temp_path, descriptor)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        descriptor = -1
        _verify_or_absent_recovery_file(path)
        os.replace(temp_path, path)
        final_descriptor, final_stat = _open_existing_recovery_file(
            path, os.O_RDONLY
        )
        os.close(final_descriptor)
        if final_stat.st_size != len(payload):
            raise ScreenshotBatchError(
                "local screenshot recovery ledger changed after replacement"
            )
    except (OSError, ScreenshotBatchError) as exc:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, ScreenshotBatchError):
            raise
        raise ScreenshotBatchError("local screenshot cleanup journal I/O failed") from None


def _merge_recovery_reports(
    previous: LocalScreenshotRecoveryReport,
    *,
    batches: tuple[LocalRecoveryBatchEvidence, ...] = (),
    issues: tuple[LocalRecoveryIssue, ...] = (),
    skipped_fresh_directories: int | None = None,
) -> LocalScreenshotRecoveryReport:
    by_batch = {item.batch_locator: item for item in previous.batches}
    for item in batches:
        prior = by_batch.get(item.batch_locator)
        if prior is None:
            by_batch[item.batch_locator] = item
            continue
        by_batch[item.batch_locator] = LocalRecoveryBatchEvidence(
            batch_locator=item.batch_locator,
            asset_locators=tuple(sorted(set(prior.asset_locators) | set(item.asset_locators))),
            attempts=item.attempts or prior.attempts,
        )
    merged_issues = {
        (item.batch_locator, item.category, item.observed_at.isoformat()): item
        for item in previous.issues
    }
    merged_issues.update(
        {
            (item.batch_locator, item.category, item.observed_at.isoformat()): item
            for item in issues
        }
    )
    return LocalScreenshotRecoveryReport(
        batches=tuple(by_batch[key] for key in sorted(by_batch)),
        issues=tuple(merged_issues[key] for key in sorted(merged_issues)),
        skipped_fresh_directories=(
            previous.skipped_fresh_directories
            if skipped_fresh_directories is None
            else skipped_fresh_directories
        ),
    )


def reconcilable_local_screenshot_recovery(
    report: LocalScreenshotRecoveryReport,
    *,
    minimum_age_seconds: float = 0,
    now: datetime | None = None,
) -> LocalScreenshotRecoveryReport:
    """Exclude pending/live events until their database write grace has elapsed."""

    if minimum_age_seconds < 0:
        raise ValueError("minimum_age_seconds must not be negative")
    observed_at = now or datetime.now(timezone.utc)

    def is_mature(item: LocalRecoveryBatchEvidence) -> bool:
        if not item.attempts:
            return False
        latest_attempt = max(attempt.attempted_at for attempt in item.attempts)
        return (observed_at - latest_attempt).total_seconds() >= minimum_age_seconds

    return LocalScreenshotRecoveryReport(
        batches=tuple(item for item in report.batches if is_mature(item)),
        issues=report.issues,
        skipped_fresh_directories=report.skipped_fresh_directories,
    )


def acknowledge_local_screenshot_recovery(
    report: LocalScreenshotRecoveryReport,
) -> bool:
    """Remove only exact committed events while preserving concurrent additions."""

    if any(not item.attempts for item in report.batches):
        return False
    root = _resolved_local_temp_root(_temp_root())
    path = _recovery_ledger_path(root)
    with _local_recovery_lock(root):
        current = _load_recovery_ledger(path)
        current_batches = {item.batch_locator: item for item in current.batches}
        for expected in report.batches:
            if current_batches.get(expected.batch_locator) != expected:
                return False
            del current_batches[expected.batch_locator]
        current_issues = list(current.issues)
        for expected in report.issues:
            try:
                current_issues.remove(expected)
            except ValueError:
                return False
        remaining = LocalScreenshotRecoveryReport(
            batches=tuple(current_batches[key] for key in sorted(current_batches)),
            issues=tuple(current_issues),
            skipped_fresh_directories=current.skipped_fresh_directories,
        )
        try:
            if remaining.batches or remaining.issues:
                _write_recovery_ledger(path, remaining)
            else:
                _unlink_verified_recovery_file(path)
        except FileNotFoundError:
            return False
        except OSError:
            raise ScreenshotBatchError("local screenshot cleanup journal I/O failed") from None
    return True


def _pending_cleanup_evidence(batch: StagedBatch) -> LocalRecoveryBatchEvidence:
    return LocalRecoveryBatchEvidence(
        batch_locator=batch.batch_locator,
        asset_locators=tuple(sorted(item.locator for item in batch.screenshots)),
        attempts=(),
    )


def begin_local_screenshot_cleanup(batch: StagedBatch, terminal_reason: str) -> None:
    del terminal_reason
    root = _resolved_local_temp_root(batch.temp_root)
    path = _recovery_ledger_path(root)
    with _local_recovery_lock(root):
        current = _load_recovery_ledger(path)
        updated = _merge_recovery_reports(
            current,
            batches=(_pending_cleanup_evidence(batch),),
        )
        _write_recovery_ledger(path, updated)


def finish_local_screenshot_cleanup(
    batch: StagedBatch,
    attempts: tuple[CleanupAttempt, ...],
) -> None:
    root = _resolved_local_temp_root(batch.temp_root)
    path = _recovery_ledger_path(root)
    completed = LocalRecoveryBatchEvidence(
        batch_locator=batch.batch_locator,
        asset_locators=tuple(sorted(item.locator for item in batch.screenshots)),
        attempts=attempts,
    )
    with _local_recovery_lock(root):
        current = _load_recovery_ledger(path)
        updated = _merge_recovery_reports(current, batches=(completed,))
        _write_recovery_ledger(path, updated)


def acknowledge_local_screenshot_cleanup(
    batch: StagedBatch,
    attempts: tuple[CleanupAttempt, ...],
) -> bool:
    report = LocalScreenshotRecoveryReport(
        batches=(
            LocalRecoveryBatchEvidence(
                batch_locator=batch.batch_locator,
                asset_locators=tuple(sorted(item.locator for item in batch.screenshots)),
                attempts=attempts,
            ),
        ),
        issues=(),
        skipped_fresh_directories=0,
    )
    return acknowledge_local_screenshot_recovery(report)


def _recover_orphaned_local_screenshot_files_locked(
    *,
    root: Path,
    minimum_age_seconds: float,
    now_timestamp: float | None = None,
) -> LocalScreenshotRecoveryReport:
    """Delete only stale, random-named children of this node's G04 temp root.

    The recovery boundary deliberately does not follow links and never scans outside
    the configured root. It is used at API startup and periodically for process-crash
    recovery; normal request terminal paths retain their database cleanup receipts.
    """

    if minimum_age_seconds < 0:
        raise ValueError("minimum_age_seconds must not be negative")
    root = validate_no_link_or_reparse_components(root)
    ledger_path = _recovery_ledger_path(root)
    report = _load_recovery_ledger(ledger_path)
    observed_at = datetime.now(timezone.utc)

    resumed_batches = []
    for prior in report.batches:
        if prior.attempts or os.path.lexists(root / prior.batch_locator):
            continue
        resumed_batches.append(
            LocalRecoveryBatchEvidence(
                batch_locator=prior.batch_locator,
                asset_locators=prior.asset_locators,
                attempts=(
                    CleanupAttempt(
                        attempt_number=1,
                        terminal_reason="CRASH_RECOVERY",
                        attempted_at=observed_at,
                        deleted_locators=(),
                        already_absent_locators=prior.asset_locators,
                        failed_locators=(),
                        remaining_locators=(),
                        error_categories=(),
                        directory_removed=True,
                        succeeded=True,
                    ),
                ),
            )
        )
    if resumed_batches:
        report = _merge_recovery_reports(
            report,
            batches=tuple(resumed_batches),
            skipped_fresh_directories=0,
        )
        _write_recovery_ledger(ledger_path, report)

    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return report
    except OSError:
        raise ScreenshotBatchError("local screenshot recovery I/O failed") from None
    if stat_is_link_or_reparse(root_stat) or not stat_module.S_ISDIR(root_stat.st_mode):
        raise ScreenshotBatchError("local screenshot temporary root is unsafe")
    try:
        secure_owner_only(root, is_directory=True)
        secured_root_stat = root.lstat()
        if (
            stat_is_link_or_reparse(secured_root_stat)
            or not stat_module.S_ISDIR(secured_root_stat.st_mode)
            or not os.path.samestat(root_stat, secured_root_stat)
        ):
            raise ScreenshotBatchError(
                "local screenshot temporary root changed while it was secured"
            )
    except ScreenshotBatchError:
        report = _merge_recovery_reports(
            report,
            issues=(
                LocalRecoveryIssue(
                    batch_locator="ROOT",
                    category="TEMP_ROOT_ACL_NOT_OWNER_ONLY",
                    observed_at=observed_at,
                ),
            ),
            skipped_fresh_directories=0,
        )
        _write_recovery_ledger(ledger_path, report)
        return report

    observed = now_timestamp if now_timestamp is not None else datetime.now().timestamp()
    recovered: list[LocalRecoveryBatchEvidence] = []
    issues: list[LocalRecoveryIssue] = []
    skipped_fresh = 0
    try:
        children = tuple(sorted(root.iterdir(), key=lambda item: item.name))
    except OSError:
        report = _merge_recovery_reports(
            report,
            issues=(
                LocalRecoveryIssue(
                    batch_locator="ROOT",
                    category="TEMP_ROOT_ENUMERATION_FAILED",
                    observed_at=observed_at,
                ),
            ),
            skipped_fresh_directories=0,
        )
        _write_recovery_ledger(ledger_path, report)
        return report
    for child in children:
        if not _LOCAL_BATCH_DIRECTORY.fullmatch(child.name):
            continue
        try:
            child_stat = child.lstat()
        except OSError:
            issues.append(
                LocalRecoveryIssue(
                    batch_locator=child.name,
                    category="BATCH_STAT_FAILED",
                    observed_at=datetime.now(timezone.utc),
                )
            )
            continue
        if stat_is_link_or_reparse(child_stat) or not stat_module.S_ISDIR(
            child_stat.st_mode
        ):
            issues.append(
                LocalRecoveryIssue(
                    batch_locator=child.name,
                    category="UNSAFE_BATCH_PATH",
                    observed_at=datetime.now(timezone.utc),
                )
            )
            continue
        if observed - child_stat.st_mtime < minimum_age_seconds:
            skipped_fresh += 1
            continue
        prior = next(
            (item for item in report.batches if item.batch_locator == child.name),
            None,
        )
        if prior is not None and prior.attempts:
            continue
        try:
            entries = tuple(sorted(child.iterdir(), key=lambda item: item.name))
        except OSError:
            issues.append(
                LocalRecoveryIssue(
                    batch_locator=child.name,
                    category="BATCH_ENUMERATION_FAILED",
                    observed_at=datetime.now(timezone.utc),
                )
            )
            continue
        current_assets: dict[str, tuple[Path, int]] = {}
        unsafe_category: str | None = None
        for entry in entries:
            if not _LOCAL_BATCH_DIRECTORY.fullmatch(entry.name):
                unsafe_category = "UNEXPECTED_BATCH_ENTRY"
                break
            try:
                entry_stat = entry.lstat()
                if stat_is_link_or_reparse(entry_stat) or not stat_module.S_ISREG(
                    entry_stat.st_mode
                ):
                    unsafe_category = "UNSAFE_ASSET_PATH"
                    break
            except OSError:
                unsafe_category = "ASSET_STAT_FAILED"
                break
            current_assets[entry.name] = (entry, entry_stat.st_size)
        if unsafe_category is not None:
            issues.append(
                LocalRecoveryIssue(
                    batch_locator=child.name,
                    category=unsafe_category,
                    observed_at=datetime.now(timezone.utc),
                )
            )
            continue
        known_locators = set(current_assets)
        if prior is not None:
            known_locators.update(prior.asset_locators)
        assets = [
            StagedScreenshot(
                part_index=index,
                media_type="application/octet-stream",
                byte_size=current_assets.get(locator, (child / locator, 0))[1],
                sha256="",
                locator=locator,
                path=current_assets.get(locator, (child / locator, 0))[0],
            )
            for index, locator in enumerate(sorted(known_locators))
        ]
        batch = StagedBatch(
            batch_locator=child.name,
            temp_root=root,
            directory=child,
            screenshots=tuple(assets),
            body_bytes=sum(item.byte_size for item in assets),
            total_file_bytes=sum(item.byte_size for item in assets),
        )
        pending = LocalRecoveryBatchEvidence(
            batch_locator=child.name,
            asset_locators=tuple(item.locator for item in assets),
            attempts=(),
        )
        report = _merge_recovery_reports(
            report,
            batches=(pending,),
            issues=tuple(issues),
            skipped_fresh_directories=skipped_fresh,
        )
        # Write before the first unlink so a process death can never erase the
        # only binding between random locators and the later database receipt.
        _write_recovery_ledger(ledger_path, report)
        try:
            attempts = cleanup_staged_batch(batch, "CRASH_RECOVERY")
        except ScreenshotCleanupError as exc:
            attempts = exc.attempts
        except ScreenshotBatchError as exc:
            issues.append(
                LocalRecoveryIssue(
                    batch_locator=child.name,
                    category=exc.code,
                    observed_at=datetime.now(timezone.utc),
                )
            )
            continue
        recovered_batch = LocalRecoveryBatchEvidence(
            batch_locator=child.name,
            asset_locators=tuple(item.locator for item in assets),
            attempts=attempts,
        )
        recovered.append(recovered_batch)
        report = _merge_recovery_reports(
            report,
            batches=(recovered_batch,),
            issues=tuple(issues),
            skipped_fresh_directories=skipped_fresh,
        )
        _write_recovery_ledger(ledger_path, report)
    report = _merge_recovery_reports(
        report,
        batches=tuple(recovered),
        issues=tuple(issues),
        skipped_fresh_directories=skipped_fresh,
    )
    if report.batches or report.issues:
        _write_recovery_ledger(ledger_path, report)
    return report


def recover_orphaned_local_screenshot_files(
    *,
    minimum_age_seconds: float,
    now_timestamp: float | None = None,
) -> LocalScreenshotRecoveryReport:
    try:
        root = _resolved_local_temp_root(_temp_root())
        with _local_recovery_lock(root):
            return _recover_orphaned_local_screenshot_files_locked(
                root=root,
                minimum_age_seconds=minimum_age_seconds,
                now_timestamp=now_timestamp,
            )
    except ScreenshotBatchError:
        raise
    except OSError:
        raise ScreenshotBatchError("local screenshot recovery I/O failed") from None


def _request_hash(batch: StagedBatch) -> str:
    return canonical_sha256(
        {
            "screenshots": [
                {
                    "position": item.part_index,
                    "media_type": item.media_type,
                    "byte_size": item.byte_size,
                    "sha256": item.sha256,
                }
                for item in batch.screenshots
            ]
        }
    )


def _image_statuses(document_or_error: Any, count: int) -> dict[int, str]:
    images = getattr(document_or_error, "images", None)
    if images is None:
        images = getattr(document_or_error, "image_results", ())
    statuses = {
        int(image.image_index): str(image.status)
        for image in images or ()
        if hasattr(image, "image_index") and hasattr(image, "status")
    }
    return {index: statuses.get(index, "FAILED") for index in range(count)}


def _asset_inputs(
    batch: StagedBatch,
    document_or_error: Any,
) -> tuple[ScreenshotBatchAssetInput, ...]:
    statuses = _image_statuses(document_or_error, len(batch.screenshots))
    return tuple(
        ScreenshotBatchAssetInput(
            upload_position=item.part_index,
            content_hash=item.sha256,
            media_type=item.media_type,
            byte_size=item.byte_size,
            storage_locator=item.locator,
            ocr_status=statuses[item.part_index],
        )
        for item in batch.screenshots
    )


def _pending_asset_inputs(batch: StagedBatch) -> tuple[ScreenshotBatchAssetInput, ...]:
    return tuple(
        ScreenshotBatchAssetInput(
            upload_position=item.part_index,
            content_hash=item.sha256,
            media_type=item.media_type,
            byte_size=item.byte_size,
            storage_locator=item.locator,
            ocr_status="PENDING",
        )
        for item in batch.screenshots
    )


def _failure_asset_inputs(
    batch: StagedBatch,
    document_or_error: Any,
) -> tuple[ScreenshotBatchAssetInput, ...]:
    statuses = _image_statuses(document_or_error, len(batch.screenshots))
    return tuple(
        ScreenshotBatchAssetInput(
            upload_position=item.part_index,
            content_hash=item.sha256,
            media_type=item.media_type,
            byte_size=item.byte_size,
            storage_locator=item.locator,
            ocr_status=statuses[item.part_index],
        )
        for item in batch.screenshots
        if item.byte_size > 0
    )


def _cleanup_inputs(
    batch: StagedBatch | None,
    attempts: tuple[CleanupAttempt, ...],
) -> tuple[ScreenshotCleanupReceiptInput, ...]:
    locator_to_position = (
        {item.locator: item.part_index for item in batch.screenshots}
        if batch is not None
        else {}
    )
    values: list[ScreenshotCleanupReceiptInput] = []
    for attempt in attempts:
        locators = set(attempt.deleted_locators)
        locators.update(attempt.already_absent_locators)
        locators.update(attempt.failed_locators)
        locators.update(attempt.remaining_locators)
        if not locators:
            values.append(
                ScreenshotCleanupReceiptInput(
                    upload_position=None,
                    attempt_number=attempt.attempt_number,
                    terminal_reason=attempt.terminal_reason,
                    cleanup_status=("DELETED" if attempt.succeeded else "DELETE_FAILED"),
                    attempted_at=attempt.attempted_at,
                    error_category=(attempt.error_categories[0] if attempt.error_categories else None),
                )
            )
            continue
        for locator in sorted(locators, key=lambda value: locator_to_position.get(value, 99)):
            if locator in attempt.deleted_locators:
                cleanup_status = "DELETED"
            elif locator in attempt.already_absent_locators:
                cleanup_status = "ALREADY_ABSENT"
            else:
                cleanup_status = "DELETE_FAILED"
            values.append(
                ScreenshotCleanupReceiptInput(
                    upload_position=locator_to_position.get(locator),
                    attempt_number=attempt.attempt_number,
                    terminal_reason=attempt.terminal_reason,
                    cleanup_status=cleanup_status,
                    attempted_at=attempt.attempted_at,
                    error_category=(attempt.error_categories[0] if attempt.error_categories else None),
                )
            )
    return tuple(values)


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    """Walk wrapper exceptions without trusting only the outer wait_for error."""

    values: list[BaseException] = []
    pending: list[BaseException] = [exc]
    observed: set[int] = set()
    while pending:
        current = pending.pop(0)
        if id(current) in observed:
            continue
        observed.add(id(current))
        values.append(current)
        cause = current.__cause__
        context = current.__context__
        if cause is not None:
            pending.append(cause)
        if context is not None and context is not cause:
            pending.append(context)
    return tuple(values)


def _exception_evidence(
    exc: BaseException,
) -> tuple[StagedBatch | None, tuple[CleanupAttempt, ...]]:
    batch: StagedBatch | None = None
    attempts: tuple[CleanupAttempt, ...] = ()
    for current in _exception_chain(exc):
        candidate_batch = getattr(current, "batch", None)
        if batch is None and isinstance(candidate_batch, StagedBatch):
            batch = candidate_batch
        candidate_attempts = tuple(getattr(current, "cleanup_attempts", ()))
        if not attempts and candidate_attempts:
            attempts = candidate_attempts
    return batch, attempts


def _exception_contains(exc: BaseException, error_type: type[BaseException]) -> bool:
    return any(isinstance(current, error_type) for current in _exception_chain(exc))


def _exception_code(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return ScreenshotStagingTimeoutError.code
    for current in _exception_chain(exc):
        code = getattr(current, "code", None)
        if isinstance(code, str) and code:
            return code
    return type(exc).__name__


def _upload_error(exc: BaseException) -> HTTPException:
    if _exception_contains(exc, ScreenshotCleanupError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "SCREENSHOT_CLEANUP_RETRY_REQUIRED",
                "message": "截图未能安全清理，请稍后重试",
            },
        )
    if _exception_contains(exc, MultipartRequiredError):
        return HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"code": "MULTIPART_REQUIRED", "message": "请选择 PNG、JPEG 或 WebP 截图"},
        )
    if _exception_contains(exc, ScreenshotBatchTooLargeError) or _exception_contains(
        exc, ScreenshotFileTooLargeError
    ):
        return HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "SCREENSHOT_BATCH_TOO_LARGE", "message": "截图大小超过限制"},
        )
    if isinstance(exc, TimeoutError) or _exception_contains(exc, ScreenshotStagingTimeoutError):
        return HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"code": "SCREENSHOT_PROCESSING_TIMED_OUT", "message": "截图读取超时，请重试"},
        )
    if _exception_contains(exc, ScreenshotStagingCancelledError):
        return HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail={"code": "SCREENSHOT_UPLOAD_CANCELLED", "message": "截图上传已取消"},
        )
    if any(isinstance(current, ScreenshotBatchError) for current in _exception_chain(exc)):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "SCREENSHOT_BATCH_INVALID", "message": "截图格式或数量不符合要求"},
        )
    raise exc


async def _record_failure(
    *,
    service: TripUnderstandingApplicationService,
    owner_user_id: str,
    idempotency_key: str,
    batch_ref: str,
    request_hash: str,
    failure_status: str,
    error_category: str,
    expires_at: datetime,
    batch: StagedBatch | None,
    document_or_error: Any,
    cleanup_attempts: tuple[CleanupAttempt, ...],
) -> None:
    await service.store_screenshot_batch_failure(
        ScreenshotBatchFailurePersistenceInput(
            batch_ref=batch_ref,
            owner_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            status=failure_status,
            expires_at=expires_at,
            last_error_category=error_category,
            assets=(
                _failure_asset_inputs(batch, document_or_error)
                if batch is not None
                else ()
            ),
            cleanup_receipts=_cleanup_inputs(batch, cleanup_attempts),
        )
    )


async def _record_claim_cleanup(
    *,
    service: TripUnderstandingApplicationService,
    owner_user_id: str,
    idempotency_key: str,
    batch: StagedBatch,
    cleanup_attempts: tuple[CleanupAttempt, ...],
    privacy_blocked: bool,
) -> None:
    await service.record_screenshot_cleanup(
        ScreenshotCleanupPersistenceInput(
            owner_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            assets=_pending_asset_inputs(batch),
            cleanup_receipts=_cleanup_inputs(batch, cleanup_attempts),
            privacy_blocked=privacy_blocked,
        )
    )


async def _cleanup_batch(
    batch: StagedBatch,
    terminal_reason: str,
) -> tuple[tuple[CleanupAttempt, ...], ScreenshotCleanupError | None]:
    # Keep the bounded 1-6 file deletion section free of await points.  A task
    # cancellation cannot otherwise interrupt shield's caller while the worker
    # thread continues without returning the receipts needed for persistence.
    try:
        begin_local_screenshot_cleanup(batch, terminal_reason)
    except ScreenshotBatchError:
        attempt = CleanupAttempt(
            attempt_number=1,
            terminal_reason=terminal_reason,
            attempted_at=datetime.now(timezone.utc),
            deleted_locators=(),
            already_absent_locators=(),
            failed_locators=tuple(item.locator for item in batch.screenshots),
            remaining_locators=tuple(item.locator for item in batch.screenshots),
            error_categories=("CLEANUP_JOURNAL_UNAVAILABLE",),
            directory_removed=False,
            succeeded=False,
        )
        return (attempt,), ScreenshotCleanupError(
            "cleanup journal could not be written",
            attempts=(attempt,),
            batch=batch,
        )
    try:
        attempts = cleanup_staged_batch(batch, terminal_reason)
    except ScreenshotCleanupError as exc:
        finish_local_screenshot_cleanup(batch, exc.attempts)
        return exc.attempts, exc
    finish_local_screenshot_cleanup(batch, attempts)
    return attempts, None


async def _acknowledge_cleanup(
    batch: StagedBatch,
    attempts: tuple[CleanupAttempt, ...],
) -> None:
    # A fail-closed journal write failure means no event exists to acknowledge.
    # The durable database receipt retains the locator binding, while the local
    # directory remains discoverable by the next node-local recovery scan.
    if any(
        "CLEANUP_JOURNAL_UNAVAILABLE" in attempt.error_categories
        for attempt in attempts
    ):
        return
    acknowledged = await asyncio.to_thread(
        acknowledge_local_screenshot_cleanup,
        batch,
        attempts,
    )
    if acknowledged:
        return
    final = attempts[-1] if attempts else None
    accounted = (
        set(final.deleted_locators) | set(final.already_absent_locators)
        if final is not None
        else set()
    )
    expected = {item.locator for item in batch.screenshots}
    try:
        conclusively_absent = (
            final is not None
            and final.succeeded
            and final.directory_removed
            and not final.failed_locators
            and not final.remaining_locators
            and accounted == expected
            and not batch.directory.exists()
        )
    except OSError:
        raise ScreenshotBatchError("local screenshot cleanup state could not be verified") from None
    if not conclusively_absent:
        raise ScreenshotBatchError("local screenshot cleanup journal changed")


def _idempotency_error(exc: BaseException) -> HTTPException:
    if isinstance(exc, IdempotencyInProgressError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REQUEST_IN_PROGRESS", "message": "截图正在读取，请稍后重试"},
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "请重新选择截图"},
    )


@router.post(
    "",
    response_model=ScreenshotBatchAcceptedView,
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "parameters": [
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "minLength": 1, "maxLength": 200},
            }
        ],
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["screenshots"],
                        "properties": {
                            "screenshots": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 6,
                                "items": {"type": "string", "format": "binary"},
                            }
                        },
                        "additionalProperties": False,
                    },
                    "encoding": {"screenshots": {"style": "form"}},
                }
            },
        },
    },
)
async def create_screenshot_batch(
    request: Request,
    response: Response,
    repository: RepositoryDep,
    current_user: CurrentUserDep,
    ocr_engine: OcrEngineDep,
):
    key = _require_idempotency_key(request.headers.get("Idempotency-Key"))
    settings = get_settings()
    observed_at = datetime.now(timezone.utc)
    expires_at = observed_at + timedelta(minutes=settings.screenshot_batch_ttl_minutes)
    opaque_ref = _batch_ref(current_user, key)
    service = TripUnderstandingApplicationService(repository)
    try:
        preflight = await service.preflight_screenshot_batch(
            owner_user_id=current_user,
            idempotency_key=key,
            batch_ref=opaque_ref,
            now=observed_at,
        )
    except (IdempotencyConflictError, IdempotencyInProgressError) as exc:
        raise _idempotency_error(exc) from exc
    if preflight is not None:
        response.headers["Idempotency-Replayed"] = "true"
        response.headers["Cache-Control"] = "no-store"
        return preflight.accepted
    request_hash = canonical_sha256(
        {"content_type": request.headers.get("content-type", ""), "state": "UNREAD"}
    )

    try:
        staged = await asyncio.wait_for(
            stage_screenshot_multipart(
                request.headers.get("content-type", ""),
                request.stream(),
                None,
                _temp_root(),
                begin_local_screenshot_cleanup,
                finish_local_screenshot_cleanup,
            ),
            timeout=settings.screenshot_staging_deadline_seconds,
        )
    except BaseException as exc:
        staged_on_error, attempts = _exception_evidence(exc)
        privacy_blocked = _exception_contains(exc, ScreenshotCleanupError) or bool(
            attempts and not attempts[-1].succeeded
        )
        if attempts:
            await asyncio.shield(
                _record_failure(
                    service=service,
                    owner_user_id=current_user,
                    idempotency_key=key,
                    batch_ref=opaque_ref,
                    request_hash=request_hash,
                    failure_status=(
                        "PRIVACY_BLOCKED"
                        if privacy_blocked
                        else "TIMED_OUT"
                        if isinstance(exc, TimeoutError)
                        or _exception_contains(exc, ScreenshotStagingTimeoutError)
                        else "CANCELLED"
                        if _exception_contains(exc, asyncio.CancelledError)
                        else "FAILED"
                    ),
                    error_category=(
                        "SCREENSHOT_CLEANUP_FAILED"
                        if privacy_blocked
                        else _exception_code(exc)
                    ),
                    expires_at=expires_at,
                    batch=staged_on_error,
                    document_or_error=exc,
                    cleanup_attempts=attempts,
                )
            )
            if staged_on_error is not None:
                await asyncio.shield(
                    _acknowledge_cleanup(staged_on_error, attempts)
                )
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise _upload_error(exc) from exc

    request_hash = _request_hash(staged)
    claim_payload = ScreenshotBatchClaimInput(
        batch_ref=opaque_ref,
        owner_user_id=current_user,
        idempotency_key=key,
        request_hash=request_hash,
        expires_at=expires_at,
        assets=_pending_asset_inputs(staged),
    )
    try:
        replay = await service.claim_screenshot_batch(claim_payload, now=observed_at)
    except (IdempotencyConflictError, IdempotencyInProgressError) as exc:
        attempts, cleanup_error = await _cleanup_batch(staged, "IDEMPOTENCY_REJECTED")
        await _record_claim_cleanup(
            service=service,
            owner_user_id=current_user,
            idempotency_key=key,
            batch=staged,
            cleanup_attempts=attempts,
            privacy_blocked=cleanup_error is not None,
        )
        await _acknowledge_cleanup(staged, attempts)
        if cleanup_error is not None:
            raise _upload_error(cleanup_error) from cleanup_error
        raise _idempotency_error(exc) from exc
    except BaseException:
        attempts, cleanup_error = await _cleanup_batch(staged, "CLAIM_FAILED")
        await _record_claim_cleanup(
            service=service,
            owner_user_id=current_user,
            idempotency_key=key,
            batch=staged,
            cleanup_attempts=attempts,
            privacy_blocked=cleanup_error is not None,
        )
        await _acknowledge_cleanup(staged, attempts)
        if cleanup_error is not None:
            raise _upload_error(cleanup_error) from cleanup_error
        raise

    if replay is not None:
        attempts, cleanup_error = await _cleanup_batch(staged, "IDEMPOTENT_REPLAY")
        await _record_claim_cleanup(
            service=service,
            owner_user_id=current_user,
            idempotency_key=key,
            batch=staged,
            cleanup_attempts=attempts,
            privacy_blocked=cleanup_error is not None,
        )
        await _acknowledge_cleanup(staged, attempts)
        if cleanup_error is not None:
            raise _upload_error(cleanup_error) from cleanup_error
        response.headers["Idempotency-Replayed"] = "true"
        response.headers["Cache-Control"] = "no-store"
        return replay.accepted

    run_spec = ScreenshotOcrRunSpecV1(
        max_concurrency=1,
        per_image_timeout_seconds=settings.screenshot_ocr_per_image_deadline_seconds,
        batch_timeout_seconds=settings.screenshot_ocr_batch_deadline_seconds,
        low_confidence_threshold=settings.screenshot_ocr_low_confidence_threshold,
    )
    try:
        document_or_error = await extract_screenshot_document(
            tuple(
                StagedScreenshotAsset(path=item.path, content_hash=item.sha256)
                for item in staged.screenshots
            ),
            ocr_engine,
            run_spec,
        )
    except (ScreenshotOcrAllFailedError, ScreenshotOcrTimeoutError) as exc:
        document_or_error = exc
    except asyncio.CancelledError:
        attempts, cleanup_error = await _cleanup_batch(staged, "CANCELLED")
        await asyncio.shield(
            _record_failure(
                service=service,
                owner_user_id=current_user,
                idempotency_key=key,
                batch_ref=opaque_ref,
                request_hash=request_hash,
                failure_status=(
                    "PRIVACY_BLOCKED" if cleanup_error is not None else "CANCELLED"
                ),
                error_category=(
                    "SCREENSHOT_CLEANUP_FAILED"
                    if cleanup_error is not None
                    else "SCREENSHOT_UPLOAD_CANCELLED"
                ),
                expires_at=expires_at,
                batch=staged,
                document_or_error=None,
                cleanup_attempts=attempts,
            )
        )
        await asyncio.shield(_acknowledge_cleanup(staged, attempts))
        raise
    except BaseException as exc:
        attempts, cleanup_error = await _cleanup_batch(staged, "FAILED")
        await _record_failure(
            service=service,
            owner_user_id=current_user,
            idempotency_key=key,
            batch_ref=opaque_ref,
            request_hash=request_hash,
            failure_status=(
                "PRIVACY_BLOCKED" if cleanup_error is not None else "FAILED"
            ),
            error_category=(
                "SCREENSHOT_CLEANUP_FAILED"
                if cleanup_error is not None
                else "SCREENSHOT_READING_UNAVAILABLE"
            ),
            expires_at=expires_at,
            batch=staged,
            document_or_error=exc,
            cleanup_attempts=attempts,
        )
        await _acknowledge_cleanup(staged, attempts)
        if cleanup_error is not None:
            raise _upload_error(cleanup_error) from cleanup_error
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "SCREENSHOT_READING_UNAVAILABLE",
                "message": "暂时无法读取截图，请稍后重试",
            },
        ) from exc

    terminal_reason = (
        "TIMED_OUT"
        if isinstance(document_or_error, ScreenshotOcrTimeoutError)
        else "FAILED"
        if isinstance(document_or_error, ScreenshotOcrAllFailedError)
        else "SUCCEEDED"
    )
    cleanup_attempts, cleanup_error = await _cleanup_batch(staged, terminal_reason)
    if cleanup_error is not None:
        await _record_failure(
            service=service,
            owner_user_id=current_user,
            idempotency_key=key,
            batch_ref=opaque_ref,
            request_hash=request_hash,
            failure_status="PRIVACY_BLOCKED",
            error_category="SCREENSHOT_CLEANUP_FAILED",
            expires_at=expires_at,
            batch=staged,
            document_or_error=document_or_error,
            cleanup_attempts=cleanup_attempts,
        )
        await _acknowledge_cleanup(staged, cleanup_attempts)
        raise _upload_error(cleanup_error) from cleanup_error

    if isinstance(document_or_error, ScreenshotOcrTimeoutError):
        await _record_failure(
            service=service,
            owner_user_id=current_user,
            idempotency_key=key,
            batch_ref=opaque_ref,
            request_hash=request_hash,
            failure_status="TIMED_OUT",
            error_category=document_or_error.code,
            expires_at=expires_at,
            batch=staged,
            document_or_error=document_or_error,
            cleanup_attempts=cleanup_attempts,
        )
        await _acknowledge_cleanup(staged, cleanup_attempts)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "code": "SCREENSHOT_PROCESSING_TIMED_OUT",
                "message": "截图读取超时，请重试",
            },
        )
    if isinstance(document_or_error, ScreenshotOcrAllFailedError):
        all_no_text = all(
            image.status == "NO_TEXT" for image in document_or_error.image_results
        )
        await _record_failure(
            service=service,
            owner_user_id=current_user,
            idempotency_key=key,
            batch_ref=opaque_ref,
            request_hash=request_hash,
            failure_status="FAILED",
            error_category=document_or_error.code,
            expires_at=expires_at,
            batch=staged,
            document_or_error=document_or_error,
            cleanup_attempts=cleanup_attempts,
        )
        await _acknowledge_cleanup(staged, cleanup_attempts)
        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
                if all_no_text
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail={
                "code": (
                    "SCREENSHOT_TEXT_NOT_FOUND"
                    if all_no_text
                    else "SCREENSHOT_READING_UNAVAILABLE"
                ),
                "message": (
                    "没有在截图中找到可用文字"
                    if all_no_text
                    else "暂时无法读取截图，请稍后重试"
                ),
            },
        )

    document = document_or_error
    payload = ScreenshotBatchPersistenceInput(
        batch_ref=opaque_ref,
        owner_user_id=current_user,
        idempotency_key=key,
        request_hash=request_hash,
        source_document_json=document.model_dump_json(),
        source_document_hash=document.document_hash,
        semantic_text_hash=hashlib.sha256(
            document.semantic_text.encode("utf-8")
        ).hexdigest(),
        outcome="PARTIAL" if document.partial else "COMPLETE",
        expires_at=expires_at,
        assets=_asset_inputs(staged, document),
        cleanup_receipts=_cleanup_inputs(staged, cleanup_attempts),
    )
    try:
        outcome = await service.store_screenshot_batch(
            payload,
            now=datetime.now(timezone.utc),
        )
    except (IdempotencyConflictError, IdempotencyInProgressError) as exc:
        raise _idempotency_error(exc) from exc
    await _acknowledge_cleanup(staged, cleanup_attempts)
    if outcome.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    response.headers["Cache-Control"] = "no-store"
    return outcome.accepted
