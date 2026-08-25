"""Formal P6 G1 runner for the external real-authorized OCR custody set."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from app.importing.screenshots import PaddleOcrEngine
from evals.trip_check_v1.p6.contracts_v1 import (
    P6ContractError,
    digest,
    file_sha256,
    read_actual_repo_state,
    validate_candidate_run_spec,
    validate_gate_receipt,
    validate_real_ocr_dataset_binding,
)


class OcrEngine(Protocol):
    async def recognize(self, image_path: Path) -> list[Any]: ...


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P6ContractError(reason) from exc
    if not isinstance(value, dict):
        raise P6ContractError(reason)
    return value


def _external_file(path_value: object, repo_root: Path, reason: str) -> Path:
    if not isinstance(path_value, str):
        raise P6ContractError(reason)
    path = Path(path_value)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(repo_root.resolve(strict=True))
    except ValueError:
        if not resolved.is_file():
            raise P6ContractError(reason)
        return resolved
    except (OSError, RuntimeError) as exc:
        raise P6ContractError(reason) from exc
    raise P6ContractError(reason)


def _validate_self_hash(value: Mapping[str, Any], field: str, reason: str) -> None:
    if value.get(field) != digest({key: item for key, item in value.items() if key != field}):
        raise P6ContractError(reason)


def _validate_authorization(value: dict[str, Any], item: Mapping[str, Any]) -> None:
    required = {
        "schema_version", "receipt_id", "source_image_sha256", "authorization_basis",
        "authorization_scope", "status", "granted_at", "valid_until", "receipt_hash",
    }
    if set(value) != required or not (
        value["schema_version"] == "trip-check-p6-ocr-authorization-receipt-v1"
        and value["receipt_id"] == item["authorization_receipt_id"]
        and value["source_image_sha256"] == item["source_image_sha256"]
        and value["authorization_basis"] == item["authorization_basis"]
        and value["authorization_scope"] == "OCR_CANDIDATE_EVALUATION"
        and value["status"] == "GRANTED"
    ):
        raise P6ContractError("P6_REAL_OCR_AUTHORIZATION_INVALID")
    _validate_self_hash(value, "receipt_hash", "P6_REAL_OCR_AUTHORIZATION_HASH_MISMATCH")
    try:
        granted_at = datetime.fromisoformat(value["granted_at"].replace("Z", "+00:00"))
        valid_until = (
            datetime.fromisoformat(value["valid_until"].replace("Z", "+00:00"))
            if value["valid_until"] is not None
            else None
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise P6ContractError("P6_REAL_OCR_AUTHORIZATION_INVALID") from exc
    now = datetime.now(timezone.utc)
    if (
        granted_at.tzinfo is None
        or (valid_until is not None and valid_until.tzinfo is None)
        or granted_at > now
        or (valid_until is not None and valid_until <= now)
    ):
        raise P6ContractError("P6_REAL_OCR_AUTHORIZATION_EXPIRED")


def _validate_annotation(value: dict[str, Any], item: Mapping[str, Any]) -> list[dict[str, Any]]:
    required = {
        "schema_version", "item_id", "source_image_sha256", "annotation_version",
        "fields", "annotation_hash",
    }
    if set(value) != required or not (
        value["schema_version"] == "trip-check-p6-ocr-annotation-v1"
        and value["item_id"] == item["item_id"]
        and value["source_image_sha256"] == item["source_image_sha256"]
        and value["annotation_version"] == item["annotation_version"]
        and isinstance(value["fields"], list)
        and value["fields"]
    ):
        raise P6ContractError("P6_REAL_OCR_ANNOTATION_INVALID")
    _validate_self_hash(value, "annotation_hash", "P6_REAL_OCR_ANNOTATION_HASH_MISMATCH")
    field_ids: set[str] = set()
    for field in value["fields"]:
        if not isinstance(field, dict) or set(field) != {
            "field_id", "field_type", "expected_text", "must_confirm", "box",
        }:
            raise P6ContractError("P6_REAL_OCR_ANNOTATION_INVALID")
        box = field["box"]
        if not (
            isinstance(field["field_id"], str)
            and field["field_id"] not in field_ids
            and isinstance(field["field_type"], str)
            and isinstance(field["expected_text"], str)
            and field["expected_text"].strip()
            and isinstance(field["must_confirm"], bool)
            and isinstance(box, list)
            and len(box) == 4
            and all(isinstance(part, int) and part >= 0 for part in box)
            and box[0] < box[2]
            and box[1] < box[3]
        ):
            raise P6ContractError("P6_REAL_OCR_ANNOTATION_INVALID")
        field_ids.add(field["field_id"])
    return value["fields"]


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", value.casefold())


def _line_for_box(lines: list[Any], box: list[int]) -> Any | None:
    candidates: list[Any] = []
    for line in lines:
        center_x = (line.box.x_min + line.box.x_max) / 2
        center_y = (line.box.y_min + line.box.y_max) / 2
        if box[0] <= center_x <= box[2] and box[1] <= center_y <= box[3]:
            candidates.append(line)
    return max(candidates, key=lambda line: float(line.confidence), default=None)


def _tracked_source_leaks(repo_root: Path, source_hashes: set[str]) -> int:
    try:
        output = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise P6ContractError("P6_REAL_OCR_GIT_SCAN_FAILED") from exc
    hits = 0
    for raw_path in output.split(b"\0"):
        if not raw_path:
            continue
        try:
            path = repo_root / raw_path.decode("utf-8")
            if path.is_file() and file_sha256(path) in source_hashes:
                hits += 1
        except (OSError, UnicodeError):
            raise P6ContractError("P6_REAL_OCR_GIT_SCAN_FAILED")
    return hits


def _contains_image_leak(value: bytes) -> bool:
    lowered = value.lower()
    return (
        b"data:image/" in lowered
        or b"ivborw0kggo" in lowered
        or b"/9j/" in lowered
        or b"uklgr" in lowered
        or b"\x89png\r\n\x1a\n" in value
        or b"\xff\xd8\xff" in value
        or (b"RIFF" in value and b"WEBP" in value)
    )


def _application_log_leaks(log_roots: list[Path]) -> int:
    hits = 0
    for root in log_roots:
        try:
            resolved = root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise P6ContractError("P6_REAL_OCR_LOG_SCAN_FAILED") from exc
        paths = [resolved] if resolved.is_file() else [path for path in resolved.rglob("*") if path.is_file()]
        for path in paths:
            try:
                if _contains_image_leak(path.read_bytes()):
                    hits += 1
            except OSError as exc:
                raise P6ContractError("P6_REAL_OCR_LOG_SCAN_FAILED") from exc
    return hits


async def _database_leaks(database_url: str, source_hashes: set[str]) -> int:
    try:
        import asyncpg

        connection = await asyncpg.connect(database_url)
        try:
            columns = await connection.fetch(
                """
                SELECT table_schema, table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND data_type IN (
                    'text', 'character varying', 'character', 'json', 'jsonb', 'bytea'
                  )
                ORDER BY table_name, ordinal_position
                """
            )
            hits = 0
            for column in columns:
                schema = str(column["table_schema"]).replace('"', '""')
                table = str(column["table_name"]).replace('"', '""')
                name = str(column["column_name"]).replace('"', '""')
                rows = await connection.fetch(
                    f'SELECT "{name}" AS value FROM "{schema}"."{table}" '
                    f'WHERE "{name}" IS NOT NULL'
                )
                for row in rows:
                    value = row["value"]
                    if not isinstance(value, bytes):
                        value = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
                    if (
                        isinstance(row["value"], bytes)
                        and hashlib.sha256(row["value"]).hexdigest() in source_hashes
                    ) or _contains_image_leak(value):
                        hits += 1
            return hits
        finally:
            await connection.close()
    except P6ContractError:
        raise
    except Exception as exc:
        raise P6ContractError("P6_REAL_OCR_DATABASE_SCAN_FAILED") from exc


def _private_items(value: dict[str, Any], dataset: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if set(value) != {
        "schema_version", "dataset_manifest_file_sha256", "cross_split_check_receipt_path", "items",
    } or not (
        value["schema_version"] == "trip-check-p6-real-ocr-private-manifest-v1"
        and isinstance(value["items"], list)
        and len(value["items"]) == 60
    ):
        raise P6ContractError("P6_REAL_OCR_PRIVATE_MANIFEST_INVALID")
    items: dict[str, dict[str, Any]] = {}
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != {
            "item_id", "source_path", "authorization_receipt_path", "annotation_path",
        }:
            raise P6ContractError("P6_REAL_OCR_PRIVATE_MANIFEST_INVALID")
        item_id = item["item_id"]
        if not isinstance(item_id, str) or item_id in items:
            raise P6ContractError("P6_REAL_OCR_PRIVATE_MANIFEST_INVALID")
        items[item_id] = item
    if set(items) != {item["item_id"] for item in dataset["items"]}:
        raise P6ContractError("P6_REAL_OCR_PRIVATE_MANIFEST_INVALID")
    return items


async def run_real_authorized_ocr(
    *,
    candidate_run_spec_path: Path,
    dataset_manifest_path: Path,
    private_manifest_path: Path,
    output_root: Path,
    work_root: Path,
    repo_root: Path,
    engine: OcrEngine | None = None,
    formal: bool = True,
    database_url: str | None = None,
    log_roots: list[Path] | None = None,
) -> dict[str, Any]:
    spec = validate_candidate_run_spec(
        _load_json(candidate_run_spec_path, "P6_CANDIDATE_RUN_SPEC_INVALID")
    )
    dataset_file_sha = file_sha256(dataset_manifest_path)
    dataset = validate_real_ocr_dataset_binding(
        _load_json(dataset_manifest_path, "P6_REAL_OCR_DATASET_INVALID"),
        spec,
        dataset_file_sha,
    )
    if formal:
        # A self-hashed GRANTED JSON is not evidence of authorization. Keep the
        # formal path closed until the 60 source grants and an independently
        # verifiable reviewer/signature bundle exist in external custody.
        raise P6ContractError("P6_REAL_OCR_INDEPENDENT_AUTHORIZATION_REVIEW_REQUIRED")
    if formal:
        if not database_url or not log_roots:
            raise P6ContractError("P6_REAL_OCR_PRIVACY_SCAN_INPUTS_REQUIRED")
        actual = read_actual_repo_state(repo_root)
        if actual != {
            "subject_commit": spec["subject_commit"],
            "upstream_ref": spec["upstream_ref"],
            "upstream_commit": spec["upstream_commit"],
            "dirty_tree": False,
        }:
            raise P6ContractError("P6_REAL_OCR_REPO_BINDING_INVALID")
        expected_output = (Path(spec["evidence_root"]) / "g1").resolve(strict=False)
        if output_root.resolve(strict=False) != expected_output:
            raise P6ContractError("P6_REAL_OCR_OUTPUT_ROOT_INVALID")
    repo_resolved = repo_root.resolve(strict=True)
    output_resolved = output_root.resolve(strict=False)
    work_resolved = work_root.resolve(strict=False)
    for root in (output_resolved, work_resolved):
        try:
            root.relative_to(repo_resolved)
        except ValueError:
            pass
        else:
            raise P6ContractError("P6_REAL_OCR_EXTERNAL_ROOT_REQUIRED")
    if output_resolved == work_resolved:
        raise P6ContractError("P6_REAL_OCR_ROOTS_MUST_BE_DISTINCT")
    if formal and output_resolved.exists() and any(output_resolved.iterdir()):
        raise P6ContractError("P6_REAL_OCR_OUTPUT_NOT_EMPTY")
    if formal and work_resolved.exists() and any(work_resolved.iterdir()):
        raise P6ContractError("P6_REAL_OCR_WORK_ROOT_NOT_EMPTY")
    private_path = _external_file(
        str(private_manifest_path), repo_resolved, "P6_REAL_OCR_PRIVATE_MANIFEST_INVALID"
    )
    private = _load_json(private_path, "P6_REAL_OCR_PRIVATE_MANIFEST_INVALID")
    if private.get("dataset_manifest_file_sha256") != dataset_file_sha:
        raise P6ContractError("P6_REAL_OCR_PRIVATE_MANIFEST_BINDING_INVALID")
    cross_split_path = _external_file(
        private.get("cross_split_check_receipt_path"),
        repo_resolved,
        "P6_REAL_OCR_CROSS_SPLIT_RECEIPT_INVALID",
    )
    if file_sha256(cross_split_path) != dataset["cross_split_check_receipt_sha256"]:
        raise P6ContractError("P6_REAL_OCR_CROSS_SPLIT_RECEIPT_FILE_HASH_MISMATCH")
    cross_split = _load_json(
        cross_split_path, "P6_REAL_OCR_CROSS_SPLIT_RECEIPT_INVALID"
    )
    expected_source_set_hash = digest(sorted(
        item["source_image_sha256"] for item in dataset["items"]
    ))
    if set(cross_split) != {
        "schema_version", "dataset_id", "candidate_item_count", "source_hash_set_sha256",
        "historical_split_manifest_sha256", "exact_duplicate_count", "near_duplicate_count",
        "algorithm", "receipt_hash",
    } or not (
        cross_split["schema_version"] == "trip-check-p6-ocr-cross-split-receipt-v1"
        and cross_split["dataset_id"] == dataset["dataset_id"]
        and cross_split["candidate_item_count"] == 60
        and cross_split["source_hash_set_sha256"] == expected_source_set_hash
        and re.fullmatch(r"[0-9a-f]{64}", cross_split["historical_split_manifest_sha256"])
        and cross_split["exact_duplicate_count"] == 0
        and cross_split["near_duplicate_count"] == 0
        and cross_split["algorithm"] == "SHA256_PLUS_PHASH64_HAMMING_LE_4"
    ):
        raise P6ContractError("P6_REAL_OCR_CROSS_SPLIT_RECEIPT_INVALID")
    _validate_self_hash(
        cross_split, "receipt_hash", "P6_REAL_OCR_CROSS_SPLIT_RECEIPT_HASH_MISMATCH"
    )
    private_by_id = _private_items(private, dataset)
    current_engine = engine or PaddleOcrEngine(confirmation_threshold=0.85)
    run_dir = work_resolved / f"g1-{uuid.uuid4().hex}"
    run_dir.mkdir(parents=True, exist_ok=False)
    tp = fp = fn = must_confirm = confirm_caught = 0
    cleanup_count = 0
    source_hashes: set[str] = set()
    try:
        for item in dataset["items"]:
            private_item = private_by_id[item["item_id"]]
            source = _external_file(
                private_item["source_path"], repo_resolved, "P6_REAL_OCR_SOURCE_INVALID"
            )
            authorization_path = _external_file(
                private_item["authorization_receipt_path"],
                repo_resolved,
                "P6_REAL_OCR_AUTHORIZATION_INVALID",
            )
            annotation_path = _external_file(
                private_item["annotation_path"], repo_resolved, "P6_REAL_OCR_ANNOTATION_INVALID"
            )
            if file_sha256(source) != item["source_image_sha256"]:
                raise P6ContractError("P6_REAL_OCR_SOURCE_HASH_MISMATCH")
            if file_sha256(authorization_path) != item["authorization_receipt_sha256"]:
                raise P6ContractError("P6_REAL_OCR_AUTHORIZATION_FILE_HASH_MISMATCH")
            if file_sha256(annotation_path) != item["annotation_sha256"]:
                raise P6ContractError("P6_REAL_OCR_ANNOTATION_FILE_HASH_MISMATCH")
            authorization = _load_json(
                authorization_path, "P6_REAL_OCR_AUTHORIZATION_INVALID"
            )
            _validate_authorization(authorization, item)
            annotation = _load_json(annotation_path, "P6_REAL_OCR_ANNOTATION_INVALID")
            fields = _validate_annotation(annotation, item)
            source_hashes.add(item["source_image_sha256"])
            staged = run_dir / f"{item['item_id']}{source.suffix.lower()}"
            shutil.copyfile(source, staged)
            try:
                try:
                    lines = await current_engine.recognize(staged)
                except Exception as exc:
                    raise P6ContractError("P6_REAL_OCR_ENGINE_FAILED") from exc
            finally:
                staged.unlink(missing_ok=True)
                cleanup_count += int(not staged.exists())
            for field in fields:
                line = _line_for_box(lines, field["box"])
                expected = _normalized(field["expected_text"])
                predicted = _normalized(line.text) if line is not None else ""
                matched = bool(predicted) and (expected in predicted or predicted in expected)
                if matched:
                    tp += 1
                else:
                    fn += 1
                    fp += int(bool(predicted))
                if field["must_confirm"]:
                    must_confirm += 1
                    confirm_caught += int(line is not None and line.requires_confirmation)
    finally:
        if run_dir.parent != work_resolved:
            raise P6ContractError("P6_REAL_OCR_WORK_ROOT_INVALID")
        try:
            shutil.rmtree(run_dir, ignore_errors=False)
        except OSError as exc:
            raise P6ContractError("P6_REAL_OCR_CLEANUP_FAILED") from exc
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    micro_f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    confirmation_recall = confirm_caught / must_confirm if must_confirm else 0.0
    git_leaks = _tracked_source_leaks(repo_resolved, source_hashes)
    log_leaks = _application_log_leaks(log_roots or [])
    database_leaks = await _database_leaks(database_url, source_hashes) if database_url else 0
    metrics_work_files = sum(1 for path in work_resolved.rglob("*") if path.is_file())
    metrics = {
        "authorized_source_count": 60,
        "beijing_count": 20,
        "shanghai_count": 20,
        "hangzhou_count": 20,
        "dataset_item_count": 60,
        "beijing_item_count": 20,
        "shanghai_item_count": 20,
        "hangzhou_item_count": 20,
        "key_field_true_positive": tp,
        "key_field_false_positive": fp,
        "key_field_false_negative": fn,
        "key_field_micro_f1": round(micro_f1, 6),
        "must_confirm_field_count": must_confirm,
        "low_confidence_confirmation_recall": round(confirmation_recall, 6),
        "must_confirm_recall": round(confirmation_recall, 6),
        "work_copy_cleanup_count": cleanup_count,
        "work_root_terminal_file_count": metrics_work_files,
        "git_source_image_leak_count": git_leaks,
        "application_log_image_leak_count": log_leaks,
        "database_image_leak_count": database_leaks,
        "privacy_leak_count": git_leaks + log_leaks + database_leaks,
        "cleanup_failure_count": int(
            cleanup_count != 60 or metrics_work_files != 0
        ),
    }
    if not (
        micro_f1 >= 0.95
        and confirmation_recall == 1.0
        and cleanup_count == 60
        and metrics["work_root_terminal_file_count"] == 0
        and git_leaks == 0
        and log_leaks == 0
        and database_leaks == 0
    ):
        raise P6ContractError("P6_REAL_OCR_GATE_FAILED")
    receipt = {
        "schema_version": "trip-check-p6-gate-receipt-v1",
        "gate": "g1",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "status": "PASS",
        "evidence_level": "real_authorized_ocr",
        "checks_total": 11,
        "checks_passed": 11,
        "failure_count": 0,
        "metrics": metrics,
    }
    receipt["receipt_hash"] = digest(receipt)
    if not formal:
        diagnostic = {
            "schema_version": "trip-check-p6-g1-contract-fixture-v1",
            "status": "CONTRACT_FIXTURE_PASS",
            "subject_commit": spec["subject_commit"],
            "run_spec_hash": spec["run_spec_hash"],
            "metrics": metrics,
        }
        diagnostic["diagnostic_hash"] = digest(diagnostic)
        fixture_path = output_resolved / "g1_contract_fixture.json"
        output_resolved.mkdir(parents=True, exist_ok=True)
        try:
            with fixture_path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(
                    diagnostic,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ))
        except OSError as exc:
            raise P6ContractError("P6_REAL_OCR_DIAGNOSTIC_WRITE_FAILED") from exc
        return diagnostic
    receipt = validate_gate_receipt(receipt, "g1", spec)
    output_resolved.mkdir(parents=True, exist_ok=True)
    receipt_path = output_resolved / "g1_receipt.json"
    try:
        with receipt_path.open("x", encoding="utf-8") as stream:
            stream.write(
                json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
    except OSError as exc:
        raise P6ContractError("P6_REAL_OCR_RECEIPT_WRITE_FAILED") from exc
    return receipt


def run_sync(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_real_authorized_ocr(**kwargs))
