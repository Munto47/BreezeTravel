from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.experience_inference import (
    ExperienceQwenProvider,
    SemanticDraft,
    SourceAnchorIndex,
    SourceAnchorValidationError,
    _validation_issues,
    proposal_from_draft,
)
from app.trip_understanding.failures import safe_failure_binding
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.pipeline import TripUnderstandingPipeline


class CapturedClient:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(output, ensure_ascii=False)), finish_reason="stop")],
            model="controlled-model", usage=SimpleNamespace(prompt_tokens=200, completion_tokens=100),
        )


def provider(client):
    return ExperienceQwenProvider(
        api_key="test-only", base_url="https://example.invalid/v1",
        model="controlled-model", client=client,
    )


def activity(quote, place, day=1, **kwargs):
    return {"source_quote": quote, "place_name": place, "role": "PLANNED", "day_index": day, **kwargs}


@pytest.mark.parametrize("decoration", ["**", "__", "***", "___", "*", "_", "`"])
def test_markdown_decoration_maps_back_to_exact_unicode_source_offsets(decoration):
    source = f"杭州\n- 🌿 {decoration}西湖{decoration}，随后散步。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "杭州", "activities": [activity("西湖，随后散步。", "西湖")],
    }))
    mention = proposal.mentions[0]
    assert mention.span_start == source.index("西湖")
    assert source[mention.span_start:mention.span_end] == mention.raw_text == "西湖"
    assert proposal.unprocessed_count == 0


def test_occurrence_counts_visible_repeated_quotes_without_losing_original_offsets():
    source = "杭州第一天：去**西湖**散步。第二天：去西湖散步。第三天：去**西湖**散步。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "杭州", "activities": [
            activity("去西湖散步", "西湖", day, occurrence=day) for day in (1, 2, 3)
        ],
    }))
    positions = [source.index("西湖")]
    positions.append(source.index("西湖", positions[-1] + 1))
    positions.append(source.index("西湖", positions[-1] + 1))
    assert [item.span_start for item in proposal.mentions] == positions
    assert [item.day_index for item in proposal.mentions] == [1, 2, 3]
    with pytest.raises(ValueError, match="SOURCE_QUOTE_NOT_FOUND"):
        SourceAnchorIndex(source).locate("去西湖散步", 4)


@pytest.mark.parametrize("source,quote", [
    ("西湖，随后散步", "西湖,随后散步"),
    ("西湖旁休息", "西湖旁游览"),
    ("West Lake", "WestLake"),
    ("先去西湖，然后回家", "先去西湖回家"),
    ("**西湖", "西湖**"),
])
def test_anchor_mapping_does_not_rewrite_words_punctuation_or_whitespace(source, quote):
    with pytest.raises(ValueError, match="SOURCE_QUOTE_NOT_FOUND"):
        SourceAnchorIndex(source).locate(quote)


@pytest.mark.parametrize("source,quote,place", [
    ("杭州去断桥", "去断桥", "断桥残雪"),
    ("杭州去西**湖**", "去西湖", "西湖"),
])
def test_visible_quote_never_creates_a_place_not_literal_in_its_original_span(source, quote, place):
    with pytest.raises(SourceAnchorValidationError) as caught:
        proposal_from_draft(source, SemanticDraft.model_validate({
            "destination": "杭州", "activities": [activity(quote, place)],
        }))
    assert caught.value.category == "PLACE_NOT_IN_SOURCE_QUOTE"
    assert caught.value.issues[0]["field"] == "activities[0].place_name"


def test_formatted_time_evidence_and_unprocessed_text_remain_source_bound():
    source = "杭州：**09:30到西湖**，游览**60分钟**；另有**未决定的行程**。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "杭州", "activities": [activity(
            "09:30到西湖，游览60分钟", "西湖", start_time="09:30",
            visit_duration_minutes=60, time_evidence="09:30到西湖，游览60分钟",
        )], "unprocessed_quotes": ["另有未决定的行程"],
    }))
    assert proposal.mentions[0].start_time == "09:30"
    assert proposal.mentions[0].timing_source == "TEXT"
    assert proposal.unprocessed_count == 1


@pytest.mark.asyncio
async def test_three_day_parallel_places_and_both_alternatives_survive_one_formatted_response():
    # Synthetic model output proves source mapping and downstream preservation,
    # not live model completeness. It is not copied from a user's itinerary.
    source = (
        "杭州三日\nDay 1\n- **西湖** + **曲院风荷**、**断桥**。\n"
        "Day 2\n- **灵隐寺** / **法喜寺**，二选一。\n- **浙江大学**、**植物园**。\n"
        "Day 3\n- **河坊街**、**南宋御街**、**小河直街**。"
    )
    groups = [(1, ["西湖", "曲院风荷", "断桥"]), (2, ["浙江大学", "植物园"]),
              (3, ["河坊街", "南宋御街", "小河直街"])]
    activities = [activity(place, place, day) for day, places in groups for place in places]
    activities.extend(activity("灵隐寺 / 法喜寺，二选一", place, 2, role="OPTIONAL")
                      for place in ("灵隐寺", "法喜寺"))
    captured = CapturedClient({"destination": "杭州", "activities": activities})
    output = await TripUnderstandingPipeline(provider(captured), ControlledSnapshotPlaceResolver()).run(source)
    assert len(captured.calls) == 1
    assert [len(day.activities) for day in output.public_result.days] == [3, 2, 3]
    assert output.resolution_receipt["attempted_count"] == 8
    assert [item.compiled.mention.atomic_place_name for item in output.activities
            if item.compiled.mention.role == "OPTIONAL"] == ["灵隐寺", "法喜寺"]
    for item in output.activities:
        mention = item.compiled.mention
        assert source[mention.span_start:mention.span_end] == mention.raw_text


@pytest.mark.asyncio
async def test_bundled_parallel_place_list_triggers_one_grounded_repair():
    source = "北京：傍晚去**什刹海 + 后海**；晚上看**鸟巢、水立方**。"
    bundled = {
        "destination": "北京",
        "activities": [
            activity("什刹海", "什刹海", category="景点"),
            activity("鸟巢", "鸟巢", category="景点"),
        ],
    }
    repaired = {
        "destination": "北京",
        "activities": [
            activity(name, name, category="景点")
            for name in ("什刹海", "后海", "鸟巢", "水立方")
        ],
    }
    client = CapturedClient(bundled, repaired)

    result = await provider(client).propose(source)

    assert [item.atomic_place_name for item in result.mentions] == [
        "什刹海", "后海", "鸟巢", "水立方",
    ]
    assert len(client.calls) == 2
    repair = client.calls[1]["messages"][-1]["content"]
    assert "MISSING_EXPLICIT_PARALLEL_PLACE" in repair
    assert "什刹海" not in repair and "鸟巢" not in repair


def test_food_list_without_atomic_restaurant_is_allowed_to_stay_unprocessed():
    source = "北京：晚餐可以吃铜锅涮肉、炸酱面。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "北京",
        "activities": [activity(
            "铜锅涮肉、炸酱面", None, category="餐饮",
        )],
    }))

    assert len(proposal.mentions) == 1
    assert proposal.mentions[0].atomic_place_name is None


def test_suggested_parallel_group_does_not_turn_repair_into_a_hard_failure():
    source = "北京：不想走主街，可以走隔壁**北巷、南巷**，人会少一些。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "北京", "activities": [],
        "unprocessed_quotes": ["可以走隔壁北巷、南巷"],
    }))

    assert proposal.unprocessed_count == 1


@pytest.mark.asyncio
async def test_repair_names_all_bad_fields_and_keeps_valid_activities_without_private_diagnostics():
    source = "杭州：西湖。第二天灵隐寺。另有未决定行程。"
    invalid = {"destination": "杭州", "activities": [
        activity("西湖", "西湖"), activity("不存在的私人引用", "灵隐寺", 2),
    ], "unprocessed_quotes": ["另有不存在的私人计划"]}
    valid = {"destination": "杭州", "activities": [activity("西湖", "西湖"), activity("灵隐寺", "灵隐寺", 2)],
             "unprocessed_quotes": ["另有未决定行程"]}
    client = CapturedClient(invalid, valid)
    result = await provider(client).propose(source)
    assert len(result.mentions) == 2 and result.unprocessed_count == 1
    repair = client.calls[1]["messages"][-1]["content"]
    assert "activities[1].source_quote" in repair and "unprocessed_quotes[0]" in repair
    assert "私人引用" not in repair and "私人计划" not in repair
    assert result.binding["calls"][0]["outcome"] == "SOURCE_QUOTE_NOT_FOUND"
    assert len(result.binding["calls"][0]["validation_errors"]) == 2


@pytest.mark.asyncio
async def test_failure_retains_per_call_categories_without_quotes_or_model_bodies():
    invalid = {"destination": "杭州", "activities": [activity("private-missing-quote", "西湖")]}
    client = CapturedClient(invalid, TimeoutError())
    with pytest.raises(InferenceProviderUnavailableError) as caught:
        await provider(client).propose("杭州西湖")
    safe = safe_failure_binding(caught.value.provider_binding)
    assert [call["outcome"] for call in safe["calls"]] == ["SOURCE_QUOTE_NOT_FOUND", "DEADLINE_EXCEEDED"]
    assert safe["calls"][0]["validation_errors"] == [
        {"field": "activities[0].source_quote", "category": "SOURCE_QUOTE_NOT_FOUND"},
    ]
    assert "private-missing-quote" not in json.dumps(safe)
    assert "source_quote" not in safe["calls"][0]


def test_schema_repair_diagnostics_do_not_echo_unknown_fields_or_input_values():
    with pytest.raises(ValidationError) as caught:
        SemanticDraft.model_validate({"destination": "杭州", "activities": [], "private-secret-field": "private-content"})
    issues = _validation_issues(caught.value)
    assert issues == [{"field": "unknown_field", "category": "extra_forbidden"}]
    assert "private" not in json.dumps(issues)
