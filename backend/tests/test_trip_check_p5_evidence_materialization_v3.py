from __future__ import annotations

from copy import deepcopy

import pytest

from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.evidence_materialization_v3 import (
    _receipt_semantic_payload,
    build_evidence_materialization_v3,
    validate_evidence_materialization_v3,
)
from evals.trip_check_v1.p5.semantic_contract_v3 import validate_case_semantics_v3


def _case(raw_text: str, *, requires_resolution: bool) -> dict:
    product_input = {"source_type": "MANUAL_TEXT", "raw_text": raw_text}
    return {
        "case_id": "p5.v3.materialization.001",
        "city": "北京",
        "trip_days": 3,
        "group_size": 4,
        "input_kind": "TEXT",
        "product_input": product_input,
        "normalized_input_sha256": digest(product_input),
        "runner_control": {
            "provider_snapshot_id": "trip-check-p5-controlled-snapshot-v3",
            "fault_profile_id": "advice_completeness",
            "candidate_set_mode": "VALID",
            "evidence_freshness": "FRESH",
            "unknown_required": False,
            "fault_registry_version": "trip-check-p5-fault-registry-v2",
            "budget_profile": "p5-zero-api-v2",
            "seed": 20260823,
        },
        "oracle": {"requires_user_resolution": requires_resolution},
    }


def _label_free(case: dict) -> dict:
    return {key: value for key, value in case.items() if key not in {"oracle", "oracle_sha256"}}


def test_v3_materializer_closes_all_valid_pilot_places_without_oracle_input() -> None:
    case = _case(
        "北京4人住王府井。第1天 08:30 天安门广场，10:00 故宫博物院；"
        "第2天 07:30 长城（八达岭）；第3天 09:00 天坛公园。",
        requires_resolution=False,
    )

    materialization = build_evidence_materialization_v3(
        {key: value for key, value in case.items() if key != "oracle"}
    )

    assert [item["outcome"] for item in materialization["source_payload"]["entity_resolutions"]] == [
        "AUTO_RESOLVED",
        "AUTO_RESOLVED",
        "AUTO_RESOLVED",
        "AUTO_RESOLVED",
    ]
    assert all(
        receipt["source_url"].startswith("fixture://trip-check-p5-v3/")
        for receipt in materialization["receipts"]
    )
    assert validate_case_semantics_v3(_label_free(case), materialization) == []


def test_v3_materializer_rejects_oracle_bearing_input() -> None:
    case = _case(
        "北京4人。第1天 09:00 故宫博物院；第2天 09:00 颐和园。",
        requires_resolution=False,
    )

    with pytest.raises(ValueError, match="forbidden field"):
        build_evidence_materialization_v3(case)


def test_v3_materializer_proves_ambiguity_without_reading_oracle() -> None:
    case = _case(
        "北京3人。第1天 09:00 博物馆，14:00 景山公园；第2天 09:00 颐和园。",
        requires_resolution=True,
    )

    materialization = build_evidence_materialization_v3(
        {key: value for key, value in case.items() if key != "oracle"}
    )

    first = materialization["source_payload"]["entity_resolutions"][0]
    assert first["outcome"] == "NEEDS_CONFIRMATION"
    assert len(first["candidates"]) == 2
    assert validate_case_semantics_v3(_label_free(case), materialization) == []


def test_v3_materializer_hard_rejects_cross_city_place_with_receipt() -> None:
    case = _case(
        "北京2人。第1天 09:00 东方明珠广播电视塔；第2天 09:00 故宫博物院。",
        requires_resolution=True,
    )

    materialization = build_evidence_materialization_v3(
        {key: value for key, value in case.items() if key != "oracle"}
    )

    first = materialization["source_payload"]["entity_resolutions"][0]
    assert first["outcome"] == "HARD_REJECTED"
    assert first["candidates"][0]["city"] == "上海"
    assert validate_case_semantics_v3(_label_free(case), materialization) == []


def test_v3_materializer_honors_blocked_candidate_mode() -> None:
    case = _case(
        "北京4人。第1天 09:00 故宫博物院，14:00 景山公园；第2天 09:00 颐和园。",
        requires_resolution=True,
    )
    case["runner_control"]["fault_profile_id"] = "empty_candidate_set"
    case["runner_control"]["candidate_set_mode"] = "EMPTY"

    materialization = build_evidence_materialization_v3(
        {key: value for key, value in case.items() if key != "oracle"}
    )

    assert materialization["candidate_sets"] == []
    assert not any(item["operation"] == "route.candidate" for item in materialization["receipts"])
    assert validate_case_semantics_v3(_label_free(case), materialization) == []


def test_v3_materializer_rejects_tampered_resolution_receipt() -> None:
    case = _case(
        "北京4人。第1天 09:00 故宫博物院；第2天 09:00 颐和园。",
        requires_resolution=False,
    )
    materialization = build_evidence_materialization_v3(
        {key: value for key, value in case.items() if key != "oracle"}
    )
    materialization["receipts"][0]["request_hash"] = "0" * 64
    materialization["evidence_materialization_hash"] = digest(
        {
            key: value
            for key, value in materialization.items()
            if key != "evidence_materialization_hash"
        }
    )

    try:
        validate_evidence_materialization_v3(materialization)
    except ValueError as exc:
        assert "receipt semantic hash mismatch" in str(exc)
    else:
        raise AssertionError("tampered v3 receipt must fail closed")


def test_v3_materializer_rejects_tampered_top_hash_and_control_ids() -> None:
    case = _case(
        "北京4人。第1天 09:00 故宫博物院；第2天 09:00 颐和园。",
        requires_resolution=False,
    )
    materialization = build_evidence_materialization_v3(
        {key: value for key, value in case.items() if key != "oracle"}
    )
    materialization["evidence_materialization_hash"] = "0" * 64

    with pytest.raises(ValueError, match="materialization hash mismatch"):
        validate_evidence_materialization_v3(materialization)

    bad_case = {key: value for key, value in case.items() if key != "oracle"}
    bad_case["runner_control"] = {
        **bad_case["runner_control"],
        "provider_snapshot_id": "trip-check-p5-controlled-snapshot-v2",
    }
    with pytest.raises(ValueError, match="frozen v3 snapshot"):
        build_evidence_materialization_v3(bad_case)


def test_v3_validator_rejects_tampered_snapshot_and_source_lineage() -> None:
    case = _case(
        "北京4人。第1天 09:00 故宫博物院；第2天 09:00 颐和园。",
        requires_resolution=False,
    )
    original = build_evidence_materialization_v3(
        {key: value for key, value in case.items() if key != "oracle"}
    )
    snapshot_tamper = deepcopy(original)
    snapshot_tamper["provider_snapshot"]["artifact_id"] = "old-v2"
    snapshot_tamper["provider_snapshot"]["content_sha256"] = digest(
        {
            key: value
            for key, value in snapshot_tamper["provider_snapshot"].items()
            if key != "content_sha256"
        }
    )
    snapshot_tamper["evidence_materialization_hash"] = digest(
        {
            key: value
            for key, value in snapshot_tamper.items()
            if key != "evidence_materialization_hash"
        }
    )
    with pytest.raises(ValueError, match="snapshot id mismatch"):
        validate_evidence_materialization_v3(snapshot_tamper)

    source_tamper = deepcopy(original)
    source_tamper["source_payload"]["product_input"]["raw_text"] += " "
    source_tamper["source_payload"]["content_sha256"] = digest(
        {
            key: value
            for key, value in source_tamper["source_payload"].items()
            if key != "content_sha256"
        }
    )
    source_tamper["evidence_materialization_hash"] = digest(
        {
            key: value
            for key, value in source_tamper.items()
            if key != "evidence_materialization_hash"
        }
    )
    with pytest.raises(ValueError, match="input hash mismatch"):
        validate_evidence_materialization_v3(source_tamper)


def test_v3_validator_rejects_self_consistent_fake_catalog_candidates() -> None:
    case = _case(
        "北京4人。第1天 09:00 博物馆；第2天 09:00 颐和园。",
        requires_resolution=True,
    )
    forged = build_evidence_materialization_v3(
        {key: value for key, value in case.items() if key != "oracle"}
    )
    resolution = forged["source_payload"]["entity_resolutions"][0]
    fake_candidates = [
        {
            "place_id": "fake-1",
            "name": "虚构博物馆一",
            "city": "北京",
            "district": "controlled-fixture",
            "address": "controlled-fixture",
            "category": "attraction",
            "coords": {"lng": 116.1, "lat": 39.9},
            "aliases": [],
        },
        {
            "place_id": "fake-2",
            "name": "虚构博物馆二",
            "city": "北京",
            "district": "controlled-fixture",
            "address": "controlled-fixture",
            "category": "attraction",
            "coords": {"lng": 116.2, "lat": 39.9},
            "aliases": [],
        },
    ]
    resolution["candidates"] = fake_candidates
    old_receipt_id = resolution["search_receipt_id"]
    receipt = next(item for item in forged["receipts"] if item["receipt_id"] == old_receipt_id)
    receipt["response_hash"] = digest(
        {"outcome": "NEEDS_CONFIRMATION", "candidates": fake_candidates}
    )
    receipt["receipt_id"] = digest(_receipt_semantic_payload(receipt))
    resolution["search_receipt_id"] = receipt["receipt_id"]
    forged["provider_snapshot"]["receipt_ids"] = [
        receipt["receipt_id"] if item == old_receipt_id else item
        for item in forged["provider_snapshot"]["receipt_ids"]
    ]
    for artifact_name in ("source_payload", "provider_snapshot"):
        artifact = forged[artifact_name]
        artifact["content_sha256"] = digest(
            {key: value for key, value in artifact.items() if key != "content_sha256"}
        )
    forged["evidence_materialization_hash"] = digest(
        {
            key: value
            for key, value in forged.items()
            if key != "evidence_materialization_hash"
        }
    )

    with pytest.raises(ValueError, match="frozen provider catalog"):
        validate_evidence_materialization_v3(forged)


def test_v3_validator_rejects_embedded_blind_label_even_when_rehashed() -> None:
    case = _case(
        "北京4人。第1天 09:00 故宫博物院；第2天 09:00 颐和园。",
        requires_resolution=False,
    )
    tampered = build_evidence_materialization_v3(
        {key: value for key, value in case.items() if key != "oracle"}
    )
    source = tampered["source_payload"]
    source["product_input"]["blind_label"] = {"answer": "secret"}
    rebound = digest(source["product_input"])
    source["normalized_input_sha256"] = rebound
    source["source_input_sha256"] = rebound
    source["content_sha256"] = digest(
        {key: value for key, value in source.items() if key != "content_sha256"}
    )
    tampered["evidence_materialization_hash"] = digest(
        {
            key: value
            for key, value in tampered.items()
            if key != "evidence_materialization_hash"
        }
    )

    with pytest.raises(ValueError, match="forbidden field"):
        validate_evidence_materialization_v3(tampered)


def test_v3_validator_rejects_missing_facts_and_false_live_claim() -> None:
    case = _case(
        "北京4人。第1天 09:00 故宫博物院，14:00 景山公园；第2天 09:00 颐和园。",
        requires_resolution=False,
    )
    original = build_evidence_materialization_v3(_label_free(case))

    missing_facts = deepcopy(original)
    missing_facts["evidence_snapshot"]["snapshot"]["facts"] = []
    missing_facts["evidence_snapshot"]["content_sha256"] = digest(
        {
            key: value
            for key, value in missing_facts["evidence_snapshot"].items()
            if key != "content_sha256"
        }
    )
    missing_facts["evidence_materialization_hash"] = digest(
        {
            key: value
            for key, value in missing_facts.items()
            if key != "evidence_materialization_hash"
        }
    )
    with pytest.raises(ValueError, match="deterministic frozen rebuild"):
        validate_evidence_materialization_v3(missing_facts)

    false_live = deepcopy(original)
    false_live["provider_snapshot"]["execution_mode"] = "live"
    false_live["provider_snapshot"]["content_sha256"] = digest(
        {
            key: value
            for key, value in false_live["provider_snapshot"].items()
            if key != "content_sha256"
        }
    )
    false_live["evidence_materialization_hash"] = digest(
        {
            key: value
            for key, value in false_live.items()
            if key != "evidence_materialization_hash"
        }
    )
    with pytest.raises(ValueError, match="execution mode mismatch"):
        validate_evidence_materialization_v3(false_live)


def test_v3_screenshot_rejects_fabricated_cleanup_receipt() -> None:
    from evals.trip_check_v1.p5.data_contract import load_jsonl
    from evals.trip_check_v1.p5.data_contract_v2 import (
        NONBLIND_MATERIALIZATIONS_PATH_V2,
        NONBLIND_PATH_V2,
    )

    case = next(item for item in load_jsonl(NONBLIND_PATH_V2) if item["input_kind"] == "SYNTHETIC_SCREENSHOT")
    materialization = next(
        item
        for item in load_jsonl(NONBLIND_MATERIALIZATIONS_PATH_V2)
        if item["case_id"] == case["case_id"]
    )
    cleanup = deepcopy(
        next(
            item
            for item in materialization["receipts"]
            if item.get("schema_version") == "trip-check-p5-cleanup-receipt-v2"
        )
    )
    cleanup["receipt_id"] = "fabricated-cleanup"
    cleanup["cleanup_attempted_at"] = "1900-01-01T00:00:00Z"
    build_input = {
        key: case[key]
        for key in (
            "case_id",
            "city",
            "trip_days",
            "group_size",
            "input_kind",
            "product_input",
            "normalized_input_sha256",
            "runner_control",
        )
    }
    build_input["runner_control"] = {
        **build_input["runner_control"],
        "provider_snapshot_id": "trip-check-p5-controlled-snapshot-v3",
    }
    build_input.update(
        {
            "render_receipt": materialization["render_receipt"],
            "ocr_baseline_receipt": materialization["ocr_baseline_receipt"],
            "cleanup_receipt": cleanup,
        }
    )

    with pytest.raises(ValueError, match="sealed v2 source"):
        build_evidence_materialization_v3(build_input)


def test_v3_materializer_hard_rejects_cross_city_ambiguous_alias() -> None:
    case = _case(
        "北京2人。第1天 09:00 东方明珠；第2天 09:00 故宫博物院。",
        requires_resolution=True,
    )

    materialization = build_evidence_materialization_v3(
        {key: value for key, value in case.items() if key != "oracle"}
    )

    first = materialization["source_payload"]["entity_resolutions"][0]
    assert first["outcome"] == "HARD_REJECTED"
    assert {item["city"] for item in first["candidates"]} == {"上海"}
    assert validate_case_semantics_v3(_label_free(case), materialization) == []


def test_v3_materializer_rejects_out_of_range_day_and_parser_truncation() -> None:
    out_of_range = _case(
        "北京2人。第5天 09:00 天坛公园。",
        requires_resolution=False,
    )
    out_of_range["trip_days"] = 2
    with pytest.raises(ValueError, match="outside trip_days"):
        build_evidence_materialization_v3(
            {key: value for key, value in out_of_range.items() if key != "oracle"}
        )

    stops = "，".join(f"09:{index % 60:02d} 天坛公园" for index in range(51))
    truncated = _case(f"北京2人。第1天 {stops}；第2天 09:00 故宫博物院。", requires_resolution=False)
    with pytest.raises(ValueError, match="parser rejected input"):
        build_evidence_materialization_v3(
            {key: value for key, value in truncated.items() if key != "oracle"}
        )
