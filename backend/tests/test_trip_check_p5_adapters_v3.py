from __future__ import annotations

import pytest

from app.importing.entity_resolver import EntityResolver
from app.importing.models import ResolutionStatus
from app.importing.parser import ItineraryTextParser
from evals.trip_check_v1.p5.adapters_v3 import MaterializedResolutionProviderV3
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.evidence_materialization_v3 import build_evidence_materialization_v3


def _materialization(raw_text: str, *, city: str = "北京") -> dict:
    product_input = {"source_type": "MANUAL_TEXT", "raw_text": raw_text}
    return build_evidence_materialization_v3(
        {
            "case_id": "p5.v3.adapter.001",
            "city": city,
            "trip_days": 2,
            "group_size": 2,
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
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_text", "expected_status", "expected_place_id"),
    [
        (
            "北京2人。第1天 09:00 长城（八达岭）；第2天 09:00 故宫。",
            [ResolutionStatus.AUTO_MATCHED, ResolutionStatus.AUTO_MATCHED],
            ["bj-badaling", "bj-forbidden-city"],
        ),
        (
            "北京2人。第1天 09:00 博物馆；第2天 09:00 颐和园。",
            [ResolutionStatus.AMBIGUOUS, ResolutionStatus.AUTO_MATCHED],
            [None, "bj-summer-palace"],
        ),
        (
            "北京2人。第1天 09:00 东方明珠；第2天 09:00 故宫博物院。",
            [ResolutionStatus.NOT_FOUND, ResolutionStatus.AUTO_MATCHED],
            [None, "bj-forbidden-city"],
        ),
    ],
)
async def test_v3_resolution_materialization_matches_product_entity_resolver(
    raw_text: str,
    expected_status: list[ResolutionStatus],
    expected_place_id: list[str | None],
) -> None:
    materialization = _materialization(raw_text)
    raw_stops = ItineraryTextParser().parse(raw_text, import_id="v3-adapter-test").raw_stops
    resolved = await EntityResolver(MaterializedResolutionProviderV3(materialization)).resolve_all(
        raw_stops, city="北京"
    )

    assert [item.resolution_status for item in resolved] == expected_status
    assert [item.canonical_place_id for item in resolved] == expected_place_id
    first_outcome = materialization["source_payload"]["entity_resolutions"][0]["outcome"]
    expected_outcome = {
        ResolutionStatus.AUTO_MATCHED: "AUTO_RESOLVED",
        ResolutionStatus.AMBIGUOUS: "NEEDS_CONFIRMATION",
        ResolutionStatus.NOT_FOUND: "HARD_REJECTED",
    }[expected_status[0]]
    assert first_outcome == expected_outcome


@pytest.mark.asyncio
async def test_v3_resolution_provider_returns_mutation_isolated_replays() -> None:
    materialization = _materialization(
        "北京2人。第1天 09:00 长城（八达岭）；第2天 09:00 故宫博物院。"
    )
    provider = MaterializedResolutionProviderV3(materialization)

    first = await provider.search(query="长城（八达岭）", city="北京")
    first[0]["coords"]["lng"] = 0
    first[0]["aliases"].append("污染别名")
    second = await provider.search(query="长城（八达岭）", city="北京")

    assert second[0]["coords"]["lng"] == 116.016
    assert "污染别名" not in second[0]["aliases"]
