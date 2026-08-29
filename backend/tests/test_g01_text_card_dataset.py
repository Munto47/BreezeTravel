from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.trip_text_cards_v1.annotations import (
    AnnotationValidationError,
    build_blank_work_packet,
    verify_adjudication,
)
from evals.trip_text_cards_v1.contracts import TextCardInputCase
from evals.trip_text_cards_v1.gate import assess_semantic_score, readiness_receipt
from evals.trip_text_cards_v1.map_positive import load_and_validate_fixture, run as run_map_positive
from evals.trip_text_cards_v1.runner import BaselineRunError, write_baseline
from evals.trip_text_cards_v1.validator import load_cases, validate_dataset
from scripts.generate_g01_map_positive_fixture import generate as generate_map_positive
from scripts.generate_g01_text_card_inputs import generate


DATA_ROOT = Path("eval_data/trip_text_cards_v1")
MAP_DATA_ROOT = Path("eval_data/g01_map_positive_v1")


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _annotation_case(case: TextCardInputCase, *, mention_id: str = "m1") -> dict[str, object]:
    raw_text = "故宫博物院"
    start = case.input_text.index(raw_text)
    return {
        "case_id": case.case_id,
        "source_sha256": case.normalized_input_sha256,
        "destination_name": "北京",
        "mentions": [
            {
                "mention_id": mention_id,
                "span_start": start,
                "span_end": start + len(raw_text),
                "raw_text": raw_text,
                "semantic_kind": "PLACE",
                "role": "PLANNED",
                "day_index": 1,
                "atomic_place_name": raw_text,
                "executable_place": True,
                "canonical_place": {
                    "place_id": "human-verified-palace-museum",
                    "name": raw_text,
                    "city": "北京",
                    "category": "景点",
                    "authority": "HUMAN_VERIFIED_PROVIDER_RECEIPT",
                    "receipt_ref": "external-provider-receipt-001",
                },
            }
        ],
    }


def _annotation_bundle(case: TextCardInputCase, assignment: str, actor: str, *, mention_id: str) -> dict[str, object]:
    return {
        "schema_version": "g01-text-card-annotation-bundle-v1",
        "dataset_version": "g01-text-card-dataset-v1",
        "assignment_id": assignment,
        "split": "dev",
        "attestation": {
            "actor_id": actor,
            "completed_at": "2026-08-28T09:00:00+08:00",
            "is_authorized_human": True,
            "worked_independently": True,
            "saw_peer_labels_before_submission": False,
            "automated_suggestions_used": False,
        },
        "cases": [_annotation_case(case, mention_id=mention_id)],
    }


def test_dataset_has_exact_new_90_case_family_isolated_contract() -> None:
    receipt = validate_dataset(DATA_ROOT)

    assert receipt["valid"] is True
    assert receipt["split_counts"] == {"dev": 54, "validation": 18, "frozen_blind": 18}
    assert receipt["cohort_counts"] == {"DEEP_CITY": 60, "OTHER_CITY": 15, "ADVERSARIAL": 15}
    assert receipt["family_count"] == 30
    assert receipt["family_isolation"] == "PASS"
    assert receipt["repository_human_labels"] == 0
    assert receipt["gate_status"] == "NOT_RUN"


def test_generator_reproduces_every_hash_bound_dataset_byte(tmp_path: Path) -> None:
    regenerated = tmp_path / "trip_text_cards_v1"
    generate(regenerated)
    contract = json.loads((DATA_ROOT / "dataset_contract.json").read_text(encoding="utf-8"))

    for relative in [*contract["files"], "dataset_contract.json"]:
        assert (regenerated / relative).read_bytes() == (DATA_ROOT / relative).read_bytes()


def test_blank_work_packet_has_no_labels_or_model_suggestions() -> None:
    case = load_cases(DATA_ROOT)["dev"][0]
    packet = build_blank_work_packet(split="dev", assignment_id="human-assignment-a", source_cases=[case])

    assert packet["automated_labels_included"] is False
    assert packet["peer_labels_included"] is False
    assert packet["cases"][0]["annotation"] is None
    assert "prediction" not in json.dumps(packet, ensure_ascii=False).casefold()


def test_two_distinct_humans_and_third_adjudicator_can_bind_agreed_gold(tmp_path: Path) -> None:
    case = load_cases(DATA_ROOT)["dev"][0]
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    adjudication_path = tmp_path / "adjudication.json"
    first = _annotation_bundle(case, "assignment-alpha", "human-alpha", mention_id="a-1")
    second = _annotation_bundle(case, "assignment-beta", "human-beta", mention_id="b-1")
    _write_json(first_path, first)
    _write_json(second_path, second)
    adjudication = {
        "schema_version": "g01-text-card-adjudication-bundle-v1",
        "dataset_version": "g01-text-card-dataset-v1",
        "split": "dev",
        "source_assignment_ids": ["assignment-alpha", "assignment-beta"],
        "source_bundle_sha256": [
            hashlib.sha256(first_path.read_bytes()).hexdigest(),
            hashlib.sha256(second_path.read_bytes()).hexdigest(),
        ],
        "attestation": {
            "actor_id": "human-adjudicator",
            "completed_at": "2026-08-28T10:00:00+08:00",
            "is_authorized_human": True,
            "reviewed_both_independent_bundles": True,
            "automated_adjudication_used": False,
        },
        "conflicts": [],
        "gold_cases": [_annotation_case(case, mention_id="gold-1")],
    }
    _write_json(adjudication_path, adjudication)

    _gold, receipt = verify_adjudication(
        split="dev",
        source_cases=[case],
        first_path=first_path,
        second_path=second_path,
        adjudication_path=adjudication_path,
        repository_root=Path.cwd().parent,
    )

    assert receipt["actors_distinct"] is True
    assert receipt["annotator_count"] == 2
    assert receipt["adjudicator_count"] == 1
    assert receipt["evidence_span_validity"] == 1
    assert receipt["gold_executable_mentions"] == 1


def test_annotation_bundle_inside_repository_is_rejected(tmp_path: Path) -> None:
    case = load_cases(DATA_ROOT)["dev"][0]
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    bundle_path = repository_root / "labels.json"
    second_path = tmp_path / "second.json"
    adjudication_path = tmp_path / "adjudication.json"
    _write_json(bundle_path, _annotation_bundle(case, "assignment-alpha", "human-alpha", mention_id="a"))
    _write_json(second_path, _annotation_bundle(case, "assignment-beta", "human-beta", mention_id="b"))
    _write_json(adjudication_path, {})

    with pytest.raises(AnnotationValidationError, match="outside the repository"):
        verify_adjudication(
            split="dev",
            source_cases=[case],
            first_path=bundle_path,
            second_path=second_path,
            adjudication_path=adjudication_path,
            repository_root=repository_root,
        )


def test_local_baseline_runner_refuses_frozen_blind(tmp_path: Path) -> None:
    cases = load_cases(DATA_ROOT)
    with pytest.raises(BaselineRunError, match="forbidden"):
        write_baseline(
            split_cases={"frozen_blind": cases["frozen_blind"][:1]},
            output_root=tmp_path,
            backend_root=Path.cwd(),
            subject_commit="a" * 40,
        )


def test_validation_thresholds_fail_closed_on_small_auto_match_denominator() -> None:
    score = {
        "scoring_coverage": 1.0,
        "evidence_span_validity": 1.0,
        "eligibility_rule_consistency": 1.0,
        "forbidden_content_as_place_count": 0,
        "severe_wrong_auto_match_count": 0,
        "auto_match": {"precision": 1.0, "denominator": 49},
        "executable_mentions": {"precision": 1.0, "recall": 1.0},
        "day_assignment": {"f1": 1.0},
        "role_macro_f1": 1.0,
        "deep_city_auto_match": {"coverage": 1.0},
        "human_confirmation_count": {
            "population": "DEEP_CITY",
            "median": 0,
            "p90": 0,
        },
        "other_city_confirmation_required_count": {
            "population": "OTHER_CITY",
            "case_count": 1,
            "gold_executable_count": 1,
            "auto_match_count": 0,
            "total": 1,
        },
        "public_projection": {"forbidden_key_hits": 0, "full_source_leak_hits": 0},
    }

    assessment = assess_semantic_score(score, split="validation")

    assert assessment["passed"] is False
    denominator = next(item for item in assessment["checks"] if item["name"] == "auto_match_denominator")
    assert denominator["passed"] is False


def test_semantic_gate_rejects_other_city_auto_matching() -> None:
    score = {
        "scoring_coverage": 1.0,
        "evidence_span_validity": 1.0,
        "eligibility_rule_consistency": 1.0,
        "forbidden_content_as_place_count": 0,
        "severe_wrong_auto_match_count": 0,
        "auto_match": {"precision": 1.0, "denominator": 50},
        "executable_mentions": {"precision": 1.0, "recall": 1.0},
        "day_assignment": {"f1": 1.0},
        "role_macro_f1": 1.0,
        "deep_city_auto_match": {"coverage": 1.0},
        "human_confirmation_count": {
            "population": "DEEP_CITY",
            "median": 0,
            "p90": 0,
        },
        "other_city_confirmation_required_count": {
            "population": "OTHER_CITY",
            "case_count": 1,
            "gold_executable_count": 1,
            "auto_match_count": 1,
            "total": 0,
        },
        "public_projection": {"forbidden_key_hits": 0, "full_source_leak_hits": 0},
    }

    assessment = assess_semantic_score(score, split="validation")

    assert assessment["passed"] is False
    check = next(
        item for item in assessment["checks"] if item["name"] == "other_city_auto_matches"
    )
    assert check["passed"] is False


def test_current_gate_readiness_is_honestly_hitl_pending() -> None:
    receipt = readiness_receipt(
        DATA_ROOT.resolve(),
        Path("../docs/governance/g01_s0_asset_disposition.json").resolve(),
    )

    assert receipt["gate"] == "HITL_PENDING"
    assert receipt["automated_gate_pass_claim"] is False
    assert receipt["provider_readback"]["qwen_live_lane"] == "NOT_READY"
    assert receipt["provider_readback"]["amap_live_persistence"] == "BLOCKED_PENDING_WRITTEN_PERMISSION"


def test_map_positive_fixture_is_exactly_30_trips_and_120_unique_edges() -> None:
    _fixture, receipt = load_and_validate_fixture(MAP_DATA_ROOT)

    assert receipt["valid"] is True
    assert receipt["plan_count"] == 30
    assert receipt["edge_count"] == 120
    assert receipt["unique_directed_edges"] == 120
    assert receipt["city_plan_counts"] == {"北京": 10, "上海": 10, "杭州": 10}
    assert receipt["external_calls"] == 0
    assert receipt["live_provider_claim"] == "NOT_RUN"


def test_map_positive_generator_reproduces_frozen_fixture(tmp_path: Path) -> None:
    regenerated = tmp_path / "g01_map_positive_v1"
    generate_map_positive(regenerated)

    assert (regenerated / "fixture.json").read_bytes() == (MAP_DATA_ROOT / "fixture.json").read_bytes()
    assert (regenerated / "dataset_contract.json").read_bytes() == (
        MAP_DATA_ROOT / "dataset_contract.json"
    ).read_bytes()


def test_real_map_worker_renders_all_positive_fixture_edges_without_external_calls() -> None:
    receipt = run_map_positive(MAP_DATA_ROOT)

    assert receipt["fixture_subgate"] == "PASS"
    assert receipt["execution_scope"] == "IN_MEMORY_MAP_WORKER_CONTROLLED_FIXTURE"
    assert receipt["plan_count"] == 30
    assert receipt["ready_snapshot_count"] == 30
    assert receipt["edge_count"] == 120
    assert receipt["usable_edge_count"] == 120
    assert receipt["usable_coverage"] == 1
    assert receipt["walking_mode_fact_count"] == 120
    assert receipt["transit_mode_fact_count"] == 120
    assert receipt["selected_mode_counts"]["walking"] > 0
    assert receipt["selected_mode_counts"]["transit"] > 0
    assert receipt["logical_duplicate_provider_requests"] == 0
    assert receipt["external_calls"] == 0
    assert receipt["worker_failure_count"] == 0
    assert receipt["worker_to_snapshot_p95_ms"] <= 15_000
    assert receipt["live_provider_claim"] == "NOT_RUN"
    assert receipt["postgres_persistence_matrix_claim"] == "NOT_RUN"
    assert receipt["full_text_card_gate_claim"] == "NOT_RUN"
