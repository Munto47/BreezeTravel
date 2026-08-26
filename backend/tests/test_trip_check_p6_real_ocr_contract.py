from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from evals.trip_check_v1.p6.contracts_v1 import (
    P5_GATE_MANIFEST_HASH,
    P6ContractError,
    digest,
    validate_real_ocr_dataset_binding,
    validate_real_ocr_dataset_manifest,
)


SUBJECT = "a" * 40
CITIES = ("北京", "上海", "杭州")


def _run_spec() -> dict:
    payload = {
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
            "ocr_dataset_manifest_sha256": "e" * 64,
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
            "required_migration": "025_miniapp_identity_and_upload_batches.sql",
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
    payload["run_spec_hash"] = digest(payload)
    return payload


def _manifest() -> dict:
    items = []
    for city_index, city in enumerate(CITIES):
        for item_index in range(20):
            serial = city_index * 20 + item_index
            item_id = f"ocr-item-{serial:02d}"
            fingerprints = hashlib.sha256(f"fingerprint:{item_id}".encode()).hexdigest()[:16]
            items.append({
                "item_id": item_id,
                "city": city,
                "split": "candidate_eval",
                "source_type": "REAL_SCREENSHOT",
                "source_image_sha256": hashlib.sha256(f"image:{item_id}".encode()).hexdigest(),
                "source_group_hash": hashlib.sha256(f"group:{item_id}".encode()).hexdigest(),
                "perceptual_hash": fingerprints,
                "provenance_class": "RIGHTSHOLDER_OWNED",
                "authorization_receipt_id": f"auth-item-{serial:02d}",
                "authorization_basis": "RIGHTSHOLDER_ATTESTATION",
                "authorization_scope": "OCR_CANDIDATE_EVALUATION",
                "authorization_receipt_sha256": hashlib.sha256(
                    f"authorization:{item_id}".encode()
                ).hexdigest(),
                "annotation_version": "annotations-v1",
                "annotation_sha256": hashlib.sha256(f"annotation:{item_id}".encode()).hexdigest(),
                "ocr_engine": "paddleocr",
                "ocr_engine_version": "3.7.0",
                "ocr_config_sha256": "b" * 64,
                "cleanup_policy": "WORK_COPY_TERMINAL_DELETE",
            })
    payload = {
        "schema_version": "trip-check-p6-real-ocr-dataset-manifest-v1",
        "dataset_id": "real_authorized_ocr_v1",
        "dataset_version": "candidate-v1",
        "subject_commit": SUBJECT,
        "annotation_version": "annotations-v1",
        "ocr_config_sha256": "b" * 64,
        "authorization_review_receipt_sha256": "a" * 64,
        "source_license_receipt_set_sha256": "d" * 64,
        "cross_split_check_receipt_sha256": "c" * 64,
        "cross_split_exact_duplicate_count": 0,
        "cross_split_near_duplicate_count": 0,
        "item_count": 60,
        "city_counts": {"北京": 20, "上海": 20, "杭州": 20},
        "items": items,
        "created_at": "2026-08-25T00:00:00Z",
    }
    payload["manifest_hash"] = digest(payload)
    return payload


def _rehash(payload: dict) -> dict:
    payload["manifest_hash"] = digest({key: value for key, value in payload.items() if key != "manifest_hash"})
    return payload


def test_real_ocr_manifest_accepts_only_complete_balanced_sanitized_dataset():
    payload = _manifest()

    assert validate_real_ocr_dataset_manifest(payload)["city_counts"] == {
        "北京": 20,
        "上海": 20,
        "杭州": 20,
    }


def test_real_ocr_manifest_allows_items_to_share_a_licensed_source_page():
    payload = _manifest()
    for city_index in range(3):
        group_hash = hashlib.sha256(f"licensed-page:{city_index}".encode()).hexdigest()
        for item in payload["items"][city_index * 20 : (city_index + 1) * 20]:
            item["source_group_hash"] = group_hash
    _rehash(payload)

    assert validate_real_ocr_dataset_manifest(payload)["item_count"] == 60


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda value: value["items"].pop(), "P6_REAL_OCR_DATASET_SCHEMA_INVALID"),
        (
            lambda value: value["items"][0].update(source_type="SYNTHETIC_SCREENSHOT"),
            "P6_REAL_OCR_DATASET_SCHEMA_INVALID",
        ),
        (
            lambda value: value["items"][0].update(authorization_scope="OCR_DEVELOPMENT"),
            "P6_REAL_OCR_DATASET_SCHEMA_INVALID",
        ),
        (
            lambda value: value["items"][0].update(provenance_class="OPEN_LICENSE"),
            "P6_REAL_OCR_AUTHORIZATION_BINDING_INVALID",
        ),
        (
            lambda value: value["items"][0].update(source_path="D:/private/source.png"),
            "P6_REAL_OCR_DATASET_SCHEMA_INVALID",
        ),
        (
            lambda value: value["items"][1].update(
                source_image_sha256=value["items"][0]["source_image_sha256"]
            ),
            "P6_REAL_OCR_DATASET_DUPLICATE",
        ),
        (
            lambda value: value["items"][1].update(
                perceptual_hash=value["items"][0]["perceptual_hash"][:-1]
                + format(int(value["items"][0]["perceptual_hash"][-1], 16) ^ 1, "x")
            ),
            "P6_REAL_OCR_DATASET_NEAR_DUPLICATE",
        ),
        (
            lambda value: value["items"][0].update(annotation_version="annotations-v2"),
            "P6_REAL_OCR_ANNOTATION_BINDING_INVALID",
        ),
    ],
)
def test_real_ocr_manifest_rejects_missing_fake_leaking_or_unbound_items(mutate, reason):
    payload = deepcopy(_manifest())
    mutate(payload)
    _rehash(payload)

    with pytest.raises(P6ContractError) as raised:
        validate_real_ocr_dataset_manifest(payload)
    assert raised.value.reason_code == reason


def test_real_ocr_manifest_hash_is_fail_closed():
    payload = _manifest()
    payload["dataset_version"] = "candidate-v2"

    with pytest.raises(P6ContractError) as raised:
        validate_real_ocr_dataset_manifest(payload)
    assert raised.value.reason_code == "P6_REAL_OCR_DATASET_HASH_MISMATCH"


def test_real_ocr_manifest_must_bind_candidate_run_spec_file_hash():
    payload = _manifest()
    spec = _run_spec()
    spec["bindings"]["ocr_dataset_manifest_sha256"] = "e" * 64
    spec["run_spec_hash"] = digest(
        {key: value for key, value in spec.items() if key != "run_spec_hash"}
    )

    assert validate_real_ocr_dataset_binding(payload, spec, "e" * 64) == payload
    with pytest.raises(P6ContractError) as raised:
        validate_real_ocr_dataset_binding(payload, spec, "f" * 64)
    assert raised.value.reason_code == "P6_REAL_OCR_RUN_SPEC_BINDING_INVALID"
