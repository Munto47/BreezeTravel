from __future__ import annotations

from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.evidence_materialization_v3 import build_evidence_materialization_v3
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
            "seed": 20260823,
        },
        "oracle": {"requires_user_resolution": requires_resolution},
    }


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
    assert validate_case_semantics_v3(case, materialization) == []


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
    assert validate_case_semantics_v3(case, materialization) == []


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
    assert validate_case_semantics_v3(case, materialization) == []
