from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from scripts.generate_auditor_simulated import EVIDENCE_BOUNDARY, build_cases, write_dataset
from app.importing.parser import ItineraryTextParser


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_simulated_corpus_has_balanced_scope_and_provenance() -> None:
    cases = build_cases()

    assert len(cases) == 150
    assert Counter(case["city"] for case in cases) == {"北京": 50, "上海": 50, "杭州": 50}
    assert Counter(case["source_kind"] for case in cases) == {
        "SIMULATED_AI_ITINERARY": 60,
        "SIMULATED_CONTROLLED_MUTATION": 60,
        "SIMULATED_BOUNDARY": 30,
    }
    assert {case["trip_days"] for case in cases} == {2, 3, 4, 5}
    assert {case["group_size"] for case in cases} == {2, 3, 4, 5}
    assert all(case["evidence_boundary"] == EVIDENCE_BOUNDARY for case in cases)
    assert all(case["m1_eligible"] is False for case in cases)
    assert all("human_label" not in case and "human_findings" not in case for case in cases)

    for case in cases:
        assert case["simulated_organizer_profile"]["is_real_human"] is False
        assert all(item in case["simulated_findings"] for item in case["original_errors"] + case["injected_errors"])
        assert all(item["provenance"] == "original_error" for item in case["original_errors"])
        assert all(item["provenance"] == "injected_error" for item in case["injected_errors"])
        assert all(item["category"] in {"ENTITY", "OPENING", "ROUTE", "PACE", "MEMBER", "OTHER"} for item in case["simulated_findings"])
        assert all(item["expected_rule_id"] for item in case["simulated_findings"])
        assert bool(case["injected_errors"]) == (case["source_kind"] == "SIMULATED_CONTROLLED_MUTATION")
        assert case["simulated_repair_decisions"]
        assert all(
            decision["label_source"] == "simulated_organizer_not_human"
            for decision in case["simulated_repair_decisions"]
        )

    mutation_decisions = Counter(
        case["simulated_repair_decisions"][0]["decision"]
        for case in cases
        if case["source_kind"] == "SIMULATED_CONTROLLED_MUTATION"
    )
    assert mutation_decisions["ACCEPT"] > 0
    assert mutation_decisions["REJECT"] > 0
    assert all(
        case["simulated_repair_decisions"][0]["decision"] == "SKIP"
        for case in cases
        if case["source_kind"] != "SIMULATED_CONTROLLED_MUTATION"
    )


def test_controlled_mutations_change_parser_visible_stops() -> None:
    parser = ItineraryTextParser()
    mutations = [case for case in build_cases() if case["source_kind"] == "SIMULATED_CONTROLLED_MUTATION"]
    for case in mutations:
        draft = parser.parse(case["raw_itinerary"], import_id=case["case_id"])
        assert len(draft.raw_stops) >= 2
        target_reason = case["injected_errors"][0]["reason_code"]
        first_day = [stop for stop in draft.raw_stops if stop.day_index == 0]
        assert len(first_day) == 5
        if target_reason == "TIME_CHAIN_BROKEN":
            assert first_day[0].raw_time and first_day[2].raw_time == "10:30-12:30"
        elif target_reason == "DUPLICATE_PLACE":
            assert first_day[0].raw_name == first_day[2].raw_name
        elif target_reason == "TIME_DATA_INVALID":
            assert first_day[2].raw_time is None
        else:
            assert target_reason == "PLACE_NOT_RESOLVED"
            assert first_day[2].raw_name.startswith("云端秘境")


def test_baselines_do_not_claim_text_unsupported_original_errors() -> None:
    baselines = [case for case in build_cases() if case["source_kind"] == "SIMULATED_AI_ITINERARY"]
    assert all(case["original_errors"] == [] for case in baselines)
    assert all(
        {finding["provenance"] for finding in case["simulated_findings"]} == {"environment_unknown"}
        for case in baselines
    )
    parser = ItineraryTextParser()
    for case in baselines:
        draft = parser.parse(case["raw_itinerary"], import_id=case["case_id"])
        for day_index in range(case["trip_days"]):
            stops = [stop for stop in draft.raw_stops if stop.day_index == day_index]
            assert len(stops) == 5
            assert sum("餐厅" in stop.raw_name for stop in stops) == 2
            assert stops[-1].raw_name.endswith("酒店")
            assert stops[-1].raw_time == "21:00-22:00"


def test_baseline_attractions_are_unique_and_duplicate_mutation_is_incremental() -> None:
    parser = ItineraryTextParser()
    cases = build_cases()
    baselines = {
        case["source_document_id"]: case
        for case in cases
        if case["source_kind"] == "SIMULATED_AI_ITINERARY"
    }
    for baseline in baselines.values():
        draft = parser.parse(baseline["raw_itinerary"], import_id=baseline["case_id"])
        names = [stop.raw_name for stop in draft.raw_stops]
        assert len(names) == baseline["trip_days"] * 5
        assert len(names) == len(set(names))

    duplicate_mutations = [
        case for case in cases
        if case["source_kind"] == "SIMULATED_CONTROLLED_MUTATION"
        and case["injected_errors"][0]["reason_code"] == "DUPLICATE_PLACE"
    ]
    assert len(duplicate_mutations) == 15
    for mutation in duplicate_mutations:
        baseline = baselines[mutation["source_document_id"]]
        baseline_names = [
            stop.raw_name
            for stop in parser.parse(baseline["raw_itinerary"], import_id=baseline["case_id"]).raw_stops
        ]
        mutation_names = [
            stop.raw_name
            for stop in parser.parse(mutation["raw_itinerary"], import_id=mutation["case_id"]).raw_stops
        ]
        assert len(baseline_names) == len(set(baseline_names))
        repeated = {name for name in mutation_names if mutation_names.count(name) > 1}
        assert repeated == {baseline_names[0]}
        assert mutation_names.count(baseline_names[0]) == 2
    assert all(
        {finding["reason_code"] for finding in case["simulated_findings"]}
        == {"OPENING_HOURS_MISSING", "WEATHER_DATA_MISSING"}
        for case in baselines.values()
    )


def test_boundary_cases_use_real_confirmation_codes() -> None:
    boundaries = [case for case in build_cases() if case["source_kind"] == "SIMULATED_BOUNDARY"]
    allowed = {"IMPORT_PARSE_FAILED", "TIME_DATA_INVALID", "PLACE_NOT_RESOLVED"}
    assert {case["simulated_findings"][0]["reason_code"] for case in boundaries} == allowed
    assert all(case["simulated_repair_decisions"][0]["decision"] == "SKIP" for case in boundaries)


def test_source_documents_do_not_cross_splits() -> None:
    splits_by_source: dict[str, set[str]] = defaultdict(set)
    for case in build_cases():
        splits_by_source[case["source_document_id"]].add(case["split"])

    assert len(splits_by_source) == 90
    assert all(len(splits) == 1 for splits in splits_by_source.values())
    assert set().union(*splits_by_source.values()) == {"train", "validation", "test"}


def test_generator_writes_deterministic_manifest_and_jsonl(tmp_path: Path) -> None:
    manifest_path, cases_path = write_dataset(tmp_path / "first")
    second_manifest_path, second_cases_path = write_dataset(tmp_path / "second")

    assert cases_path.read_bytes() == second_cases_path.read_bytes()
    assert manifest_path.read_bytes() == second_manifest_path.read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = _read_jsonl(cases_path)
    assert manifest["case_count"] == len(cases) == 150
    assert manifest["human_labels"] is False
    assert manifest["m1_eligible"] is False
    assert manifest["evidence_boundary"] == EVIDENCE_BOUNDARY
    assert manifest["cases_sha256"] == hashlib.sha256(cases_path.read_bytes()).hexdigest()
