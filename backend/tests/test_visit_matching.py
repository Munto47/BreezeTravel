import pytest

from scripts.platform_corpus_metrics import compare_annotations
from scripts.visit_matching import validate_formal_annotation, whole_article_passed


def label(visits):
    return dict(annotation_status="reviewed", annotator_type="implementation_agent", activities=visits)


def test_overlapping_allowed_names_do_not_lose_a_constrained_visit():
    gold = [dict(name="甲馆", acceptable_names=["乙馆"], day_index=1), dict(name="甲馆", day_index=1)]
    actual = [dict(name="甲馆", day_index=1, role="PLANNED", sequence_index=1),
              dict(name="乙馆", day_index=1, role="PLANNED", sequence_index=0)]
    result = compare_annotations(label(gold), actual)
    assert result["visit_matches"] == result["matched_places"] == 2


def test_visit_found_with_wrong_role_is_separate_from_relation_correct_recall():
    gold = [dict(name="甲馆", day_index=1, role="OPTIONAL", span_start=3, span_end=5)]
    actual = [dict(name="甲馆", day_index=1, role="PLANNED", span_start=3, span_end=5)]
    result = compare_annotations(label(gold), actual)
    assert result["visit_matches"] == 1 and result["matched_places"] == 0


def test_unassessed_automatic_identity_prevents_exact_pass():
    result = compare_annotations(label([dict(name="甲馆", day_index=1)]),
        [dict(name="甲馆", day_index=1, role="PLANNED", poi_id="unreviewed", visible_correct=True)])
    assert result["unassessed_auto_confirmations"] == 1 and not result["semantic_exact"]


def test_extra_identity_outside_scored_roles_cannot_escape_assessment():
    result = compare_annotations(label([dict(name="甲馆", day_index=1)]),
        [dict(name="甲馆", day_index=1, role="PLANNED"),
         dict(name="乙馆", day_index=1, role="REFERENCE", poi_id="unreviewed")])
    assert result["unassessed_auto_confirmations"] == 1 and not result["semantic_exact"]


def test_incomplete_formal_annotation_is_rejected():
    with pytest.raises(ValueError):
        validate_formal_annotation(dict(acceptance_eligible=True, activities=[dict(name="甲馆")]))


def test_wire_array_order_is_irrelevant_but_wrong_visit_order_loses_relation_credit():
    gold = [dict(name=name, day_index=1) for name in ("甲馆", "乙馆")]
    actual = [dict(name=name, day_index=1, role="PLANNED", sequence_index=i) for i, name in enumerate(("甲馆", "乙馆"))]
    assert compare_annotations(label(gold), actual[::-1])["semantic_exact"]
    actual[0]["sequence_index"], actual[1]["sequence_index"] = 1, 0
    result = compare_annotations(label(gold), actual)
    assert result["visit_matches"] == 2 and result["matched_places"] == 0 and not result["semantic_exact"]


@pytest.mark.parametrize("change", [{"status":"FAILED"}, {"coverage":{"complete":False}},
    {"unprocessed_count":1}, {"unassessed_auto_confirmations":1}, {"wrong_auto_confirmations":1},
    {"visibility_assessed":False}, {"semantic_exact":False}])
def test_whole_article_requires_completion_identity_and_visibility(change):
    row = dict(status="COMPLETED", semantic_exact=True, coverage={"complete":True},
        unprocessed_count=0, unassessed_auto_confirmations=0, wrong_auto_confirmations=0, visibility_assessed=True)
    assert whole_article_passed(row)
    assert not whole_article_passed(row | change)
