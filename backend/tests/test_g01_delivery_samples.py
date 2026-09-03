from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.trip_text_cards_v1.runner import predict_case
from evals.trip_text_cards_v1.validator import load_cases


DATA_ROOT = Path("eval_data/trip_text_cards_v1")
CASE_IDS = (
    "G01-TC-001",
    "G01-TC-013",
    "G01-TC-025",
    "G01-TC-037",
    "G01-TC-046",
)
EXPECTED_DESTINATIONS = {
    "G01-TC-001": "北京",
    "G01-TC-013": "上海",
    "G01-TC-025": "杭州",
    "G01-TC-037": "成都",
    "G01-TC-046": "北京、上海",
}
FORBIDDEN_PUBLIC_KEYS = {
    "raw_text",
    "source",
    "source_id",
    "span",
    "span_start",
    "span_end",
    "offset",
    "confidence",
    "model",
    "provider",
    "uuid",
    "hash",
    "revision",
    "receipt",
    "run",
    "stage",
}


def _keys(value: object):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).casefold()
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


@pytest.mark.asyncio
async def test_fixed_five_delivery_samples_execute_with_conservative_public_results() -> None:
    cases = {
        case.case_id: case
        for split_cases in load_cases(DATA_ROOT).values()
        for case in split_cases
        if case.case_id in CASE_IDS
    }
    assert tuple(case_id for case_id in CASE_IDS if case_id in cases) == CASE_IDS

    predictions = {case_id: await predict_case(cases[case_id]) for case_id in CASE_IDS}

    for case_id, prediction in predictions.items():
        public = prediction.public_result
        assert prediction.destination_name == EXPECTED_DESTINATIONS[case_id]
        assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(_keys(public))
        assert public["status"] in {
            "READY",
            "PARTIAL_RESULT",
            "BASIC_ONLY",
            "LIMITED",
        }
        assert public["days"]
        assert any(day["activities"] for day in public["days"])
        assert "https://" not in json.dumps(public, ensure_ascii=False)
        assert all(
            activity["name"].strip() and not activity["name"].startswith(("http://", "https://"))
            for day in public["days"]
            for activity in day["activities"]
        )

    assert all(
        mention.resolution_status != "AUTO_MATCHED"
        for mention in predictions["G01-TC-037"].mentions
    )
    assert {
        mention.canonical_city
        for mention in predictions["G01-TC-046"].mentions
        if mention.resolution_status == "AUTO_MATCHED"
    } == {"北京", "上海"}
    assert any(
        mention.resolution_status == "NEEDS_CONFIRMATION"
        for case_id in ("G01-TC-037", "G01-TC-046")
        for mention in predictions[case_id].mentions
    )
