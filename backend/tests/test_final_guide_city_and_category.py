"""Reviewed area categories and advance booking do not alter travel days."""
import pytest

from app.trip_understanding.experience_inference import SemanticDraft, SourceAnchorValidationError, proposal_from_draft


def propose(source, name, *, day=1, category="地点", destination="北京"):
    return proposal_from_draft(source, SemanticDraft.model_validate({"destination": destination, "activities": [
        {"source_quote": name, "place_name": name, "role": "PLANNED", "category": category, "day_index": day},
    ]}))


@pytest.mark.parametrize("name", ["大栅栏", "前门大栅栏"])
def test_reviewed_beijing_area_is_not_a_restaurant(name):
    source = f"Day1：{name}吃小吃。"
    mention = propose(source, name, category="餐饮").mentions[0]
    assert mention.category_hint == "地点"
    assert mention.atomic_place_name == mention.raw_text == name
    assert source[mention.span_start:mention.span_end] == name


@pytest.mark.parametrize("name", ["青溪餐厅（大栅栏店）", "大栅栏食府"])
def test_reviewed_area_name_inside_a_restaurant_does_not_change_its_category(name):
    assert propose(f"Day1：{name}。", name, category="餐饮").mentions[0].category_hint == "餐饮"


def test_same_label_in_another_city_is_not_given_beijing_area_authority():
    assert propose("Day1：前门大栅栏。", "前门大栅栏", category="餐饮", destination="上海").mentions[0].category_hint == "餐饮"


@pytest.mark.parametrize("preparation", ["门票提前买好", "提前买票", "行李提前备好"])
def test_advance_preparation_does_not_hide_a_wrong_day_assignment(preparation):
    source = f"Day1：云岚公园。\nDay2：星河寺。\nDay3：青溪书院。{preparation}。"
    with pytest.raises(SourceAnchorValidationError, match="SOURCE_DAY_QUOTE_MISMATCH"):
        propose(source, "星河寺", day=3)
    mention = propose(source, "星河寺", day=2).mentions[0]
    assert mention.day_index == 2
    assert mention.span_start == source.index("星河寺")
