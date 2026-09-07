"""Independent synthetic regressions for source identity, roles and repair privacy."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.experience_inference import (
    ExperienceQwenProvider,
    SemanticDraft,
    SourceAnchorValidationError,
    proposal_from_draft,
)
from app.trip_understanding.pipeline import EvidenceCompiler, _model_activity_cities
from app.trip_understanding.timing_evidence import _clock_values, validated_timing


def row(name, **values):
    return {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": 1, **values}


def propose(source, rows, destination="北京"):
    return proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": destination, "activities": rows,
    }))


@pytest.mark.parametrize("narrative,city", [("上海街头", "上海"), ("杭州路上", "杭州")])
def test_city_in_street_narrative_cannot_inherit_another_days_soft_city(narrative, city):
    source = f"Day1：北京。Day2：{narrative}先去人民公园。"
    proposal = propose(source, [row("人民公园", day_index=2, city=city)])
    mention = proposal.mentions[0]
    assert mention.city_hint is None and mention.city_evidence is None
    assert _model_activity_cities(source, proposal, mention) == ("目的地待确认",)
    assert mention.day_index == 2


@pytest.mark.parametrize("street", ["苏州街", "乌鲁木齐中路"])
def test_an_actual_internal_street_does_not_create_a_second_destination(street):
    source = f"Day1：北京，星河公园，里面有{street}。"
    proposal = propose(source, [row("星河公园", city="北京")])
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == ("北京",)


def test_explicit_local_city_still_overrides_the_document_soft_city():
    source = "Day1：北京。\nDay2：上海，人民公园。"
    proposal = propose(source, [row("人民公园", day_index=2, city="上海", city_evidence="Day2：上海")])
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == ("上海",)


@pytest.mark.parametrize("instruction", ["先后去", "依次去", "按顺序去"])
def test_ordered_meal_streets_remain_planned_even_when_the_model_bundles_them(instruction):
    names = "星河街 / 云岭路"
    source = f"Day1：中午{instruction}{names}吃饭。"
    proposal = propose(source, [row(names, category="餐饮")])
    assert [(m.atomic_place_name, m.role.value, m.category_hint) for m in proposal.mentions] == [
        ("星河街", "PLANNED", "地点"), ("云岭路", "PLANNED", "地点"),
    ]
    assert [m.eligible_for_place_search for m in EvidenceCompiler().compile(source, proposal)[0]] == [True, True]


def test_explicit_both_streets_are_not_downgraded_to_optional():
    names = "星河街 / 云岭路"
    proposal = propose(f"Day1：中午去{names}吃饭，两条都去。", [row(names, category="餐饮")])
    assert [m.role.value for m in proposal.mentions] == ["PLANNED", "PLANNED"]


def test_already_optional_meal_streets_stay_unselected_and_cannot_search():
    names = "星河街 / 云岭路"
    source = f"Day1：午餐二选一：{names}。"
    proposal = propose(source, [row(names, category="餐饮", role="OPTIONAL")])
    assert [m.role.value for m in proposal.mentions] == ["OPTIONAL", "OPTIONAL"]
    assert not any(m.eligible_for_place_search for m in EvidenceCompiler().compile(source, proposal)[0])


@pytest.mark.parametrize("separator", [" → ", " + ", "、", " / "])
def test_list_expansion_preserves_first_booking_without_copying_it_to_later_stops(separator):
    names = f"星河博物馆{separator}云岭公园"
    source = f"Day1：09:00 已预约{names}，先去博物馆再去公园。"
    proposal = propose(source, [row(names, start_time="09:00", locked=True,
                                   fixed_commitment=True, timing_source="TEXT", time_evidence=source)])
    first, second = proposal.mentions
    assert (first.start_time, first.locked, first.fixed_commitment, first.timing_source) == ("09:00", True, True, "TEXT")
    assert (second.start_time, second.end_time, second.visit_duration_minutes) == (None, None, None)
    assert not second.locked and not second.fixed_commitment
    assert [m.raw_text for m in proposal.mentions] == ["星河博物馆", "云岭公园"]
    assert all(source[m.span_start:m.span_end] == m.raw_text for m in proposal.mentions)


def test_a_later_stops_booking_and_end_time_cannot_be_borrowed_by_the_first():
    names = "星河博物馆 → 云岭公园"
    source = f"Day1：09:00 {names}，已预约11:00入园，12:00离开。"
    proposal = propose(source, [row(names, start_time="09:00", end_time="12:00", locked=True,
                                   fixed_commitment=True, timing_source="TEXT", time_evidence=source)])
    first, second = proposal.mentions
    assert first.start_time == "09:00"
    assert first.end_time is None and not first.locked and not first.fixed_commitment
    assert second.start_time is None and not second.locked and not second.fixed_commitment


def test_negated_first_booking_never_becomes_a_fixed_commitment():
    names = "星河博物馆 → 云岭公园"
    source = f"Day1：09:00 尚未预约{names}。"
    proposal = propose(source, [row(names, start_time="09:00", locked=True,
                                   fixed_commitment=True, timing_source="TEXT", time_evidence=source)])
    assert proposal.mentions[0].start_time == "09:00"
    assert not any(m.locked or m.fixed_commitment for m in proposal.mentions)


@pytest.mark.parametrize("evidence,clock,start", [
    ("09:00 星河公园", "09:00", 0),
    ("0:09 星河公园", "00:09", 0),
    ("00:00 星河公园", "00:00", 0),
    ("Day1：09:00 星河公园", "09:00", 5),
    ("D2:09:00 星河公园", "09:00", 3),
    ("星期一：09:00 星河公园", "09:00", 4),
    ("周二：09:00 星河公园", "09:00", 3),
    ("礼拜三：09:00 星河公园", "09:00", 4),
    ("已预约09:00星河公园", "09:00", 3),
    ("九点星河公园", "09:00", 0),
])
def test_clock_tokens_start_at_the_actual_time_not_a_day_heading(evidence, clock, start):
    clocks = _clock_values(evidence)
    assert len(clocks) == 1
    assert clocks[0][0:2] == (clock, start)


@pytest.mark.parametrize("heading,wrong_clock", [("Day1", "01:09"), ("D2", "02:09"), ("星期一", "01:09")])
def test_a_day_heading_cannot_validate_a_fabricated_start_time(heading, wrong_clock):
    source = f"{heading}：09:00 星河公园。"
    timing, removed = validated_timing({"start_time": wrong_clock}, source)
    assert timing["start_time"] is None and removed
    timing, removed = validated_timing({"start_time": "09:00"}, source)
    assert timing["start_time"] == "09:00" and not removed


@pytest.mark.parametrize("evidence", ["0", "Day1", "D2", "记录A09:00", "编号109:00"])
def test_numbers_and_identifier_fragments_are_not_clock_values(evidence):
    assert _clock_values(evidence) == []


@pytest.mark.parametrize("qualifier", ["大约", "约", "预计"])
def test_actual_approximate_times_still_cannot_be_verified(qualifier):
    assert _clock_values(f"{qualifier}09:00到星河公园") == []


@pytest.mark.parametrize("change", ["推迟一天", "移至第二天", "移到第二天", "挪到第二天", "调整到第二天"])
def test_moved_visit_cannot_be_rebound_to_another_districts_reference(change):
    source = f"Day1：北京\n东城区青溪公园。\nDay2：北京\n参考：西城区青溪公园。\n首日安排{change}。"
    proposal = propose(source, [row("青溪公园", day_index=2, occurrence=1),
                                row("青溪公园", day_index=2, occurrence=2, role="REFERENCE")])
    planned = next(m for m in proposal.mentions if m.role.value == "PLANNED")
    assert planned.day_index == 2 and planned.span_start == source.index("青溪公园")
    assert source[planned.span_start - 3:planned.span_start] == "东城区"


def test_bringing_a_visit_forward_keeps_its_original_place_occurrence():
    source = "Day1：北京\n参考：西城区青溪公园。\nDay2：北京\n东城区青溪公园。\n末日安排提前一天。"
    proposal = propose(source, [row("青溪公园", day_index=1, occurrence=2),
                                row("青溪公园", day_index=1, occurrence=1, role="REFERENCE")])
    planned = next(m for m in proposal.mentions if m.role.value == "PLANNED")
    assert planned.day_index == 1 and planned.span_start == source.rindex("青溪公园")


def test_a_moved_main_visit_cannot_collapse_into_a_same_name_optional_visit():
    source = "Day1：\n青溪公园。\nDay2：\n> 备选：如果下雨，可换成青溪公园。\n首日的安排推迟一天。"
    proposal = propose(source, [row("青溪公园", day_index=2, occurrence=1),
                                row("青溪公园", day_index=2, occurrence=2, role="OPTIONAL")])
    assert [(m.role.value, m.day_index) for m in proposal.mentions] == [("PLANNED", 2), ("OPTIONAL", 2)]
    assert proposal.mentions[0].span_start != proposal.mentions[1].span_start


def test_choice_scope_keeps_observed_passed_and_excluded_places_out_of_visit_options():
    source = (
        "Day1：二选一\n### 方案 A：滨江\n"
        "游览云岭公园，眺望星河塔，路过石溪桥，不去枫林馆。\n"
        "### 方案 B：逛街\n游览青溪路。"
    )
    proposal = propose(source, [row("云岭公园"), row("星河塔", role="REFERENCE"),
                                row("石溪桥", role="PASS_THROUGH"), row("枫林馆", role="EXCLUDED"),
                                row("青溪路")])
    assert [(m.atomic_place_name, m.role.value) for m in proposal.mentions] == [
        ("云岭公园", "OPTIONAL"), ("星河塔", "REFERENCE"), ("石溪桥", "PASS_THROUGH"),
        ("枫林馆", "EXCLUDED"), ("青溪路", "OPTIONAL"),
    ]
    assert not any(m.eligible_for_place_search for m in EvidenceCompiler().compile(source, proposal)[0])


def test_only_an_explicit_optional_visit_can_correct_a_reference_role():
    source = "Day1：云岭公园。Iris咖啡可以打卡。"
    proposal = propose(source, [row("云岭公园"), row("Iris咖啡", role="REFERENCE", category="餐饮")])
    assert [m.role.value for m in proposal.mentions] == ["PLANNED", "OPTIONAL"]
    assert not EvidenceCompiler().compile(source, proposal)[0][1].eligible_for_place_search


def test_repair_exception_message_exposes_categories_only():
    # A slash is deliberately outside deterministic bold-list completion.
    source = "Day1：**星河公园 / 雾松私享博物馆**。"
    with pytest.raises(SourceAnchorValidationError) as raised:
        propose(source, [row("星河公园")])
    assert "雾松私享博物馆" in raised.value.repair_hints
    assert "雾松私享博物馆" not in str(raised.value)
    assert "雾松私享博物馆" not in json.dumps(raised.value.issues, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_succeeds", [True, False])
async def test_repair_hints_stay_in_current_model_request_not_bindings_logs_or_next_request(
    repair_succeeds, caplog, capsys,
):
    missing_name = "雾松私享博物馆"
    source = f"Day1：**星河公园 / {missing_name}**。"
    incomplete = {"destination": "北京", "activities": [row("星河公园")]}
    complete = {"destination": "北京", "activities": [row("星河公园"), row(missing_name)]}
    following = {"destination": "北京", "activities": [row("月泉古镇")]}
    requests = []

    async def create(**kwargs):
        requests.append(deepcopy(kwargs))
        body = incomplete if len(requests) == 1 or (len(requests) == 2 and not repair_succeeds) else complete
        if len(requests) == 3:
            body = following
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(body, ensure_ascii=False)))],
            usage=SimpleNamespace(prompt_tokens=80, completion_tokens=40), model="synthetic-model",
        )

    provider = ExperienceQwenProvider(api_key="synthetic-not-used", base_url="https://example.test/v1",
        model="synthetic-model", client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    if repair_succeeds:
        binding = (await provider.propose(source)).binding
    else:
        with pytest.raises(InferenceProviderUnavailableError) as raised:
            await provider.propose(source)
        binding = raised.value.provider_binding
        assert missing_name not in str(raised.value)
    assert len(requests) == 2
    assert len(requests[0]["messages"]) == 2
    assert requests[1]["messages"][-1]["role"] == "user"
    assert missing_name in requests[1]["messages"][-1]["content"]
    assert binding["external_calls"] == 2 and binding["repair_call_count"] == 1
    serialized = json.dumps(binding, ensure_ascii=False)
    assert missing_name not in serialized and source not in serialized and "repair_hints" not in serialized

    next_proposal = await provider.propose("Day1：月泉古镇。")
    assert len(requests) == 3 and len(requests[2]["messages"]) == 2
    assert missing_name not in json.dumps(requests[2]["messages"], ensure_ascii=False)
    assert next_proposal.binding["repair_call_count"] == 0
    captured = capsys.readouterr()
    assert missing_name not in captured.out + captured.err + caplog.text
