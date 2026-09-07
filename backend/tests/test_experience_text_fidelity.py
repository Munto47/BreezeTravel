"""Synthetic drafts exercise runtime guards; these are not live model scores."""
from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft, SourceAnchorValidationError, proposal_from_draft,
)
from app.trip_understanding.models import ActivityRole
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.timing_evidence import validated_timing


def activity(name, day=1, **values):
    return dict(source_quote=name, place_name=name, role="PLANNED", day_index=day,
                category="景点", **values)


class DraftProvider:
    def __init__(self, activities, **values):
        self.draft = SemanticDraft.model_validate(dict(destination="北京", activities=activities, **values))

    async def propose(self, source):
        return proposal_from_draft(source, self.draft)


class RecordingResolver(ControlledSnapshotPlaceResolver):
    def __init__(self):
        self.calls = []

    async def resolve(self, **values):
        self.calls.append(values)
        return await super().resolve(**values)


@pytest.mark.parametrize("evidence,fields,kept", [
    ("上午去故宫博物院", {"start_time": "09:00", "visit_duration_minutes": 60}, {}),
    ("10点到故宫博物院游览两小时", {"start_time": "10:00", "visit_duration_minutes": 120},
     {"start_time": "10:00", "visit_duration_minutes": 120}),
    ("下午两点半到故宫博物院停留一个半小时", {"start_time": "14:30", "visit_duration_minutes": 90},
     {"start_time": "14:30", "visit_duration_minutes": 90}),
    ("下午2点到故宫博物院停留1小时30分钟", {"start_time": "14:00", "visit_duration_minutes": 90},
     {"start_time": "14:00", "visit_duration_minutes": 90}),
    ("步行一小时到故宫博物院", {"visit_duration_minutes": 60}, {}),
    ("故宫博物院游览后步行60分钟", {"visit_duration_minutes": 60}, {}),
    ("故宫博物院停留半小时", {"visit_duration_minutes": 30}, {"visit_duration_minutes": 30}),
    ("故宫博物院游览10:00至12:00", {"start_time": "10:00", "end_time": "12:00"},
     {"start_time": "10:00", "end_time": "12:00"}),
    ("下午2点至4点参观故宫博物院", {"start_time": "14:00", "end_time": "16:00"},
     {"start_time": "14:00", "end_time": "16:00"}),
    ("故宫博物院需要预约", {"locked": True, "fixed_commitment": True}, {}),
    ("故宫博物院没有预约成功", {"locked": True, "fixed_commitment": True}, {}),
    ("如果故宫博物院预约成功", {"fixed_commitment": True}, {}),
    ("约10点到故宫博物院，停留约两小时", {"start_time": "10:00", "visit_duration_minutes": 120}, {}),
    ("故宫博物院已经预约，必须准时", {"locked": True, "fixed_commitment": True},
     {"locked": True, "fixed_commitment": True}),
])
def test_timing_and_commitment_require_specific_evidence(evidence, fields, kept):
    checked, removed = validated_timing(fields, evidence)
    for key in fields:
        assert checked[key] == kept.get(key, False if key in {"locked", "fixed_commitment"} else None)
    assert removed == any(fields[key] != kept.get(key, False if key in {"locked", "fixed_commitment"} else None) for key in fields)


def test_unsupported_timing_is_partial_and_cannot_become_text_facts():
    source = "北京第一天上午去故宫博物院。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": [
        activity("故宫博物院", start_time="09:00", visit_duration_minutes=60,
                 fixed_commitment=True, time_evidence="上午去故宫博物院"),
    ]}))
    mention = proposal.mentions[0]
    assert mention.start_time is None and mention.visit_duration_minutes is None
    assert mention.fixed_commitment is False and mention.timing_source == "UNSPECIFIED"
    assert proposal.unprocessed_count == 1


def test_same_name_repeat_cannot_borrow_the_earlier_day_time():
    source = "北京第一天9点到故宫博物院。第二天11点到故宫博物院。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": [
        activity("故宫博物院", start_time="09:00", time_evidence=source),
        activity("故宫博物院", 2, occurrence=2, start_time="09:00", time_evidence=source),
    ]}))
    assert proposal.mentions[0].start_time == "09:00"
    assert proposal.mentions[1].start_time is None
    assert len(proposal.mentions) == 2 and proposal.unprocessed_count == 1


def test_city_token_inside_a_street_name_is_not_city_evidence():
    proposal = proposal_from_draft("去北京路步行街。", SemanticDraft.model_validate({"destination": "北京", "activities": [
        activity("北京路步行街", city="北京", city_evidence="北京路步行街"),
    ]}))
    assert proposal.mentions[0].city_hint is None
    assert proposal.unprocessed_count == 1


@pytest.mark.asyncio
async def test_optional_only_day_and_trailing_empty_dates_are_visible_without_search():
    source = "北京，9月12日故宫博物院；9月13日景山公园或颐和园二选一；9月14日自由活动。"
    rows = [activity("故宫博物院"),
            {**activity("景山公园", 2), "role": "OPTIONAL"},
            {**activity("颐和园", 2), "role": "OPTIONAL"}]
    resolver = RecordingResolver()
    output = await TripUnderstandingPipeline(DraftProvider(rows, day_labels=["9月12日", "9月13日", "9月14日"]), resolver).run(source)
    assert [day.label for day in output.public_result.days] == ["9月12日", "9月13日", "9月14日"]
    assert [len(day.activities) for day in output.public_result.days] == [1, 0, 0]
    assert [choice.name for choice in output.public_result.days[1].alternatives] == ["景山公园", "颐和园"]
    assert len(resolver.calls) == 1
    assert set(output.public_result.days[1].alternatives[0].model_dump()) == {"name", "category", "city"}


@pytest.mark.asyncio
async def test_relative_empty_days_are_preserved_in_progress_and_result():
    source = "北京三日游。第一天故宫博物院。第二天和第三天自由活动。"
    updates = []
    async def progress(update):
        updates.append(update)
    output = await TripUnderstandingPipeline(DraftProvider([activity("故宫博物院")], day_labels=[None, None, None]),
                                             RecordingResolver()).run(source, progress_callback=progress)
    assert len(output.public_result.days) == 3
    assert len(updates[0].snapshot.days) == 3


@pytest.mark.asyncio
async def test_cross_city_places_use_individual_source_bound_cities():
    source = "北京、上海两日行程\nDay 1 北京\n故宫博物院\nDay 2 上海\n外滩"
    rows = [activity("故宫博物院", city="北京", city_evidence="Day 1 北京"),
            activity("外滩", 2, city="上海", city_evidence="Day 2 上海")]
    provider = DraftProvider(rows)
    provider.draft = provider.draft.model_copy(update={"destination": "北京、上海"})
    resolver = RecordingResolver()
    output = await TripUnderstandingPipeline(provider, resolver).run(source)
    assert [(call["city"], call["atomic_place_name"]) for call in resolver.calls] == [("北京", "故宫博物院"), ("上海", "外滩")]
    assert [day.activities[0].city for day in output.public_result.days] == ["北京", "上海"]
    assert output.public_result.status == "READY"


@pytest.mark.asyncio
async def test_cross_city_without_local_evidence_never_matches_in_the_first_city():
    source = "北京、上海两日行程\nDay 1 北京\n故宫博物院\nDay 2 上海\n故宫博物院"
    rows = [activity("故宫博物院", city="北京", city_evidence="Day 1 北京"),
            activity("故宫博物院", 2, occurrence=2, city="北京", city_evidence="Day 1 北京")]
    resolver = RecordingResolver()
    output = await TripUnderstandingPipeline(DraftProvider(rows), resolver).run(source)
    assert output.public_result.days[0].activities[0].status == "READY"
    assert output.public_result.days[1].activities[0].status == "NEEDS_CONFIRMATION"
    assert output.public_result.days[1].activities[0].city is None
    assert all(call["city"] != "北京" for call in resolver.calls[1:])


def test_plain_explicit_lists_are_checked_without_global_name_deduplication():
    with pytest.raises(SourceAnchorValidationError, match="MISSING_EXPLICIT_PARALLEL_PLACE"):
        proposal_from_draft("北京一天，故宫博物院、景山公园。", SemanticDraft.model_validate({
            "destination": "北京", "activities": [activity("故宫博物院")],
        }))
    source = "北京一天。路线：故宫博物院→景山公园。说明：故宫博物院是古代宫殿。晚上再去故宫博物院。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": [
        activity("故宫博物院"), activity("景山公园"), activity("故宫博物院", occurrence=2),
        activity("故宫博物院", occurrence=3),
    ]}))
    assert [mention.role for mention in proposal.mentions] == [ActivityRole.PLANNED, ActivityRole.PLANNED,
                                                            ActivityRole.REFERENCE, ActivityRole.PLANNED]
    assert len(proposal.mentions) == 4


@pytest.mark.parametrize("lead", ["先去", "计划去", "前往", "参观"])
def test_plain_list_action_prefix_does_not_become_a_required_place(lead):
    source = f"北京一天，{lead}故宫博物院、景山公园。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": [
        activity("故宫博物院"), activity("景山公园"),
    ]}))
    assert [mention.atomic_place_name for mention in proposal.mentions] == ["故宫博物院", "景山公园"]
    with pytest.raises(SourceAnchorValidationError, match="MISSING_EXPLICIT_PARALLEL_PLACE"):
        proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": [activity("故宫博物院")]}))


@pytest.mark.parametrize("source,field,rejected,accepted", [
    ("故宫博物院不是9点，而是10点到。", "start_time", "09:00", "10:00"),
    ("故宫博物院不是9点而是10点到。", "start_time", "09:00", "10:00"),
    ("故宫博物院不再停留2小时，实际只游览1小时。", "visit_duration_minutes", 120, 60),
    ("故宫博物院原来9点到，改成10点到。", "start_time", "09:00", "10:00"),
    ("故宫博物院9点改为10点到。", "start_time", "09:00", "10:00"),
])
def test_explicit_timing_correction_keeps_only_the_affirmed_value(source, field, rejected, accepted):
    for value in (rejected, accepted):
        proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": [
            activity("故宫博物院", **{field: value}, time_evidence=source),
        ]}))
        assert getattr(proposal.mentions[0], field) == (None if value == rejected else accepted)
        assert proposal.unprocessed_count == (1 if value == rejected else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("city,evidence", [
    ("北京", "北京路步行街，中山公园"),
    ("广州", "广州行程：北京路步行街，中山公园"),
])
async def test_city_evidence_cannot_borrow_another_places_city_substring(city, evidence):
    source = "广州行程：北京路步行街，中山公园。"
    rows = [activity("北京路步行街"), activity("中山公园", city=city, city_evidence=evidence)]
    provider = DraftProvider(rows)
    provider.draft = provider.draft.model_copy(update={"destination": "广州"})
    resolver = RecordingResolver()
    output = await TripUnderstandingPipeline(provider, resolver).run(source)
    assert resolver.calls[0]["city"] == "广州"
    if city == "北京":
        assert all(call["city"] != "北京" for call in resolver.calls)
        assert output.activities[1].compiled.mention.city_hint is None
    else:
        assert resolver.calls[1]["city"] == "广州"
        assert output.activities[1].compiled.mention.city_hint == "广州"


@pytest.mark.asyncio
async def test_null_day_labels_cannot_manufacture_days_beyond_the_source():
    source = "北京一日游，故宫博物院。"
    output = await TripUnderstandingPipeline(DraftProvider([activity("故宫博物院")], day_labels=[None, None, None]),
                                             RecordingResolver()).run(source)
    assert len(output.public_result.days) == 1
    assert output.public_result.status == "PARTIAL_RESULT"
    assert output.resolution_receipt["unprocessed_count"] == 1


def test_amended_plan_does_not_require_cancelled_places_to_reappear_in_draft():
    source = "杭州两天。原计划第1天西湖、雷峰塔，第2天灵隐寺、河坊街。最终更正：雷峰塔改到第2天灵隐寺之后，河坊街取消。这次没有取消西湖。以更正后的安排为准。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "杭州", "activities": [
        activity("西湖"), activity("灵隐寺", 2), activity("雷峰塔", 2),
    ]}))
    assert [(item.day_index, item.atomic_place_name, item.role.value) for item in proposal.mentions] == [
        (1, "西湖", "PLANNED"), (2, "灵隐寺", "PLANNED"), (2, "雷峰塔", "PLANNED"),
    ]
    assert proposal.unprocessed_count == 0


@pytest.mark.parametrize("suffix", ["前到", "以前到", "之后到", "以后到", "后到"])
def test_clock_bounds_do_not_become_exact_arrival_times(suffix):
    source = f"故宫博物院9点{suffix}。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": [
        activity("故宫博物院", start_time="09:00", time_evidence=source),
    ]}))
    assert proposal.mentions[0].start_time is None and proposal.unprocessed_count == 1
    assert validated_timing({"start_time": "09:00"}, "9点前往故宫博物院")[0]["start_time"] == "09:00"


@pytest.mark.parametrize("whole_table", [False, True])
def test_table_stay_duration_uses_only_this_rows_visit_column(whole_table):
    first = "|10:00|故宫博物院|120分钟|30分钟|"
    second = "|13:00|景山公园|60分钟|40分钟|"
    source = "北京一天\n|时间|到访地点|停留|交通时长|\n|---|---|---|---|\n" + first + "\n" + second
    for duration, accepted in ((120, 120), (30, None), (60, None)):
        proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": [
            activity("故宫博物院", start_time="10:00", visit_duration_minutes=duration,
                time_evidence=source if whole_table else first),
            activity("景山公园", start_time="13:00", visit_duration_minutes=60,
                time_evidence=source if whole_table else second),
        ]}))
        assert proposal.mentions[0].start_time == "10:00"
        assert proposal.mentions[0].visit_duration_minutes == accepted
        assert proposal.mentions[1].start_time == "13:00"
        assert proposal.mentions[1].visit_duration_minutes == 60
        assert proposal.unprocessed_count == (0 if duration == 120 else 1)


def test_table_transport_column_cannot_supply_visit_duration():
    source = "北京一天\n|时间|到访地点|交通时长|\n|---|---|---|\n|10:00|故宫博物院|游览后步行120分钟|"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": [
        activity("故宫博物院", start_time="10:00", visit_duration_minutes=120, time_evidence=source),
    ]}))
    assert proposal.mentions[0].start_time == "10:00"
    assert proposal.mentions[0].visit_duration_minutes is None and proposal.unprocessed_count == 1


def test_revised_original_plan_keeps_final_days_and_does_not_restore_cancelled_places():
    # changes-02 deterministically raised MISSING_EXPLICIT_PARALLEL_PLACE at
    # activities.parallel_group[0]: the list matcher treated 第2天灵隐寺 as a name.
    source = "杭州两天。原计划第1天西湖、雷峰塔，第2天灵隐寺、河坊街。最终更正：雷峰塔改到第2天灵隐寺之后，河坊街取消。这次没有取消西湖。以更正后的安排为准。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "杭州", "day_labels": [None, None], "activities": [
            activity("西湖"), activity("灵隐寺", 2), activity("雷峰塔", 2, occurrence=2),
            {**activity("河坊街", 2, occurrence=2), "role": "EXCLUDED"},
        ],
    }))
    assert [(item.day_index, item.atomic_place_name) for item in proposal.mentions if item.role == ActivityRole.PLANNED] == [
        (1, "西湖"), (2, "灵隐寺"), (2, "雷峰塔"),
    ]
    assert [(item.atomic_place_name, item.role) for item in proposal.mentions if item.role != ActivityRole.PLANNED] == [
        ("河坊街", ActivityRole.EXCLUDED),
    ]
    assert proposal.day_count == 2 and proposal.unprocessed_count == 0


@pytest.mark.parametrize("prefix", ["第2天", "第二天", "Day2", "9月12日", "原计划第2天"])
def test_plain_list_date_and_original_plan_prefixes_are_not_places(prefix):
    source = f"北京两天。{prefix}故宫博物院、景山公园。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": [
        activity("故宫博物院", 2), activity("景山公园", 2),
    ]}))
    assert [item.atomic_place_name for item in proposal.mentions] == ["故宫博物院", "景山公园"]
    with pytest.raises(SourceAnchorValidationError, match="MISSING_EXPLICIT_PARALLEL_PLACE"):
        proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": [activity("故宫博物院", 2)]}))


@pytest.mark.asyncio
async def test_narrative_list_does_not_reject_correct_cross_city_empty_day_and_alternative():
    # Reproduced MISSING_EXPLICIT_PARALLEL_PLACE before the conservative
    # strategy: 依次去西湖 was wrongly treated as a required atomic place.
    source = "上海和杭州三日游。Day 1 在上海，依次去外滩、豫园。Day 2 留在上海，当天没有确定的活动。备选：武康路（有空再考虑）。Day 3 到杭州，依次去西湖、河坊街。"
    rows = [activity("外滩", city="上海", city_evidence="Day 1 在上海"),
        activity("豫园", city="上海", city_evidence="Day 1 在上海"),
        {**activity("武康路", 2, city="上海", city_evidence="Day 2 留在上海"), "role": "OPTIONAL"},
        activity("西湖", 3, city="杭州", city_evidence="Day 3 到杭州"),
        activity("河坊街", 3, city="杭州", city_evidence="Day 3 到杭州")]
    provider = DraftProvider(rows, day_labels=[None, None, None])
    provider.draft = provider.draft.model_copy(update={"destination": "上海、杭州"})
    resolver = RecordingResolver()
    output = await TripUnderstandingPipeline(provider, resolver).run(source)
    assert [[card.name for card in day.activities] for day in output.public_result.days] == [
        ["外滩", "豫园"], [], ["西湖", "河坊街"],
    ]
    assert [item.name for item in output.public_result.days[1].alternatives] == ["武康路"]
    assert [(call["city"], call["atomic_place_name"]) for call in resolver.calls] == [
        ("上海", "外滩"), ("上海", "豫园"), ("杭州", "西湖"), ("杭州", "河坊街"),
    ]
    # This fixed POI snapshot leaves 西湖/河坊街 to confirm; parsing itself
    # must preserve every activity without a supplementary-list failure.
    assert output.resolution_receipt["unprocessed_count"] == 0


@pytest.mark.parametrize("sentence", [
    "依次去西湖、河坊街", "顺路看看西湖、河坊街", "接着安排到 西湖、河坊街", "西湖、顺路看看河坊街",
])
def test_unrecognised_narrative_is_not_a_supplementary_list_requirement(sentence):
    source = f"杭州一天。{sentence}。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "杭州", "activities": [
        activity("西湖"), activity("河坊街"),
    ]}))
    assert [item.atomic_place_name for item in proposal.mentions] == ["西湖", "河坊街"]


@pytest.mark.parametrize("omitted", ["故宫博物院", "景山公园", "天坛公园"])
def test_pure_list_still_rejects_missing_first_middle_or_last_place(omitted):
    names = ["故宫博物院", "景山公园", "天坛公园"]
    with pytest.raises(SourceAnchorValidationError, match="MISSING_EXPLICIT_PARALLEL_PLACE"):
        proposal_from_draft("北京一天。" + "、".join(names) + "。", SemanticDraft.model_validate({
            "destination": "北京", "activities": [activity(name) for name in names if name != omitted],
        }))
