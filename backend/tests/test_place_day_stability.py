"""Missing day assignments must not silently collapse a multi-day itinerary."""
import json

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, SourceAnchorValidationError, proposal_from_draft, _unambiguous_literal_place_day
from app.trip_understanding.errors import InferenceProviderUnavailableError
from tests.test_experience_inference import Client, provider


SOURCE = "上海两天，第一天原定上海博物馆人民广场馆、豫园，第二天外滩。最终第一天改为上海博物馆东馆、豫园，第二天仍是外滩。"


def draft(assigned):
    return {"destination": "上海", "activities": [
        {"source_quote": name, "place_name": name, "role": "PLANNED", **({"day_index": day} if assigned else {})}
        for name, day in [("上海博物馆东馆", 1), ("豫园", 1), ("外滩", 2)]
    ]}


def test_missing_days_cannot_default_every_planned_noun_to_day_one():
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(SOURCE, SemanticDraft.model_validate(draft(False)))
    assert [item["category"] for item in error.value.issues] == ["MISSING_EXPLICIT_DAY"] * 3


@pytest.mark.asyncio
async def test_existing_single_repair_preserves_all_nouns_and_their_final_days():
    client = Client(json.dumps(draft(False), ensure_ascii=False), json.dumps(draft(True), ensure_ascii=False))
    result = await provider(client).propose(SOURCE)
    assert [(mention.day_index, mention.atomic_place_name) for mention in result.mentions] == [
        (1, "上海博物馆东馆"), (1, "豫园"), (2, "外滩")]
    assert len(client.calls) == 2
    assert result.binding["repair_call_count"] == 1


def test_single_day_without_day_field_keeps_one_soft_day():
    result = proposal_from_draft("上海。豫园、外滩。", SemanticDraft.model_validate({
        "destination": "上海", "activities": [
            {"source_quote": name, "place_name": name, "role": "PLANNED"} for name in ("豫园", "外滩")]
    }))
    assert [(mention.day_index, mention.atomic_place_name) for mention in result.mentions] == [(1, "豫园"), (1, "外滩")]


@pytest.mark.asyncio
async def test_repeated_missing_fields_recover_only_from_consistent_explicit_source_days():
    payload = json.dumps(draft(False), ensure_ascii=False)
    client = Client(payload, payload)
    result = await provider(client).propose(SOURCE)
    assert [(mention.day_index, mention.atomic_place_name) for mention in result.mentions] == [
        (1, "上海博物馆东馆"), (1, "豫园"), (2, "外滩")]
    assert result.binding["source_grounded_day_activities"] == 3
    assert result.binding["fallback_used"] is True
    assert len(client.calls) == 2
    assert all(call["outcome"] == "MISSING_EXPLICIT_DAY" for call in result.binding["calls"])


@pytest.mark.parametrize("source,name,day", [
    ("第1天豫园和外滩。第2天上海博物馆东馆。", "外滩", 1),
    ("第1天豫园和外滩。第2天上海博物馆东馆。", "上海博物馆东馆", 2),
    ("Day 1 豫园；Day 2 外滩。", "外滩", 2),
    ("第十一天灵隐寺。第十二天西湖。", "西湖", 12),
    ("第1天西湖；第2天再去西湖。", "西湖", None),
    ("第1天西湖；最终西湖改到第2天。", "西湖", None),
    ("先提到外滩。第2天外滩。", "外滩", None),
    ("第1天豫园。另一天去外滩。", "外滩", None),
    ("第1天豫园。9月12日外滩。", "外滩", None),
    ("第1天豫园。次日去外滩。第3天上海博物馆。", "外滩", None),
    ("第1天豫园。翌日去外滩。", "外滩", None),
    ("第1天豫园。第二晚外滩。", "外滩", None),
    ("第1天豫园。9/12 外滩。", "外滩", None),
    ("第1天豫园。2026-09-12 外滩。", "外滩", None),
    ("第1天豫园。明天去外滩。", "外滩", None),
    ("第1天豫园。周三去外滩。", "外滩", None),
    ("参考链接https://example.com/Day1，去外滩日期未定。", "外滩", None),
    ("订单ID2ABC已经保存，外滩日期未定。", "外滩", None),
    ("https://example.com/Day2 外滩", "外滩", None),
    ("第1天豫园，参考https://example.com/Day2 外滩。", "外滩", None),
    ("第1天豫园。外滩日期待定。", "外滩", None),
    ("第1天和第2天都去外滩。", "外滩", None),
    ("第1天至第2天都去外滩。", "外滩", None),
    ("第1天外滩，第2天豫园。最终把前者也移到第2天，放在豫园之前。", "外滩", None),
    ("第1天外滩，第2天豫园。两天对调。", "外滩", None),
])
def test_literal_day_grounding_is_bounded(source, name, day):
    assert _unambiguous_literal_place_day(source, name) == day


@pytest.mark.asyncio
async def test_conflicting_days_are_not_recovered_by_guessing_or_dropping_places():
    source = "上海。第1天外滩。第2天再次外滩。"
    payload = json.dumps({"destination": "上海", "activities": [
        {"source_quote": "外滩", "place_name": "外滩", "role": "PLANNED", "occurrence": count} for count in (1, 2)]})
    client = Client(payload, payload)
    with pytest.raises(InferenceProviderUnavailableError):
        await provider(client).propose(source)
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_relative_day_does_not_become_successful_previous_day_after_two_failures():
    source = "上海三天。第1天豫园。次日去外滩。第3天上海博物馆。"
    payload = json.dumps({"destination": "上海", "activities": [
        {"source_quote": name, "place_name": name, "role": "PLANNED", **fields}
        for name, fields in [("豫园", {"day_index": 1}), ("外滩", {}), ("上海博物馆", {"day_index": 3})]
    ]}, ensure_ascii=False)
    client = Client(payload, payload)
    with pytest.raises(InferenceProviderUnavailableError):
        await provider(client).propose(source)
    assert len(client.calls) == 2
