"""Synthetic source-label boundaries; no model, resolver or network calls."""
from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    proposal_from_draft,
)
from app.trip_understanding.pipeline import EvidenceCompiler
from app.trip_understanding.place_labels import normalized_place_label


def propose(source, quote, name, role="OPTIONAL"):
    return proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "北京",
        "activities": [{"source_quote": quote, "place_name": name, "role": role, "day_index": 1}],
    }))


@pytest.mark.parametrize("alias_prefix", ["也叫", "又称", "又名", "别名", "别名是"])
def test_a_closed_alias_note_keeps_the_principal_literal_name(alias_prefix):
    label = f"星河公园（{alias_prefix}青溪园）"
    assert normalized_place_label(label) == "星河公园"
    assert normalized_place_label(normalized_place_label(label)) == "星河公园"


@pytest.mark.parametrize("role", ["PLANNED", "OPTIONAL"])
def test_alias_normalization_keeps_the_full_source_evidence_and_original_role(role):
    label = "星河公园（又名青溪园）"
    source = f"Day1：{label}。"
    proposal = propose(source, label, label, role)
    mention = proposal.mentions[0]
    assert mention.atomic_place_name == "星河公园"
    assert mention.raw_text == label
    assert source[mention.span_start:mention.span_end] == label
    assert mention.role.value == role
    compiled = EvidenceCompiler().compile(source, proposal)[0]
    assert compiled[0].eligible_for_place_search is (role == "PLANNED")


@pytest.mark.parametrize("label,expected", [
    ("星河博物馆（东院区，免费）", "星河博物馆（东院区）"),
    ("星河博物馆（人民广场馆，免费）", "星河博物馆（人民广场馆）"),
    ("Iris咖啡（云岭店，需预约）", "Iris咖啡（云岭店）"),
    ("云汀书院（星城中心 62 楼，咖啡看城市全景）", "云汀书院（星城中心62楼）"),
    ("星河博物馆（东院区，又名青溪院）", "星河博物馆（东院区，又名青溪院）"),
    ("星河博物馆（又名青溪院，免费）", "星河博物馆（又名青溪院，免费）"),
    ("云汀书院（Iris咖啡）", "云汀书院（Iris咖啡）"),
    ("云汀书院（星城中心62楼，又名云端店）", "云汀书院（星城中心62楼，又名云端店）"),
    ("星河公园（又名https://example.test）", "星河公园（又名https://example.test）"),
    ("星河公园（又名青溪园)", "星河公园（又名青溪园)"),
])
def test_identity_and_unclassified_alias_annotations_are_not_erased(label, expected):
    assert normalized_place_label(label) == expected


def test_an_alias_prefix_does_not_turn_an_action_sentence_into_a_closed_alias():
    label = "星河公园（也叫东馆出来再去云岭公园）"
    assert normalized_place_label(label) == label


@pytest.mark.parametrize("suffix", ["长城", "公园", "博物馆", "博物院", "景区"])
def test_only_an_unselected_literal_noun_can_drop_a_model_added_suffix(suffix):
    source = "Day1：备选青溪。"
    proposal = propose(source, "青溪", "青溪" + suffix)
    mention = proposal.mentions[0]
    assert (mention.atomic_place_name, mention.raw_text, mention.role.value, mention.day_index) == (
        "青溪", "青溪", "OPTIONAL", 1,
    )
    assert source[mention.span_start:mention.span_end] == "青溪"
    assert not EvidenceCompiler().compile(source, proposal)[0][0].eligible_for_place_search


@pytest.mark.parametrize("role", ["PLANNED", "REFERENCE", "EXCLUDED", "PASS_THROUGH"])
def test_other_roles_cannot_accept_a_suffix_absent_from_the_quote(role):
    with pytest.raises(SourceAnchorValidationError) as raised:
        propose("Day1：青溪。", "青溪", "青溪公园", role)
    assert any(issue["category"] == "PLACE_NOT_IN_SOURCE_QUOTE" for issue in raised.value.issues)


@pytest.mark.parametrize("expanded", [
    "青溪酒店", "青溪长城景区", "青溪公园东门", "青溪博物馆（东馆）", "青溪景区附近", "云岭青溪公园",
])
def test_unapproved_or_identity_changing_expansions_still_require_repair(expanded):
    with pytest.raises(SourceAnchorValidationError) as raised:
        propose("Day1：备选青溪。", "青溪", expanded)
    assert any(issue["category"] == "PLACE_NOT_IN_SOURCE_QUOTE" for issue in raised.value.issues)


@pytest.mark.parametrize("source,quote,expanded", [
    ("Day1：备选星河**山谷**。", "星河山谷", "星河山谷公园"),
    ("Day1：先去星河公园再去云岭公园。", "先去星河公园再去云岭公园", "先去星河公园再去云岭公园景区"),
])
def test_nonliteral_or_nonatomic_quotes_cannot_use_the_optional_recovery(source, quote, expanded):
    with pytest.raises(SourceAnchorValidationError) as raised:
        propose(source, quote, expanded)
    assert any(issue["category"] == "PLACE_NOT_IN_SOURCE_QUOTE" for issue in raised.value.issues)


@pytest.mark.parametrize("qualifier", ["（东院区）", "（人民广场馆）", "（星城中心62楼）", "北门"])
def test_optional_recovery_cannot_discard_an_attached_identity_restriction(qualifier):
    source = f"Day1：备选青溪{qualifier}。"
    if qualifier == "（星城中心62楼）":
        proposal = propose(source, "青溪", "青溪公园")
        mention = proposal.mentions[0]
        assert mention.atomic_place_name == mention.raw_text == "青溪" + qualifier
        assert mention.span_start == source.index("青溪")
        assert source[mention.span_start:mention.span_end] == "青溪" + qualifier
        assert mention.role.value == "OPTIONAL"
        assert not EvidenceCompiler().compile(source, proposal)[0][0].eligible_for_place_search
        return
    with pytest.raises(SourceAnchorValidationError) as raised:
        propose(source, "青溪", "青溪公园")
    assert any(issue["category"] == "PLACE_QUALIFIER_OMITTED" for issue in raised.value.issues)


def test_an_existing_source_suffix_is_never_misclassified_as_model_added():
    source = "Day1：备选青溪公园。"
    try:
        proposal = propose(source, "青溪", "青溪公园")
    except SourceAnchorValidationError as exc:
        # A short source quote may request repair; it must not silently shorten
        # a complete name already present at that exact source location.
        assert any(issue["category"] == "PLACE_NOT_IN_SOURCE_QUOTE" for issue in exc.issues)
    else:
        assert proposal.mentions[0].atomic_place_name == "青溪公园"
        assert proposal.mentions[0].raw_text == "青溪公园"
        assert not EvidenceCompiler().compile(source, proposal)[0][0].eligible_for_place_search


def test_a_complete_literal_qualified_optional_label_remains_intact():
    label = "青溪（东院区）"
    source = f"Day1：备选{label}。"
    proposal = propose(source, label, label)
    assert proposal.mentions[0].atomic_place_name == label
    assert proposal.mentions[0].raw_text == label
    assert not EvidenceCompiler().compile(source, proposal)[0][0].eligible_for_place_search


def test_an_expanded_parenthesized_label_conservatively_requests_repair():
    label = "青溪（东院区）"
    with pytest.raises(SourceAnchorValidationError) as raised:
        propose(f"Day1：备选{label}。", label, label + "博物馆")
    assert any(issue["category"] == "PLACE_NOT_IN_SOURCE_QUOTE" for issue in raised.value.issues)
