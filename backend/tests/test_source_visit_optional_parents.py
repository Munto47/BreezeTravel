"""An unselected visit retains its own details without becoming a main stop."""
import json
from pathlib import Path

import pytest
import httpx

from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.models import ActivityRole, SourceSemanticPlan, UserFacingTripResult
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.semantic_supplement import source_visit_parents
from app.trip_understanding.source_visit_supplement import apply_source_visit_supplement
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_semantic_supplement_budget import Client, provider
from tests.test_source_visit_supplement import plan


def row(name, evidence, parent_index=0, optional=False):
    return dict(parent_index=parent_index, kind="VISIT", source_quote=name,
                optional=optional, evidence=evidence)


def apply(source, before, rows):
    return apply_source_visit_supplement(source, before, rows,
        parent_ids=[m.mention_id for m in before.mentions if not m.parent_mention_id])


def optional_plan(source, parents):
    before = plan(source, parents)
    before.mentions = [m.model_copy(update={"role": ActivityRole.OPTIONAL}) for m in before.mentions]
    return before


@pytest.mark.asyncio
async def test_optional_parent_details_keep_role_day_branch_and_public_outlet():
    source = "上海。\nDay1：青岚乐园，必玩：云海航船、星光环线。"
    before = optional_plan(source, [("青岚乐园", 1, 1)])
    parent = before.mentions[0]
    parent.choice_group_id = "group-1"
    parent.branch_id = "branch-2"
    parent.branch_label = "方案B"
    after = apply(source, before, [row("云海航船", "必玩：云海航船、星光环线"),
                                   row("星光环线", "必玩：云海航船、星光环线")])
    assert after.mentions[0] == parent and len(after.mentions) == 3
    assert after.unprocessed_count == before.unprocessed_count
    for child in after.mentions[1:]:
        assert child.role == ActivityRole.REFERENCE and child.parent_mention_id == parent.mention_id
        assert child.day_index == 1 and child.branch_id == parent.branch_id
        assert child.choice_group_id == parent.choice_group_id and child.branch_label == parent.branch_label
    # Both persisted plan and public default readers preserve these additive fields.
    saved = SourceSemanticPlan.model_validate(after.model_dump(mode="json"))
    result = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=saved)
    public = UserFacingTripResult.model_validate(result.public_result.model_dump(mode="json"))
    assert not public.days[0].activities and result.resolution_receipt["attempted_count"] == 0
    assert [a.name for a in public.days[0].alternatives] == ["青岚乐园"]
    assert [d.name for d in public.days[0].alternatives[0].source_details] == ["云海航船", "星光环线"]
    assert not any(d.category == "PUBLIC_PROJECTION_OMISSION" for d in result.proposal.diagnostics)


def test_parent_table_appends_optional_visits_without_reindexing_existing_main_visits():
    source = "上海。\nDay1：青岚乐园，若有空去晨光公园，然后去晚霞公园。"
    before = plan(source, [("青岚乐园", 1, 1), ("晨光公园", 1, 1), ("晚霞公园", 1, 1)])
    before.mentions[1].role = ActivityRole.OPTIONAL
    assert [p.atomic_place_name for p in source_visit_parents(before)] == ["青岚乐园", "晚霞公园", "晨光公园"]


@pytest.mark.parametrize("role", [ActivityRole.REFERENCE, ActivityRole.EXCLUDED, ActivityRole.PASS_THROUGH])
def test_non_visit_parent_never_acquires_details(role):
    source = "上海。\nDay1：青岚乐园，园内参观云海航船。"
    before = optional_plan(source, [("青岚乐园", 1, 1)])
    before.mentions[0].role = role
    after = apply(source, before, [row("云海航船", "园内参观云海航船")])
    assert after.mentions == before.mentions and after.unprocessed_count > before.unprocessed_count
    assert source_visit_parents(before) == []


@pytest.mark.parametrize("source,parents,detail", [
    ("上海。\nDay1：青岚乐园。\nDay2：晨光公园，园内参观云海航船。",
     [("青岚乐园", 1, 1), ("晨光公园", 2, 1)], row("云海航船", "园内参观云海航船")),
    ("上海。\nDay1：青岚乐园，眺望晨光公园。", [("青岚乐园", 1, 1)], row("晨光公园", "眺望晨光公园")),
    ("上海。\nDay1：青岚乐园，园内取消参观云海航船。", [("青岚乐园", 1, 1)], row("云海航船", "园内取消参观云海航船")),
    ("上海。\nDay1：青岚乐园。之后去晨光公园，园内参观云海航船。",
     [("青岚乐园", 1, 1), ("晨光公园", 1, 1)], row("云海航船", "园内参观云海航船")),
    ("上海。\nDay1：二选一。\n方案A：青岚乐园，园内参观晨光亭。\n方案B：青岚乐园，园内参观云海航船。",
     [("青岚乐园", 1, 1), ("青岚乐园", 1, 2)], row("云海航船", "园内参观云海航船")),
    ("上海。\nDay1：二选一。\n方案A：青岚乐园。\n方案B：园内参观云海航船。",
     [("青岚乐园", 1, 1)], row("云海航船", "园内参观云海航船")),
    ("上海。\nDay1.5：青岚乐园，园内参观云海航船。",
     [("青岚乐园", 1, 1)], row("云海航船", "园内参观云海航船")),
    ("上海。\nDay1：更正，把青岚乐园改到Day2，园内参观云海航船。",
     [("青岚乐园", 1, 1)], row("云海航船", "园内参观云海航船")),
])
def test_optional_parent_does_not_borrow_another_scope_or_cancelled_detail(source, parents, detail):
    before = optional_plan(source, parents)
    after = apply(source, before, [detail])
    assert after.mentions == before.mentions and after.unprocessed_count > before.unprocessed_count


def test_two_optional_occurrences_keep_their_distinct_children():
    source = "上海。\nDay1：二选一。\n方案A：青岚乐园，园内参观晨光亭。\n方案B：青岚乐园，园内参观云海航船。"
    before = optional_plan(source, [("青岚乐园", 1, 1), ("青岚乐园", 1, 2)])
    for index, parent in enumerate(before.mentions):
        parent.branch_id = f"branch-{index}"
    after = apply(source, before, [row("晨光亭", "园内参观晨光亭"), row("云海航船", "园内参观云海航船", 1)])
    assert [m.parent_mention_id for m in after.mentions[2:]] == [m.mention_id for m in before.mentions]
    assert [m.branch_id for m in after.mentions[2:]] == ["branch-0", "branch-1"]
    assert after.unprocessed_count == before.unprocessed_count


def test_parent_condition_is_not_a_new_condition_on_each_internal_item():
    source = "上海。\nDay1：如果有时间去青岚乐园，园内参观晨光亭；若有余力，再看园内云海航船。"
    before = optional_plan(source, [("青岚乐园", 1, 1)])
    after = apply(source, before, [row("晨光亭", "园内参观晨光亭"),
                                  row("云海航船", "若有余力，再看园内云海航船", optional=True)])
    assert len(after.mentions) == 3 and after.unprocessed_count == before.unprocessed_count
    assert [m.role for m in after.mentions[1:]] == [ActivityRole.REFERENCE, ActivityRole.OPTIONAL]


@pytest.mark.parametrize("gate_first", [True, False])
def test_reconciled_optional_does_not_become_an_independent_parent_for_the_exit(gate_first):
    from tests.test_source_visit_optional_reconciliation import prepared, detail, SOURCE

    source = SOURCE + "最后从北门出。"
    before = prepared(source)
    gate = dict(parent_index=0, kind="EXIT", source_quote="北门", optional=False, evidence="最后从北门出")
    rows = [gate, detail()] if gate_first else [detail(), gate]
    after = apply(source, before, rows)
    assert len(after.mentions) == 3 and after.unprocessed_count == before.unprocessed_count
    assert all(m.parent_mention_id == before.mentions[0].mention_id for m in after.mentions[1:])


def test_failed_optional_order_cannot_authorize_a_gate_across_the_original_independent_visit():
    from tests.test_source_visit_optional_reconciliation import prepared, detail, SOURCE

    source = SOURCE + "最后从北门出。"
    before = apply(source, prepared(source), [detail("晨光亭")])
    gate = dict(parent_index=0, kind="EXIT", source_quote="北门", optional=False, evidence="最后从北门出")
    after = apply(source, before, [gate, detail()])
    assert after.mentions == before.mentions and after.unprocessed_count > before.unprocessed_count


def test_typed_first_answer_relation_also_keeps_optional_parent():
    source = "上海。\nDay1：青岚乐园，园内参观晨光亭，若有时间参观云海航船。"
    draft = SemanticDraft.model_validate(dict(destination="上海", activities=[
        dict(source_quote="青岚乐园", place_name="青岚乐园", role="OPTIONAL", day_index=1, category="景点"),
        dict(source_quote="晨光亭", place_name="晨光亭", role="REFERENCE", day_index=1, category="景点",
             parent_source_quote="青岚乐园", role_evidence="园内参观晨光亭"),
        dict(source_quote="云海航船", place_name="云海航船", role="OPTIONAL", day_index=1, category="景点",
             parent_source_quote="青岚乐园", role_evidence="若有时间参观云海航船"),
    ]))
    result = _proposal_from_live_draft(source, draft)
    assert len(result.mentions) == 3
    assert [m.parent_mention_id for m in result.mentions[1:]] == [result.mentions[0].mention_id] * 2
    assert [m.role for m in result.mentions] == [ActivityRole.OPTIONAL, ActivityRole.REFERENCE, ActivityRole.OPTIONAL]


@pytest.mark.asyncio
async def test_captured_shanghai_first_and_controlled_four_details_reach_optional_parent_only():
    # Original live first answer remains unchanged. This second answer is
    # deliberately controlled, not evidence of new model extraction quality.
    fixture = json.loads((Path(__file__).parent / "fixtures/live_owner_shanghai_meal_context.json").read_text(encoding="utf-8"))
    source, first = fixture["source"], json.loads(fixture["response"])
    before = _proposal_from_live_draft(source, SemanticDraft.model_validate(first))
    parents = source_visit_parents(before)
    disney = next(m for m in before.mentions if m.atomic_place_name == "上海迪士尼")
    names = ["飞跃地平线", "加勒比海盗", "创极速光轮", "疯狂动物城园区"]
    evidence = "必玩：" + "、".join(names)
    parent_index = next((i for i, m in enumerate(parents) if m.mention_id == disney.mention_id), len(parents))
    second = dict(city_fields=[], source_visits=[row(name, evidence, parent_index) for name in names])
    client = Client(first, second)
    result = await TripUnderstandingPipeline(provider(client, deadline=5), FixedReplayPlaces()).run(source)
    assert len(client.calls) == result.inference_binding["external_calls"] == 2
    assert json.loads(client.calls[1]["messages"][1]["content"])["parents"][parent_index]["role"] == "OPTIONAL"
    assert result.proposal.mentions[:len(before.mentions)] == before.mentions
    assert sum(m.role == ActivityRole.PLANNED and bool(m.atomic_place_name) for m in result.proposal.mentions) == 17
    day3 = result.public_result.days[2]
    assert day3.activities == []
    alternative = next(a for a in day3.alternatives if a.name == "上海迪士尼")
    assert [d.name for d in alternative.source_details] == names
    assert not any(d.optional for d in alternative.source_details)
    assert all(name not in [a.name for d in result.public_result.days for a in (*d.activities, *d.alternatives)] for name in names)
    assert result.resolution_receipt["attempted_count"] == 17
    assert not any(d.category == "PUBLIC_PROJECTION_OMISSION" for d in result.proposal.diagnostics)
    assert not alternative.choice_group_selectable and not result.public_result.coverage.complete


async def build_shanghai_optional_parent_live_result():
    """Real first + separately obtained real second + saved POIs; no new HTTP."""
    from collections import defaultdict, deque

    folder = Path(__file__).parent / "fixtures"
    first = json.loads((folder / "live_owner_shanghai_meal_context.json").read_text(encoding="utf-8"))
    second = json.loads((folder / "live_shanghai_optional_parent_second.json").read_text(encoding="utf-8"))
    responses = defaultdict(deque)
    for call in first["place_calls"]:
        responses[(call["path"], tuple(sorted(call["query"].items())))].append(call)
    used = []

    def reply(request):
        query = {key: value for key, value in request.url.params.multi_items()
                 if key.lower() not in {"key", "sig", "token"}}
        key = (request.url.path, tuple(sorted(query.items())))
        assert responses[key], "Unexpected request; there is no external fallback."
        call = responses[key].popleft()
        used.append(key)
        return httpx.Response(200, json=call["response"], request=request)

    client = Client(first["response"], second["response"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
        places = AmapPlaceResolver(api_key="fixed-only", client=http)
        result = await TripUnderstandingPipeline(provider(client, deadline=10), places).run(first["source"])
        await places.aclose()
    assert len(client.calls) == 2 and len(used) == 20 and not any(responses.values())
    return result


@pytest.mark.asyncio
async def test_actual_shanghai_second_failure_preserves_mainline_and_reports_every_rejected_row():
    result = await build_shanghai_optional_parent_live_result()
    saved = json.loads((Path(__file__).parent / "fixtures/live_owner_shanghai_meal_context.json").read_text(encoding="utf-8"))
    identity = {a.compiled.mention.atomic_place_name: a.place.canonical_place_id if a.place else None
                for a in result.activities if a.compiled.mention.role == ActivityRole.PLANNED and a.compiled.mention.atomic_place_name}
    assert len(identity) == 17
    assert [identity[item["source_name"]] for item in saved["expected_main_identities"]] == [
        item["poi_id"] for item in saved["expected_main_identities"]]
    public = result.public_result
    assert sum(card.status == "READY" for day in public.days for card in day.activities) == 13
    assert sum(card.status == "NEEDS_CONFIRMATION" for day in public.days for card in day.activities) == 4
    assert not public.days[2].activities
    assert all(not card.source_details for day in public.days for card in (*day.activities, *day.alternatives))
    rejected = [d for d in result.proposal.diagnostics if d.category == "SOURCE_VISIT_UNRESOLVED"]
    assert {d.field for d in rejected} == {f"source_visits[{i}]" for i in range(26)}
    assert public.coverage.unprocessed_count >= 26 and not public.coverage.complete
    assert all(not card.choice_group_selectable for card in public.days[2].alternatives)
    # This test preserves an observed model failure. The controlled four-detail
    # sibling test is transport coverage and must not overwrite this outcome.
