from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.trip_check_v1.p6.contracts_v1 import (
    P5_GATE_MANIFEST_HASH,
    P6ContractError,
    digest,
    file_sha256,
    validate_gate_receipt,
)
from evals.trip_check_v1.p6.real_ocr_runner import (
    FORMAL_OCR_CONFIG_SHA256,
    _validate_annotation,
    run_real_authorized_ocr,
)


SUBJECT = "a" * 40
CITIES = ("北京", "上海", "杭州")


def _write_hashed(path: Path, payload: dict, hash_field: str) -> str:
    value = dict(payload)
    value[hash_field] = digest(value)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return file_sha256(path)


def _run_spec(dataset_file_sha: str) -> dict:
    value = {
        "schema_version": "trip-check-p6-candidate-run-spec-v1",
        "subject_commit": SUBJECT,
        "upstream_ref": "origin/codex/trip-check-p6-candidate-evidence",
        "upstream_commit": SUBJECT,
        "dirty_tree": False,
        "p5_gate_manifest_hash": P5_GATE_MANIFEST_HASH,
        "scope": {
            "cities": ["北京", "上海", "杭州"],
            "single_city": True,
            "group_size": {"min": 2, "max": 5},
            "trip_days": {"min": 2, "max": 5},
            "input_types": ["TEXT", "SCREENSHOT"],
        },
        "bindings": {
            "config_sha256": "1" * 64,
            "ocr_dataset_manifest_sha256": dataset_file_sha,
            "model_manifest_sha256": "2" * 64,
            "rule_manifest_sha256": "3" * 64,
            "snapshot_manifest_sha256": "4" * 64,
            "migration_manifest_sha256": "5" * 64,
        },
        "provider_live_matrix": {
            "max_calls": 18,
            "amap_route_calls": 12,
            "qweather_forecast_calls": 3,
            "qweather_alert_calls": 3,
            "retry_budget": 0,
            "fixture_fallback_required_zero": True,
        },
        "database": {
            "engine": "postgresql",
            "required_migration": "024_advice_bundles.sql",
            "isolated": True,
            "migration_hash_readback_required": True,
        },
        "public_candidate": {
            "base_url": "https://www.breezetravel.cn",
            "controlled_snapshot_only": True,
            "health_path": "/health",
            "evidence_path": "/api/evidence/latest",
        },
        "evidence_root": (
            "D:/munto/code/claudeProject/agentTravel-p6-artifacts/"
            f"p6-candidate/{SUBJECT}"
        ),
        "human_evidence": False,
    }
    value["run_spec_hash"] = digest(value)
    return value


def _fixture(tmp_path: Path) -> dict[str, Path]:
    repo_root = tmp_path / "repo"
    custody = tmp_path / "custody"
    output = tmp_path / "output"
    work = tmp_path / "work"
    repo_root.mkdir()
    custody.mkdir()
    output.mkdir()
    work.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo_root, check=True)
    (repo_root / "safe.txt").write_text("not an image", encoding="utf-8")
    subprocess.run(["git", "add", "safe.txt"], cwd=repo_root, check=True)
    source_receipt_paths = []
    source_receipt_by_city = {}
    for city_index, city in enumerate(CITIES):
        source_group_hash = digest({
            "site": "zh.wikivoyage.org",
            "page_id": city_index + 1,
            "revision_id": city_index + 100,
        })
        source_receipt = custody / f"source-{city_index}.json"
        source_receipt_sha = _write_hashed(source_receipt, {
            "schema_version": "trip-check-p6-open-license-source-receipt-v1",
            "receipt_id": f"source-license-{city_index}",
            "site": "zh.wikivoyage.org",
            "page_id": city_index + 1,
            "page_title": city,
            "revision_id": city_index + 100,
            "revision_timestamp": "2025-01-01T00:00:00Z",
            "canonical_revision_url": (
                f"https://zh.wikivoyage.org/w/index.php?title={city}&oldid={city_index + 100}"
            ),
            "history_url": f"https://zh.wikivoyage.org/w/index.php?title={city}&action=history",
            "api_query_url": "https://zh.wikivoyage.org/w/api.php?action=query",
            "license_name": "Creative Commons Attribution-ShareAlike 4.0",
            "license_url": "https://creativecommons.org/licenses/by-sa/4.0/deed.zh",
            "rightsinfo_api_url": "https://zh.wikivoyage.org/w/api.php?action=query&meta=siteinfo",
            "attribution_required": True,
            "share_alike_required": True,
            "source_group_hash": source_group_hash,
            "retrieved_at": "2026-01-01T00:00:00Z",
        }, "receipt_hash")
        source_receipt_paths.append(source_receipt)
        source_receipt_by_city[city] = (source_group_hash, source_receipt_sha)
    items = []
    private_items = []
    for city_index, city in enumerate(CITIES):
        for item_index in range(20):
            serial = city_index * 20 + item_index
            item_id = f"ocr-item-{serial:02d}"
            source = custody / f"{item_id}.png"
            source.write_bytes(b"fixture-image-" + str(serial).encode())
            source_sha = file_sha256(source)
            authorization = custody / f"{item_id}.authorization.json"
            source_group_hash, source_receipt_sha = source_receipt_by_city[city]
            authorization_sha = _write_hashed(authorization, {
                "schema_version": "trip-check-p6-ocr-authorization-receipt-v2",
                "receipt_id": f"auth-item-{serial:02d}",
                "source_image_sha256": source_sha,
                "authorization_basis": "OPEN_LICENSE",
                "authorization_scope": "OCR_CANDIDATE_EVALUATION",
                "provenance_class": "OPEN_LICENSE",
                "source_group_hash": source_group_hash,
                "source_license_receipt_sha256": source_receipt_sha,
                "license_url": "https://creativecommons.org/licenses/by-sa/4.0/deed.zh",
                "canonical_revision_url": (
                    f"https://zh.wikivoyage.org/w/index.php?title={city}"
                    f"&oldid={city_index + 100}"
                ),
                "attribution_text": "Wikivoyage contributors, CC BY-SA 4.0",
                "reuse_obligations": ["ATTRIBUTION", "LINK_LICENSE", "SHARE_ALIKE"],
                "status": "ELIGIBLE",
                "verified_at": "2026-01-01T00:00:00Z",
                "valid_until": None,
            }, "receipt_hash")
            annotation = custody / f"{item_id}.annotation.json"
            annotation_sha = _write_hashed(annotation, {
                "schema_version": "trip-check-p6-ocr-annotation-v1",
                "item_id": item_id,
                "source_image_sha256": source_sha,
                "annotation_version": "annotations-v1",
                "annotation_unit": (
                    "BROWSER_PARENT_BOUND_BLOCK_LINE_WITH_FROZEN_SEGMENT_FALLBACK_V3"
                ),
                "coverage_class": "SELECTED_KEY_FIELDS",
                "ignored_boxes": [[0, 0, 1265, 712]],
                "fields": [
                    {
                        "field_id": f"field-{serial:02d}-{index}",
                        "field_type": "PLACE",
                        "expected_text": f"field-{serial:02d}-{index}",
                        "must_confirm": False,
                        "box": [index * 110, 0, index * 110 + 100, 100],
                    }
                    for index in range(3)
                ],
            }, "annotation_hash")
            fingerprint = digest({"item_id": item_id})[:16]
            items.append({
                "item_id": item_id,
                "city": city,
                "split": "candidate_eval",
                "source_type": "REAL_SCREENSHOT",
                "source_image_sha256": source_sha,
                "source_group_hash": source_group_hash,
                "perceptual_hash": fingerprint,
                "provenance_class": "OPEN_LICENSE",
                "authorization_receipt_id": f"auth-item-{serial:02d}",
                "authorization_basis": "OPEN_LICENSE",
                "authorization_scope": "OCR_CANDIDATE_EVALUATION",
                "authorization_receipt_sha256": authorization_sha,
                "annotation_version": "annotations-v1",
                "annotation_sha256": annotation_sha,
                "ocr_engine": "paddleocr",
                "ocr_engine_version": "3.7.0",
                "ocr_config_sha256": FORMAL_OCR_CONFIG_SHA256,
                "cleanup_policy": "WORK_COPY_TERMINAL_DELETE",
            })
            private_items.append({
                "item_id": item_id,
                "source_path": str(source.resolve()),
                "authorization_receipt_path": str(authorization.resolve()),
                "annotation_path": str(annotation.resolve()),
            })
    cross_split_path = custody / "cross-split.json"
    cross_split_sha = _write_hashed(cross_split_path, {
        "schema_version": "trip-check-p6-ocr-cross-split-receipt-v1",
        "dataset_id": "real_authorized_ocr_v1",
        "candidate_item_count": 60,
        "source_hash_set_sha256": digest(sorted(
            item["source_image_sha256"] for item in items
        )),
        "historical_split_manifest_sha256": "9" * 64,
        "exact_duplicate_count": 0,
        "near_duplicate_count": 0,
        "algorithm": "SHA256_PLUS_PHASH64_HAMMING_LE_4",
    }, "receipt_hash")
    source_receipt_sha256s = sorted(file_sha256(path) for path in source_receipt_paths)
    authorization_receipt_sha256s = [
        item["authorization_receipt_sha256"] for item in items
    ]
    review_input_hash = digest({
        "dataset_id": "real_authorized_ocr_v1",
        "subject_commit": SUBJECT,
        "source_license_receipt_sha256s": source_receipt_sha256s,
        "authorization_receipt_sha256s": sorted(authorization_receipt_sha256s),
        "source_image_sha256s": sorted(item["source_image_sha256"] for item in items),
    })
    authorization_review_path = custody / "authorization-review.json"
    authorization_review_sha = _write_hashed(authorization_review_path, {
        "schema_version": "trip-check-p6-ocr-authorization-review-v1",
        "dataset_id": "real_authorized_ocr_v1",
        "subject_commit": SUBJECT,
        "review_input_hash": review_input_hash,
        "reviewer_kind": "INDEPENDENT_AGENT_READ_ONLY_REVIEW",
        "reviewer_id": "fixture-independent-review",
        "reviewed_at": "2026-01-01T00:00:00Z",
        "source_license_receipt_sha256s": source_receipt_sha256s,
        "authorization_receipt_set_sha256": digest(sorted(authorization_receipt_sha256s)),
        "item_count": 60,
        "eligible_count": 60,
        "rejected_count": 0,
        "checks": {
            "official_rightsinfo_verified": True,
            "revision_urls_verified": True,
            "image_hash_bindings_verified": True,
            "attribution_obligations_recorded": True,
            "scope_is_candidate_ocr_only": True,
        },
        "status": "PASS",
    }, "receipt_hash")
    dataset_path = custody / "dataset.json"
    dataset = {
        "schema_version": "trip-check-p6-real-ocr-dataset-manifest-v1",
        "dataset_id": "real_authorized_ocr_v1",
        "dataset_version": "candidate-v1",
        "subject_commit": SUBJECT,
        "annotation_version": "annotations-v1",
        "ocr_config_sha256": FORMAL_OCR_CONFIG_SHA256,
        "authorization_review_receipt_sha256": authorization_review_sha,
        "source_license_receipt_set_sha256": digest(source_receipt_sha256s),
        "cross_split_check_receipt_sha256": cross_split_sha,
        "cross_split_exact_duplicate_count": 0,
        "cross_split_near_duplicate_count": 0,
        "item_count": 60,
        "city_counts": {"北京": 20, "上海": 20, "杭州": 20},
        "items": items,
        "created_at": "2026-08-25T00:00:00Z",
    }
    _write_hashed(dataset_path, dataset, "manifest_hash")
    run_spec_path = custody / "run-spec.json"
    run_spec_path.write_text(
        json.dumps(_run_spec(file_sha256(dataset_path)), sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    private_path = custody / "private.json"
    private_path.write_text(json.dumps({
        "schema_version": "trip-check-p6-real-ocr-private-manifest-v1",
        "dataset_manifest_file_sha256": file_sha256(dataset_path),
        "cross_split_check_receipt_path": str(cross_split_path.resolve()),
        "authorization_review_receipt_path": str(authorization_review_path.resolve()),
        "source_license_receipt_paths": [
            str(path.resolve()) for path in source_receipt_paths
        ],
        "items": private_items,
    }), encoding="utf-8")
    return {
        "repo_root": repo_root,
        "output_root": output,
        "work_root": work,
        "candidate_run_spec_path": run_spec_path,
        "dataset_manifest_path": dataset_path,
        "private_manifest_path": private_path,
    }


class PassingEngine:
    async def recognize(self, image_path: Path):
        prefix = image_path.stem.replace("ocr-item-", "field-")
        return [
            SimpleNamespace(
                text=f"{prefix}-{index}",
                confidence=0.5,
                requires_confirmation=True,
                box=SimpleNamespace(
                    x_min=index * 110 + 10,
                    y_min=10,
                    x_max=index * 110 + 90,
                    y_max=90,
                ),
            )
            for index in range(3)
        ]


class FailingEngine:
    async def recognize(self, image_path: Path):
        raise RuntimeError(f"failed on {image_path.name}")


class SubstringEngine(PassingEngine):
    async def recognize(self, image_path: Path):
        lines = await super().recognize(image_path)
        lines[0].text = f"prefix-{lines[0].text}-suffix"
        return lines


class SingleFieldTypoEngine(PassingEngine):
    async def recognize(self, image_path: Path):
        lines = await super().recognize(image_path)
        if image_path.stem == "ocr-item-00":
            lines[0].text = f"{lines[0].text[:-1]}x"
        return lines


def test_real_ocr_annotation_rejects_non_rendered_line_unit():
    item = {
        "item_id": "ocr-beijing-01",
        "source_image_sha256": "a" * 64,
        "annotation_version": "annotations-v1",
    }
    annotation = {
        "schema_version": "trip-check-p6-ocr-annotation-v1",
        "item_id": item["item_id"],
        "source_image_sha256": item["source_image_sha256"],
        "annotation_version": item["annotation_version"],
        "annotation_unit": "DOM_PARENT_ELEMENT",
        "coverage_class": "SELECTED_KEY_FIELDS",
        "ignored_boxes": [[0, 0, 1265, 712]],
        "fields": [
            {
                "field_id": f"field-{index}",
                "field_type": "PLACE",
                "expected_text": f"place-{index}",
                "must_confirm": False,
                "box": [index * 110, 0, index * 110 + 100, 100],
            }
            for index in range(3)
        ],
    }
    annotation["annotation_hash"] = digest(annotation)

    with pytest.raises(P6ContractError) as raised:
        _validate_annotation(annotation, item)
    assert raised.value.reason_code == "P6_REAL_OCR_ANNOTATION_INVALID"


@pytest.mark.asyncio
async def test_real_ocr_runner_scores_atomic_fields_and_deletes_all_work_copies(tmp_path):
    paths = _fixture(tmp_path)

    receipt = await run_real_authorized_ocr(**paths, engine=PassingEngine(), formal=False)

    assert receipt["status"] == "CONTRACT_FIXTURE_PASS"
    assert receipt["metrics"]["key_field_micro_f1"] == 1.0
    assert receipt["metrics"]["key_field_exact_recall"] == 1.0
    assert receipt["metrics"]["key_field_exact_match_count"] == 180
    assert receipt["metrics"]["key_field_count"] == 180
    assert receipt["metrics"]["low_confidence_confirmation_recall"] == 1.0
    assert receipt["metrics"]["work_copy_cleanup_count"] == 60
    assert list(paths["work_root"].rglob("*")) == []
    assert not (paths["output_root"] / "g1_receipt.json").exists()
    output_text = (paths["output_root"] / "g1_contract_fixture.json").read_text(
        encoding="utf-8"
    )
    assert "source_path" not in output_text
    assert "expected_text" not in output_text
    run_spec = json.loads(paths["candidate_run_spec_path"].read_text(encoding="utf-8"))
    with pytest.raises(P6ContractError):
        validate_gate_receipt(receipt, "g1", run_spec)


@pytest.mark.asyncio
async def test_real_ocr_runner_deletes_work_copy_when_engine_fails(tmp_path):
    paths = _fixture(tmp_path)

    with pytest.raises(P6ContractError) as raised:
        await run_real_authorized_ocr(**paths, engine=FailingEngine(), formal=False)
    assert raised.value.reason_code == "P6_REAL_OCR_ENGINE_FAILED"
    assert list(paths["work_root"].rglob("*")) == []
    assert not (paths["output_root"] / "g1_receipt.json").exists()


@pytest.mark.asyncio
async def test_real_ocr_runner_rejects_substring_only_field_matches(tmp_path):
    paths = _fixture(tmp_path)

    receipt = await run_real_authorized_ocr(
        **paths, engine=SubstringEngine(), formal=False
    )
    assert receipt["status"] == "CONTRACT_FIXTURE_REJECT"
    assert receipt["reason_code"] == "P6_REAL_OCR_GATE_FAILED"


@pytest.mark.asyncio
async def test_real_ocr_runner_uses_character_micro_f1_and_discloses_exact_recall(
    tmp_path,
):
    paths = _fixture(tmp_path)

    receipt = await run_real_authorized_ocr(
        **paths,
        engine=SingleFieldTypoEngine(),
        formal=False,
    )

    assert receipt["status"] == "CONTRACT_FIXTURE_PASS"
    assert receipt["metrics"]["key_field_micro_f1"] > 0.99
    assert receipt["metrics"]["key_field_exact_match_count"] == 179
    assert receipt["metrics"]["key_field_count"] == 180
    assert receipt["metrics"]["key_field_exact_recall"] == pytest.approx(179 / 180, abs=1e-6)


@pytest.mark.asyncio
async def test_real_ocr_runner_rejects_application_log_image_leak(tmp_path):
    paths = _fixture(tmp_path)
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "app.log").write_text("payload=data:image/png;base64,iVBORw0KGgo", encoding="utf-8")

    receipt = await run_real_authorized_ocr(
        **paths,
        engine=PassingEngine(),
        formal=False,
        log_roots=[log_root],
    )
    assert receipt["status"] == "CONTRACT_FIXTURE_REJECT"
    assert receipt["reason_code"] == "P6_REAL_OCR_GATE_FAILED"
    assert not (paths["output_root"] / "g1_receipt.json").exists()


@pytest.mark.asyncio
async def test_formal_real_ocr_requires_database_and_log_scan_inputs(tmp_path):
    paths = _fixture(tmp_path)

    with pytest.raises(P6ContractError) as raised:
        await run_real_authorized_ocr(**paths, formal=True)
    assert raised.value.reason_code == "P6_REAL_OCR_PRIVACY_SCAN_INPUTS_REQUIRED"


@pytest.mark.asyncio
async def test_formal_real_ocr_forbids_injected_engine(tmp_path):
    paths = _fixture(tmp_path)

    with pytest.raises(P6ContractError) as raised:
        await run_real_authorized_ocr(**paths, engine=PassingEngine(), formal=True)
    assert raised.value.reason_code == "P6_REAL_OCR_FORMAL_ENGINE_INJECTION_FORBIDDEN"
