"""Dining alternatives apply to their exact source occurrence, not a road name."""
from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.pipeline import EvidenceCompiler


def test_separate_planned_meal_streets_become_optional_without_changing_the_morning_revisit():
    source = "Day1\n上午去云岭路。\n- 中午：云岭路 / 星河路吃本地面、简餐。"
    rows = [
        {"source_quote": "云岭路", "place_name": "云岭路", "role": "PLANNED", "day_index": 1, "occurrence": 1},
        {"source_quote": "云岭路", "place_name": "云岭路", "role": "PLANNED", "day_index": 1, "occurrence": 2},
        {"source_quote": "星河路", "place_name": "星河路", "role": "PLANNED", "day_index": 1, "occurrence": 1},
    ]
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "上海", "activities": rows}))
    assert len(proposal.mentions) == len(rows)
    assert [mention.atomic_place_name for mention in proposal.mentions] == ["云岭路", "云岭路", "星河路"]
    assert [mention.day_index for mention in proposal.mentions] == [1, 1, 1]
    assert [mention.role.value for mention in proposal.mentions] == ["PLANNED", "OPTIONAL", "OPTIONAL"]
    assert [mention.span_start for mention in proposal.mentions] == [
        source.index("云岭路"), source.rindex("云岭路"), source.index("星河路"),
    ]
    assert [mention.raw_text for mention in proposal.mentions] == ["云岭路", "云岭路", "星河路"]
    compiled = EvidenceCompiler().compile(source, proposal)[0]
    assert [mention.eligible_for_place_search for mention in compiled] == [True, False, False]


def test_explicitly_visiting_both_meal_streets_keeps_both_planned():
    source = "Day1\n午餐：青溪街 / 望星胡同吃饭，分别吃面和点心。"
    rows = [
        {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": 1}
        for name in ["青溪街", "望星胡同"]
    ]
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": rows}))
    assert [mention.atomic_place_name for mention in proposal.mentions] == ["青溪街", "望星胡同"]
    assert [mention.role.value for mention in proposal.mentions] == ["PLANNED", "PLANNED"]
