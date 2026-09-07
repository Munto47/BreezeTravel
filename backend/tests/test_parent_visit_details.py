import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft


@pytest.mark.parametrize("parent,detail,suffix", [
    ("星河广场", "纪念碑", "安检进场，打卡纪念碑，拍照留念"),
    ("青溪博物院", "望星门", "（门票自理），望星门出，游览三小时"),
    ("青溪公园", "南门", "，南门进，散步"),
])
def test_parent_visit_details_do_not_become_extra_route_stops(parent, detail, suffix):
    source = f"Day1：{parent}{suffix}。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "上海", "activities": [
        {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": 1}
        for name in [parent, detail]
    ]}))
    assert [m.role.value for m in proposal.mentions] == ["PLANNED", "REFERENCE"]
    assert proposal.mentions[1].atomic_place_name == detail


@pytest.mark.parametrize("suffix,detail", [
    ("，然后去看纪念碑。", "纪念碑"),
    ("，再去打卡纪念碑。", "纪念碑"),
    ("。\n下一站：纪念碑。", "纪念碑"),
    ("，参观星河纪念碑。", "星河纪念碑"),
    ("，接着从望星门出。", "望星门"),
    ("，望星门摄影展。", "望星门"),
])
def test_explicit_separate_or_named_visits_remain_planned(suffix, detail):
    source = "Day1：青溪公园" + suffix
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "上海", "activities": [
        {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": 1}
        for name in ["青溪公园", detail]
    ]}))
    assert [m.role.value for m in proposal.mentions] == ["PLANNED", "PLANNED"]


@pytest.mark.parametrize("source,names", [
    ("Day1：青溪园｜建议南门进：星河街→望月阁→长廊，逛两小时。", ["青溪园", "星河街", "望月阁", "长廊"]),
    ("Day1：青溪公园，买联票，打卡望月殿、星河坛。", ["青溪公园", "望月殿", "星河坛"]),
])
def test_explicit_internal_route_or_ticket_buildings_remain_parent_details(source, names):
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "上海", "activities": [
        {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": 1} for name in names
    ]}))
    assert [m.role.value for m in proposal.mentions] == ["PLANNED"] + ["REFERENCE"] * (len(names) - 1)


@pytest.mark.parametrize("source", [
    "Day1：青溪公园→望月阁。",
    "Day1：青溪公园买联票，然后打卡望月阁。",
    "Day1：青溪公园，南门进：星河街，出园后去望月阁。",
])
def test_standalone_arrows_or_leaving_parent_does_not_remove_next_stop(source):
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "上海", "activities": [
        {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": 1}
        for name in ["青溪公园", "望月阁"]
    ]}))
    assert [m.role.value for m in proposal.mentions] == ["PLANNED", "PLANNED"]
