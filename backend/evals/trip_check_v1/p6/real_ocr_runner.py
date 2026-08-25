"""Formal P6 G1 runner for the external real-authorized OCR custody set."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import shutil
import subprocess
import uuid
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from PIL import Image

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


OCR_CONFIRMATION_THRESHOLD = 0.85
FORMAL_OCR_CONFIG = {
    "engine": "paddleocr",
    "engine_version": "3.7.0",
    "lang": "ch",
    "confirmation_threshold": OCR_CONFIRMATION_THRESHOLD,
    "use_doc_orientation_classify": False,
    "use_doc_unwarping": False,
    "use_textline_orientation": False,
    "enable_mkldnn": False,
}
FORMAL_OCR_CONFIG_SHA256 = digest(FORMAL_OCR_CONFIG)
FIELD_TYPES = {"CITY", "DATE", "HOTEL", "PLACE", "ROUTE_MODE", "TIME"}


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


def _parse_past_timestamp(value: object, reason: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise P6ContractError(reason) from exc
    if parsed.tzinfo is None or parsed > datetime.now(timezone.utc):
        raise P6ContractError(reason)
    return parsed


def _validate_source_license_receipt(value: dict[str, Any]) -> None:
    required = {
        "schema_version", "receipt_id", "site", "page_id", "page_title",
        "revision_id", "revision_timestamp", "canonical_revision_url", "history_url",
        "api_query_url", "license_name", "license_url", "rightsinfo_api_url",
        "attribution_required", "share_alike_required", "source_group_hash",
        "retrieved_at", "receipt_hash",
    }
    if set(value) != required or not (
        value["schema_version"] == "trip-check-p6-open-license-source-receipt-v1"
        and value["site"] == "zh.wikivoyage.org"
        and isinstance(value["page_id"], int)
        and value["page_id"] > 0
        and isinstance(value["revision_id"], int)
        and value["revision_id"] > 0
        and isinstance(value["page_title"], str)
        and value["page_title"] in {"北京", "上海", "杭州"}
        and value["canonical_revision_url"].startswith("https://zh.wikivoyage.org/")
        and value["history_url"].startswith("https://zh.wikivoyage.org/")
        and value["api_query_url"].startswith("https://zh.wikivoyage.org/w/api.php?")
        and value["license_name"] == "Creative Commons Attribution-ShareAlike 4.0"
        and value["license_url"].startswith(
            "https://creativecommons.org/licenses/by-sa/4.0/"
        )
        and value["rightsinfo_api_url"].startswith(
            "https://zh.wikivoyage.org/w/api.php?"
        )
        and value["attribution_required"] is True
        and value["share_alike_required"] is True
        and re.fullmatch(r"[0-9a-f]{64}", value["source_group_hash"])
    ):
        raise P6ContractError("P6_REAL_OCR_SOURCE_LICENSE_INVALID")
    _parse_past_timestamp(value["revision_timestamp"], "P6_REAL_OCR_SOURCE_LICENSE_INVALID")
    _parse_past_timestamp(value["retrieved_at"], "P6_REAL_OCR_SOURCE_LICENSE_INVALID")
    _validate_self_hash(value, "receipt_hash", "P6_REAL_OCR_SOURCE_LICENSE_HASH_MISMATCH")


def _validate_authorization(
    value: dict[str, Any], item: Mapping[str, Any], source_receipt_sha256s: set[str]
) -> None:
    required = {
        "schema_version", "receipt_id", "source_image_sha256", "authorization_basis",
        "authorization_scope", "provenance_class", "source_group_hash",
        "source_license_receipt_sha256", "license_url", "canonical_revision_url",
        "attribution_text", "reuse_obligations", "status", "verified_at",
        "valid_until", "receipt_hash",
    }
    if set(value) != required or not (
        value["schema_version"] == "trip-check-p6-ocr-authorization-receipt-v2"
        and value["receipt_id"] == item["authorization_receipt_id"]
        and value["source_image_sha256"] == item["source_image_sha256"]
        and value["source_group_hash"] == item["source_group_hash"]
        and value["source_license_receipt_sha256"] in source_receipt_sha256s
        and value["authorization_basis"] == item["authorization_basis"] == "OPEN_LICENSE"
        and value["provenance_class"] == item["provenance_class"] == "OPEN_LICENSE"
        and value["authorization_scope"] == "OCR_CANDIDATE_EVALUATION"
        and value["license_url"].startswith(
            "https://creativecommons.org/licenses/by-sa/4.0/"
        )
        and value["canonical_revision_url"].startswith("https://zh.wikivoyage.org/")
        and isinstance(value["attribution_text"], str)
        and len(value["attribution_text"].strip()) >= 12
        and value["reuse_obligations"] == ["ATTRIBUTION", "LINK_LICENSE", "SHARE_ALIKE"]
        and value["status"] == "ELIGIBLE"
        and value["valid_until"] is None
    ):
        raise P6ContractError("P6_REAL_OCR_AUTHORIZATION_INVALID")
    _validate_self_hash(value, "receipt_hash", "P6_REAL_OCR_AUTHORIZATION_HASH_MISMATCH")
    _parse_past_timestamp(value["verified_at"], "P6_REAL_OCR_AUTHORIZATION_INVALID")


def _validate_annotation(value: dict[str, Any], item: Mapping[str, Any]) -> list[dict[str, Any]]:
    required = {
        "schema_version", "item_id", "source_image_sha256", "annotation_version",
        "annotation_unit", "coverage_class", "ignored_boxes", "fields", "annotation_hash",
    }
    if set(value) != required or not (
        value["schema_version"] == "trip-check-p6-ocr-annotation-v1"
        and value["item_id"] == item["item_id"]
        and value["source_image_sha256"] == item["source_image_sha256"]
        and value["annotation_version"] == item["annotation_version"]
        and value["annotation_unit"]
        == "BROWSER_PARENT_BOUND_BLOCK_LINE_WITH_FROZEN_SEGMENT_FALLBACK_V3"
        and value["coverage_class"] == "SELECTED_KEY_FIELDS"
        and value["ignored_boxes"] == [[0, 0, 1265, 712]]
        and isinstance(value["fields"], list)
        and len(value["fields"]) >= 3
    ):
        raise P6ContractError("P6_REAL_OCR_ANNOTATION_INVALID")
    _validate_self_hash(value, "annotation_hash", "P6_REAL_OCR_ANNOTATION_HASH_MISMATCH")
    field_ids: set[str] = set()
    boxes: list[list[int]] = []
    for field in value["fields"]:
        if not isinstance(field, dict) or set(field) != {
            "field_id", "field_type", "expected_text", "must_confirm", "box",
        }:
            raise P6ContractError("P6_REAL_OCR_ANNOTATION_INVALID")
        box = field["box"]
        if not (
            isinstance(field["field_id"], str)
            and field["field_id"] not in field_ids
            and field["field_type"] in FIELD_TYPES
            and isinstance(field["expected_text"], str)
            and field["expected_text"].strip()
            and len(_normalized(field["expected_text"])) >= 2
            and isinstance(field["must_confirm"], bool)
            and isinstance(box, list)
            and len(box) == 4
            and all(isinstance(part, int) and part >= 0 for part in box)
            and box[0] < box[2]
            and box[1] < box[3]
            and box[2] <= 1265
            and box[3] <= 712
        ):
            raise P6ContractError("P6_REAL_OCR_ANNOTATION_INVALID")
        if any(
            not (
                box[2] <= other[0]
                or box[0] >= other[2]
                or box[3] <= other[1]
                or box[1] >= other[3]
            )
            for other in boxes
        ):
            raise P6ContractError("P6_REAL_OCR_ANNOTATION_INVALID")
        field_ids.add(field["field_id"])
        boxes.append(box)
    return value["fields"]


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", value.casefold())


def _lines_for_box(lines: list[Any], box: list[int]) -> list[Any]:
    candidates: list[Any] = []
    for line in lines:
        center_x = (line.box.x_min + line.box.x_max) / 2
        center_y = (line.box.y_min + line.box.y_max) / 2
        if box[0] <= center_x <= box[2] and box[1] <= center_y <= box[3]:
            candidates.append(line)
    return sorted(candidates, key=lambda line: (line.box.y_min, line.box.x_min))


def _perceptual_hash(path: Path) -> str:
    try:
        with Image.open(path) as image:
            pixels = np.asarray(
                image.convert("L").resize((32, 32), Image.Resampling.LANCZOS),
                dtype=np.float64,
            )
    except (OSError, ValueError) as exc:
        raise P6ContractError("P6_REAL_OCR_SOURCE_IMAGE_INVALID") from exc
    size = 32
    positions = np.arange(size, dtype=np.float64)
    frequencies = positions[:, None]
    transform = np.cos(math.pi * (2 * positions + 1) * frequencies / (2 * size))
    transform[0] *= math.sqrt(1 / size)
    transform[1:] *= math.sqrt(2 / size)
    low = (transform @ pixels @ transform.T)[:8, :8].reshape(-1)
    median = float(np.median(low[1:]))
    fingerprint = 0
    for bit in low > median:
        fingerprint = (fingerprint << 1) | int(bit)
    return f"{fingerprint:016x}"


def _git_source_leaks(repo_root: Path, source_paths: list[Path]) -> tuple[int, int, int]:
    try:
        listed = set()
        for args in (
            ("ls-files", "--cached", "--others", "--exclude-standard", "-z"),
            ("ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
        ):
            output = subprocess.run(
                ["git", *args], cwd=repo_root, check=True, capture_output=True
            ).stdout
            listed.update(path for path in output.split(b"\0") if path)
        history_output = subprocess.run(
            ["git", "rev-list", "--objects", "--all"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise P6ContractError("P6_REAL_OCR_GIT_SCAN_FAILED") from exc
    source_bytes = [path.read_bytes() for path in source_paths]
    source_hashes = {hashlib.sha256(value).hexdigest() for value in source_bytes}
    source_sizes = {len(value) for value in source_bytes}
    encoded_markers = [base64.b64encode(value)[:192] for value in source_bytes]
    hits = 0
    files_scanned = 0
    for raw_path in listed:
        try:
            path = repo_root / raw_path.decode("utf-8")
            if not path.is_file():
                continue
            files_scanned += 1
            size = path.stat().st_size
            if size in source_sizes and file_sha256(path) in source_hashes:
                hits += 1
                continue
            if (
                size <= 20 * 1024 * 1024
                and path.suffix.lower()
                in {
                    ".env", ".html", ".js", ".json", ".jsonl", ".log", ".md",
                    ".py", ".text", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
                }
            ):
                content = path.read_bytes()
                hits += int(any(marker in content for marker in encoded_markers))
        except (OSError, UnicodeError):
            raise P6ContractError("P6_REAL_OCR_GIT_SCAN_FAILED")
    history_ids = {line.split(" ", 1)[0] for line in history_output.splitlines() if line}
    history_hits = 0
    for source in source_paths:
        try:
            blob_id = subprocess.run(
                ["git", "hash-object", "--no-filters", str(source)],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise P6ContractError("P6_REAL_OCR_GIT_SCAN_FAILED") from exc
        history_hits += int(blob_id in history_ids)
    return hits + history_hits, files_scanned, len(history_ids)


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


def _application_log_leaks(log_roots: list[Path]) -> tuple[int, int]:
    hits = 0
    files_scanned = 0
    for root in log_roots:
        try:
            resolved = root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise P6ContractError("P6_REAL_OCR_LOG_SCAN_FAILED") from exc
        paths = [resolved] if resolved.is_file() else [path for path in resolved.rglob("*") if path.is_file()]
        for path in paths:
            files_scanned += 1
            try:
                if _contains_image_leak(path.read_bytes()):
                    hits += 1
            except OSError as exc:
                raise P6ContractError("P6_REAL_OCR_LOG_SCAN_FAILED") from exc
    return hits, files_scanned


async def _database_leaks(database_url: str, source_hashes: set[str]) -> tuple[int, int]:
    try:
        import asyncpg

        connection = await asyncpg.connect(database_url)
        try:
            tables = {
                str(row["table_name"])
                for row in await connection.fetch(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                    """
                )
            }
            required_tables = {
                "trip_workspaces", "itinerary_imports", "trip_brief_revisions",
                "trip_temporary_assets", "trip_check_runs", "advice_bundles",
            }
            if not required_tables.issubset(tables):
                raise P6ContractError("P6_REAL_OCR_DATABASE_SCHEMA_INVALID")
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
            return hits, len(columns)
        finally:
            await connection.close()
    except P6ContractError:
        raise
    except Exception as exc:
        raise P6ContractError("P6_REAL_OCR_DATABASE_SCAN_FAILED") from exc


def _private_items(value: dict[str, Any], dataset: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if set(value) != {
        "schema_version", "dataset_manifest_file_sha256", "cross_split_check_receipt_path",
        "authorization_review_receipt_path", "source_license_receipt_paths", "items",
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


def _validate_authorization_review(
    value: dict[str, Any],
    dataset: Mapping[str, Any],
    source_receipt_sha256s: list[str],
    authorization_receipt_sha256s: list[str],
) -> None:
    required = {
        "schema_version", "dataset_id", "subject_commit", "review_input_hash",
        "reviewer_kind", "reviewer_id", "reviewed_at",
        "source_license_receipt_sha256s", "authorization_receipt_set_sha256",
        "item_count", "eligible_count", "rejected_count", "checks", "status",
        "receipt_hash",
    }
    expected_input_hash = digest({
        "dataset_id": dataset["dataset_id"],
        "subject_commit": dataset["subject_commit"],
        "source_license_receipt_sha256s": sorted(source_receipt_sha256s),
        "authorization_receipt_sha256s": sorted(authorization_receipt_sha256s),
        "source_image_sha256s": sorted(
            item["source_image_sha256"] for item in dataset["items"]
        ),
    })
    expected_checks = {
        "official_rightsinfo_verified": True,
        "revision_urls_verified": True,
        "image_hash_bindings_verified": True,
        "attribution_obligations_recorded": True,
        "scope_is_candidate_ocr_only": True,
    }
    if set(value) != required or not (
        value["schema_version"] == "trip-check-p6-ocr-authorization-review-v1"
        and value["dataset_id"] == dataset["dataset_id"]
        and value["subject_commit"] == dataset["subject_commit"]
        and value["review_input_hash"] == expected_input_hash
        and value["reviewer_kind"] == "INDEPENDENT_AGENT_READ_ONLY_REVIEW"
        and re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", value["reviewer_id"])
        and value["source_license_receipt_sha256s"] == sorted(source_receipt_sha256s)
        and value["authorization_receipt_set_sha256"]
        == digest(sorted(authorization_receipt_sha256s))
        and value["item_count"] == 60
        and value["eligible_count"] == 60
        and value["rejected_count"] == 0
        and value["checks"] == expected_checks
        and value["status"] == "PASS"
    ):
        raise P6ContractError("P6_REAL_OCR_AUTHORIZATION_REVIEW_INVALID")
    _parse_past_timestamp(value["reviewed_at"], "P6_REAL_OCR_AUTHORIZATION_REVIEW_INVALID")
    _validate_self_hash(
        value, "receipt_hash", "P6_REAL_OCR_AUTHORIZATION_REVIEW_HASH_MISMATCH"
    )


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
        if engine is not None:
            raise P6ContractError("P6_REAL_OCR_FORMAL_ENGINE_INJECTION_FORBIDDEN")
        if dataset["ocr_config_sha256"] != FORMAL_OCR_CONFIG_SHA256:
            raise P6ContractError("P6_REAL_OCR_CONFIG_BINDING_INVALID")
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
    source_receipt_values = private.get("source_license_receipt_paths")
    if not isinstance(source_receipt_values, list) or len(source_receipt_values) != 3:
        raise P6ContractError("P6_REAL_OCR_SOURCE_LICENSE_INVALID")
    source_receipt_paths = [
        _external_file(value, repo_resolved, "P6_REAL_OCR_SOURCE_LICENSE_INVALID")
        for value in source_receipt_values
    ]
    source_receipt_sha256s = [file_sha256(path) for path in source_receipt_paths]
    if (
        len(set(source_receipt_sha256s)) != 3
        or digest(sorted(source_receipt_sha256s))
        != dataset["source_license_receipt_set_sha256"]
    ):
        raise P6ContractError("P6_REAL_OCR_SOURCE_LICENSE_SET_MISMATCH")
    source_receipts_by_group: dict[str, dict[str, Any]] = {}
    source_receipt_sha_by_group: dict[str, str] = {}
    for path, receipt_sha in zip(
        source_receipt_paths, source_receipt_sha256s, strict=True
    ):
        source_receipt = _load_json(path, "P6_REAL_OCR_SOURCE_LICENSE_INVALID")
        _validate_source_license_receipt(source_receipt)
        source_receipts_by_group[source_receipt["source_group_hash"]] = source_receipt
        source_receipt_sha_by_group[source_receipt["source_group_hash"]] = receipt_sha
    if set(source_receipts_by_group) != {
        item["source_group_hash"] for item in dataset["items"]
    }:
        raise P6ContractError("P6_REAL_OCR_SOURCE_LICENSE_SET_MISMATCH")
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
    authorization_receipt_sha256s = [
        file_sha256(_external_file(
            private_by_id[item["item_id"]]["authorization_receipt_path"],
            repo_resolved,
            "P6_REAL_OCR_AUTHORIZATION_INVALID",
        ))
        for item in dataset["items"]
    ]
    if authorization_receipt_sha256s != [
        item["authorization_receipt_sha256"] for item in dataset["items"]
    ]:
        raise P6ContractError("P6_REAL_OCR_AUTHORIZATION_FILE_HASH_MISMATCH")
    review_path = _external_file(
        private.get("authorization_review_receipt_path"),
        repo_resolved,
        "P6_REAL_OCR_AUTHORIZATION_REVIEW_INVALID",
    )
    if file_sha256(review_path) != dataset["authorization_review_receipt_sha256"]:
        raise P6ContractError("P6_REAL_OCR_AUTHORIZATION_REVIEW_FILE_HASH_MISMATCH")
    _validate_authorization_review(
        _load_json(review_path, "P6_REAL_OCR_AUTHORIZATION_REVIEW_INVALID"),
        dataset,
        source_receipt_sha256s,
        authorization_receipt_sha256s,
    )
    current_engine = engine or PaddleOcrEngine(
        confirmation_threshold=OCR_CONFIRMATION_THRESHOLD
    )
    if formal and (
        getattr(current_engine, "name", None) != "paddleocr"
        or getattr(current_engine, "version", None) != "3.7.0"
        or getattr(current_engine, "confirmation_threshold", None)
        != OCR_CONFIRMATION_THRESHOLD
    ):
        raise P6ContractError("P6_REAL_OCR_ENGINE_BINDING_INVALID")
    run_dir = work_resolved / f"g1-{uuid.uuid4().hex}"
    run_dir.mkdir(parents=True, exist_ok=False)
    tp = fp = fn = must_confirm = confirm_caught = 0
    exact_match_count = field_count = 0
    cleanup_count = 0
    source_hashes: set[str] = set()
    source_paths: list[Path] = []
    perceptual_hashes: list[int] = []
    field_type_counts = {field_type: 0 for field_type in sorted(FIELD_TYPES)}
    field_type_matches = {field_type: 0 for field_type in sorted(FIELD_TYPES)}
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
            if formal and _perceptual_hash(source) != item["perceptual_hash"]:
                raise P6ContractError("P6_REAL_OCR_PERCEPTUAL_HASH_MISMATCH")
            perceptual_hashes.append(int(item["perceptual_hash"], 16))
            for forbidden_root in (output_resolved, work_resolved):
                try:
                    source.relative_to(forbidden_root)
                except ValueError:
                    pass
                else:
                    raise P6ContractError("P6_REAL_OCR_CUSTODY_ROOT_INVALID")
            if file_sha256(authorization_path) != item["authorization_receipt_sha256"]:
                raise P6ContractError("P6_REAL_OCR_AUTHORIZATION_FILE_HASH_MISMATCH")
            if file_sha256(annotation_path) != item["annotation_sha256"]:
                raise P6ContractError("P6_REAL_OCR_ANNOTATION_FILE_HASH_MISMATCH")
            authorization = _load_json(
                authorization_path, "P6_REAL_OCR_AUTHORIZATION_INVALID"
            )
            _validate_authorization(
                authorization, item, set(source_receipt_sha256s)
            )
            if (
                authorization["source_license_receipt_sha256"]
                != source_receipt_sha_by_group[item["source_group_hash"]]
                or source_receipts_by_group[item["source_group_hash"]]["license_url"]
                != authorization["license_url"]
                or source_receipts_by_group[item["source_group_hash"]][
                    "canonical_revision_url"
                ]
                != authorization["canonical_revision_url"]
            ):
                raise P6ContractError("P6_REAL_OCR_AUTHORIZATION_BINDING_INVALID")
            annotation = _load_json(annotation_path, "P6_REAL_OCR_ANNOTATION_INVALID")
            fields = _validate_annotation(annotation, item)
            source_hashes.add(item["source_image_sha256"])
            source_paths.append(source)
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
                selected_lines = _lines_for_box(lines, field["box"])
                expected = _normalized(field["expected_text"])
                predicted = _normalized("".join(line.text for line in selected_lines))
                matched = bool(predicted) and expected == predicted
                expected_counts = Counter(expected)
                predicted_counts = Counter(predicted)
                tp += sum((expected_counts & predicted_counts).values())
                fp += sum((predicted_counts - expected_counts).values())
                fn += sum((expected_counts - predicted_counts).values())
                field_count += 1
                exact_match_count += int(matched)
                field_type_counts[field["field_type"]] += 1
                if matched:
                    field_type_matches[field["field_type"]] += 1
                low_confidence = any(
                    float(line.confidence) < OCR_CONFIRMATION_THRESHOLD
                    for line in selected_lines
                )
                if low_confidence:
                    must_confirm += 1
                    confirm_caught += int(all(
                        line.requires_confirmation
                        for line in selected_lines
                        if float(line.confidence) < OCR_CONFIRMATION_THRESHOLD
                    ))
    finally:
        if run_dir.parent != work_resolved:
            raise P6ContractError("P6_REAL_OCR_WORK_ROOT_INVALID")
        try:
            shutil.rmtree(run_dir, ignore_errors=False)
        except OSError as exc:
            raise P6ContractError("P6_REAL_OCR_CLEANUP_FAILED") from exc
    for index, fingerprint in enumerate(perceptual_hashes):
        if any(
            (fingerprint ^ other).bit_count() <= 4
            for other in perceptual_hashes[index + 1 :]
        ):
            raise P6ContractError("P6_REAL_OCR_DATASET_NEAR_DUPLICATE")
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    micro_f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    confirmation_recall = confirm_caught / must_confirm if must_confirm else 0.0
    git_leaks, git_files_scanned, git_history_objects = _git_source_leaks(
        repo_resolved, source_paths
    )
    log_leaks, log_files_scanned = _application_log_leaks(log_roots or [])
    if database_url:
        database_leaks, database_columns_scanned = await _database_leaks(
            database_url, source_hashes
        )
    else:
        database_leaks = database_columns_scanned = 0
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
        "key_field_exact_match_count": exact_match_count,
        "key_field_count": field_count,
        "key_field_exact_recall": round(
            exact_match_count / field_count if field_count else 0.0,
            6,
        ),
        "must_confirm_field_count": must_confirm,
        "low_confidence_confirmation_recall": round(confirmation_recall, 6),
        "must_confirm_recall": round(confirmation_recall, 6),
        "ocr_config_binding_count": 60,
        "source_license_receipt_count": len(source_receipt_paths),
        "authorization_review_pass_count": 1,
        "git_files_scanned": git_files_scanned,
        "git_history_objects_scanned": git_history_objects,
        "application_log_roots_scanned": len(log_roots or []),
        "application_log_files_scanned": log_files_scanned,
        "database_columns_scanned": database_columns_scanned,
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
    for field_type in sorted(FIELD_TYPES):
        metrics[f"{field_type.lower()}_field_count"] = field_type_counts[field_type]
        metrics[f"{field_type.lower()}_field_match_count"] = field_type_matches[field_type]
    gate_passed = (
        micro_f1 >= 0.95
        and confirmation_recall == 1.0
        and must_confirm > 0
        and cleanup_count == 60
        and metrics["work_root_terminal_file_count"] == 0
        and git_leaks == 0
        and log_leaks == 0
        and database_leaks == 0
    )
    if not gate_passed and formal:
        raise P6ContractError("P6_REAL_OCR_GATE_FAILED")
    receipt = {
        "schema_version": "trip-check-p6-gate-receipt-v1",
        "gate": "g1",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "status": "PASS",
        "evidence_level": "real_authorized_ocr",
        "checks_total": 18,
        "checks_passed": 18,
        "failure_count": 0,
        "metrics": metrics,
    }
    receipt["receipt_hash"] = digest(receipt)
    if not formal:
        diagnostic = {
            "schema_version": "trip-check-p6-g1-contract-fixture-v1",
            "status": (
                "CONTRACT_FIXTURE_PASS" if gate_passed else "CONTRACT_FIXTURE_REJECT"
            ),
            "subject_commit": spec["subject_commit"],
            "run_spec_hash": spec["run_spec_hash"],
            "metrics": metrics,
        }
        if not gate_passed:
            diagnostic["reason_code"] = "P6_REAL_OCR_GATE_FAILED"
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
