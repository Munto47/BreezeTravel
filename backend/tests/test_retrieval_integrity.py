"""P0 gates: live retrieval is fail-closed, classified, auditable, and canonical."""

import asyncio
import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import HumanMessage

from app.agents.nodes import amap_search
from app.agents.nodes.tool_executor import _merge_unique_places, _retrieval_audit_from_exception
from app.schemas.place import (
    Coordinates,
    EvidenceStatus,
    GeoEvidence,
    Place,
    PlaceCategory,
    PlaceSource,
    RetrievalExecutionMode,
)
from app.schemas.retrieval import RetrievalAudit
from app.tools.runtime import ToolErrorCategory, ToolReceipt, ToolRuntimeError
from scripts.run_daily_query_eval import (
    build_candidate_snapshot,
    _judge_payload_places,
    _judge_stage_metadata,
    high_risk_honesty_checks,
    retrieval_integrity_checks,
)


def _state() -> dict:
    return {
        "messages": [HumanMessage(content="北京景点")],
        "trip_city": "北京",
        "trip_district": None,
        "query_rewrite": "景点",
        "working_context": {},
    }


def _place(place_id: str, mode: RetrievalExecutionMode) -> Place:
    return Place(
        place_id=place_id,
        name="颐和园",
        category=PlaceCategory.ATTRACTION,
        address="新建宫门路19号",
        coords=Coordinates(lng=116.2755, lat=39.9999),
        city="北京",
        district="海淀区",
        source=PlaceSource.AMAP_POI,
        execution_mode=mode,
        retrieval_provider="amap" if mode == RetrievalExecutionMode.LIVE else "amap_fixture",
        retrieval_response_hash="a" * 64,
    )


def test_unknown_amap_type_is_not_promoted_to_attraction():
    assert amap_search._parse_amap_type("购物服务;商场", "060100") == PlaceCategory.UNKNOWN
    assert amap_search._parse_amap_type("餐饮服务;咖啡厅", "050500") == PlaceCategory.FOOD
    assert amap_search._parse_amap_type("住宿服务;宾馆酒店", "100100") == PlaceCategory.HOTEL
    assert amap_search._parse_amap_type("科教文化服务;美术馆;美术馆", "140400") == PlaceCategory.ATTRACTION
    assert amap_search._parse_amap_type("科教文化服务;学校;高等院校", "141201") == PlaceCategory.UNKNOWN


def test_amap_v5_business_fields_are_parsed_as_observed_evidence():
    place = amap_search._parse_amap_place({
        "id": "B000TEST",
        "name": "测试餐厅",
        "type": "餐饮服务;中餐厅",
        "typecode": "050100",
        "address": "测试路1号",
        "location": "116.400000,39.900000",
        "adname": "东城区",
        "business": {
            "rating": "4.7",
            "cost": "145.00",
            "opentime_today": "10:00-22:30",
            "opentime_week": "周一至周日 10:00-22:30",
            "tel": "010-12345678",
            "keytag": "上海菜",
            "rectag": "本帮菜",
            "tag": "红烧肉,素菜",
        },
        "photos": [{"url": "https://example.invalid/a.jpg"}],
    }, "北京")

    assert place is not None
    assert place.amap_rating == 4.7
    assert place.amap_price == 145.0
    assert place.opening_hours == "10:00-22:30"
    assert place.phone == "010-12345678"
    assert place.amap_photos == ["https://example.invalid/a.jpg"]
    assert place.tags == ["上海菜", "本帮菜", "红烧肉", "素菜"]


def test_precise_nearby_cuisine_search_drops_only_broad_food_typecode():
    assert amap_search._effective_typecodes("湘菜", "121.43,31.19", ["050000"]) == []
    assert amap_search._effective_typecodes("餐厅", "121.43,31.19", ["050000"]) == ["050000"]
    assert amap_search._effective_typecodes("湘菜", "", ["050000"]) == ["050000"]


def test_candidate_snapshot_freezes_post_evidence_selected_places():
    place = _place("live-1", RetrievalExecutionMode.LIVE)
    report = {
        "generated_at": "2026-08-12T00:00:00+00:00",
        "cases": [{
            "id": "case-1",
            "city": "北京",
            "intent": "attraction",
            "persona": "test",
            "dimensions": [],
            "query": "北京景点",
            "execution_tree_sha256": "tree",
            "output": {
                "places": [place.model_dump(mode="json")],
                "done": {"retrieval_snapshots": [{
                    "places": [place.model_dump(mode="json")],
                    "audits": [{"execution_mode": "live", "provider": "amap"}],
                    "receipt": {"status": "ok"},
                }]},
            },
        }],
    }

    snapshot = build_candidate_snapshot(report)

    assert snapshot["schema_version"] == "1.1"
    assert snapshot["integrity"]["passed"] is True
    assert snapshot["cases"][0]["selected_places"][0]["place_id"] == "live-1"


def test_frozen_synthesis_never_calls_llm_or_route_provider():
    place = _place("live-1", RetrievalExecutionMode.LIVE)
    with (
        patch("app.agents.nodes.synthesizer._get_llm") as llm,
        patch("app.agents.nodes.synthesizer.enrich_geo_route_evidence") as route,
    ):
        result = __import__(
            "app.agents.nodes.synthesizer", fromlist=["synthesize_frozen_places"]
        ).synthesize_frozen_places([place], "北京", {}, "", "北京景点")

    llm.assert_not_called()
    route.assert_not_called()
    assert result["synthesized_places"][0].place_id == "live-1"
    assert result["final_response"]


def test_live_mode_missing_key_fails_closed_without_fixture():
    with (
        patch.object(amap_search.settings, "runtime_profile", "local_real"),
        patch.object(amap_search.settings, "demo_mode", False),
        patch.object(amap_search.settings, "amap_mock", False),
        patch.object(amap_search.settings, "amap_api_key", ""),
        patch.object(amap_search, "_load_mock_places") as fixture,
    ):
        with pytest.raises(amap_search.AmapSearchError) as raised:
            asyncio.run(amap_search.run(_state()))
    fixture.assert_not_called()
    assert raised.value.audit.status == "configuration_error"
    assert raised.value.audit.fallback_reason == "missing_api_key"


def test_live_mode_provider_exception_fails_closed_without_fixture():
    with (
        patch.object(amap_search.settings, "runtime_profile", "local_real"),
        patch.object(amap_search.settings, "demo_mode", False),
        patch.object(amap_search.settings, "amap_mock", False),
        patch.object(amap_search.settings, "amap_api_key", "configured"),
        patch.object(amap_search, "_fetch_amap_poi", new=AsyncMock(side_effect=TimeoutError())),
        patch.object(amap_search, "_load_mock_places") as fixture,
    ):
        with pytest.raises(amap_search.AmapSearchError) as raised:
            asyncio.run(amap_search.run(_state()))
    fixture.assert_not_called()
    assert raised.value.audit.execution_mode == RetrievalExecutionMode.LIVE
    assert raised.value.audit.status == "error"


def test_empty_live_result_remains_empty_and_audited():
    audit = RetrievalAudit(
        query="景点",
        city="北京",
        provider="amap",
        execution_mode=RetrievalExecutionMode.LIVE,
        retrieved_at=datetime.now(timezone.utc),
        response_hash="b" * 64,
        result_count=0,
        status="empty",
    )
    with (
        patch.object(amap_search.settings, "runtime_profile", "local_real"),
        patch.object(amap_search.settings, "demo_mode", False),
        patch.object(amap_search.settings, "amap_mock", False),
        patch.object(amap_search.settings, "amap_api_key", "configured"),
        patch.object(amap_search, "_fetch_amap_poi", new=AsyncMock(return_value=([], audit))),
        patch.object(amap_search, "_load_mock_places") as fixture,
    ):
        result = asyncio.run(amap_search.run(_state()))
    fixture.assert_not_called()
    assert result["amap_places"] == []
    assert result["retrieval_audits"][0]["status"] == "empty"


def test_canonical_dedupe_merges_different_ids_and_prefers_live_record():
    fixture = _place("fixture-bj-01", RetrievalExecutionMode.FIXTURE)
    live = _place("B000A83V46", RetrievalExecutionMode.LIVE).model_copy(
        update={"amap_rating": 4.9}
    )
    merged = _merge_unique_places([], [fixture, live])
    assert len(merged) == 1
    assert merged[0].place_id == "B000A83V46"
    assert merged[0].execution_mode == RetrievalExecutionMode.LIVE


def test_formal_integrity_gate_rejects_fixture_and_accepts_live_receipt():
    fixture_result = {
        "places": [_place("fixture-bj-01", RetrievalExecutionMode.FIXTURE).model_dump(mode="json")],
        "done": {"retrieval_audits": []},
    }
    rejected = retrieval_integrity_checks(fixture_result)
    assert rejected["passed"] is False
    assert rejected["fixture_places"] == 1

    live = _place("B000A83V46", RetrievalExecutionMode.LIVE).model_dump(mode="json")
    live_result = {
        "places": [live],
        "done": {
            "retrieval_audits": [{
                "query": "颐和园", "city": "北京", "provider": "amap",
                "execution_mode": "live", "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "response_hash": "a" * 64, "result_count": 1, "status": "ok",
            }],
            "tool_failures": [],
        },
    }
    accepted = retrieval_integrity_checks(live_result)
    assert accepted["passed"] is True
    assert accepted["fixture_places"] == 0
    assert accepted["provider_available"] is True
    assert accepted["quality_eligible"] is True


def test_circuit_open_is_infrastructure_failure_not_quality_eligible():
    result = {
        "places": [],
        "done": {
            "retrieval_audits": [{
                "query": "餐厅", "city": "北京", "provider": "amap",
                "execution_mode": "live", "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "response_hash": None, "result_count": 0, "status": "blocked",
                "attempted": False, "fallback_reason": "circuit_open",
                "error_category": "circuit_open", "provider_health_failure": False,
            }],
            "tool_failures": [{"tool": "search_places", "reason": "circuit_open"}],
        },
    }
    integrity = retrieval_integrity_checks(result)
    assert integrity["data_purity_passed"] is True
    assert integrity["provider_available"] is False
    assert integrity["infrastructure_failure"] is True
    assert integrity["quality_eligible"] is False


def test_anchor_not_found_is_query_failure_not_provider_outage():
    result = {
        "places": [],
        "done": {
            "retrieval_audits": [{
                "query": "anchor:想住", "city": "上海", "provider": "amap",
                "execution_mode": "live", "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "response_hash": "c" * 64, "result_count": 0, "status": "empty",
                "attempted": True, "fallback_reason": "anchor_not_found",
                "error_category": "anchor_not_found", "provider_health_failure": False,
            }],
            "tool_failures": [{"tool": "search_places", "reason": "anchor_not_found"}],
        },
    }
    integrity = retrieval_integrity_checks(result)
    assert integrity["provider_available"] is True
    assert integrity["business_retrieval_failure"] is True
    assert integrity["quality_eligible"] is False


def test_sse_deadline_is_an_application_failure_even_without_tool_failure():
    result = {
        "places": [],
        "errors": [{"error_category": "deadline_exceeded"}],
        "done": {
            "retrieval_audits": [{
                "query": "历史文化街区", "city": "北京", "provider": "amap",
                "execution_mode": "live", "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "response_hash": "d" * 64, "result_count": 0, "status": "empty",
            }],
            "tool_failures": [],
        },
    }
    integrity = retrieval_integrity_checks(result)
    assert integrity["data_purity_passed"] is True
    assert integrity["application_failure"] is True
    assert integrity["application_failure_categories"] == ["deadline_exceeded"]
    assert integrity["quality_eligible"] is False


def test_judge_receives_user_visible_confirmation_and_route_evidence():
    place = _place("B000A83V46", RetrievalExecutionMode.LIVE).model_copy(update={
        "selection_evidence_status": EvidenceStatus.REQUIRES_CONFIRMATION,
        "confirmation_actions": ["致电 010-12345678 确认家庭房"],
        "phone": "010-12345678",
        "geo_evidence": [GeoEvidence(**{
            "slot_id": "slot-01", "anchor_place": "故宫博物院",
            "status": "VERIFIED", "satisfies_constraint": True,
            "transport_mode": "walking", "estimated_travel_minutes": 12,
            "source": "amap_walking_route",
        })],
    })
    payload = _judge_payload_places({"places": [place.model_dump(mode="json")]})[0]
    assert payload["selection_evidence_status"] == "REQUIRES_CONFIRMATION"
    assert payload["confirmation_actions"] == ["致电 010-12345678 确认家庭房"]
    assert payload["geo_evidence"][0]["estimated_travel_minutes"] == 12
    assert payload["phone"] == "010-12345678"


def test_offline_judge_metadata_binds_source_report_and_current_runner(tmp_path):
    report_path = tmp_path / "live.json"
    dataset_path = tmp_path / "cases.json"
    report_path.write_text('{"generated_at":"live-time","reproducibility":{"runner_sha256":"old"}}', encoding="utf-8")
    dataset_path.write_text('{"schema_version":"test"}', encoding="utf-8")

    metadata = _judge_stage_metadata(
        report_path,
        dataset_path,
        {"generated_at": "live-time", "reproducibility": {"runner_sha256": "old"}},
    )

    assert metadata["source_report_generated_at"] == "live-time"
    assert metadata["source_report_sha256"] == hashlib.sha256(report_path.read_bytes()).hexdigest()
    assert metadata["source_report_reproducibility"]["runner_sha256"] == "old"
    assert metadata["judge_reproducibility"]["runner_sha256"]


def test_high_risk_honesty_audit_accepts_explicit_confirmation_flow():
    result = {"places": [{
        "name": "测试酒店",
        "selection_evidence_status": "REQUIRES_CONFIRMATION",
        "constraint_evidence": [{"constraint": "family_room", "status": "REQUIRES_CONFIRMATION"}],
        "confirmation_actions": ["致电酒店确认家庭房"],
        "description": "高德 POI 记录位于东城区；具体房型需确认。",
        "tags": [],
    }], "text": "家庭房尚未被 POI 数据证实，请致电确认。"}

    audit = high_risk_honesty_checks(result)

    assert audit["passed"] is True
    assert audit["confirmation_action_coverage"] == 1.0
    assert audit["unsupported_affirmative_claim_count"] == 0


def test_high_risk_honesty_audit_treats_contact_confirmation_as_an_action_not_a_claim():
    result = {"places": [{
        "name": "测试咖啡馆",
        "selection_evidence_status": "REQUIRES_CONFIRMATION",
        "constraint_evidence": [{"constraint": "dairy_free", "status": "REQUIRES_CONFIRMATION"}],
        "confirmation_actions": ["致电门店确认：无乳糖或植物奶"],
        "description": "高德 POI 记录位于徐汇区。",
        "tags": [],
    }], "text": "以下条件尚未证实：致电门店确认：无乳糖或植物奶。"}

    assert high_risk_honesty_checks(result)["passed"] is True


def test_high_risk_honesty_audit_rejects_unverified_affirmative_card_copy():
    result = {"places": [{
        "name": "测试酒店",
        "selection_evidence_status": "UNKNOWN",
        "constraint_evidence": [{"constraint": "family_room", "status": "UNKNOWN"}],
        "confirmation_actions": [],
        "description": "酒店提供家庭房，适合亲子入住。",
        "tags": ["亲子"],
    }], "text": "测试酒店提供家庭房。"}

    audit = high_risk_honesty_checks(result)

    assert audit["passed"] is False
    assert audit["confirmation_action_coverage"] == 0.0
    assert audit["unsupported_affirmative_claim_count"] == 2


def test_preflight_tool_failure_still_produces_retrieval_audit():
    receipt = ToolReceipt(
        call_id="call-1", trace_id="trace-1", tool="search_places",
        status="error", duration_ms=0, error_category=ToolErrorCategory.CIRCUIT_OPEN,
        degraded=True, provider="amap", circuit_state="open", circuit_failure_count=5,
    )
    error = ToolRuntimeError("provider circuit is open", receipt)
    audit = _retrieval_audit_from_exception(
        error,
        {"name": "search_places", "args": {"query": "餐厅", "city": "北京", "slot_id": "slot-02"}},
        {"trip_city": "北京"},
    )
    assert audit is not None
    assert audit["slot_id"] == "slot-02"
    assert audit["status"] == "blocked"
    assert audit["attempted"] is False
    assert audit["error_category"] == "circuit_open"
