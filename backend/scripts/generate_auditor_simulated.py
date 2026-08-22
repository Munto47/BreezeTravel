"""Generate the isolated, synthetic Auditor evaluation corpus.

This corpus is intentionally model/simulation evidence.  It must never be
merged into ``eval_data/auditor`` or counted as human M1 calibration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from scripts.auditor_proxy_contract import (  # noqa: E402
    CALIBRATION_LANE,
    EVIDENCE_TYPE,
    build_role_contracts,
    canonical_sha256,
)


SCHEMA_VERSION = "auditor-simulated-v3"
EVIDENCE_BOUNDARY = EVIDENCE_TYPE
DEFAULT_OUTPUT_DIR = BACKEND / "eval_data" / "auditor_simulated"

CITY_PLACES = {
    "北京": [
        "故宫博物院", "景山公园", "南锣鼓巷", "颐和园", "天坛公园",
        "什刹海", "北海公园", "国家博物馆", "圆明园", "奥林匹克森林公园",
    ],
    "上海": [
        "上海博物馆", "外滩", "豫园", "武康路", "上海中心",
        "田子坊", "世纪公园", "上海科技馆", "朱家角古镇", "共青森林公园",
    ],
    "杭州": [
        "西湖", "灵隐寺", "西溪湿地", "河坊街", "良渚博物院",
        "京杭大运河", "中国丝绸博物馆", "九溪烟树", "湘湖", "杭州植物园",
    ],
}

_NAME_SUFFIXES = "甲乙丙丁戊己庚辛壬癸"
CITY_FOOD_AND_HOTELS = {
    "北京": {
        "foods": [f"北京模拟{suffix}餐厅" for suffix in _NAME_SUFFIXES],
        "hotels": [f"北京模拟{suffix}酒店" for suffix in _NAME_SUFFIXES[:5]],
    },
    "上海": {
        "foods": [f"上海模拟{suffix}餐厅" for suffix in _NAME_SUFFIXES],
        "hotels": [f"上海模拟{suffix}酒店" for suffix in _NAME_SUFFIXES[:5]],
    },
    "杭州": {
        "foods": [f"杭州模拟{suffix}餐厅" for suffix in _NAME_SUFFIXES],
        "hotels": [f"杭州模拟{suffix}酒店" for suffix in _NAME_SUFFIXES[:5]],
    },
}

INJECTED_FINDINGS = [
    ("PACE", "TIME_CHAIN_BROKEN", "首日两个 parser 可读时间段发生重叠", "HIGH", "VIOLATED"),
    ("PACE", "DUPLICATE_PLACE", "首日两个 parser 可读 stop 指向同一地点", "HIGH", "VIOLATED"),
    ("PACE", "TIME_DATA_INVALID", "首日第二个 stop 缺少可解析时间", "HIGH", "UNKNOWN"),
    ("ENTITY", "PLACE_NOT_RESOLVED", "首日第二个 stop 是无法解析的模拟地点", "BLOCKER", "VIOLATED"),
]

ENVIRONMENT_FINDINGS = [
    ("OPENING", "OPENING_HOURS_MISSING", "离线合成集不提供营业时间事实", "HIGH", "UNKNOWN"),
    ("OTHER", "WEATHER_DATA_MISSING", "离线合成集不提供逐日天气事实", "MEDIUM", "UNKNOWN"),
]

REASON_RULE_MAPPING = {
    "TIME_CHAIN_BROKEN": "constraint.time_chain",
    "TIME_DATA_INVALID": "constraint.time_chain",
    "DUPLICATE_PLACE": "constraint.duplicate",
    "PLACE_NOT_RESOLVED": "audit.input_completeness",
    "IMPORT_PARSE_FAILED": "import.parser",
    "OPENING_HOURS_MISSING": "constraint.opening_hours",
    "TRAVEL_TIME_MISSING": "constraint.travel_time",
    "WEATHER_DATA_MISSING": "constraint.weather",
}


def _finding(case_id: str, index: int, spec: tuple[str, str, str, str, str], provenance: str) -> dict[str, Any]:
    category, reason_code, description, severity, expected_status = spec
    return {
        "finding_id": f"{case_id}-finding-{index:02d}",
        "category": category,
        "reason_code": reason_code,
        "expected_rule_id": REASON_RULE_MAPPING[reason_code],
        "severity": severity,
        "expected_status": expected_status,
        "description": description,
        "provenance": provenance,
        "is_original_error": provenance == "original_error",
        "injected_by_simulation": provenance == "injected_error",
    }


def _split_for(index: int, *, boundary: bool = False) -> str:
    # Per city: regular source documents 12/4/4 and boundary documents 6/2/2.
    train_limit, validation_limit = (6, 8) if boundary else (12, 16)
    if index < train_limit:
        return "train"
    if index < validation_limit:
        return "validation"
    return "test"


def _raw_plan(city: str, days: int, group_size: int, seed: int) -> str:
    places = CITY_PLACES[city]
    foods = CITY_FOOD_AND_HOTELS[city]["foods"]
    hotels = CITY_FOOD_AND_HOTELS[city]["hotels"]
    lines = [f"同行：{group_size}人，目的地{city}，偏好公共交通，预算适中。"]
    for day in range(1, days + 1):
        first = places[(seed + 2 * (day - 1)) % len(places)]
        second = places[(seed + 2 * (day - 1) + 1) % len(places)]
        lunch = foods[(seed + 2 * (day - 1)) % len(foods)]
        dinner = foods[(seed + 2 * (day - 1) + 1) % len(foods)]
        hotel = hotels[(seed + day - 1) % len(hotels)]
        start = 9 + (seed + day) % 2
        lines.append(
            f"第{day}天：{start:02d}:00-{start + 2:02d}:00 {first}；"
            f"12:15-13:15 {lunch}；14:00-16:00 {second}；"
            f"18:30-19:30 {dinner}；21:00-22:00 {hotel}。"
        )
    return "\n".join(lines)


def _mutate_plan(raw: str, city: str, mutation_index: int) -> str:
    mutation = mutation_index % len(INJECTED_FINDINGS)
    lines = raw.splitlines()
    first_day = lines[1]
    segments = first_day.split("；")
    if mutation == 0:
        # Replace the second stop time itself: parser sees two overlapping stops.
        segments[2] = segments[2].replace("14:00-16:00", "10:30-12:30")
    if mutation == 1:
        first_name = CITY_PLACES[city][mutation_index % len(CITY_PLACES[city])]
        # Both stop names are changed to the same canonical fixture place.
        second_name = CITY_PLACES[city][(mutation_index + 1) % len(CITY_PLACES[city])]
        segments[2] = segments[2].replace(second_name, first_name)
    if mutation == 2:
        segments[2] = segments[2].replace("14:00-16:00 ", "")
    if mutation == 3:
        known_name = CITY_PLACES[city][(mutation_index + 1) % len(CITY_PLACES[city])]
        segments[2] = segments[2].replace(known_name, f"云端秘境{mutation_index + 1}号馆")
    lines[1] = "；".join(segments)
    return "\n".join(lines)


def _boundary_plan(
    city: str, days: int, group_size: int, seed: int, variant: int,
) -> tuple[str, str, str]:
    if variant == 0:
        return "说明：仅有模糊需求，地点和时间都未提供。", "IMPORT_PARSE_FAILED", "UNKNOWN"
    if variant == 4:
        return "注意：截图字迹完全无法辨认。", "IMPORT_PARSE_FAILED", "UNKNOWN"

    lines = _raw_plan(city, days, group_size, seed).splitlines()
    segments = lines[1].split("；")
    second_name = CITY_PLACES[city][(seed + 1) % len(CITY_PLACES[city])]
    if variant == 1:
        segments[2] = segments[2].replace("14:00-16:00 ", "")
        reason, status = "TIME_DATA_INVALID", "UNKNOWN"
    elif variant == 2:
        segments[2] = segments[2].replace(second_name, "云端未知地点")
        reason, status = "PLACE_NOT_RESOLVED", "VIOLATED"
    else:
        segments[2] = segments[2].replace(second_name, "待定")
        reason, status = "PLACE_NOT_RESOLVED", "VIOLATED"
    lines[1] = "；".join(segments)
    return "\n".join(lines), reason, status


def _profile(city: str, group_size: int, seed: int) -> dict[str, Any]:
    return {
        "persona_id": f"sim-organizer-{city}-{seed:02d}",
        "label_source": "gpt-5.6-sol-subagent-simulation",
        "city_familiarity": ["first_visit", "visited_once", "local_familiar"][seed % 3],
        "group_size": group_size,
        "priority": ["少走回头路", "预约风险优先", "照顾同行节奏"][seed % 3],
        "is_real_human": False,
    }


def _repair_decisions(case_id: str, source_kind: str, seed: int) -> list[dict[str, Any]]:
    if source_kind == "SIMULATED_CONTROLLED_MUTATION":
        target_reason = INJECTED_FINDINGS[seed % len(INJECTED_FINDINGS)][1]
        decision = "ACCEPT" if target_reason in {"TIME_CHAIN_BROKEN", "DUPLICATE_PLACE"} else "REJECT"
        reason_code = "POSTCHECK_REMOVES_TARGET_VIOLATION" if decision == "ACCEPT" else "TARGET_REQUIRES_CONFIRMATION"
    elif source_kind == "SIMULATED_BOUNDARY":
        decision, reason_code = "SKIP", "INSUFFICIENT_RESOLVED_INPUT"
    else:
        decision, reason_code = "SKIP", "NO_CONTROLLED_MUTATION_TO_REPAIR"
    return [
        {
            "decision_id": f"{case_id}-repair-01",
            "decision": decision,
            "reason_code": reason_code,
            "target_reason_code": (
                INJECTED_FINDINGS[seed % len(INJECTED_FINDINGS)][1]
                if source_kind == "SIMULATED_CONTROLLED_MUTATION"
                else None
            ),
            "label_source": "simulated_organizer_not_human",
        }
    ]


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for city_index, city in enumerate(CITY_PLACES):
        city_code = ("bj", "sh", "hz")[city_index]
        for source_index in range(20):
            source_id = f"sim-{city_code}-source-{source_index + 1:02d}"
            split = _split_for(source_index)
            days = 2 + source_index % 4
            group_size = 2 + (source_index + city_index) % 4
            raw = _raw_plan(city, days, group_size, source_index)
            # Baselines contain no text-implied defect. Missing live facts are
            # represented separately as UNKNOWN environment findings.
            original_specs: list[tuple[str, str, str, str, str]] = []

            original_id = f"sim-{city_code}-original-{source_index + 1:02d}"
            original_findings = [
                _finding(original_id, idx + 1, spec, "original_error")
                for idx, spec in enumerate(original_specs)
            ]
            environment_findings = [
                _finding(original_id, idx + 1, spec, "environment_unknown")
                for idx, spec in enumerate(ENVIRONMENT_FINDINGS)
            ]
            cases.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "evidence_boundary": EVIDENCE_BOUNDARY,
                    "case_id": original_id,
                    "source_document_id": source_id,
                    "split": split,
                    "city": city,
                    "trip_days": days,
                    "group_size": group_size,
                    "source_kind": "SIMULATED_AI_ITINERARY",
                    "raw_itinerary": raw,
                    "simulated_organizer_profile": _profile(city, group_size, source_index),
                    "original_errors": original_findings,
                    "injected_errors": [],
                    "simulated_findings": environment_findings,
                    "simulated_repair_decisions": _repair_decisions(
                        original_id, "SIMULATED_AI_ITINERARY", source_index
                    ),
                    "m1_eligible": False,
                }
            )

            mutation_id = f"sim-{city_code}-mutation-{source_index + 1:02d}"
            injected = [_finding(mutation_id, 1, INJECTED_FINDINGS[source_index % 4], "injected_error")]
            # Give retained original findings IDs scoped to the mutation case.
            retained: list[dict[str, Any]] = []
            mutation_environment = [
                _finding(mutation_id, idx + 2, spec, "environment_unknown")
                for idx, spec in enumerate(ENVIRONMENT_FINDINGS)
            ]
            cases.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "evidence_boundary": EVIDENCE_BOUNDARY,
                    "case_id": mutation_id,
                    "source_document_id": source_id,
                    "split": split,
                    "city": city,
                    "trip_days": days,
                    "group_size": group_size,
                    "source_kind": "SIMULATED_CONTROLLED_MUTATION",
                    "raw_itinerary": _mutate_plan(raw, city, source_index),
                    "simulated_organizer_profile": _profile(city, group_size, source_index),
                    "original_errors": retained,
                    "injected_errors": injected,
                    "simulated_findings": injected + mutation_environment,
                    "simulated_repair_decisions": _repair_decisions(
                        mutation_id, "SIMULATED_CONTROLLED_MUTATION", source_index
                    ),
                    "m1_eligible": False,
                }
            )

        for boundary_index in range(10):
            case_id = f"sim-{city_code}-boundary-{boundary_index + 1:02d}"
            source_id = f"sim-{city_code}-boundary-source-{boundary_index + 1:02d}"
            days = 2 + boundary_index % 4
            group_size = 2 + (boundary_index + city_index) % 4
            raw_boundary, boundary_reason, boundary_status = _boundary_plan(
                city,
                days,
                group_size,
                boundary_index,
                boundary_index % 5,
            )
            spec = (
                "OTHER" if boundary_reason == "IMPORT_PARSE_FAILED" else "PACE" if boundary_reason == "TIME_DATA_INVALID" else "ENTITY",
                boundary_reason,
                "边界输入缺少可靠的时间或地点，必须保留 UNKNOWN/未解析状态并请求确认",
                "HIGH" if boundary_status == "UNKNOWN" else "BLOCKER",
                boundary_status,
            )
            finding = _finding(case_id, 1, spec, "original_error")
            boundary_environment = [
                _finding(case_id, idx + 2, environment_spec, "environment_unknown")
                for idx, environment_spec in enumerate(ENVIRONMENT_FINDINGS)
            ] if boundary_reason != "IMPORT_PARSE_FAILED" else []
            cases.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "evidence_boundary": EVIDENCE_BOUNDARY,
                    "case_id": case_id,
                    "source_document_id": source_id,
                    "split": _split_for(boundary_index, boundary=True),
                    "city": city,
                    "trip_days": days,
                    "group_size": group_size,
                    "source_kind": "SIMULATED_BOUNDARY",
                    "raw_itinerary": raw_boundary,
                    "simulated_organizer_profile": _profile(city, group_size, 20 + boundary_index),
                    "original_errors": [finding],
                    "injected_errors": [],
                    "simulated_findings": [finding, *boundary_environment],
                    "simulated_repair_decisions": _repair_decisions(
                        case_id, "SIMULATED_BOUNDARY", boundary_index
                    ),
                    "m1_eligible": False,
                }
            )
    return cases


def write_dataset(output_dir: Path = DEFAULT_OUTPUT_DIR) -> tuple[Path, Path]:
    cases = build_cases()
    output_dir.mkdir(parents=True, exist_ok=True)
    cases_path = output_dir / "cases.jsonl"
    payload = "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases)
    cases_path.write_text(payload, encoding="utf-8", newline="\n")

    city_counts = Counter(case["city"] for case in cases)
    kind_counts = Counter(case["source_kind"] for case in cases)
    split_counts = Counter(case["split"] for case in cases)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": "breezetravel-auditor-simulated-v3-2026-08-20",
        "evidence_boundary": EVIDENCE_BOUNDARY,
        "evidence_type": EVIDENCE_TYPE,
        "calibration_lane": CALIBRATION_LANE,
        "human_labels": False,
        "human_validated": False,
        "m1_eligible": False,
        "m1_dev_eligible": True,
        "m1_human_eligible": False,
        "m1_policy": "must_not_increment_human_counts_or_satisfy_m1",
        "generated_by": "deterministic_synthetic_fixture_generator",
        "deterministic": True,
        "cases_file": "cases.jsonl",
        "cases_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "case_count": len(cases),
        "source_document_count": len({case["source_document_id"] for case in cases}),
        "city_counts": dict(sorted(city_counts.items())),
        "source_kind_counts": dict(sorted(kind_counts.items())),
        "split_counts": dict(sorted(split_counts.items())),
        "constraints": {"cities": list(CITY_PLACES), "trip_days": [2, 5], "group_size": [2, 5]},
    }
    role_contracts = build_role_contracts()
    roles_path = output_dir / "proxy_role_contracts.json"
    roles_payload = json.dumps(
        role_contracts, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    roles_path.write_text(roles_payload, encoding="utf-8", newline="\n")
    manifest["proxy_role_contracts_file"] = roles_path.name
    manifest["proxy_role_contracts_sha256"] = hashlib.sha256(
        roles_payload.encode("utf-8")
    ).hexdigest()
    manifest["proxy_role_contracts_canonical_sha256"] = canonical_sha256(role_contracts)
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest_path, cases_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    manifest_path, cases_path = write_dataset(args.output_dir)
    print(f"wrote {manifest_path}")
    print(f"wrote {cases_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
