from __future__ import annotations

import hashlib
import json
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.trip_intake.models import TripIntakeExtraction, validate_extraction_evidence
from scripts.generate_trip_nlu_v2 import (
    SPLITS,
    build_case,
    expanded,
    stable_spread,
    write_json,
    write_jsonl,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "eval_data" / "trip_nlu_v2"
OUTPUT_ROOT = ROOT / "eval_data" / "trip_nlu_v2_remediation"
REGRESSION_SOURCE_IDS = (
    "TRIP_NLU_0078",
    "TRIP_NLU_0085",
    "TRIP_NLU_0095",
    "TRIP_NLU_RV2_0007",
    "TRIP_NLU_RV2_0010",
    "TRIP_NLU_RV2_0018",
    "TRIP_NLU_RV2_0019",
)
VALIDATION_CONTRACT = "trip-nlu-v2-remediation-validation-v2"
VALIDATION_NOTE = (
    "独立验证备注：本段仅声明抽取边界，不增加地点、人数、日期、时长或偏好。"
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _replace_source_id(value: Any, old: str, new: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source_id" and item == old:
                value[key] = new
            else:
                _replace_source_id(item, old, new)
    elif isinstance(value, list):
        for item in value:
            _replace_source_id(item, old, new)


def _rename_case(case: dict[str, Any], case_id: str) -> dict[str, Any]:
    result = json.loads(json.dumps(case, ensure_ascii=False))
    old_source_id = result["source_id"]
    new_source_id = f"{case_id}:text"
    result["case_id"] = case_id
    result["source_id"] = new_source_id
    _replace_source_id(result["expected"], old_source_id, new_source_id)
    return result


def _difficulty(config: dict[str, Any], destinations: list[str], parties: list[str], durations: list[str], flags: dict[str, set[int]]) -> list[str]:
    ranked = sorted(
        range(config["count"]),
        key=lambda index: (
            (5 if destinations[index] in {"multiple", "uncertain", "missing"} else 0)
            + (4 if parties[index] == "UNKNOWN" else 2 if parties[index] != "EXACT" else 0)
            + (4 if durations[index] == "UNKNOWN" else 2 if durations[index] != "EXACT" else 0)
            + (1 if index in flags["roles"] else 0)
            + (1 if index in flags["interference"] else 0)
            + (1 if index in flags["fictional"] else 0),
            hashlib.sha256(f"{VALIDATION_CONTRACT}:difficulty:{index}".encode()).hexdigest(),
        ),
        reverse=True,
    )
    hard = set(ranked[: config["difficulty"]["hard"]])
    easy = set(ranked[-config["difficulty"]["easy"] :])
    return [
        "hard" if index in hard else "easy" if index in easy else "medium"
        for index in range(config["count"])
    ]


def _build_validation() -> list[dict[str, Any]]:
    config = SPLITS["validation"]
    destinations = stable_spread(
        expanded(config["destination"]), f"{VALIDATION_CONTRACT}:destination"
    )
    parties = stable_spread(
        expanded(config["party"]), f"{VALIDATION_CONTRACT}:party"
    )
    durations = stable_spread(
        expanded(config["duration"]), f"{VALIDATION_CONTRACT}:duration"
    )
    flag_positions: dict[str, set[int]] = {}
    for flag in ("preference", "roles", "interference", "fictional"):
        ranked = stable_spread(
            list(range(config["count"])), f"{VALIDATION_CONTRACT}:{flag}"
        )
        flag_positions[flag] = set(ranked[: config["minimums"][flag]])
    exact_positions = [index for index, value in enumerate(parties) if value == "EXACT"]
    semantic_positions = set(
        stable_spread(exact_positions, f"{VALIDATION_CONTRACT}:semantic-party")[
            : config["minimums"]["semantic_party"]
        ]
    )
    difficulties = _difficulty(
        config, destinations, parties, durations, flag_positions
    )
    prompt_offset = int(hashlib.sha256(VALIDATION_CONTRACT.encode()).hexdigest()[:8], 16)
    cases: list[dict[str, Any]] = []
    for index in range(config["count"]):
        family_number = index // 3 + 1
        generator_source = "DETERMINISTIC" if family_number <= 4 else "USER_PROMPT"
        family_id = f"REMEDIATION_V2_{'D' if family_number <= 4 else 'P'}_{family_number:02d}"
        flags = {
            "preference": index in flag_positions["preference"],
            "roles": index in flag_positions["roles"],
            "interference": index in flag_positions["interference"],
            "fictional": index in flag_positions["fictional"],
            "semantic_party": index in semantic_positions,
        }
        case, _ = build_case(
            index + 1,
            "validation_v2",
            index,
            destinations[index],
            parties[index],
            durations[index],
            difficulties[index],
            flags,
            family_id,
            generator_source,
            prompt_offset,
        )
        case = _rename_case(case, f"TRIP_NLU_RV2_{index + 1:04d}")
        case["input_text"] = f'{case["input_text"]}\n{VALIDATION_NOTE}'
        case["annotation"]["evaluation_split"] = "validation_v2"
        extraction = TripIntakeExtraction.model_validate(case["expected"])
        validate_extraction_evidence(
            extraction, {case["source_id"]: case["input_text"]}
        )
        cases.append(case)
    return cases


def _build_regression(validation_v2: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validation = {
        item["case_id"]: item
        for item in _read_jsonl(SOURCE_ROOT / "validation.jsonl")
    }
    validation.update({item["case_id"]: item for item in validation_v2})
    cases = []
    for index, source_case_id in enumerate(REGRESSION_SOURCE_IDS, start=1):
        case = _rename_case(validation[source_case_id], f"TRIP_NLU_REG_{index:04d}")
        case["annotation"]["evaluation_split"] = "regression"
        case["annotation"]["regression_source_case_id"] = source_case_id
        case["annotation"]["source_family_id"] = f"REGRESSION_FAILURE_{index:02d}"
        cases.append(case)
    return cases


def _assert_isolation(validation: list[dict[str, Any]]) -> float:
    comparison = [
        *_read_jsonl(SOURCE_ROOT / "dev.jsonl"),
        *_read_jsonl(SOURCE_ROOT / "validation.jsonl"),
        *_read_jsonl(SOURCE_ROOT / "frozen_blind.inputs.jsonl"),
    ]
    maximum = 0.0
    for left in validation:
        for right in comparison:
            ratio = SequenceMatcher(
                None, left["input_text"], right["input_text"]
            ).ratio()
            maximum = max(maximum, ratio)
            if ratio >= 0.90:
                raise ValueError(
                    f"validation_v2 near duplicate: {left['case_id']} / "
                    f"{right['case_id']} ratio={ratio:.4f}"
                )
    return maximum


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    validation = _build_validation()
    regression = _build_regression(validation)
    maximum_similarity = _assert_isolation(validation)
    write_jsonl(OUTPUT_ROOT / "regression.jsonl", regression)
    write_jsonl(OUTPUT_ROOT / "validation_v2.jsonl", validation)
    files = {
        name: _sha256(OUTPUT_ROOT / name)
        for name in ("regression.jsonl", "validation_v2.jsonl")
    }
    write_json(
        OUTPUT_ROOT / "manifest.json",
        {
            "schema_version": "trip-nlu-v2-remediation-manifest-v1",
            "generator_contract": VALIDATION_CONTRACT,
            "regression_count": len(regression),
            "validation_count": len(validation),
            "regression_source_case_ids": list(REGRESSION_SOURCE_IDS),
            "validation_difficulty": dict(
                sorted(Counter(item["annotation"]["difficulty"] for item in validation).items())
            ),
            "validation_max_similarity_to_original_120": round(maximum_similarity, 6),
            "original_frozen_blind_modified": False,
            "files": files,
        },
    )


if __name__ == "__main__":
    main()
