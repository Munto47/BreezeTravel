"""Constructed input and deterministic places for projection contract tests."""
from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.source_visit_supplement import apply_source_visit_supplement
from tests.semantic_page_replays import FixedReplayPlaces


SOURCE = ("上海两日。\nDay1：上海博物馆东馆，馆内看青铜器展。然后去陆家嘴。"
          "如果下雨，就把陆家嘴替换成上海科技馆。最后回上海博物馆东馆，仅取物，不参观。"
          "\nDay2：世纪公园，然后去外滩。豫园只是备选。南京路这次不去。")
VISITS = [
    ("上海博物馆东馆", 1, "PLANNED", 1, None),
    ("青铜器展", 1, "PLANNED", 1, "U1"),
    ("陆家嘴", 1, "PLANNED", 1, None),
    ("上海科技馆", 1, "OPTIONAL", 1, None),
    ("上海博物馆东馆", 1, "PLANNED", 2, None),
    ("世纪公园", 2, "PLANNED", 1, None),
    ("外滩", 2, "PLANNED", 1, None),
    ("豫园", 2, "OPTIONAL", 1, None),
    ("南京路", 2, "EXCLUDED", 1, None),
]


def demo_annotation(source=SOURCE):
    activities = []
    for index, (name, day, role, occurrence, parent) in enumerate(VISITS, 1):
        start = -1
        for _ in range(occurrence):
            start = source.index(name, start + 1)
        item = dict(id=f"U{index}", name=name, day_index=day, role=role,
                    span_start=start, span_end=start + len(name), parent_id=parent,
                    replaces_id=None, purpose=None)
        if parent:
            item.update(relation_type="INTERNAL_DETAIL", detail_kind="VISIT")
        elif role == "PLANNED":
            item["expected_poi_ids"] = [f"synthetic-replay:上海:{name}"]
        if index == 4:
            item.update(replaces_id="U3", replacement_condition="如果下雨")
        if index == 5:
            item["purpose"] = "PICKUP_ONLY"
        activities.append(item)
    return dict(annotation_status="reviewed", annotator_type="implementation_agent",
                roles_in_scope=["PLANNED", "OPTIONAL", "EXCLUDED"], activities=activities)


async def replay():
    rows = []
    for name, day, role, occurrence, parent in VISITS:
        if parent:
            continue
        item = dict(source_quote=name, place_name=name, day_index=day, role=role,
                    occurrence=occurrence, category="景点", city="上海", city_evidence="上海两日")
        if name == "上海科技馆":
            item["conditional_replacement"] = dict(target_quote="陆家嘴", target_occurrence=1,
                condition_quote="如果下雨", evidence="如果下雨，就把陆家嘴替换成上海科技馆")
        rows.append(item)
    proposal = _proposal_from_live_draft(SOURCE, SemanticDraft.model_validate(dict(destination="上海", activities=rows)))
    roots = [item.mention_id for item in proposal.mentions if not item.parent_mention_id]
    proposal = apply_source_visit_supplement(SOURCE, proposal, [
        dict(parent_index=0, kind="VISIT", source_quote="青铜器展", occurrence=1,
             optional=False, evidence="上海博物馆东馆，馆内看青铜器展"),
        dict(parent_index=3, kind="PICKUP_ONLY", source_quote="上海博物馆东馆", occurrence=2,
             optional=False, evidence="最后回上海博物馆东馆，仅取物，不参观"),
    ], parent_ids=roots)
    return await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(SOURCE, prepared_plan=proposal)
