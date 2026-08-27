from __future__ import annotations

import json
import os
import hashlib
import difflib
import itertools
import unicodedata
from pathlib import Path

import pytest

from evals.trip_nlu_v2.scorer import score_predictions
from evals.trip_nlu_v2.gate import run_gate
from evals.trip_nlu_v2.validator import (
    DatasetValidationError,
    _contains_forbidden_key,
    _read_jsonl,
    validate_dataset,
)
from scripts.export_trip_nlu_v1 import export_case
from scripts.generate_trip_nlu_v2 import generate


DATA_ROOT = Path("eval_data/trip_nlu_v2")
EXTERNAL_BLIND = Path(os.environ.get("TRIP_NLU_V2_BLIND_LABELS", ""))


def _write_run_spec(tmp_path: Path, predictions: Path, labels: Path = EXTERNAL_BLIND) -> Path:
    run_spec = tmp_path / "run-spec.json"
    manifest = json.loads((DATA_ROOT / "manifest.json").read_text(encoding="utf-8"))
    run_spec.write_text(
        json.dumps(
            {
                "schema_version": "trip-nlu-v2-run-spec-v1",
                "run_id": "test-perfect-run",
                "dataset_manifest_sha256": hashlib.sha256(
                    (DATA_ROOT / "manifest.json").read_bytes()
                ).hexdigest(),
                "blind_label_sha256": hashlib.sha256(labels.read_bytes()).hexdigest(),
                "product_outputs_sha256": hashlib.sha256(predictions.read_bytes()).hexdigest(),
                "validator_sha256": manifest["code_bindings"]["validator_sha256"],
                "scorer_sha256": manifest["code_bindings"]["scorer_sha256"],
                "schema_sha256": manifest["code_bindings"]["schema_sha256"],
                "model_binding": {
                    "model_name": "fixture-oracle",
                    "model_version": "test-v1",
                    "prompt_sha256": hashlib.sha256(b"test-prompt").hexdigest(),
                    "schema_sha256": manifest["code_bindings"]["schema_sha256"],
                    "config_sha256": hashlib.sha256(b"test-config").hexdigest(),
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return run_spec


def test_public_validator_proves_exact_120_case_contract_without_reading_blind_truth() -> None:
    receipt = validate_dataset(DATA_ROOT)
    assert receipt["valid"] is True
    assert receipt["case_count"] == 120
    assert receipt["blind_labels_read"] is False
    assert receipt["evidence_span_validity"] == 1
    assert receipt["coverage"]["destination"] == {
        "北京": 30,
        "上海": 30,
        "杭州": 30,
        "other": 12,
        "multiple": 6,
        "uncertain": 6,
        "missing": 6,
    }
    assert receipt["coverage"]["minimums"]["preference"] >= 96


def test_dataset_covers_explicit_no_preference_aliases_return_roles_and_requirements() -> None:
    cases = [*_read_jsonl(DATA_ROOT / "dev.jsonl"), *_read_jsonl(DATA_ROOT / "validation.jsonl")]
    truth_text = json.dumps(cases, ensure_ascii=False)
    assert "NO_PREFERENCE" in truth_text
    assert any(alias in truth_text for alias in ("帝都", "魔都", "杭城"))
    assert "RETURN_LOCATION" in truth_text
    typo_cases = [case for case in cases if "typo" in case["annotation"]["noise_types"]]
    assert typo_cases
    assert all(
        any(
            mention["raw_text"] != mention["normalized_name"]
            for mention in case["expected"]["locations"]["mentions"]
            if mention["normalized_name"]
        )
        for case in typo_cases
    )
    assert {
        "accessibility",
        "accommodation",
        "budget",
        "children",
        "dietary",
        "elderly",
        "pet",
        "physical",
        "time",
        "transport",
    }.issubset(
        {
            item["category"]
            for case in cases
            for item in case["expected"]["preferences"]["items"]
            if item["polarity"] == "REQUIREMENT"
        }
    )


def test_truth_has_no_cross_split_near_duplicates_and_hard_covers_ambiguity() -> None:
    split_cases = {
        "dev": _read_jsonl(DATA_ROOT / "dev.jsonl"),
        "validation": _read_jsonl(DATA_ROOT / "validation.jsonl"),
        "frozen_blind": _read_jsonl(DATA_ROOT / "frozen_blind.inputs.jsonl"),
    }
    maximum = 0.0
    for left_name, right_name in itertools.combinations(split_cases, 2):
        for left in split_cases[left_name]:
            for right in split_cases[right_name]:
                left_text = unicodedata.normalize("NFC", left["input_text"].replace("\r\n", "\n"))
                right_text = unicodedata.normalize("NFC", right["input_text"].replace("\r\n", "\n"))
                maximum = max(maximum, difflib.SequenceMatcher(None, left_text, right_text).ratio())
    assert maximum < 0.82
    public_hard = [
        case
        for cases in (split_cases["dev"], split_cases["validation"])
        for case in cases
        if case["annotation"]["difficulty"] == "hard"
    ]
    assert any(case["annotation"]["coverage"]["destination"] in {"multiple", "uncertain", "missing"} for case in public_hard)
    assert any(case["annotation"]["coverage"]["party"] in {"UNKNOWN", "RANGE"} for case in public_hard)
    assert any(case["annotation"]["coverage"]["duration"] in {"UNKNOWN", "RANGE"} for case in public_hard)


def test_unknown_statements_have_evidence_and_conflicting_days_nights_have_issue() -> None:
    cases = [*_read_jsonl(DATA_ROOT / "dev.jsonl"), *_read_jsonl(DATA_ROOT / "validation.jsonl")]
    unknown_party = [case for case in cases if case["annotation"]["coverage"]["party"] == "UNKNOWN"]
    unknown_days = [case for case in cases if case["annotation"]["coverage"]["duration"] == "UNKNOWN"]
    assert unknown_party and all(case["expected"]["party_size"]["total"]["evidence"] for case in unknown_party)
    assert unknown_days and all(case["expected"]["temporal"]["days"]["evidence"] for case in unknown_days)
    conflicts = [
        case
        for case in cases
        if any(issue["code"] == "DAYS_NIGHTS_CONFLICT" for issue in case["expected"]["issues"])
    ]
    assert conflicts
    range_cases = [case for case in cases if case["annotation"]["coverage"]["party"] == "RANGE"]
    assert all("朋友" not in case["expected"]["party_size"]["composition"]["tags"] for case in range_cases)


def test_generator_refuses_blind_truth_output_inside_repository(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside the repository"):
        generate(
            tmp_path / "data",
            DATA_ROOT / "leaked-labels",
            tmp_path / "missing-prompt.txt",
        )


@pytest.mark.skipif(not EXTERNAL_BLIND.is_file(), reason="external blind labels are intentionally not in Git")
def test_isolated_validator_recomputes_blind_truth_against_commitment() -> None:
    receipt = validate_dataset(DATA_ROOT, external_blind_labels=EXTERNAL_BLIND)
    assert receipt["blind_labels_read"] is True
    assert receipt["coverage"]["party"]["UNKNOWN"] == 18


@pytest.mark.skipif(not EXTERNAL_BLIND.is_file(), reason="external blind labels are intentionally not in Git")
def test_isolated_gate_binds_valid_predictions_evidence_and_external_truth(tmp_path: Path) -> None:
    labels = _read_jsonl(EXTERNAL_BLIND)
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        "".join(
            json.dumps({"case_id": item["case_id"], "prediction": item["expected"]}, ensure_ascii=False)
            + "\n"
            for item in labels
        ),
        encoding="utf-8",
    )
    receipt = run_gate(DATA_ROOT, predictions, EXTERNAL_BLIND, _write_run_spec(tmp_path, predictions))
    assert receipt["gate"] == "PASS"
    assert receipt["evidence_span_validity"] == 1
    assert receipt["run_id"] == "test-perfect-run"


@pytest.mark.skipif(not EXTERNAL_BLIND.is_file(), reason="external blind labels are intentionally not in Git")
def test_gate_rejects_unbound_run_spec(tmp_path: Path) -> None:
    labels = _read_jsonl(EXTERNAL_BLIND)
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        "".join(
            json.dumps({"case_id": item["case_id"], "prediction": item["expected"]}, ensure_ascii=False)
            + "\n"
            for item in labels
        ),
        encoding="utf-8",
    )
    run_spec = _write_run_spec(tmp_path, predictions)
    payload = json.loads(run_spec.read_text(encoding="utf-8"))
    payload["product_outputs_sha256"] = "0" * 64
    run_spec.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DatasetValidationError, match="RunSpec"):
        run_gate(DATA_ROOT, predictions, EXTERNAL_BLIND, run_spec)


def test_blind_inputs_and_seal_do_not_leak_nested_truth() -> None:
    blind = _read_jsonl(DATA_ROOT / "frozen_blind.inputs.jsonl")
    seal = json.loads((DATA_ROOT / "sealed/frozen_blind.labels.jsonl").read_text(encoding="utf-8"))
    assert not any(_contains_forbidden_key(item) for item in blind)
    assert seal["scoring_payload_present"] is False
    assert "expected" not in json.dumps(seal)


def test_external_labels_inside_repository_are_rejected() -> None:
    with pytest.raises(DatasetValidationError, match="outside the repository"):
        validate_dataset(
            DATA_ROOT,
            external_blind_labels=DATA_ROOT / "frozen_blind.inputs.jsonl",
        )


def test_perfect_complete_blind_predictions_pass_aggregate_gate(tmp_path: Path) -> None:
    labels_path = DATA_ROOT / "validation.jsonl"
    labels = _read_jsonl(labels_path)
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        "".join(
            json.dumps(
                {"case_id": item["case_id"], "prediction": item["expected"]},
                ensure_ascii=False,
            )
            + "\n"
            for item in labels
        ),
        encoding="utf-8",
    )
    receipt = score_predictions(predictions, labels_path)
    assert receipt["gate"] == "PASS"
    assert receipt["case_count"] == 24
    assert receipt["case_details_present"] is False
    assert all(value["micro_f1"] == 1 for value in receipt["metrics"].values())


def test_incomplete_predictions_are_rejected_instead_of_scoring_valid_rows(tmp_path: Path) -> None:
    labels_path = DATA_ROOT / "validation.jsonl"
    labels = _read_jsonl(labels_path)
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        json.dumps({"case_id": labels[0]["case_id"], "prediction": labels[0]["expected"]}),
        encoding="utf-8",
    )
    with pytest.raises(DatasetValidationError, match="complete 24-case"):
        score_predictions(predictions, labels_path)


def test_critical_error_counters_detect_role_negation_old_plan_and_unknown_reversals(
    tmp_path: Path,
) -> None:
    labels_path = DATA_ROOT / "validation.jsonl"
    labels = _read_jsonl(labels_path)
    predictions_payload = []
    mutated = 0
    for label in labels:
        prediction = json.loads(json.dumps(label["expected"]))
        if mutated == 0:
            origin = next(
                (item for item in prediction["locations"]["mentions"] if item["role"] == "ORIGIN"),
                None,
            )
            primary = next(
                (
                    item
                    for item in prediction["locations"]["mentions"]
                    if item["role"] == "PRIMARY_DESTINATION"
                ),
                None,
            )
            if origin is not None and primary is not None:
                origin["role"], primary["role"] = "PRIMARY_DESTINATION", "ORIGIN"
                prediction["locations"]["primary_mention_id"] = origin["mention_id"]
                mutated += 1
        elif mutated == 1:
            for mention in prediction["locations"]["mentions"]:
                if mention["role"] == "EXCLUDED":
                    mention["role"] = "REQUESTED_PLACE"
                    mutated += 1
                    break
        elif mutated == 2:
            for mention in prediction["locations"]["mentions"]:
                if mention["role"] == "OTHER_MENTION":
                    mention["role"] = "REQUESTED_PLACE"
                    mutated += 1
                    break
        elif mutated == 3 and prediction["party_size"]["total"]["quantifier"] == "UNKNOWN":
            prediction["party_size"]["total"].update(
                {"quantifier": "EXACT", "min": 2, "max": 2, "derivation": "EXPLICIT_COUNT"}
            )
            mutated += 1
        predictions_payload.append({"case_id": label["case_id"], "prediction": prediction})
    assert mutated == 4
    predictions = tmp_path / "mutated.jsonl"
    predictions.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in predictions_payload),
        encoding="utf-8",
    )
    receipt = score_predictions(predictions, labels_path)
    assert receipt["gate"] == "REJECT"
    assert receipt["critical_errors"]["origin_destination_reversal"] >= 1
    assert receipt["critical_errors"]["negation_reversal"] >= 1
    assert receipt["critical_errors"]["old_plan_reversal"] >= 1
    assert receipt["critical_errors"]["unknown_promoted_to_exact"] >= 1


def test_critical_error_counters_detect_hallucination_and_numeric_cross_class(tmp_path: Path) -> None:
    labels_path = DATA_ROOT / "validation.jsonl"
    labels = _read_jsonl(labels_path)
    payload = []
    hallucinated = numeric_crossed = False
    for label in labels:
        prediction = json.loads(json.dumps(label["expected"]))
        if not hallucinated and prediction["locations"]["mentions"]:
            invented = json.loads(json.dumps(prediction["locations"]["mentions"][0]))
            invented.update(
                {
                    "mention_id": "invented-location",
                    "raw_text": invented["raw_text"],
                    "normalized_name": "不存在市",
                    "role": "OTHER_MENTION",
                }
            )
            prediction["locations"]["mentions"].append(invented)
            hallucinated = True
        party = prediction["party_size"]["total"]
        days = prediction["temporal"]["days"]
        day_bounds = {value for value in (days["min"], days["max"]) if value is not None}
        party_bounds = {value for value in (party["min"], party["max"]) if value is not None}
        if not numeric_crossed and day_bounds and day_bounds != party_bounds:
            crossed = min(day_bounds)
            party.update(
                {"quantifier": "EXACT", "min": crossed, "max": crossed, "derivation": "EXPLICIT_COUNT"}
            )
            numeric_crossed = True
        payload.append({"case_id": label["case_id"], "prediction": prediction})
    assert hallucinated and numeric_crossed
    predictions = tmp_path / "mutated.jsonl"
    predictions.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in payload),
        encoding="utf-8",
    )
    receipt = score_predictions(predictions, labels_path)
    assert receipt["critical_errors"]["hallucination"] >= 1
    assert receipt["critical_errors"]["numeric_cross_class"] >= 1


@pytest.mark.parametrize("mutation", ["unseen_party_number", "return_destination_swap", "unknown_nights"])
def test_single_critical_error_cannot_hide_behind_micro_f1(tmp_path: Path, mutation: str) -> None:
    labels_path = DATA_ROOT / "validation.jsonl"
    labels = _read_jsonl(labels_path)
    payload = []
    changed = False
    for label in labels:
        prediction = json.loads(json.dumps(label["expected"]))
        if not changed and mutation == "unseen_party_number":
            party = prediction["party_size"]["total"]
            if party["quantifier"] == "EXACT":
                party.update({"min": 99, "max": 99})
                changed = True
        elif not changed and mutation == "return_destination_swap":
            returning = next(
                (item for item in prediction["locations"]["mentions"] if item["role"] == "RETURN_LOCATION"),
                None,
            )
            primary = next(
                (
                    item
                    for item in prediction["locations"]["mentions"]
                    if item["role"] == "PRIMARY_DESTINATION"
                ),
                None,
            )
            if returning is not None and primary is not None:
                returning["role"], primary["role"] = "PRIMARY_DESTINATION", "RETURN_LOCATION"
                prediction["locations"]["primary_mention_id"] = returning["mention_id"]
                changed = True
        elif not changed and mutation == "unknown_nights":
            nights = prediction["temporal"]["nights"]
            if nights["quantifier"] == "UNKNOWN":
                nights.update(
                    {
                        "quantifier": "EXACT",
                        "min": 99,
                        "max": 99,
                        "derivation": "EXPLICIT_COUNT",
                        "evidence": prediction["party_size"]["total"]["evidence"],
                    }
                )
                changed = True
        payload.append({"case_id": label["case_id"], "prediction": prediction})
    assert changed
    predictions = tmp_path / f"{mutation}.jsonl"
    predictions.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in payload),
        encoding="utf-8",
    )
    receipt = score_predictions(predictions, labels_path)
    assert receipt["gate"] == "REJECT"
    assert any(receipt["critical_errors"].values())


@pytest.mark.parametrize(
    "mutation",
    ["drop_date_range", "drop_issues", "drop_issue_evidence", "drop_departure"],
)
def test_contract_controls_are_exact_gate_requirements(tmp_path: Path, mutation: str) -> None:
    labels_path = DATA_ROOT / "validation.jsonl"
    labels = _read_jsonl(labels_path)
    payload = []
    changed = False
    for label in labels:
        prediction = json.loads(json.dumps(label["expected"]))
        if not changed and mutation == "drop_date_range" and prediction["temporal"]["date_range"]:
            prediction["temporal"]["date_range"] = None
            changed = True
        elif not changed and mutation == "drop_issues" and prediction["issues"]:
            prediction["issues"] = []
            changed = True
        elif not changed and mutation == "drop_issue_evidence" and prediction["issues"]:
            prediction["issues"][0]["evidence"] = []
            changed = True
        elif not changed and mutation == "drop_departure" and prediction["temporal"]["departure"]:
            prediction["temporal"]["departure"] = None
            changed = True
        payload.append({"case_id": label["case_id"], "prediction": prediction})
    assert changed
    predictions = tmp_path / f"{mutation}.jsonl"
    predictions.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in payload),
        encoding="utf-8",
    )
    receipt = score_predictions(predictions, labels_path)
    assert receipt["gate"] == "REJECT"
    assert receipt["metrics"]["contract_controls"]["micro_f1"] < 1


def test_v1_export_is_a_derived_view_and_keeps_original_evidence_quotes() -> None:
    source = _read_jsonl(DATA_ROOT / "dev.jsonl")[0]
    exported = export_case(source)
    assert exported["case_id"] == source["case_id"]
    primary_id = source["expected"]["locations"]["primary_mention_id"]
    primary = next(
        item for item in source["expected"]["locations"]["mentions"] if item["mention_id"] == primary_id
    )
    assert exported["expected"]["locations"]["primary_city"] == primary["normalized_name"].removesuffix("市")
    for quotes in exported["annotation"]["evidence_spans"].values():
        assert all(quote in source["input_text"] for quote in quotes)
