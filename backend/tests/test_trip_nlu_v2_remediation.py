from pathlib import Path
import json

from evals.trip_nlu_v2.analysis_scorer import score_nonblind
from evals.trip_nlu_v2.validator import _read_jsonl
from scripts.validate_trip_nlu_v2_remediation import OUTPUT_ROOT, validate


def test_remediation_pack_is_isolated_and_does_not_read_blind_labels() -> None:
    receipt = validate()

    assert receipt["structurally_valid"] is True
    assert receipt["regression_count"] == 7
    assert receipt["validation_count"] == 24
    assert receipt["validation_family_count"] == 8
    assert receipt["original_frozen_blind_modified"] is False
    assert receipt["blind_labels_read"] is False


def test_nonblind_scorer_accepts_remediation_case_namespace(tmp_path: Path) -> None:
    labels = _read_jsonl(OUTPUT_ROOT / "validation_v2.jsonl")[:2]
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        "".join(
            __import__("json").dumps(
                {"case_id": item["case_id"], "prediction": item["expected"]},
                ensure_ascii=False,
            )
            + "\n"
            for item in labels
        ),
        encoding="utf-8",
    )

    receipt = score_nonblind(predictions, OUTPUT_ROOT / "validation_v2.jsonl", case_ids={item["case_id"] for item in labels})

    assert receipt["gate"] == "PASS"
    assert receipt["case_count"] == 2


def test_nonblind_scorer_ignores_internal_location_id_permutations(tmp_path: Path) -> None:
    label = next(
        item
        for item in _read_jsonl(OUTPUT_ROOT / "validation_v2.jsonl")
        if item["case_id"] == "TRIP_NLU_RV2_0018"
    )
    prediction = json.loads(json.dumps(label["expected"]))
    old_primary = prediction["locations"]["primary_mention_id"]
    replacements = {}
    for index, mention in enumerate(reversed(prediction["locations"]["mentions"]), start=1):
        replacements[mention["mention_id"]] = f"runtime-location-{index}"
        mention["mention_id"] = replacements[mention["mention_id"]]
    if old_primary is not None:
        prediction["locations"]["primary_mention_id"] = replacements[old_primary]
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(
        json.dumps(
            {"case_id": label["case_id"], "prediction": prediction},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    receipt = score_nonblind(
        predictions,
        OUTPUT_ROOT / "validation_v2.jsonl",
        case_ids={label["case_id"]},
    )

    assert receipt["metrics"]["locations"]["micro_f1"] == 1.0
