from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from evals.dual_entry_scorer import score_metric_oracles
from evals.continuous import preflight
from evals.continuous.http_builder import _load_selected_builder_cases_and_labels
from evals.frozen_suggestion_oracle import (
    LABEL_AUTHORITY,
    build_oracle,
    canonical_builder_actuals,
    load_bound_oracle,
    overlay_case_oracles,
    validate_oracle,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATH = REPO_ROOT / "backend/evidence/real_provider_local_authorized/suggestion_snapshot_2026-08-21.json"
CHAIN_SOURCE_PATH = (
    REPO_ROOT
    / "backend/evidence/real_provider_local_authorized/suggestion_chain_snapshot_2026-08-21-v2.json"
)
ORACLE_PATH = (
    REPO_ROOT
    / "backend/eval_data/dual_entry_v1/builder_oracles/three_city_frozen_suggestion_ranking_v1.json"
)
RUN_SPEC_PATH = REPO_ROOT / "backend/evals/run_specs/dual-entry-builder-http-slice.json"


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_json(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return _hash_bytes(raw)


def _binding() -> dict[str, str]:
    return {
        "path": ORACLE_PATH.relative_to(REPO_ROOT).as_posix(),
        "sha256": _hash_bytes(ORACLE_PATH.read_bytes()),
        "source_snapshot_path": SOURCE_PATH.relative_to(REPO_ROOT).as_posix(),
        "source_snapshot_sha256": _hash_bytes(SOURCE_PATH.read_bytes()),
    }


def test_checked_in_oracle_is_hash_bound_to_three_city_real_canonical_receipts():
    artifact = load_bound_oracle(_binding(), REPO_ROOT)

    assert artifact["content"]["label_authority"] == LABEL_AUTHORITY
    assert artifact["content"]["is_human_label"] is False
    assert artifact["content"]["rubric"]["product_ranking_fields_read"] is False
    assert [row["city"] for row in artifact["content"]["cities"]] == ["北京", "上海", "杭州"]
    assert all(row["candidate_universe_size"] == 6 for row in artifact["content"]["cities"])
    for row in artifact["content"]["cities"]:
        for candidate in row["candidates"]:
            assert candidate["canonical_candidate_id"] == candidate["entity_receipt_ref"]["canonical_place_id"]
            assert len(candidate["entity_receipt_ref"]["receipt_sha256"]) == 64
            assert candidate["route_receipt_refs"]
            assert all(len(item["receipt_sha256"]) == 64 for item in candidate["route_receipt_refs"])
            assert all(len(item["fact_receipt_sha256"]) == 64 for item in candidate["current_fact_receipt_refs"])
            assert candidate["subjective_boundaries"]["human_preference_fit"] == "UNKNOWN"
            assert candidate["subjective_boundaries"]["opening_fit_without_visit_slot"] == "N_A"
            assert candidate["subjective_boundaries"]["community_or_official_prior_used_as_current_fact"] is False


def test_oracle_generation_rejects_product_score_pollution():
    snapshot = json.loads(SOURCE_PATH.read_bytes())
    snapshot["cities"][0]["candidates"][0]["total_score"] = 0.999

    with pytest.raises(ValueError, match="ORACLE_PRODUCT_RANKING_FIELD_POLLUTION:total_score"):
        build_oracle(
            snapshot,
            source_path=SOURCE_PATH.relative_to(REPO_ROOT).as_posix(),
            source_sha256=_hash_bytes(SOURCE_PATH.read_bytes()),
        )


def test_chain_oracle_uses_only_initial_fixed_anchor_and_leaves_three_round_safety_to_g2():
    raw = CHAIN_SOURCE_PATH.read_bytes()
    snapshot = json.loads(raw)

    artifact = build_oracle(
        snapshot,
        source_path=CHAIN_SOURCE_PATH.relative_to(REPO_ROOT).as_posix(),
        source_sha256=_hash_bytes(raw),
    )

    assert [row["city"] for row in artifact["content"]["cities"]] == ["北京", "上海", "杭州"]
    assert artifact["content"]["source"]["ranking_scope"] == "INITIAL_FIXED_ANCHOR_ONLY"
    assert artifact["content"]["source"]["three_round_product_safety_scored_by"] == (
        "G2_DETERMINISTIC_HTTP_CHECKS"
    )
    for city_oracle, captured_city in zip(
        artifact["content"]["cities"], snapshot["cities"], strict=True
    ):
        first_round = captured_city["rounds"][0]
        assert city_oracle["anchor"] == first_round["anchor"]
        assert city_oracle["candidate_universe_size"] == len(first_round["candidates"])


def test_oracle_file_and_content_tampering_fail_closed():
    artifact = json.loads(ORACLE_PATH.read_bytes())
    snapshot = json.loads(SOURCE_PATH.read_bytes())
    artifact["content"]["cities"][0]["candidates"][0]["relevance_grade"] = 0

    with pytest.raises(ValueError, match="ORACLE_ARTIFACT_HASH_MISMATCH"):
        validate_oracle(
            artifact,
            artifact_sha256=_binding()["sha256"],
            source_snapshot=snapshot,
            source_sha256=_binding()["source_snapshot_sha256"],
        )


def test_rehashed_candidate_id_mismatch_still_fails_source_recomputation():
    artifact = json.loads(ORACLE_PATH.read_bytes())
    snapshot = json.loads(SOURCE_PATH.read_bytes())
    artifact["content"]["cities"][0]["candidates"][0]["canonical_candidate_id"] = "synthetic-id"
    artifact["content_sha256"] = _hash_json(artifact["content"])
    artifact_sha256 = _hash_json(artifact)

    with pytest.raises(ValueError, match="ORACLE_SOURCE_RECOMPUTE_MISMATCH"):
        validate_oracle(
            artifact,
            artifact_sha256=artifact_sha256,
            source_snapshot=snapshot,
            source_sha256=_binding()["source_snapshot_sha256"],
        )


def test_wrong_city_duplicate_hard_and_unknown_are_forced_zero_by_fixed_rubric():
    source_sha = _hash_bytes(SOURCE_PATH.read_bytes())
    base = json.loads(SOURCE_PATH.read_bytes())
    mutations = []

    wrong_city = copy.deepcopy(base)
    wrong = wrong_city["cities"][0]["candidates"][0]
    wrong["canonical_place"]["city"] = wrong["provider_receipt"]["city"] = "上海"
    mutations.append(wrong_city)

    duplicate = copy.deepcopy(base)
    dup = duplicate["cities"][0]["candidates"][0]
    anchor_id = duplicate["cities"][0]["anchor"]["place_id"]
    dup["canonical_place"]["place_id"] = anchor_id
    dup["provider_receipt"]["canonical_place_id"] = anchor_id
    dup["provider_receipt"]["provider_place_id"] = anchor_id
    dup["route_times"]["route_receipts"][0]["destination_place_id"] = anchor_id
    mutations.append(duplicate)

    hard = copy.deepcopy(base)
    hard["cities"][0]["candidates"][0]["hard_block_codes"] = ["MEMBER_HARD_CONSTRAINT"]
    mutations.append(hard)

    unknown = copy.deepcopy(base)
    route = unknown["cities"][0]["candidates"][0]["route_times"]
    route.update({
        "status": "UNKNOWN",
        "previous_to_candidate_minutes": None,
        "route_receipts": [],
        "reason_code": "ROUTE_PROVIDER_UNAVAILABLE",
    })
    mutations.append(unknown)

    for mutation in mutations:
        artifact = build_oracle(
            mutation,
            source_path=SOURCE_PATH.relative_to(REPO_ROOT).as_posix(),
            source_sha256=source_sha,
        )
        candidate = artifact["content"]["cities"][0]["candidates"][0]
        assert candidate["relevance_grade"] == 0
        assert candidate["relevant_at_4"] is False
        assert candidate["eligibility_checks"]["exclusion_codes"]


def test_canonical_actual_ids_score_and_synthetic_id_mismatch_is_real_reject():
    artifact = load_bound_oracle(_binding(), REPO_ROOT)
    city = artifact["content"]["cities"][0]
    label = overlay_case_oracles(
        {"case_id": "development-builder-beijing", "metric_oracles": {}},
        artifact,
        city="北京",
    )
    ideal = sorted(
        city["candidates"],
        key=lambda item: (-item["relevance_grade"], item["canonical_candidate_id"]),
    )[:5]
    output = {
        "rounds": [{
            "suggestion_set": {
                "candidates": [
                    {"candidate_id": f"ephemeral-{index}", "canonical_place": {"place_id": item["canonical_candidate_id"]}}
                    for index, item in enumerate(ideal)
                ]
            }
        }]
    }
    score = score_metric_oracles(label, canonical_builder_actuals(output))
    assert score["metrics"]["builder_ndcg_at_5"]["value"] == 1
    assert score["metrics"]["builder_recall_at_5"]["value"] == 1

    polluted_output = copy.deepcopy(output)
    for index, item in enumerate(polluted_output["rounds"][0]["suggestion_set"]["candidates"]):
        item["canonical_place"]["place_id"] = f"synthetic-{index}"
    rejected = score_metric_oracles(label, canonical_builder_actuals(polluted_output))
    assert rejected["metrics"]["builder_ndcg_at_5"]["value"] == 0
    assert rejected["metrics"]["builder_recall_at_5"]["value"] == 0


def test_development_builder_run_spec_overlays_all_selected_cases_with_canonical_oracle():
    spec = json.loads(RUN_SPEC_PATH.read_bytes())

    cases, labels = _load_selected_builder_cases_and_labels(spec, REPO_ROOT)

    assert [case["case_id"] for case in cases] == spec["dataset"]["case_ids"]
    for case in cases:
        oracles = labels[case["case_id"]]["metric_oracles"]
        assert oracles["builder_ndcg_at_5"]["applicability"] == "APPLICABLE"
        assert oracles["builder_ndcg_at_5"]["identity_key"] == "canonical_place.place_id"
        assert oracles["builder_recall_at_5"]["label_authority"] == LABEL_AUTHORITY

    hangzhou_context = labels["dev.hz.builder.insert-edge-context"]["metric_oracles"]
    graded_ids = {
        item["candidate_id"] for item in hangzhou_context["builder_ndcg_at_5"]["relevance_items"]
    }
    # This case requests NEARBY/FUN/FOOD only. POPULAR-only candidates are not
    # part of its retrieval universe and therefore cannot lower its ideal DCG.
    assert "B0MGA5E3CK" not in graded_ids
    assert "B0I0MC0UKW" not in graded_ids


def test_preflight_binds_and_rejects_changed_graded_oracle_bytes(tmp_path):
    valid = preflight(RUN_SPEC_PATH, repo_root=REPO_ROOT, environ={})
    assert valid.valid
    assert valid.bindings["graded_ranking_oracle"]["artifact_file_sha256"] == _binding()["sha256"]

    spec = json.loads(RUN_SPEC_PATH.read_bytes())
    spec["dataset"]["graded_ranking_oracle"]["sha256"] = "0" * 64
    mutated = tmp_path / "mutated-builder-spec.json"
    mutated.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    rejected = preflight(mutated, repo_root=REPO_ROOT, environ={})
    assert rejected.valid is False
    assert any(
        error["code"] == "GRADED_RANKING_ORACLE_SHA256_MISMATCH"
        for error in rejected.errors
    )
