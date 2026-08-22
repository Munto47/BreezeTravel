"""Idempotently add the controlled-fixture Import HTTP development matrix.

The output is development truth only.  It deliberately does not create or
rename a G2 frozen-snapshot gate and never touches frozen-blind labels.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

from scripts.validate_dual_entry_testset import (
    expected_subject_receipt_records,
    expected_subject_receipt_refs,
    normalized_input_sha256,
)


ROOT = Path(__file__).resolve().parents[1] / "eval_data" / "dual_entry_v1"


CITY_CASES: dict[str, list[dict[str, Any]]] = {
    "北京": [
        {
            "slug": "clean-meals-hotel-2d",
            "days": 2,
            "group": 2,
            "tags": ["controlled-http-30", "normal-route", "meals", "hotel"],
            "text": "第1天 09:00-11:00 故宫博物院 → 12:00-13:00 四季民福（故宫旁） → 20:00-21:00 北京饭店\n第2天 09:00-11:00 天坛公园 → 12:00-13:00 老北京炸酱面（前门店） → 20:00-21:00 桔子水晶酒店（王府井店）",
            "ids": ["B000A7BD6T", "B000A7BD75", "B000A7BD71", "B000A7BD6Z", "B000A7BD73", "B000A7BD72"],
            "audit": True,
        },
        {
            "slug": "overlap-2d",
            "days": 2,
            "group": 3,
            "tags": ["controlled-http-30", "time-overlap", "route-gap"],
            "text": "第1天 09:00-12:00 故宫博物院 → 11:30-13:00 四季民福（故宫旁） → 20:00-21:00 北京饭店\n第2天 09:00-11:00 景山公园 → 20:00-21:00 桔子水晶酒店（王府井店）",
            "ids": ["B000A7BD6T", "B000A7BD75", "B000A7BD71", "fixture-bj-jingshan-01", "B000A7BD72"],
            "audit": True,
            "repair": True,
        },
        {
            "slug": "outside-opening-2d",
            "days": 2,
            "group": 4,
            "tags": ["controlled-http-30", "opening-hours", "late-visit"],
            "text": "第1天 18:00-20:00 故宫博物院 → 20:30-21:30 北京饭店\n第2天 09:00-11:00 颐和园 → 20:00-21:00 桔子水晶酒店（王府井店）",
            "ids": ["B000A7BD6T", "B000A7BD71", "B000A7BD6V", "B000A7BD72"],
            "audit": True,
        },
        {
            "slug": "fixed-visit-3d",
            "days": 3,
            "group": 5,
            "tags": ["controlled-http-30", "fixed-appointment", "locked"],
            "text": "第1天 09:00-11:00 故宫博物院（已预约不可改） → 20:00-21:00 北京饭店\n第2天 09:00-11:00 天坛公园 → 20:00-21:00 桔子水晶酒店（王府井店）\n第3天 09:00-11:00 颐和园",
            "ids": ["B000A7BD6T", "B000A7BD71", "B000A7BD6Z", "B000A7BD72", "B000A7BD6V"],
            "audit": True,
        },
        {
            "slug": "duplicate-place-3d",
            "days": 3,
            "group": 2,
            "tags": ["controlled-http-30", "duplicate-place", "canonical-id"],
            "text": "第1天 09:00-11:00 景山公园 → 14:00-16:00 景山公园 → 20:00-21:00 北京饭店\n第2天 09:00-11:00 天坛公园 → 20:00-21:00 桔子水晶酒店（王府井店）\n第3天 09:00-11:00 颐和园",
            "ids": ["fixture-bj-jingshan-01", "fixture-bj-jingshan-01", "B000A7BD71", "B000A7BD6Z", "B000A7BD72", "B000A7BD6V"],
            "audit": True,
            "entity_na": True,
        },
        {
            "slug": "placeholder-not-found-5d",
            "days": 5,
            "group": 3,
            "tags": ["controlled-http-30", "not-found", "unknown-not-pass"],
            "text": "第1天 09:00-11:00 故宫博物院\n第2天 待定\n第3天 09:00-11:00 天坛公园\n第4天 09:00-11:00 颐和园\n第5天 09:00-11:00 景山公园",
            "ids": ["B000A7BD6T", None, "B000A7BD6Z", "B000A7BD6V", "fixture-bj-jingshan-01"],
            "apply": False,
        },
    ],
    "上海": [
        {
            "slug": "clean-meals-hotel-2d",
            "days": 2,
            "group": 2,
            "tags": ["controlled-http-30", "normal-route", "meals", "hotel"],
            "text": "第1天 09:00-11:00 外滩 → 12:00-13:00 南翔馒头店（豫园店） → 20:00-21:00 上海外滩华尔道夫酒店\n第2天 09:00-11:00 豫园 → 12:00-13:00 小杨生煎（吴江路店） → 20:00-21:00 全季酒店（南京东路店）",
            "ids": ["B00155H52F", "B00155H523", "B00155H521", "B00155H52G", "B00155H52M", "B00155H522"],
            "audit": True,
        },
        {
            "slug": "overlap-2d",
            "days": 2,
            "group": 3,
            "tags": ["controlled-http-30", "time-overlap", "route-gap"],
            "text": "第1天 09:00-12:00 外滩 → 11:30-13:00 南翔馒头店（豫园店） → 20:00-21:00 上海外滩华尔道夫酒店\n第2天 09:00-11:00 豫园 → 20:00-21:00 全季酒店（南京东路店）",
            "ids": ["B00155H52F", "B00155H523", "B00155H521", "B00155H52G", "B00155H522"],
            "audit": True,
            "repair": True,
        },
        {
            "slug": "outside-opening-2d",
            "days": 2,
            "group": 4,
            "tags": ["controlled-http-30", "opening-hours", "late-visit"],
            "text": "第1天 18:00-20:00 豫园 → 20:30-21:30 上海外滩华尔道夫酒店\n第2天 09:00-11:00 外滩 → 20:00-21:00 全季酒店（南京东路店）",
            "ids": ["B00155H52G", "B00155H521", "B00155H52F", "B00155H522"],
            "audit": True,
        },
        {
            "slug": "fixed-visit-3d",
            "days": 3,
            "group": 5,
            "tags": ["controlled-http-30", "fixed-appointment", "locked"],
            "text": "第1天 09:00-11:00 外滩（已预约不可改） → 20:00-21:00 上海外滩华尔道夫酒店\n第2天 09:00-11:00 豫园 → 20:00-21:00 全季酒店（南京东路店）\n第3天 09:00-11:00 东方明珠广播电视塔",
            "ids": ["B00155H52F", "B00155H521", "B00155H52G", "B00155H522", "B00155H52H"],
            "audit": True,
        },
        {
            "slug": "duplicate-place-3d",
            "days": 3,
            "group": 2,
            "tags": ["controlled-http-30", "duplicate-place", "canonical-id"],
            "text": "第1天 09:00-11:00 外滩 → 14:00-16:00 外滩 → 20:00-21:00 上海外滩华尔道夫酒店\n第2天 09:00-11:00 豫园 → 20:00-21:00 全季酒店（南京东路店）\n第3天 09:00-11:00 东方明珠广播电视塔",
            "ids": ["B00155H52F", "B00155H52F", "B00155H521", "B00155H52G", "B00155H522", "B00155H52H"],
            "audit": True,
            "entity_na": True,
        },
        {
            "slug": "placeholder-not-found-5d",
            "days": 5,
            "group": 3,
            "tags": ["controlled-http-30", "not-found", "unknown-not-pass"],
            "text": "第1天 09:00-11:00 外滩\n第2天 待确认\n第3天 09:00-11:00 豫园\n第4天 09:00-11:00 田子坊\n第5天 09:00-11:00 东方明珠广播电视塔",
            "ids": ["B00155H52F", None, "B00155H52G", "B00155H52I", "B00155H52H"],
            "apply": False,
        },
    ],
    "杭州": [
        {
            "slug": "clean-meals-hotel-2d",
            "days": 2,
            "group": 2,
            "tags": ["controlled-http-30", "normal-route", "meals", "hotel"],
            "text": "第1天 09:00-11:00 西湖风景名胜区 → 12:00-13:00 楼外楼（孤山路店） → 20:00-21:00 杭州西湖国宾馆\n第2天 09:00-11:00 灵隐寺 → 12:00-13:00 知味观（仁和店） → 20:00-21:00 杭州西湖悦庭酒店",
            "ids": ["B0FFHZ0001", "B0FFHZ0007", "B0FFHZ0015", "B0FFHZ0002", "B0FFHZ0008", "B0FFHZ0016"],
            "audit": True,
        },
        {
            "slug": "overlap-2d",
            "days": 2,
            "group": 3,
            "tags": ["controlled-http-30", "time-overlap", "route-gap"],
            "text": "第1天 09:00-12:00 西湖风景名胜区 → 11:30-13:00 楼外楼（孤山路店） → 20:00-21:00 杭州西湖国宾馆\n第2天 09:00-11:00 灵隐寺 → 20:00-21:00 杭州西湖悦庭酒店",
            "ids": ["B0FFHZ0001", "B0FFHZ0007", "B0FFHZ0015", "B0FFHZ0002", "B0FFHZ0016"],
            "audit": True,
            "repair": True,
        },
        {
            "slug": "outside-opening-2d",
            "days": 2,
            "group": 4,
            "tags": ["controlled-http-30", "opening-hours", "late-visit"],
            "text": "第1天 19:00-21:00 灵隐寺 → 21:15-22:00 杭州西湖国宾馆\n第2天 09:00-11:00 西湖风景名胜区 → 20:00-21:00 杭州西湖悦庭酒店",
            "ids": ["B0FFHZ0002", "B0FFHZ0015", "B0FFHZ0001", "B0FFHZ0016"],
            "audit": True,
        },
        {
            "slug": "fixed-visit-3d",
            "days": 3,
            "group": 5,
            "tags": ["controlled-http-30", "fixed-appointment", "locked"],
            "text": "第1天 09:00-11:00 灵隐寺（已预约不可改） → 20:00-21:00 杭州西湖国宾馆\n第2天 09:00-11:00 西湖风景名胜区 → 20:00-21:00 杭州西湖悦庭酒店\n第3天 09:00-11:00 雷峰塔",
            "ids": ["B0FFHZ0002", "B0FFHZ0015", "B0FFHZ0001", "B0FFHZ0016", "B0FFHZ0003"],
            "audit": True,
        },
        {
            "slug": "duplicate-place-3d",
            "days": 3,
            "group": 2,
            "tags": ["controlled-http-30", "duplicate-place", "canonical-id"],
            "text": "第1天 09:00-11:00 西湖风景名胜区 → 14:00-16:00 西湖风景名胜区 → 20:00-21:00 杭州西湖国宾馆\n第2天 09:00-11:00 灵隐寺 → 20:00-21:00 杭州西湖悦庭酒店\n第3天 09:00-11:00 雷峰塔",
            "ids": ["B0FFHZ0001", "B0FFHZ0001", "B0FFHZ0015", "B0FFHZ0002", "B0FFHZ0016", "B0FFHZ0003"],
            "audit": True,
            "entity_na": True,
        },
        {
            "slug": "placeholder-not-found-5d",
            "days": 5,
            "group": 3,
            "tags": ["controlled-http-30", "not-found", "unknown-not-pass"],
            "text": "第1天 09:00-11:00 西湖风景名胜区\n第2天 未知地点\n第3天 09:00-11:00 灵隐寺\n第4天 09:00-11:00 雷峰塔\n第5天 09:00-11:00 西溪湿地国家公园",
            "ids": ["B0FFHZ0001", None, "B0FFHZ0002", "B0FFHZ0003", "B0FFHZ0004"],
            "apply": False,
        },
    ],
}


CITY_CODE = {"北京": "bj", "上海": "sh", "杭州": "hz"}
PLACE_NAMES = {
    "B000A7BD6T": "故宫博物院",
    "B000A7BD75": "四季民福（故宫旁）",
    "B000A7BD71": "北京饭店",
    "B000A7BD72": "桔子水晶酒店（王府井店）",
    "B000A7BD6Z": "天坛公园",
    "B000A7BD73": "老北京炸酱面（前门店）",
    "fixture-bj-jingshan-01": "景山公园",
    "B000A7BD6V": "颐和园",
    "B00155H52F": "外滩",
    "B00155H523": "南翔馒头店（豫园店）",
    "B00155H521": "上海外滩华尔道夫酒店",
    "B00155H522": "全季酒店（南京东路店）",
    "B00155H52G": "豫园",
    "B00155H52M": "小杨生煎（吴江路店）",
    "B00155H52H": "东方明珠广播电视塔",
    "B00155H52I": "田子坊",
    "B0FFHZ0001": "西湖风景名胜区",
    "B0FFHZ0007": "楼外楼（孤山路店）",
    "B0FFHZ0015": "杭州西湖国宾馆",
    "B0FFHZ0016": "杭州西湖悦庭酒店",
    "B0FFHZ0002": "灵隐寺",
    "B0FFHZ0008": "知味观（仁和店）",
    "B0FFHZ0003": "雷峰塔",
    "B0FFHZ0004": "西溪湿地国家公园",
}
PLACEHOLDER_BY_CITY = {"北京": "待定", "上海": "待确认", "杭州": "未知地点"}


def _na(reason: str) -> dict[str, str]:
    return {"applicability": "N_A", "reason_code": reason}


def _finding(
    reason_code: str,
    status: str,
    subject: str,
) -> dict[str, Any]:
    return {
        "reason_code": reason_code,
        "status": status,
        "severity": "HIGH",
        "subject": subject,
        "affected_member": None,
    }


def _controlled_finding_truth(definition: dict[str, Any], names: list[str]) -> list[dict[str, Any]]:
    """Return stable blocker/high truth implied by input and fixture seams."""
    slug = definition["slug"]
    route_pairs: list[tuple[int, int]] = []
    extras: list[dict[str, Any]] = []
    if slug == "clean-meals-hotel-2d":
        route_pairs = [(0, 1), (1, 2), (3, 4), (4, 5)]
    elif slug == "overlap-2d":
        route_pairs = [(1, 2), (3, 4)]
        extras.append(_finding("TIME_CHAIN_BROKEN", "VIOLATED", f"{names[0]}->{names[1]}"))
    elif slug == "outside-opening-2d":
        route_pairs = [(0, 1), (2, 3)]
        extras.append(
            _finding("OUTSIDE_OPENING_HOURS", "VIOLATED", str(definition["ids"][0]))
        )
    elif slug == "fixed-visit-3d":
        route_pairs = [(0, 1), (2, 3)]
    elif slug == "duplicate-place-3d":
        route_pairs = [(0, 1), (1, 2), (3, 4)]
        duplicate_id = str(definition["ids"][0])
        extras.append(_finding("DUPLICATE_PLACE", "VIOLATED", duplicate_id))
    return [
        _finding("ROUTE_GAP_EVIDENCE_UNKNOWN", "UNKNOWN", f"{names[left]}->{names[right]}")
        for left, right in route_pairs
    ] + extras


def build_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cases: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    for city, definitions in CITY_CASES.items():
        for definition in definitions:
            case_id = f"dev.{CITY_CODE[city]}.import.ctrl-{definition['slug']}"
            names = [
                PLACE_NAMES[place_id] if place_id is not None else PLACEHOLDER_BY_CITY[city]
                for place_id in definition["ids"]
            ]
            fixed = [
                "fixed-appointment" in definition["tags"] and index == 0
                for index, _ in enumerate(names)
            ]
            expected_entities = []
            for name, place_id in zip(names, definition["ids"], strict=True):
                expected_entities.append(
                    {
                        "raw_name": name,
                        "status": "NOT_FOUND" if place_id is None else "AUTO_MATCHED",
                        "canonical_place_id": place_id,
                    }
                )
            input_payload = {
                "raw_itinerary": definition["text"],
                "controlled_facts": {
                    "fixture_contract": {
                        "authority": "DATASET_INPUT_BYTES_ONLY",
                        "current_fact_authority": False,
                        "canonical_entities": expected_entities,
                        "route_fact_status": "UNAVAILABLE",
                    }
                },
            }
            steps = ["create_workspace", "import_text", "resolve_candidates"]
            if definition.get("apply", True):
                steps.append("apply_import")
            if definition.get("audit"):
                steps.extend(["collect_evidence", "full_audit"])
            if definition.get("repair"):
                steps.extend(["generate_repair", "preview_repair", "postcheck", "apply_repair"])
            steps.append("readback")
            case = {
                    "schema_version": "dual-entry-case-v1",
                    "case_id": case_id,
                    "split": "dev",
                    "entry": "IMPORT",
                    "city": city,
                    "trip_days": definition["days"],
                    "group_size": definition["group"],
                    "source_family_id": "source-lineage-unavailable",
                    "template_family_id": "template-lineage-unavailable",
                    "generator_family_id": "generator-lineage-unavailable",
                    "mutation_parent_case_id": None,
                    "lineage_status": {
                        "source_family": "UNAVAILABLE",
                        "template_family": "UNAVAILABLE",
                        "generator_family": "UNAVAILABLE",
                        "mutation_family": "NOT_APPLICABLE",
                    },
                    "normalized_input_sha256": normalized_input_sha256(input_payload),
                    "receipt_refs": {"subject_evidence_refs": [], "source_receipts": []},
                    "data_origin": "high_fidelity_synthetic",
                    "tags": definition["tags"],
                    "input": input_payload,
                    "execution": {
                        "provider_mode": "controlled_fixture",
                        "fault_profile": None,
                        "steps": steps,
                    },
                }
            case["receipt_refs"]["subject_evidence_refs"] = expected_subject_receipt_refs(case)
            cases.append(case)
            duplicate_input = bool(definition.get("entity_na"))
            entity_oracle = (
                _na("NO_STRUCTURED_ENTITY_TRUTH")
                if duplicate_input
                else {
                    "applicability": "APPLICABLE",
                    "metric_version": "exact-set-precision-recall-v1",
                    "unit_key_fields": ["raw_name", "status", "canonical_place_id"],
                    "ground_truth_items": expected_entities,
                }
            )
            deterministic_truth: dict[str, Any] = {
                "must_pass": ["SOURCE_SPAN_READBACK", "ENTITY_STATUS_EXPLICIT"],
                "must_fail": [],
                "must_be_unknown": ["PLACEHOLDER_IDENTITY"] if None in definition["ids"] else [],
                "must_not_happen": ["UNKNOWN_PROMOTED_TO_CONFIRMED"],
            }
            expected_findings = _controlled_finding_truth(definition, names)
            if expected_findings:
                deterministic_truth["expected_findings"] = expected_findings
            if definition.get("repair"):
                deterministic_truth["repair_oracle"] = {
                    "max_options": 2,
                    "postcheck_required": True,
                    "locked_items_preserved": True,
                    "no_new_hard_violation": True,
                    "allowed_operation_types": ["SHIFT", "MOVE"],
                }
            if not duplicate_input:
                deterministic_truth["expected_parse"] = {
                    "stop_names": names,
                    "fixed_commitment_names": [
                        name for name, is_fixed in zip(names, fixed, strict=True) if is_fixed
                    ],
                    "source_span_readback_rate": 1.0,
                }
                deterministic_truth["expected_resolutions"] = [
                    {
                        **item,
                        "requires_user_confirmation": item["status"] != "AUTO_MATCHED",
                    }
                    for item in expected_entities
                ]
            labels.append(
                {
                    "schema_version": "dual-entry-label-v1",
                    "case_id": case_id,
                    "deterministic_truth": deterministic_truth,
                    "metric_oracles": {
                        "parse_f1": _na("NO_STRUCTURED_PARSE_TRUTH") if duplicate_input else {
                            "applicability": "APPLICABLE",
                            "metric_version": "set-f1-v1",
                            "normalization": "unicode-nfc-trim-collapse-space",
                            "ground_truth_items": [{"stop_name": name} for name in names],
                        },
                        "entity_precision_recall": entity_oracle,
                        "finding_precision_recall": (
                            {
                                "applicability": "APPLICABLE",
                                "metric_version": "exact-set-blocker-high-v1",
                                "unit_key_fields": [
                                    "reason_code",
                                    "status",
                                    "subject",
                                    "affected_member",
                                ],
                                "ground_truth_items": [
                                    {
                                        "reason_code": item["reason_code"],
                                        "status": item["status"],
                                        "subject": item["subject"],
                                        "affected_member": item["affected_member"],
                                    }
                                    for item in expected_findings
                                ],
                                "scope_severities": ["BLOCKER", "HIGH"],
                            }
                            if expected_findings
                            else _na("NO_STRUCTURED_FINDING_TRUTH")
                        ),
                        "repair_postcheck": (
                            {
                                "applicability": "APPLICABLE",
                                "metric_version": "predicate-pass-rate-v1",
                                "max_options": 2,
                                "allowed_operation_types": ["SHIFT", "MOVE"],
                                "required_predicates": [
                                    {"predicate": "postcheck_executed", "expected": True},
                                    {"predicate": "locked_items_preserved", "expected": True},
                                    {"predicate": "no_new_hard_violation", "expected": True},
                                ],
                            }
                            if definition.get("repair")
                            else _na("NO_STRUCTURED_REPAIR_TRUTH")
                        ),
                        "builder_ndcg_at_5": _na("NO_GRADED_RANKING_TRUTH"),
                        "builder_recall_at_5": _na("NO_RELEVANT_CANDIDATE_TRUTH"),
                    },
                    "gate_assertions": [
                        f"stop_count_exact_{len(names)}",
                        "unknown_never_satisfied",
                    ],
                }
            )
    return cases, labels


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _scope_finding_oracles(labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Freeze the release-facing finding metric to explicit BLOCKER/HIGH scope.

    MEDIUM findings remain in deterministic truth for diagnostics, but they do
    not silently enter the blocker/high precision and recall denominator.
    HIGH UNKNOWN findings remain ordinary expected findings in this scope.
    """

    for label in labels:
        oracle = label.get("metric_oracles", {}).get("finding_precision_recall", {})
        if oracle.get("applicability") != "APPLICABLE":
            continue
        expected = label.get("deterministic_truth", {}).get("expected_findings", [])
        oracle["metric_version"] = "exact-set-blocker-high-v1"
        oracle["scope_severities"] = ["BLOCKER", "HIGH"]
        oracle["ground_truth_items"] = [
            {
                "reason_code": item["reason_code"],
                "status": item["status"],
                "subject": item.get("subject"),
                "affected_member": item.get("affected_member"),
            }
            for item in expected
            if item.get("severity") in {"BLOCKER", "HIGH"}
        ]
    return labels


def _align_existing_controlled_fixture_truth() -> None:
    """Repair an old pilot assertion that contradicted the checked-in fixture.

    The fixture has exactly one identity-compatible ``故宫博物院`` row.  The
    former label asserted ambiguity without a second candidate receipt, which
    made an accurate AUTO_MATCHED response look like a product failure.
    """
    inputs_path = ROOT / "pilot.inputs.jsonl"
    inputs = _load(inputs_path)
    for case in inputs:
        if case["case_id"] != "pilot.bj.import.classic-3d":
            continue
        case["input"]["controlled_facts"]["entity_resolution"] = (
            "故宫博物院在受控fixture唯一精确命中"
        )
        case["normalized_input_sha256"] = normalized_input_sha256(case["input"])
        case["receipt_refs"]["subject_evidence_refs"] = expected_subject_receipt_refs(case)
    _write(inputs_path, inputs)

    dev_inputs_path = ROOT / "dev.inputs.jsonl"
    dev_inputs = _load(dev_inputs_path)
    for case in dev_inputs:
        if case["case_id"] != "dev.bj.import.ambiguous-shorthand":
            continue
        case["input"]["controlled_facts"] = {
            "fixture_resolution_statuses": {
                "故宫": "AMBIGUOUS",
                "奥森": "NOT_FOUND",
                "国博": "NOT_FOUND",
            }
        }
        case["normalized_input_sha256"] = normalized_input_sha256(case["input"])
        case["receipt_refs"]["subject_evidence_refs"] = expected_subject_receipt_refs(case)
    _write(dev_inputs_path, dev_inputs)

    labels_path = ROOT / "pilot.labels.jsonl"
    labels = _load(labels_path)
    for label in labels:
        if label["case_id"] != "pilot.bj.import.classic-3d":
            continue
        truth = label["deterministic_truth"]
        truth["must_not_happen"] = [
            "SILENT_ENTITY_MISMATCH"
            if item == "AUTO_ACCEPT_AMBIGUOUS_GUGONG"
            else item
            for item in truth["must_not_happen"]
        ]
        truth["expected_resolutions"] = [
            {
                "raw_name": "故宫博物院",
                "status": "AUTO_MATCHED",
                "canonical_place_id": "B000A7BD6T",
                "requires_user_confirmation": False,
            }
        ]
        label["metric_oracles"]["entity_precision_recall"]["ground_truth_items"] = [
            {
                "raw_name": "故宫博物院",
                "status": "AUTO_MATCHED",
                "canonical_place_id": "B000A7BD6T",
            }
        ]
    _write(labels_path, labels)

    dev_labels_path = ROOT / "dev.labels.jsonl"
    dev_labels = _load(dev_labels_path)
    fixture_shorthand_truth = [
        {
            "raw_name": "故宫",
            "status": "AMBIGUOUS",
            "canonical_place_id": None,
            "requires_user_confirmation": True,
        },
        {
            "raw_name": "奥森",
            "status": "NOT_FOUND",
            "canonical_place_id": None,
            "requires_user_confirmation": True,
        },
        {
            "raw_name": "国博",
            "status": "NOT_FOUND",
            "canonical_place_id": None,
            "requires_user_confirmation": True,
        },
    ]
    for label in dev_labels:
        if label["case_id"] != "dev.bj.import.ambiguous-shorthand":
            continue
        label["deterministic_truth"]["expected_resolutions"] = fixture_shorthand_truth
        label["metric_oracles"]["entity_precision_recall"]["ground_truth_items"] = [
            {
                "raw_name": item["raw_name"],
                "status": item["status"],
                "canonical_place_id": item["canonical_place_id"],
            }
            for item in fixture_shorthand_truth
        ]
    _write(dev_labels_path, dev_labels)


def main() -> None:
    new_cases, new_labels = build_rows()
    input_path = ROOT / "dev.inputs.jsonl"
    label_path = ROOT / "dev.labels.jsonl"
    existing_cases = [row for row in _load(input_path) if "controlled-http-30" not in row.get("tags", [])]
    new_ids = {row["case_id"] for row in new_cases}
    existing_labels = [row for row in _load(label_path) if row["case_id"] not in new_ids]
    _write(input_path, [*existing_cases, *new_cases])
    _write(label_path, _scope_finding_oracles([*existing_labels, *new_labels]))

    _align_existing_controlled_fixture_truth()

    # The explicit blocker/high scope is a dataset-wide metric contract, not a
    # special rule for only the newly added development rows.
    for entry in ("pilot", "regression"):
        path = ROOT / f"{entry}.labels.jsonl"
        _write(path, _scope_finding_oracles(_load(path)))

    manifest_path = ROOT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][1]["case_count"] = len([*existing_cases, *new_cases])
    all_cases = [
        row
        for entry in manifest["files"]
        for row in _load(ROOT / entry["inputs"])
    ]
    manifest["current_counts"] = {
        "total": len(all_cases),
        "by_city": {
            city: sum(row["city"] == city for row in all_cases)
            for city in ("北京", "上海", "杭州")
        },
        "by_entry": {
            entry: sum(row["entry"] == entry for row in all_cases)
            for entry in ("IMPORT", "BUILDER")
        },
        "by_split": {
            split: sum(row["split"] == split for row in all_cases)
            for split in ("pilot", "dev", "regression", "frozen_blind")
        },
    }
    manifest["controlled_fixture_import_http_30"] = {
        "case_count": 30,
        "city_counts": {"北京": 11, "上海": 9, "杭州": 10},
        "claim_scope": "controlled_fixture_development_30",
        "g2_frozen_snapshot_claim": False,
    }
    subject_receipts = [
        record
        for case in all_cases
        for record in expected_subject_receipt_records(case)
    ]
    registry_path = ROOT / manifest["subject_receipt_registry"]
    _write(registry_path, subject_receipts)
    manifest["subject_receipt_registry_sha256"] = hashlib.sha256(
        registry_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
