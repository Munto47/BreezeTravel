from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from scripts import validate_dual_entry_testset as validator_module
from scripts.validate_dual_entry_testset import (
    DATASET_ROOT,
    RUN_SPEC_ROOT,
    expected_subject_receipt_records,
    expected_subject_receipt_refs,
    normalized_input_sha256,
    validate_dataset,
)


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _isolated_validation_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dataset = tmp_path / "dual_entry_v1"
    run_specs = tmp_path / "run_specs"
    shutil.copytree(DATASET_ROOT, dataset)
    shutil.copytree(RUN_SPEC_ROOT, run_specs)
    monkeypatch.setattr(validator_module, "DATASET_ROOT", dataset)
    monkeypatch.setattr(validator_module, "RUN_SPEC_ROOT", run_specs)
    return dataset


def test_dual_entry_testset_is_structurally_valid_and_three_city_balanced():
    report = validate_dataset()
    assert report["errors"] == []
    assert report["structurally_valid"] is True
    assert report["case_count"] == report["label_count"] == 96
    assert report["development_label_count"] == 78
    assert report["sealed_blind_label_count"] == 18
    assert report["city_counts"] == {"北京": 32, "上海": 32, "杭州": 32}
    assert report["entry_counts"] == {"IMPORT": 55, "BUILDER": 41}
    assert report["split_counts"] == {"pilot": 6, "dev": 60, "regression": 12, "frozen_blind": 18}


def test_every_case_hash_and_receipt_index_recomputes_from_bytes_and_registry():
    manifest = json.loads((DATASET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    sources = {row["source_document_id"]: row for row in _jsonl(DATASET_ROOT / "source_registry.jsonl")}
    registry_path = DATASET_ROOT / manifest["subject_receipt_registry"]
    assert hashlib.sha256(registry_path.read_bytes()).hexdigest() == manifest["subject_receipt_registry_sha256"]
    receipt_rows = _jsonl(registry_path)
    receipt_by_id = {row["receipt_id"]: row for row in receipt_rows}
    assert len(receipt_rows) == len(receipt_by_id) == 277
    cases = [row for entry in manifest["files"] for row in _jsonl(DATASET_ROOT / entry["inputs"])]
    assert len(cases) == 96
    for case in cases:
        assert case["normalized_input_sha256"] == normalized_input_sha256(case["input"])
        assert case["template_family_id"]
        assert case["generator_family_id"]
        assert "mutation_parent_case_id" in case
        assert case["receipt_refs"]["subject_evidence_refs"] == expected_subject_receipt_refs(case)
        for expected_record, receipt_ref in zip(
            expected_subject_receipt_records(case),
            case["receipt_refs"]["subject_evidence_refs"],
            strict=True,
        ):
            actual_record = receipt_by_id[receipt_ref["receipt_id"]]
            assert actual_record == expected_record
            assert hashlib.sha256(
                json.dumps(actual_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest() == receipt_ref["record_sha256"]
        expected_sources = [
            {
                "source_document_id": source_id,
                "raw_sha256": sources[source_id]["raw_hash"],
                "extract_sha256": sources[source_id]["extract_hash"],
            }
            for source_id in sorted(case.get("source_document_refs", []))
        ]
        assert case["receipt_refs"]["source_receipts"] == expected_sources


def test_metric_oracles_are_computable_or_explicit_na_without_human_truth_claim():
    report = validate_dataset()
    expected_applicable = {
        "parse_f1": 20,
        "entity_precision_recall": 19,
        "finding_precision_recall": 31,
        "repair_postcheck": 10,
        "builder_ndcg_at_5": 11,
        "builder_recall_at_5": 13,
    }
    assert report["human_ground_truth_case_count"] == 0
    for metric_name, applicable in expected_applicable.items():
        assert report["metric_oracle_coverage"][metric_name] == {
            "applicable": applicable,
            "not_applicable": 78 - applicable,
        }

    manifest = json.loads((DATASET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    labels = [
        row
        for entry in manifest["files"]
        if "labels" in entry
        for row in _jsonl(DATASET_ROOT / entry["labels"])
    ]
    for label in labels:
        assert set(label["metric_oracles"]) == set(expected_applicable)
        for oracle in label["metric_oracles"].values():
            if oracle["applicability"] == "N_A":
                assert set(oracle) == {"applicability", "reason_code"}
        finding = label["metric_oracles"]["finding_precision_recall"]
        if finding["applicability"] == "APPLICABLE":
            assert finding["metric_version"] == "exact-set-blocker-high-v1"
            assert finding["scope_severities"] == ["BLOCKER", "HIGH"]


def test_release_gate_is_honestly_seeded_but_not_claimed_ready():
    report = validate_dataset()
    assert report["release_ready"] is False
    assert any("import 12/90" in item for item in report["warnings"])
    assert any("builder 6/45" in item for item in report["warnings"])
    assert any("G2_FOUR_STOP_BUILDER seeds 3/9" in item for item in report["warnings"])
    assert any("G5_BROWSER_RECOVERY seeds 6/9" in item for item in report["warnings"])
    assert "NON_PR_SOURCE_ARCHIVES_NOT_YET_INGESTED" not in report["declared_release_blockers"]
    assert "IMPORT_AND_RELEASE_PROVIDER_SNAPSHOT_COVERAGE_INCOMPLETE" in report["declared_release_blockers"]
    assert "FROZEN_BLIND_STATIC_SUBJECT_EVIDENCE_UNAVAILABLE" in report["declared_release_blockers"]
    assert "TEMPLATE_AND_GENERATOR_LINEAGE_UNAVAILABLE" in report["declared_release_blockers"]
    assert "HUMAN_CALIBRATION_IS_0_OF_30" in report["declared_release_blockers"]
    assert any("generator_family=96" in item and "template_family=96" in item for item in report["warnings"])
    pollution = report["pollution_contract"]
    assert pollution["legacy_unbound_builder_subjects_before"] == 111
    assert pollution["legacy_gap_classification"] == {
        "real_amap_snapshot_exact_identity_overlap": 0,
        "real_amap_snapshot_exact_receipt_binding": 0,
        "controlled_fixture_execution": 90,
        "unavailable_no_historical_call": 21,
    }
    assert pollution["static_subject_registry_records"] == 277
    assert pollution["static_subject_evidence"] == {
        "real_provider": 0,
        "controlled_fixture_execution": 242,
        "unavailable_no_historical_call": 35,
    }
    assert any("21/111" in item for item in report["warnings"])


def test_blind_inputs_do_not_contain_oracle_and_release_spec_isolates_labels():
    inputs = _jsonl(DATASET_ROOT / "frozen_blind.inputs.jsonl")
    assert inputs
    assert len(inputs) == 18
    assert sum(row["entry"] == "BUILDER" for row in inputs) == 6
    assert all(not ({"expected", "oracle", "deterministic_truth", "judge_scores"} & row.keys()) for row in inputs)
    manifest = json.loads((DATASET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    blind_entry = next(entry for entry in manifest["files"] if entry["split"] == "frozen_blind")
    assert "labels" not in blind_entry
    assert blind_entry["label_storage"] == "external_bundle_only"
    seal_path = DATASET_ROOT / blind_entry["labels_seal"]
    seal_bytes = seal_path.read_bytes()
    assert hashlib.sha256(seal_bytes).hexdigest() == blind_entry["labels_seal_sha256"]
    seal = json.loads(seal_bytes)
    assert seal["scoring_payload_present"] is False
    assert seal["external_bundle_required"] is True
    assert seal["case_count"] == 18
    assert "labels" not in seal
    assert not ({"deterministic_truth", "metric_oracles", "gate_assertions"} & set(seal))
    release_spec = json.loads((RUN_SPEC_ROOT / "dual-entry-release-blind.json").read_text(encoding="utf-8"))
    assert release_spec["dataset"]["label_access"] == "isolated_scorer_only"
    assert release_spec["models"]["judge"]["hidden_labels_allowed"] is False
    assert "sut_access_to_labels" in release_spec["prohibitions"]
    assert "repository_blind_label_payload" in release_spec["prohibitions"]
    assert "in_process_blind_scoring" in release_spec["prohibitions"]


def test_builder_cases_use_four_to_six_candidates_or_explicit_provider_failure():
    manifest = json.loads((DATASET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    cases = [row for entry in manifest["files"] for row in _jsonl(DATASET_ROOT / entry["inputs"])]
    builders = [row for row in cases if row["entry"] == "BUILDER"]
    assert builders
    for row in builders:
        candidates = row["input"]["candidate_snapshot"]
        assert len(candidates) in {0, 4, 5, 6}
        if not candidates:
            assert row["input"].get("provider_error") or row["execution"].get("fault_profile")


def test_builder_final_2_contract_coverage_is_explicit_and_three_city_balanced():
    report = validate_dataset()
    coverage = report["builder_contract_coverage"]
    assert coverage["development_builder_cases"] == 32
    assert coverage["p5_contract_cases"] == 12
    assert coverage["p5_contract_by_city"] == {"北京": 4, "上海": 4, "杭州": 4}
    assert coverage["g2_four_stop_seeds"] == 3
    assert coverage["g5_recovery_seeds"] == 6
    assert coverage["missing_scenario_tags"] == []


def test_builder_contract_labels_bind_query_set_accept_event_interaction_and_recovery_oracles():
    manifest = json.loads((DATASET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    cases = [row for entry in manifest["files"] for row in _jsonl(DATASET_ROOT / entry["inputs"])]
    labels = {
        row["case_id"]: row
        for entry in manifest["files"]
        if "labels" in entry
        for row in _jsonl(DATASET_ROOT / entry["labels"])
    }
    development = [
        row for row in cases if row["entry"] == "BUILDER" and row["split"] in {"dev", "regression"}
    ]
    for case in development:
        tags = set(case["tags"])
        truth = labels[case["case_id"]]["deterministic_truth"]
        if tags & {"anchor-query", "insert-edge"}:
            assert case["input"]["request_context"]["query_mode"] == truth["query_oracle"]["query_mode"]
        if "p5-contract" in tags and "frozen-set" in tags:
            assert case["input"]["suggestion_set_fixture"]
            assert len(truth["suggestion_set_oracle"]["frozen_fields"]) >= 9
        if "p5-contract" in tags and "idempotency" in tags:
            assert len(case["input"]["accept_attempts"]) >= 3
            assert truth["acceptance_oracle"]["same_key_same_request_replays"] is True
            assert truth["acceptance_oracle"]["same_key_different_request_conflicts"] is True
        if "p5-contract" in tags and "accept-rollback" in tags:
            assert truth["acceptance_oracle"]["revision_increment"] == 0
            assert len(truth["acceptance_oracle"]["rollback_fault_points"]) >= 3
        if "g2-seed" in tags:
            assert len(truth["event_oracle"]["ordered_types"]) >= 8
        if "g5-seed" in tags:
            assert truth["recovery_oracle"]["authoritative_store"] == "POSTGRESQL"
            assert truth["recovery_oracle"]["yjs_is_fact_source"] is False
        if "button-equivalence" in tags:
            interaction = truth["interaction_oracle"]
            assert all(
                interaction[field] is True
                for field in (
                    "drag_button_command_hash_equal",
                    "semantic_content_hash_equal",
                    "changed_days_equal",
                    "changed_edges_equal",
                    "affected_rules_equal",
                )
            )


def test_nightly_builder_gate_seeds_are_fail_closed_and_do_not_inflate_blind_counts():
    spec = json.loads((RUN_SPEC_ROOT / "dual-entry-nightly-snapshot.json").read_text(encoding="utf-8"))
    seeds = spec["dataset"]["gate_case_seeds"]
    assert len(seeds["G2_FOUR_STOP_BUILDER"]["case_ids"]) == 3
    assert seeds["G2_FOUR_STOP_BUILDER"]["required_min"] == 9
    assert len(seeds["G5_BROWSER_RECOVERY"]["case_ids"]) == 6
    assert seeds["G5_BROWSER_RECOVERY"]["required_min"] == 9
    assert all(group["execution_status"] == "SEED_ONLY_NOT_EXECUTED" for group in seeds.values())
    release = json.loads((RUN_SPEC_ROOT / "dual-entry-release-blind.json").read_text(encoding="utf-8"))
    assert release["thresholds"]["frozen_blind_builder_min"] == 45
    assert release["dataset"]["splits"] == ["frozen_blind"]


def test_builder_recorded_family_and_canonical_candidate_sequences_do_not_cross_blind_boundary():
    manifest = json.loads((DATASET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    cases = [row for entry in manifest["files"] for row in _jsonl(DATASET_ROOT / entry["inputs"])]
    builders = [row for row in cases if row["entry"] == "BUILDER"]
    blind = [row for row in builders if row["split"] == "frozen_blind"]
    development = [row for row in builders if row["split"] != "frozen_blind"]
    recorded_blind_sources = {
        row["source_family_id"]
        for row in blind
        if row["lineage_status"]["source_family"] == "RECORDED"
    }
    recorded_development_sources = {
        row["source_family_id"]
        for row in development
        if row["lineage_status"]["source_family"] == "RECORDED"
    }
    assert recorded_blind_sources.isdisjoint(recorded_development_sources)

    def sequence(row: dict) -> tuple[str, ...]:
        return tuple(
            candidate.get("canonical_place_id", candidate["id"])
            for candidate in row["input"]["candidate_snapshot"]
        )

    assert {sequence(row) for row in blind if sequence(row)}.isdisjoint(
        {sequence(row) for row in development if sequence(row)}
    )
    assert len({sequence(row) for row in builders if sequence(row)}) == sum(
        bool(sequence(row)) for row in builders
    )


def test_unavailable_lineage_uses_shared_sentinel_not_case_id_placeholders():
    manifest = json.loads((DATASET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    cases = [row for entry in manifest["files"] for row in _jsonl(DATASET_ROOT / entry["inputs"])]
    assert {row["template_family_id"] for row in cases} == {"template-lineage-unavailable"}
    assert {row["generator_family_id"] for row in cases} == {"generator-lineage-unavailable"}
    assert len({row["source_family_id"] for row in cases}) < len(cases)
    assert all(row["case_id"] not in row["source_family_id"] for row in cases)
    assert all(row["case_id"] not in row["template_family_id"] for row in cases)
    assert all(row["case_id"] not in row["generator_family_id"] for row in cases)
    assert all(row["lineage_status"]["template_family"] == "UNAVAILABLE" for row in cases)
    assert all(row["lineage_status"]["generator_family"] == "UNAVAILABLE" for row in cases)
    assert all(row["data_origin"] != "controlled_mutation" for row in cases)
    assert all(row["mutation_parent_case_id"] is None for row in cases)
    assert all(row["lineage_status"]["mutation_family"] == "NOT_APPLICABLE" for row in cases)


def test_sources_state_usage_boundaries_and_do_not_claim_unavailable_pdf_as_ingested():
    sources = _jsonl(DATASET_ROOT / "source_registry.jsonl")
    by_id = {row["source_document_id"]: row for row in sources}
    hangzhou = by_id["official-hangzhou-route-pdf-20260821"]
    assert hangzhou["access_status"] == "UNAVAILABLE_ON_CHECK"
    assert hangzhou["raw_hash"] is None
    wikivoyage = by_id["open-wikivoyage-reuse-policy-20260821"]
    assert "CC BY-SA" in wikivoyage["license_or_terms"]
    assert "FACT" not in wikivoyage["usage_modes"]
    official_routes = [row for row in sources if row["source_type"] == "OFFICIAL_ROUTE"]
    assert all("STRUCTURE" in row["usage_modes"] for row in official_routes)


def test_pr_referenced_source_archives_match_registry_bytes_and_trace_extracts():
    sources = {row["source_document_id"]: row for row in _jsonl(DATASET_ROOT / "source_registry.jsonl")}
    expected_ids = {
        "official-beijing-route-library-20260821",
        "official-shanghai-citywalk-20240616",
    }
    for source_id in expected_ids:
        source = sources[source_id]
        raw_path = DATASET_ROOT / source["raw_archive_path"]
        extract_path = DATASET_ROOT / source["extract_archive_path"]
        assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == source["raw_hash"]
        assert hashlib.sha256(extract_path.read_bytes()).hexdigest() == source["extract_hash"]
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        extract = json.loads(extract_path.read_text(encoding="utf-8"))
        assert raw["source_document_id"] == extract["source_document_id"] == source_id
        assert raw["canonical_url"] == extract["canonical_url"] == source["canonical_url"]
        assert extract["derivation"]["raw_archive_path"] == source["raw_archive_path"]
        assert extract["derivation"]["source_body_sha256"] == raw["http"]["remote_body_sha256"]
        assert set(extract["allowed_use"]) <= set(source["usage_modes"])
        assert "FACT" not in extract["allowed_use"]


def test_validator_fails_closed_on_family_and_mutation_pollution(tmp_path, monkeypatch):
    dataset = _isolated_validation_tree(tmp_path, monkeypatch)
    dev_rows = _jsonl(dataset / "dev.inputs.jsonl")
    blind_rows = _jsonl(dataset / "frozen_blind.inputs.jsonl")
    dev_parent = next(row for row in dev_rows if row["entry"] == "IMPORT")
    blind_child = next(row for row in blind_rows if row["entry"] == dev_parent["entry"])
    blind_child["data_origin"] = "controlled_mutation"
    for field in ("source_family_id", "template_family_id", "generator_family_id"):
        blind_child[field] = dev_parent[field]
    blind_child["lineage_status"] = {
        "source_family": "RECORDED",
        "template_family": "RECORDED",
        "generator_family": "RECORDED",
        "mutation_family": "RECORDED",
    }
    dev_parent["lineage_status"] = {
        "source_family": "RECORDED",
        "template_family": "RECORDED",
        "generator_family": "RECORDED",
        "mutation_family": "NOT_APPLICABLE",
    }
    blind_child["mutation_parent_case_id"] = dev_parent["case_id"]
    _write_jsonl(dataset / "dev.inputs.jsonl", dev_rows)
    _write_jsonl(dataset / "frozen_blind.inputs.jsonl", blind_rows)

    errors = validator_module.validate_dataset()["errors"]
    assert any("source_family_id" in error and "development/blind" in error for error in errors)
    assert any("template_family_id" in error and "development/blind" in error for error in errors)
    assert any("generator_family_id" in error and "development/blind" in error for error in errors)
    assert any("mutation_family" in error and "development/blind" in error for error in errors)


def test_validator_fails_closed_on_duplicate_normalized_input(tmp_path, monkeypatch):
    dataset = _isolated_validation_tree(tmp_path, monkeypatch)
    dev_rows = _jsonl(dataset / "dev.inputs.jsonl")
    first, second = [row for row in dev_rows if row["entry"] == "IMPORT"][:2]
    second["input"] = first["input"]
    second["normalized_input_sha256"] = normalized_input_sha256(second["input"])
    second["receipt_refs"] = first["receipt_refs"]
    _write_jsonl(dataset / "dev.inputs.jsonl", dev_rows)

    errors = validator_module.validate_dataset()["errors"]
    assert any("normalized_input_sha256" in error and "duplicate cases" in error for error in errors)


def test_validator_fails_closed_on_duplicate_builder_canonical_sequence(tmp_path, monkeypatch):
    dataset = _isolated_validation_tree(tmp_path, monkeypatch)
    dev_rows = _jsonl(dataset / "dev.inputs.jsonl")
    blind_rows = _jsonl(dataset / "frozen_blind.inputs.jsonl")
    dev_builder = next(row for row in dev_rows if row["entry"] == "BUILDER" and row["input"]["candidate_snapshot"])
    dev_sequence = [
        candidate.get("canonical_place_id", candidate["id"])
        for candidate in dev_builder["input"]["candidate_snapshot"]
    ]
    blind_builder = next(
        row
        for row in blind_rows
        if row["entry"] == "BUILDER" and len(row["input"]["candidate_snapshot"]) == len(dev_sequence)
    )
    for candidate, canonical_place_id in zip(
        blind_builder["input"]["candidate_snapshot"], dev_sequence, strict=True
    ):
        candidate["canonical_place_id"] = canonical_place_id
    blind_builder["normalized_input_sha256"] = normalized_input_sha256(blind_builder["input"])
    _write_jsonl(dataset / "frozen_blind.inputs.jsonl", blind_rows)

    errors = validator_module.validate_dataset()["errors"]
    assert any("builder canonical candidate sequence is duplicated" in error for error in errors)


def test_static_subject_receipts_never_promote_fixture_or_source_prior_to_provider_fact():
    report = validate_dataset()
    assert report["pollution_contract"]["static_subject_evidence"]["real_provider"] == 0
    manifest = json.loads((DATASET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    records = _jsonl(DATASET_ROOT / manifest["subject_receipt_registry"])
    development = [row for row in records if row["split"] != "frozen_blind"]
    blind = [row for row in records if row["split"] == "frozen_blind"]
    assert development and blind
    assert all(row["evidence_class"] == "CONTROLLED_FIXTURE_EXECUTION" for row in development)
    assert all(row["evidence_class"] == "UNAVAILABLE" for row in blind)
    assert all(row["provider_call_attempted"] is False for row in records)
    assert all(row["current_fact_authority"] is False for row in records)
    assert all(row["live_provider_evidence"] is False for row in records)
    assert all(row["observed_at"] is None and row["source_artifact"] is None for row in records)


def test_repeated_poi_occurrences_have_distinct_stop_level_receipts():
    manifest = json.loads((DATASET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    cases = [row for entry in manifest["files"] for row in _jsonl(DATASET_ROOT / entry["inputs"])]
    target = next(row for row in cases if row["case_id"] == "dev.sh.builder.insert-edge-four-tiers")
    by_path = {
        ref["subject_path"]: ref["receipt_id"]
        for ref in target["receipt_refs"]["subject_evidence_refs"]
    }
    assert target["input"]["seed"]["place_id"] == target["input"]["initial_route"][0]["place_id"]
    assert by_path["input.seed"] != by_path["input.initial_route[0]"]


def test_runtime_receipt_policy_requires_import_rejected_candidates_and_does_not_accept_static_substitution():
    manifest = json.loads((DATASET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    policy = manifest["runtime_receipt_policy"]
    assert "IMPORT_REJECTED_CANDIDATE" in policy["required_subjects"]
    assert "BUILDER_ROUTE_LEG" in policy["required_subjects"]
    assert "BUILDER_CURRENT_FACT" in policy["required_subjects"]
    assert policy["static_fixture_substitution_allowed"] is False
    assert policy["source_prior_substitution_allowed"] is False
    assert validate_dataset()["pollution_contract"][
        "import_not_found_cases_requiring_runtime_rejected_candidate_receipts"
    ] == 1


def test_validator_fails_closed_on_subject_receipt_registry_file_tamper(tmp_path, monkeypatch):
    dataset = _isolated_validation_tree(tmp_path, monkeypatch)
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    registry_path = dataset / manifest["subject_receipt_registry"]
    rows = _jsonl(registry_path)
    rows[0]["provider"] = "tampered_fixture"
    _write_jsonl(registry_path, rows)

    errors = validator_module.validate_dataset()["errors"]
    assert any("subject_receipt_registry: file hash mismatch" in error for error in errors)
    assert any("subject receipt bytes mismatch" in error for error in errors)


def test_validator_recomputes_subject_binding_after_registry_hash_is_resealed(tmp_path, monkeypatch):
    dataset = _isolated_validation_tree(tmp_path, monkeypatch)
    manifest_path = dataset / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    registry_path = dataset / manifest["subject_receipt_registry"]
    rows = _jsonl(registry_path)
    rows[0]["subject_sha256"] = "0" * 64
    _write_jsonl(registry_path, rows)
    manifest["subject_receipt_registry_sha256"] = hashlib.sha256(registry_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    errors = validator_module.validate_dataset()["errors"]
    assert not any("subject_receipt_registry: file hash mismatch" in error for error in errors)
    assert any("subject receipt bytes mismatch" in error for error in errors)


def test_validator_rejects_receipt_reuse_across_two_subject_paths(tmp_path, monkeypatch):
    dataset = _isolated_validation_tree(tmp_path, monkeypatch)
    dev_rows = _jsonl(dataset / "dev.inputs.jsonl")
    target = next(row for row in dev_rows if row["case_id"] == "dev.sh.builder.insert-edge-four-tiers")
    refs = target["receipt_refs"]["subject_evidence_refs"]
    by_path = {ref["subject_path"]: ref for ref in refs}
    by_path["input.initial_route[0]"]["receipt_id"] = by_path["input.seed"]["receipt_id"]
    by_path["input.initial_route[0]"]["record_sha256"] = by_path["input.seed"]["record_sha256"]
    _write_jsonl(dataset / "dev.inputs.jsonl", dev_rows)

    errors = validator_module.validate_dataset()["errors"]
    assert any("subject evidence refs do not match input bytes" in error for error in errors)
