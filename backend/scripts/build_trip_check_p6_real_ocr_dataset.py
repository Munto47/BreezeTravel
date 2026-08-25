"""Prepare and finalize the external P6 real-authorized OCR evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evals.trip_check_v1.p6.contracts_v1 import digest, file_sha256  # noqa: E402
from evals.trip_check_v1.p6.real_ocr_runner import (  # noqa: E402
    FORMAL_OCR_CONFIG_SHA256,
    _validate_authorization_review,
)


DATASET_ID = "real_authorized_ocr_v1"
ANNOTATION_VERSION = "wikivoyage-rendered-line-v1"
DOM_ANNOTATION_SCHEMA = "trip-check-p6-rendered-line-annotation-source-v1"
ANNOTATION_UNIT = "BROWSER_RENDERED_TEXT_LINE"
CITY_CONFIG = {
    "beijing": {"label": "北京", "page_title": "北京"},
    "shanghai": {"label": "上海", "page_title": "上海"},
    "hangzhou": {"label": "杭州", "page_title": "杭州"},
}
IMAGE_WIDTH = 1265
IMAGE_HEIGHT = 712
LICENSE_NAME = "Creative Commons Attribution-ShareAlike 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/deed.zh"
API_ROOT = "https://zh.wikivoyage.org/w/api.php"


class DatasetBuildError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json_new(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(_canonical_json(value))
            stream.write("\n")
    except FileExistsError as exc:
        raise DatasetBuildError(f"output already exists: {path}") from exc


def _write_self_hashed(path: Path, value: dict[str, Any], hash_field: str) -> str:
    payload = dict(value)
    payload[hash_field] = digest(payload)
    _write_json_new(path, payload)
    return file_sha256(path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DatasetBuildError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise DatasetBuildError(f"JSON object required: {path}")
    return value


def _verify_self_hash(value: dict[str, Any], hash_field: str, label: str) -> None:
    expected = digest({key: item for key, item in value.items() if key != hash_field})
    if value.get(hash_field) != expected:
        raise DatasetBuildError(f"{label} self-hash mismatch")


def _api_query(params: dict[str, str]) -> tuple[str, dict[str, Any]]:
    url = f"{API_ROOT}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "BreezeTravel-P6-Evidence/1.0 (candidate OCR verification)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            value = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DatasetBuildError("MediaWiki API query failed") from exc
    if not isinstance(value, dict) or "error" in value:
        raise DatasetBuildError("MediaWiki API returned an error")
    return url, value


def _perceptual_hash(path: Path) -> str:
    try:
        with Image.open(path) as image:
            pixels = np.asarray(
                image.convert("L").resize((32, 32), Image.Resampling.LANCZOS),
                dtype=np.float64,
            )
    except OSError as exc:
        raise DatasetBuildError(f"invalid image: {path}") from exc
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


def _validate_boxes(fields: list[dict[str, Any]], item_id: str) -> None:
    if not 3 <= len(fields) <= 6:
        raise DatasetBuildError(f"{item_id} must contain 3 to 6 atomic fields")
    boxes: list[list[int]] = []
    for field in fields:
        box = field.get("box")
        text = field.get("text")
        normalized_text = (
            re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", text.casefold())
            if isinstance(text, str)
            else ""
        )
        if not (
            set(field) == {"box", "color", "field_type", "font_size", "text"}
            and isinstance(box, list)
            and len(box) == 4
            and all(isinstance(part, int) for part in box)
            and 0 <= box[0] < box[2] <= IMAGE_WIDTH
            and 0 <= box[1] < box[3] <= IMAGE_HEIGHT
            and isinstance(text, str)
            and 2 <= len(text.strip()) <= 48
            and len(normalized_text) >= 2
            and field.get("color") == "rendered-line"
            and isinstance(field.get("font_size"), (int, float))
            and 10 <= field["font_size"] <= 48
            and 12 <= box[3] - box[1] <= 48
            and box[2] - box[0] <= max(80, len(normalized_text) * 32)
            and field.get("field_type")
            in {"CITY", "DATE", "HOTEL", "PLACE", "ROUTE_MODE", "TIME"}
        ):
            raise DatasetBuildError(f"invalid atomic field in {item_id}")
        if any(
            not (
                box[2] <= other[0]
                or box[0] >= other[2]
                or box[3] <= other[1]
                or box[1] >= other[3]
            )
            for other in boxes
        ):
            raise DatasetBuildError(f"overlapping atomic fields in {item_id}")
        boxes.append(box)


def _git_head(repo_root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DatasetBuildError("cannot read repository HEAD") from exc


def _source_receipts(output_root: Path, retrieved_at: str) -> tuple[list[Path], dict[str, dict[str, Any]]]:
    rights_url, rights = _api_query({
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "meta": "siteinfo",
        "siprop": "general|rightsinfo",
    })
    rights_info = rights.get("query", {}).get("rightsinfo", {})
    if not (
        isinstance(rights_info, dict)
        and rights_info.get("url", "").startswith(
            "https://creativecommons.org/licenses/by-sa/4.0/"
        )
        and "Attribution" in rights_info.get("text", "")
        and "Share" in rights_info.get("text", "")
    ):
        raise DatasetBuildError("official MediaWiki rightsinfo is not CC BY-SA 4.0")
    result_paths: list[Path] = []
    by_city: dict[str, dict[str, Any]] = {}
    for city_key, config in CITY_CONFIG.items():
        query_url, response = _api_query({
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "info|revisions",
            "titles": config["page_title"],
            "inprop": "url",
            "rvprop": "ids|timestamp|user",
        })
        pages = response.get("query", {}).get("pages", [])
        if not isinstance(pages, list) or len(pages) != 1 or "missing" in pages[0]:
            raise DatasetBuildError(f"source page not found for {city_key}")
        page = pages[0]
        revisions = page.get("revisions")
        if not isinstance(revisions, list) or len(revisions) != 1:
            raise DatasetBuildError(f"source revision missing for {city_key}")
        revision = revisions[0]
        group_hash = digest({
            "site": "zh.wikivoyage.org",
            "page_id": page["pageid"],
            "revision_id": revision["revid"],
        })
        encoded_title = urllib.parse.quote(config["page_title"])
        receipt = {
            "schema_version": "trip-check-p6-open-license-source-receipt-v1",
            "receipt_id": f"source-license-{city_key}",
            "site": "zh.wikivoyage.org",
            "page_id": page["pageid"],
            "page_title": page["title"],
            "revision_id": revision["revid"],
            "revision_timestamp": revision["timestamp"],
            "canonical_revision_url": (
                f"https://zh.wikivoyage.org/w/index.php?title={encoded_title}"
                f"&oldid={revision['revid']}"
            ),
            "history_url": (
                f"https://zh.wikivoyage.org/w/index.php?title={encoded_title}&action=history"
            ),
            "api_query_url": query_url,
            "license_name": LICENSE_NAME,
            "license_url": LICENSE_URL,
            "rightsinfo_api_url": rights_url,
            "attribution_required": True,
            "share_alike_required": True,
            "source_group_hash": group_hash,
            "retrieved_at": retrieved_at,
        }
        path = output_root / "source_receipts" / f"{city_key}.source-license.json"
        receipt_sha = _write_self_hashed(path, receipt, "receipt_hash")
        receipt["file_sha256"] = receipt_sha
        receipt["path"] = str(path.resolve())
        result_paths.append(path)
        by_city[city_key] = receipt
    return result_paths, by_city


def _historical_manifest(repo_root: Path, output_root: Path) -> tuple[Path, list[dict[str, Any]]]:
    try:
        output = subprocess.run(
            ["git", "ls-files", "-z", "*.png", "*.jpg", "*.jpeg", "*.webp"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DatasetBuildError("cannot enumerate historical repository images") from exc
    items: list[dict[str, Any]] = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8")
        path = repo_root / relative
        if not path.is_file():
            continue
        try:
            fingerprint = _perceptual_hash(path)
        except DatasetBuildError:
            fingerprint = None
        items.append({
            "path_sha256": hashlib.sha256(relative.encode("utf-8")).hexdigest(),
            "source_image_sha256": file_sha256(path),
            "perceptual_hash": fingerprint,
        })
    manifest = {
        "schema_version": "trip-check-p6-ocr-historical-split-manifest-v1",
        "source": "git_tracked_raster_assets",
        "subject_commit": _git_head(repo_root),
        "item_count": len(items),
        "items": items,
    }
    path = output_root / "historical_split_manifest.json"
    _write_self_hashed(path, manifest, "manifest_hash")
    return path, items


def prepare(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"[0-9a-f]{40}", args.subject_commit):
        raise DatasetBuildError("subject commit must be a 40-character lowercase SHA")
    if _git_head(args.repo_root) != args.subject_commit:
        raise DatasetBuildError("subject commit must equal repository HEAD")
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=False)
    dom = _load_json(args.dom_annotations)
    if not (
        dom.get("schema_version") == DOM_ANNOTATION_SCHEMA
        and dom.get("annotation_unit") == ANNOTATION_UNIT
    ):
        raise DatasetBuildError("rendered-line DOM annotation binding is invalid")
    viewport = dom.get("viewport")
    if viewport != {
        "dom_width": 1280,
        "dom_height": 720,
        "captured_width": IMAGE_WIDTH,
        "captured_height": IMAGE_HEIGHT,
    }:
        raise DatasetBuildError("DOM annotation viewport binding is invalid")
    cities = dom.get("cities")
    if not isinstance(cities, list) or {city.get("city") for city in cities} != set(CITY_CONFIG):
        raise DatasetBuildError("DOM annotation city set is invalid")
    retrieved_at = _utc_now()
    source_receipt_paths, source_by_city = _source_receipts(output_root, retrieved_at)
    historical_path, historical_items = _historical_manifest(args.repo_root, output_root)
    prepared_items: list[dict[str, Any]] = []
    candidate_hashes: set[str] = set()
    candidate_phashes: list[int] = []
    for city in cities:
        city_key = city["city"]
        dom_items = city.get("items")
        if not isinstance(dom_items, list) or len(dom_items) != 20:
            raise DatasetBuildError(f"{city_key} must contain exactly 20 items")
        source_receipt = source_by_city[city_key]
        for dom_item in dom_items:
            item_id = dom_item.get("item_id")
            if not isinstance(item_id, str) or not re.fullmatch(
                rf"ocr-{city_key}-\d{{2}}", item_id
            ):
                raise DatasetBuildError(f"invalid item id for {city_key}")
            fields = dom_item.get("fields")
            if not isinstance(fields, list):
                raise DatasetBuildError(f"missing fields for {item_id}")
            _validate_boxes(fields, item_id)
            candidates = [
                args.raw_root / city_key / f"{item_id}{suffix}"
                for suffix in (".png", ".jpg", ".jpeg", ".webp")
                if (args.raw_root / city_key / f"{item_id}{suffix}").is_file()
            ]
            if len(candidates) != 1:
                raise DatasetBuildError(f"exactly one source image required for {item_id}")
            source_path = candidates[0]
            try:
                with Image.open(source_path) as source_image:
                    expected_format = {
                        ".png": "PNG",
                        ".jpg": "JPEG",
                        ".jpeg": "JPEG",
                        ".webp": "WEBP",
                    }[source_path.suffix.lower()]
                    valid_shape = (
                        source_image.size == (IMAGE_WIDTH, IMAGE_HEIGHT)
                        and source_image.format == expected_format
                    )
            except OSError as exc:
                raise DatasetBuildError(f"invalid source image: {source_path}") from exc
            if not valid_shape:
                raise DatasetBuildError(f"unexpected image shape or format: {source_path}")
            source_sha = file_sha256(source_path)
            fingerprint = _perceptual_hash(source_path)
            if source_sha in candidate_hashes:
                raise DatasetBuildError("candidate dataset contains an exact duplicate")
            current = int(fingerprint, 16)
            if any((current ^ prior).bit_count() <= 4 for prior in candidate_phashes):
                raise DatasetBuildError("candidate dataset contains a near duplicate")
            candidate_hashes.add(source_sha)
            candidate_phashes.append(current)
            annotation = {
                "schema_version": "trip-check-p6-ocr-annotation-v1",
                "item_id": item_id,
                "source_image_sha256": source_sha,
                "annotation_version": ANNOTATION_VERSION,
                "annotation_unit": ANNOTATION_UNIT,
                "coverage_class": "SELECTED_KEY_FIELDS",
                "ignored_boxes": [[0, 0, IMAGE_WIDTH, IMAGE_HEIGHT]],
                "fields": [
                    {
                        "field_id": f"field-{item_id.removeprefix('ocr-')}-{index:02d}",
                        "field_type": field["field_type"],
                        "expected_text": field["text"].strip(),
                        "must_confirm": False,
                        "box": field["box"],
                    }
                    for index, field in enumerate(fields)
                ],
            }
            annotation_path = output_root / "annotations" / f"{item_id}.annotation.json"
            annotation_sha = _write_self_hashed(annotation_path, annotation, "annotation_hash")
            attribution = (
                f"{source_receipt['page_title']} contributors, Wikivoyage, revision "
                f"{source_receipt['revision_id']}, CC BY-SA 4.0"
            )
            authorization = {
                "schema_version": "trip-check-p6-ocr-authorization-receipt-v2",
                "receipt_id": f"auth-{item_id.removeprefix('ocr-')}",
                "source_image_sha256": source_sha,
                "authorization_basis": "OPEN_LICENSE",
                "authorization_scope": "OCR_CANDIDATE_EVALUATION",
                "provenance_class": "OPEN_LICENSE",
                "source_group_hash": source_receipt["source_group_hash"],
                "source_license_receipt_sha256": source_receipt["file_sha256"],
                "license_url": LICENSE_URL,
                "canonical_revision_url": source_receipt["canonical_revision_url"],
                "attribution_text": attribution,
                "reuse_obligations": ["ATTRIBUTION", "LINK_LICENSE", "SHARE_ALIKE"],
                "status": "ELIGIBLE",
                "verified_at": retrieved_at,
                "valid_until": None,
            }
            authorization_path = output_root / "authorization" / f"{item_id}.authorization.json"
            authorization_sha = _write_self_hashed(
                authorization_path, authorization, "receipt_hash"
            )
            prepared_items.append({
                "item_id": item_id,
                "city": CITY_CONFIG[city_key]["label"],
                "source_path": str(source_path.resolve()),
                "source_image_sha256": source_sha,
                "source_group_hash": source_receipt["source_group_hash"],
                "perceptual_hash": fingerprint,
                "authorization_receipt_id": authorization["receipt_id"],
                "authorization_receipt_path": str(authorization_path.resolve()),
                "authorization_receipt_sha256": authorization_sha,
                "annotation_path": str(annotation_path.resolve()),
                "annotation_sha256": annotation_sha,
            })
    historical_hashes = {item["source_image_sha256"] for item in historical_items}
    historical_phashes = [
        int(item["perceptual_hash"], 16)
        for item in historical_items
        if item["perceptual_hash"] is not None
    ]
    exact_cross = sum(item["source_image_sha256"] in historical_hashes for item in prepared_items)
    near_cross = sum(
        any((int(item["perceptual_hash"], 16) ^ prior).bit_count() <= 4 for prior in historical_phashes)
        for item in prepared_items
    )
    if exact_cross or near_cross:
        raise DatasetBuildError("candidate images overlap a historical repository split")
    source_receipt_sha256s = sorted(file_sha256(path) for path in source_receipt_paths)
    authorization_sha256s = sorted(
        item["authorization_receipt_sha256"] for item in prepared_items
    )
    review_input_hash = digest({
        "dataset_id": DATASET_ID,
        "subject_commit": args.subject_commit,
        "source_license_receipt_sha256s": source_receipt_sha256s,
        "authorization_receipt_sha256s": authorization_sha256s,
        "source_image_sha256s": sorted(
            item["source_image_sha256"] for item in prepared_items
        ),
    })
    prepared = {
        "schema_version": "trip-check-p6-real-ocr-prepared-bundle-v1",
        "dataset_id": DATASET_ID,
        "subject_commit": args.subject_commit,
        "annotation_version": ANNOTATION_VERSION,
        "ocr_config_sha256": FORMAL_OCR_CONFIG_SHA256,
        "prepared_at": retrieved_at,
        "source_license_receipt_paths": [str(path.resolve()) for path in source_receipt_paths],
        "source_license_receipt_sha256s": source_receipt_sha256s,
        "historical_split_manifest_path": str(historical_path.resolve()),
        "historical_split_manifest_sha256": file_sha256(historical_path),
        "cross_split_exact_duplicate_count": exact_cross,
        "cross_split_near_duplicate_count": near_cross,
        "review_input_hash": review_input_hash,
        "items": sorted(prepared_items, key=lambda item: item["item_id"]),
    }
    prepared_path = output_root / "prepared_manifest.json"
    _write_self_hashed(prepared_path, prepared, "manifest_hash")
    review_request = {
        "schema_version": "trip-check-p6-ocr-authorization-review-request-v1",
        "dataset_id": DATASET_ID,
        "subject_commit": args.subject_commit,
        "review_input_hash": review_input_hash,
        "prepared_manifest_file_sha256": file_sha256(prepared_path),
        "required_checks": [
            "official_rightsinfo_verified",
            "revision_urls_verified",
            "image_hash_bindings_verified",
            "attribution_obligations_recorded",
            "scope_is_candidate_ocr_only",
        ],
        "required_reviewer_kind": "INDEPENDENT_AGENT_READ_ONLY_REVIEW",
    }
    _write_self_hashed(output_root / "review_request.json", review_request, "request_hash")
    print(_canonical_json({
        "status": "PREPARED",
        "prepared_manifest": str(prepared_path.resolve()),
        "review_input_hash": review_input_hash,
        "item_count": len(prepared_items),
    }))
    return 0


def finalize(args: argparse.Namespace) -> int:
    prepared_path = args.prepared_manifest.resolve(strict=True)
    prepared = _load_json(prepared_path)
    _verify_self_hash(prepared, "manifest_hash", "prepared manifest")
    if prepared.get("subject_commit") != args.subject_commit:
        raise DatasetBuildError("prepared bundle subject mismatch")
    review_path = args.authorization_review.resolve(strict=True)
    review = _load_json(review_path)
    review_sha = file_sha256(review_path)
    items = prepared.get("items")
    if not isinstance(items, list) or len(items) != 60:
        raise DatasetBuildError("prepared bundle item count is invalid")
    source_receipt_sha256s = prepared["source_license_receipt_sha256s"]
    authorization_sha256s = [item["authorization_receipt_sha256"] for item in items]
    provisional_dataset = {
        "dataset_id": DATASET_ID,
        "subject_commit": args.subject_commit,
        "items": items,
    }
    try:
        _validate_authorization_review(
            review,
            provisional_dataset,
            source_receipt_sha256s,
            authorization_sha256s,
        )
    except Exception as exc:
        raise DatasetBuildError("authorization review receipt is invalid") from exc
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=False)
    cross_split = {
        "schema_version": "trip-check-p6-ocr-cross-split-receipt-v1",
        "dataset_id": DATASET_ID,
        "candidate_item_count": 60,
        "source_hash_set_sha256": digest(sorted(
            item["source_image_sha256"] for item in items
        )),
        "historical_split_manifest_sha256": prepared["historical_split_manifest_sha256"],
        "exact_duplicate_count": prepared["cross_split_exact_duplicate_count"],
        "near_duplicate_count": prepared["cross_split_near_duplicate_count"],
        "algorithm": "SHA256_PLUS_PHASH64_HAMMING_LE_4",
    }
    cross_path = output_root / "cross_split_receipt.json"
    cross_sha = _write_self_hashed(cross_path, cross_split, "receipt_hash")
    dataset_items = [
        {
            "item_id": item["item_id"],
            "city": item["city"],
            "split": "candidate_eval",
            "source_type": "REAL_SCREENSHOT",
            "source_image_sha256": item["source_image_sha256"],
            "source_group_hash": item["source_group_hash"],
            "perceptual_hash": item["perceptual_hash"],
            "provenance_class": "OPEN_LICENSE",
            "authorization_receipt_id": item["authorization_receipt_id"],
            "authorization_basis": "OPEN_LICENSE",
            "authorization_scope": "OCR_CANDIDATE_EVALUATION",
            "authorization_receipt_sha256": item["authorization_receipt_sha256"],
            "annotation_version": prepared["annotation_version"],
            "annotation_sha256": item["annotation_sha256"],
            "ocr_engine": "paddleocr",
            "ocr_engine_version": "3.7.0",
            "ocr_config_sha256": prepared["ocr_config_sha256"],
            "cleanup_policy": "WORK_COPY_TERMINAL_DELETE",
        }
        for item in items
    ]
    dataset = {
        "schema_version": "trip-check-p6-real-ocr-dataset-manifest-v1",
        "dataset_id": DATASET_ID,
        "dataset_version": args.dataset_version,
        "subject_commit": args.subject_commit,
        "annotation_version": prepared["annotation_version"],
        "ocr_config_sha256": prepared["ocr_config_sha256"],
        "authorization_review_receipt_sha256": review_sha,
        "source_license_receipt_set_sha256": digest(sorted(source_receipt_sha256s)),
        "cross_split_check_receipt_sha256": cross_sha,
        "cross_split_exact_duplicate_count": 0,
        "cross_split_near_duplicate_count": 0,
        "item_count": 60,
        "city_counts": {"北京": 20, "上海": 20, "杭州": 20},
        "items": dataset_items,
        "created_at": _utc_now(),
    }
    dataset_path = output_root / "dataset_manifest.json"
    _write_self_hashed(dataset_path, dataset, "manifest_hash")
    private = {
        "schema_version": "trip-check-p6-real-ocr-private-manifest-v1",
        "dataset_manifest_file_sha256": file_sha256(dataset_path),
        "cross_split_check_receipt_path": str(cross_path.resolve()),
        "authorization_review_receipt_path": str(review_path),
        "source_license_receipt_paths": prepared["source_license_receipt_paths"],
        "items": [
            {
                "item_id": item["item_id"],
                "source_path": item["source_path"],
                "authorization_receipt_path": item["authorization_receipt_path"],
                "annotation_path": item["annotation_path"],
            }
            for item in items
        ],
    }
    private_path = output_root / "private_manifest.json"
    _write_json_new(private_path, private)
    attribution = {
        "schema_version": "trip-check-p6-ocr-attribution-manifest-v1",
        "dataset_id": DATASET_ID,
        "license_name": LICENSE_NAME,
        "license_url": LICENSE_URL,
        "reuse_obligations": ["ATTRIBUTION", "LINK_LICENSE", "SHARE_ALIKE"],
        "source_license_receipt_sha256s": source_receipt_sha256s,
        "item_count": 60,
        "human_evidence": False,
    }
    attribution_path = output_root / "attribution_manifest.json"
    _write_self_hashed(attribution_path, attribution, "manifest_hash")
    print(_canonical_json({
        "status": "FINALIZED",
        "dataset_manifest": str(dataset_path.resolve()),
        "dataset_manifest_file_sha256": file_sha256(dataset_path),
        "private_manifest": str(private_path.resolve()),
        "attribution_manifest": str(attribution_path.resolve()),
    }))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--raw-root", type=Path, required=True)
    prepare_parser.add_argument("--dom-annotations", type=Path, required=True)
    prepare_parser.add_argument("--output-root", type=Path, required=True)
    prepare_parser.add_argument("--subject-commit", required=True)
    prepare_parser.add_argument("--repo-root", type=Path, default=BACKEND_ROOT.parent)
    prepare_parser.set_defaults(handler=prepare)
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--prepared-manifest", type=Path, required=True)
    finalize_parser.add_argument("--authorization-review", type=Path, required=True)
    finalize_parser.add_argument("--output-root", type=Path, required=True)
    finalize_parser.add_argument("--subject-commit", required=True)
    finalize_parser.add_argument("--dataset-version", default="candidate-v1")
    finalize_parser.set_defaults(handler=finalize)
    args = parser.parse_args()
    try:
        return args.handler(args)
    except DatasetBuildError as exc:
        print(_canonical_json({"status": "REJECT", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
