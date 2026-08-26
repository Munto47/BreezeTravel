from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


BACKEND_ROOT = Path(__file__).resolve().parents[3]
P5_ROOT = BACKEND_ROOT / "evals" / "trip_check_v1" / "p5"
P4_ROOT = BACKEND_ROOT / "evals" / "trip_check_v1" / "p4"
PILOT_PATH = BACKEND_ROOT / "evals" / "trip_check_v1" / "pilot.jsonl"
NONBLIND_PATH = P5_ROOT / "cases_nonblind_v1.jsonl"
BLIND_INPUT_PATH = P5_ROOT / "frozen_blind.inputs.jsonl"
BLIND_SEAL_PATH = P5_ROOT / "sealed" / "frozen_blind.seal.json"
MANIFEST_PATH = P5_ROOT / "dataset_v1.manifest.json"

CITIES = ("北京", "上海", "杭州")
SPLIT_COUNTS = {"pilot": 18, "dev": 180, "regression": 72, "frozen_blind": 90}
CITY_TOTAL = 120
INPUT_KINDS = ("TEXT", "SYNTHETIC_SCREENSHOT")
DIFFICULTIES = ("CLEAN", "MEDIUM", "HARD")
FAULT_CLASSES = (
    "advice_completeness",
    "empty_candidate_set",
    "candidate_receipt_missing",
    "route_conflict",
    "duplicate_apply",
    "concurrent_apply",
    "solver_unsat",
    "solver_timeout",
    "solver_fallback",
)

_CITY_PLACES = {
    "北京": ("故宫博物院", "天坛公园", "颐和园", "景山公园", "中国国家博物馆"),
    "上海": ("外滩", "豫园", "东方明珠广播电视塔", "田子坊", "上海迪士尼乐园"),
    "杭州": ("西湖风景名胜区", "灵隐寺", "雷峰塔", "西溪湿地国家公园", "河坊街·清河坊"),
}
_PRIVATE_PATTERN = (
    "PHONE_OR_ID_OR_EMAIL_FORBIDDEN",
    "PRIVATE_KEY_FORBIDDEN",
    "ACCESS_TOKEN_FORBIDDEN",
)


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize(item) for item in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _prefix(city: str) -> str:
    return {"北京": "bj", "上海": "sh", "杭州": "hz"}[city]


def _raw_text(*, city: str, days: int, travelers: int, unique_index: int, fault_class: str) -> str:
    places = _CITY_PLACES[city]
    first = places[unique_index % len(places)]
    second = places[(unique_index + 1) % len(places)]
    third = places[(unique_index + 2) % len(places)]
    minute = unique_index % 60
    later_minute = (unique_index // 60) % 60
    start_hour = 8 + unique_index % 2
    if fault_class == "empty_candidate_set":
        second = f"{city}待确认地点{unique_index:03d}"
    if fault_class == "route_conflict":
        return (
            f"{city}{travelers}人，{days}天。第1天 {start_hour:02d}:{minute:02d}-{start_hour + 2:02d}:{minute:02d} {first}，"
            f"{start_hour + 1:02d}:{later_minute:02d}-{start_hour + 3:02d}:{later_minute:02d} {second}；"
            f"第2天 09:{later_minute:02d}-11:{later_minute:02d} {third}。"
        )
    return (
        f"{city}{travelers}人，{days}天。第1天 {start_hour:02d}:{minute:02d}-{start_hour + 2:02d}:{minute:02d} {first}，"
        f"13:{later_minute:02d}-15:{later_minute:02d} {second}；"
        f"第2天 09:{minute:02d}-11:{minute:02d} {third}。"
    )


def _product_input(
    *, city: str, days: int, travelers: int, unique_index: int, fault_class: str, input_kind: str
) -> dict[str, Any]:
    raw_text = _raw_text(
        city=city,
        days=days,
        travelers=travelers,
        unique_index=unique_index,
        fault_class=fault_class,
    )
    if input_kind == "TEXT":
        return {"source_type": "MANUAL_TEXT", "raw_text": raw_text}
    return {
        "source_type": "SYNTHETIC_SCREENSHOT",
        "ocr_text": raw_text,
        "render_spec": {
            "schema_version": "trip-check-p5-render-spec-v1",
            "format": ("PNG", "JPEG", "WEBP")[unique_index % 3],
            "theme": ("LIGHT", "DARK")[unique_index % 2],
            "layout": ("CHAT", "MEMO", "GUIDE")[unique_index % 3],
            "width": 1080,
            "height": 1920,
            "seed": 20260823 + unique_index,
            "text_sha256": digest(raw_text),
        },
    }


def _lineage(*, unique_index: int, fault_class: str) -> dict[str, Any]:
    root = {"root_index": unique_index, "fault_class": fault_class}
    return {
        "source_family_id": f"p5-source-{digest(root)[:20]}",
        "template_family_id": f"p5-template-{digest({'bucket': unique_index // 3, **root})[:20]}",
        "generator_family_id": "p5-executable-input-factory-v1",
        "mutation_parent_case_id": None,
        "lineage_status": "RECORDED",
    }


def _case(
    *,
    case_id: str,
    split: str,
    city: str,
    days: int,
    travelers: int,
    unique_index: int,
    fault_class: str,
    source_ref: dict[str, Any],
    oracle: dict[str, Any] | None,
    product_input_override: dict[str, Any] | None = None,
    input_kind_override: str | None = None,
) -> dict[str, Any]:
    input_kind = input_kind_override or INPUT_KINDS[unique_index % len(INPUT_KINDS)]
    difficulty = DIFFICULTIES[unique_index % len(DIFFICULTIES)]
    product_input = product_input_override or _product_input(
        city=city,
        days=days,
        travelers=travelers,
        unique_index=unique_index,
        fault_class=fault_class,
        input_kind=input_kind,
    )
    row: dict[str, Any] = {
        "schema_version": "trip-check-p5-eval-case-v1",
        "case_id": case_id,
        "split": split,
        "city": city,
        "trip_days": days,
        "group_size": travelers,
        "input_kind": input_kind,
        "difficulty": difficulty,
        "coverage_tags": [fault_class, input_kind.lower(), difficulty.lower()],
        "product_input": product_input,
        "runner_control": {
            "provider_snapshot_id": "trip-check-p5-controlled-snapshot-v1",
            "fault_profile_id": fault_class,
            "seed": 20260823 + unique_index,
            "budget_profile": "p5-zero-api-v1",
        },
        "lineage": _lineage(unique_index=unique_index, fault_class=fault_class),
        "source_ref": source_ref,
        "normalized_input_sha256": digest(product_input),
        "provenance": {
            "generated_by": "p5_executable_input_factory_v1",
            "reviewed_by": "independent_p5_contract_review_v1",
            "contains_human_data": False,
            "evidence_class": "controlled_fixture",
        },
    }
    if oracle is not None:
        row["oracle"] = oracle
        row["oracle_sha256"] = digest(oracle)
    row["case_hash"] = digest(row)
    return row


def _pilot_oracle(row: dict[str, Any]) -> dict[str, Any]:
    expected = row["expected"]
    return {
        "task_success_required": True,
        "requires_user_resolution": expected["requires_user_resolution"],
        "required_reason_codes": expected["required_reason_codes"],
        "wrong_city_or_poi_max": expected["wrong_poi_auto_accept_max"],
        "max_new_blocker_high_unknown": (
            expected["repair_new_high_max"] + expected["repair_new_unknown_max"]
        ),
        "unknown_must_be_preserved": False,
        "advice_required": not expected["requires_user_resolution"],
        "specific_place_allowed": not expected["requires_user_resolution"],
        "expected_strategy_outcome": "FEASIBLE",
    }


def _p4_oracle(row: dict[str, Any]) -> dict[str, Any]:
    fixture = row["fixture"]
    return {
        "task_success_required": True,
        "requires_user_resolution": row["fault_class"] == "empty_candidate_set",
        "required_reason_codes": [fixture["finding"]["reason_code"]],
        "wrong_city_or_poi_max": 0,
        "max_new_blocker_high_unknown": row["oracle"]["max_new_blocker_high_unknown"],
        "unknown_must_be_preserved": fixture["finding"]["status"] == "UNKNOWN",
        "advice_required": row["oracle"]["advice_required"],
        "specific_place_allowed": row["oracle"]["specific_place_allowed"],
        "expected_strategy_outcome": row["oracle"]["expected_strategy_outcome"],
    }


def build_nonblind_cases() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, source in enumerate(load_jsonl(PILOT_PATH)):
        rows.append(_case(
            case_id=f"p5.pilot.{_prefix(source['city'])}.{index + 1:03d}",
            split="pilot",
            city=source["city"],
            days=source["days"],
            travelers=source["traveler_count"],
            unique_index=index,
            fault_class="advice_completeness",
            source_ref={"contract": source["schema_version"], "case_id": source["case_id"]},
            oracle=_pilot_oracle(source),
            product_input_override={"source_type": "MANUAL_TEXT", "raw_text": source["raw_text"]},
            input_kind_override="TEXT",
        ))
    offset = 1000
    for split, source_path in (
        ("dev", P4_ROOT / "dev_v1.jsonl"),
        ("regression", P4_ROOT / "regression_v1.jsonl"),
    ):
        split_offset = offset if split == "dev" else 3000
        for index, source in enumerate(load_jsonl(source_path)):
            unique_index = split_offset + index
            rows.append(_case(
                case_id=f"p5.{split}.{_prefix(source['city'])}.{index + 1:03d}",
                split=split,
                city=source["city"],
                days=source["fixture"]["days"],
                travelers=source["fixture"]["traveler_count"],
                unique_index=unique_index,
                fault_class=source["fault_class"],
                source_ref={
                    "contract": source["schema_version"],
                    "case_id": source["case_id"],
                    "case_hash": source["case_hash"],
                    "fixture_hash": source["fixture_hash"],
                    "oracle_hash": source["oracle_hash"],
                },
                oracle=_p4_oracle(source),
            ))
    return rows


def build_blind_inputs() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for city_index, city in enumerate(CITIES):
        for index in range(30):
            unique_index = 5000 + city_index * 100 + index
            fault_class = FAULT_CLASSES[index % len(FAULT_CLASSES)]
            rows.append(_case(
                case_id=f"p5.blind.{_prefix(city)}.{index + 1:03d}",
                split="frozen_blind",
                city=city,
                days=2 + index % 4,
                travelers=2 + (index // 2) % 4,
                unique_index=unique_index,
                fault_class=fault_class,
                source_ref={
                    "contract": "trip-check-p5-blind-source-v1",
                    "case_id": f"blind-custodian-{city_index}-{index:02d}",
                },
                oracle=None,
            ))
    return rows


def split_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "city_counts": dict(sorted(Counter(row["city"] for row in rows).items())),
        "input_kind_counts": dict(sorted(Counter(row["input_kind"] for row in rows).items())),
        "difficulty_counts": dict(sorted(Counter(row["difficulty"] for row in rows).items())),
        "case_ids_sha256": digest(sorted(row["case_id"] for row in rows)),
        "content_canonical_sha256": digest(rows),
    }


def source_artifacts() -> dict[str, Any]:
    paths = {
        "pilot": PILOT_PATH,
        "p4_dev": P4_ROOT / "dev_v1.jsonl",
        "p4_regression": P4_ROOT / "regression_v1.jsonl",
        "p4_manifest": P4_ROOT / "dataset_v1.manifest.json",
    }
    return {
        name: {
            "path": path.relative_to(BACKEND_ROOT).as_posix(),
            "file_sha256": file_sha256(path),
        }
        for name, path in paths.items()
    }


def legacy_overlap_debt() -> dict[str, Any]:
    dev = load_jsonl(P4_ROOT / "dev_v1.jsonl")
    regression = load_jsonl(P4_ROOT / "regression_v1.jsonl")
    dev_fixtures = {row["fixture_hash"] for row in dev}
    dev_oracles = {row["oracle_hash"] for row in dev}
    return {
        "status": "RECORDED_NOT_USED_AS_P5_ISOLATION_PROOF",
        "regression_fixture_hashes_overlapping_dev": sum(
            row["fixture_hash"] in dev_fixtures for row in regression
        ),
        "regression_oracle_hashes_overlapping_dev": sum(
            row["oracle_hash"] in dev_oracles for row in regression
        ),
        "p5_normalized_input_overlap_allowed": 0,
    }


def build_manifest(
    nonblind: list[dict[str, Any]], blind: list[dict[str, Any]], seal: dict[str, Any]
) -> dict[str, Any]:
    split_rows = {
        split: [row for row in nonblind if row["split"] == split]
        for split in ("pilot", "dev", "regression")
    }
    split_rows["frozen_blind"] = blind
    manifest: dict[str, Any] = {
        "schema_version": "trip-check-p5-dataset-manifest-v1",
        "dataset_id": "trip-check-p5-360-v1",
        "frozen": True,
        "hash_policy_version": "p5-canonical-json-nfc-lf-v1",
        "family_isolation_policy": "normalized-input-plus-lineage-v1",
        "counts": {
            "total": len(nonblind) + len(blind),
            "by_split": {split: len(rows) for split, rows in split_rows.items()},
            "by_city": dict(sorted(Counter(row["city"] for rows in split_rows.values() for row in rows).items())),
        },
        "splits": {
            **{
                split: {
                    **split_summary(rows),
                    "path": NONBLIND_PATH.relative_to(BACKEND_ROOT).as_posix(),
                    "source_contract": "trip-check-p5-eval-case-v1",
                    "adapter_id": "p5-nonblind-loader-v1",
                }
                for split, rows in split_rows.items()
                if split != "frozen_blind"
            },
            "frozen_blind": {
                **split_summary(blind),
                "path": BLIND_INPUT_PATH.relative_to(BACKEND_ROOT).as_posix(),
                "source_contract": "trip-check-p5-eval-case-v1",
                "adapter_id": "p5-blind-input-loader-v1",
                "label_access": "isolated_scorer_only",
                "label_storage": "external_bundle_only",
                "seal_path": BLIND_SEAL_PATH.relative_to(BACKEND_ROOT).as_posix(),
                "seal_sha256": file_sha256(BLIND_SEAL_PATH),
            },
        },
        "source_artifacts": source_artifacts(),
        "legacy_overlap_debt": legacy_overlap_debt(),
        "blind_commitments": {
            "labels_canonical_sha256": seal["labels_canonical_sha256"],
            "external_bundle_sha256": seal["external_bundle_sha256"],
            "rubric_sha256": seal["rubric_sha256"],
            "run_spec_template_sha256": seal["run_spec_template_sha256"],
        },
        "evidence_boundary": {
            "controlled_fixture": "DATASET_ONLY",
            "live_provider": "NOT_RUN",
            "public_e2e": "NOT_RUN",
            "human_evidence": "NOT_RUN",
        },
    }
    manifest["dataset_content_sha256"] = digest({
        "source_artifacts": manifest["source_artifacts"],
        "nonblind_file_sha256": file_sha256(NONBLIND_PATH),
        "blind_inputs_file_sha256": file_sha256(BLIND_INPUT_PATH),
        "blind_seal_sha256": file_sha256(BLIND_SEAL_PATH),
    })
    manifest["manifest_hash"] = digest(manifest)
    return manifest


def private_markers() -> tuple[str, ...]:
    return _PRIVATE_PATTERN
