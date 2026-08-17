"""Evaluate the fixed Beijing/Shanghai/Hangzhou set against replay or live SSE.

This runner owns deterministic checks and product-chain execution only. Paid
API judging is fail-closed; semantic evaluation is imported from the independent
GPT-5.6-sol subagent panel implemented in ``scripts.agent_judge_panel``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import re
import subprocess
import os
import time
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests
from dotenv import load_dotenv

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# This runner is normally invoked from ``backend`` while the project's real
# secrets live in the repository-root .env used by Docker Compose.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
from app.config import settings  # noqa: E402 - load repository .env before Settings import


DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "eval_data" / "daily_queries" / "cases.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "results" / "daily_query_eval_latest.json"
RUNNER_VERSION = "5.0"
TRANSIT_OR_NON_DESTINATION = ("地铁站", "公交站", "停车场", "出入口", "售票处")
TARGET_CITIES = {"北京", "上海", "杭州"}
INTENT_CATEGORIES = {
    "attraction": ["attraction"],
    "food": ["food"],
    "hotel": ["hotel"],
    "mixed": ["attraction", "food"],
    "all": ["attraction", "food", "hotel"],
}
INFRASTRUCTURE_FAILURE_CATEGORIES = {"timeout", "provider_429", "provider_5xx", "circuit_open"}
APPLICATION_FAILURE_CATEGORIES = {
    "internal", "invalid_payload", "unauthorized", "configuration_error", "cancelled",
    "tool_exception", "tool_timeout", "deadline_exceeded",
}
BUSINESS_RETRIEVAL_FAILURE_CATEGORIES = {"anchor_not_found", "empty_result"}
_PENDING_AFFIRMATIVE_PATTERNS = {
    "family_room": ("提供家庭房", "有家庭房", "配备家庭房", "配备亲子房"),
    "accessible_room": ("提供无障碍客房", "无台阶入口", "轮椅可达", "无障碍设施齐全"),
    "shuttle": ("提供接驳", "有接驳车", "有班车", "免费接送"),
    "pet_policy": ("允许宠物入住", "宠物友好", "可带宠物"),
    "parking": ("提供停车位", "免费停车", "停车方便"),
    "laundry": ("提供洗衣机", "可洗衣", "洗衣设施"),
    "kitchen": ("可以做饭", "配备厨房", "可使用厨房"),
    "quiet_room": ("房间安静", "隔音很好", "隔音良好", "保证安静"),
    "allergen_handling": ("无过敏原", "避免交叉污染", "过敏安全", "绝对安全"),
    "dietary_policy": ("清真认证", "严格素食", "纯素餐厅", "完全符合饮食要求"),
    "dairy_free": ("不含乳制品", "无乳糖", "提供植物奶"),
    "private_room_quiet": ("提供包间", "有包间", "环境安静", "不吵"),
    "attraction_accessibility": ("无台阶入口", "轮椅可达", "无障碍设施齐全"),
}


def _expected_for(case: dict[str, Any]) -> dict[str, Any]:
    """Expand compact dataset defaults while keeping hard expectations explicit."""
    categories = INTENT_CATEGORIES.get(str(case.get("intent") or ""), [])
    defaults: dict[str, Any] = {
        "allowed_categories": categories,
        "required_category_coverage": categories,
        "min_places": 4 if case.get("intent") == "mixed" else 2,
        "require_city": bool(case.get("id")),
    }
    query = str(case.get("query") or "")
    if case.get("intent") == "all":
        defaults.update({"min_places": 9, "max_places": 15, "min_per_category": 2, "max_per_category": 5})
        if any(term in query for term in ("老人", "少走路", "少折腾", "低强度")):
            defaults.update({"min_places": 6, "max_places": 9, "min_per_category": 2, "max_per_category": 3})
    defaults.update(case.get("expected") or {})
    is_night_walk = (
        any(term in query for term in ("夜景散步", "晚上散步", "夜间散步", "散步看夜景"))
        or ("晚上" in query and "散步" in query)
    )
    if case.get("intent") == "attraction" and is_night_walk:
        # A single POI with non-conflicting hours is preferable to padding a
        # safety-sensitive night request with a venue known to be closed.
        defaults["min_places"] = 1
    return defaults


def validate_dataset(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    cases = dataset.get("cases") or []
    errors: list[str] = []
    ids = [str(case.get("id") or "") for case in cases]
    if len(ids) != len(set(ids)):
        errors.append("case id 存在重复")
    city_counts = Counter(str(case.get("city") or "") for case in cases)
    if set(city_counts) != TARGET_CITIES or any(city_counts[city] != 50 for city in TARGET_CITIES):
        errors.append(f"数据集必须严格覆盖北京/上海/杭州各50条，当前为 {dict(city_counts)}")
    for case in cases:
        missing = [key for key in ("id", "city", "intent", "persona", "query") if not case.get(key)]
        if missing:
            errors.append(f"{case.get('id', '<unknown>')} 缺少字段：{','.join(missing)}")
        if case.get("intent") not in INTENT_CATEGORIES:
            errors.append(f"{case.get('id')} intent 不受支持：{case.get('intent')}")
        if not (case.get("expected") or {}).get("semantic_requirement"):
            errors.append(f"{case.get('id')} 缺少 semantic_requirement")
    if errors:
        raise ValueError("；".join(errors))
    return cases


def _read_sse(response: requests.Response) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw in response.iter_lines(decode_unicode=True):
        if raw and raw.startswith("data:"):
            events.append(json.loads(raw[5:].strip()))
    return events


def _materialize_response(events: list[dict[str, Any]]) -> dict[str, Any]:
    places: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    text = ""
    thinking: list[str] = []
    errors: list[dict[str, Any]] = []
    done: dict[str, Any] = {}
    for event in events:
        kind = event.get("event")
        data = event.get("data") or {}
        if kind == "place":
            place = data.get("place") or {}
            place_id = str(place.get("place_id") or "")
            if place_id and place_id not in places:
                order.append(place_id)
            if place_id:
                places[place_id] = place
        elif kind == "place_update":
            place_id = str(data.get("place_id") or "")
            if place_id in places:
                places[place_id].update(data.get("fields") or {})
        elif kind == "place_remove":
            place_id = str(data.get("place_id") or "")
            places.pop(place_id, None)
            order = [item for item in order if item != place_id]
        elif kind == "text_reset":
            text = ""
        elif kind == "text":
            text += str(data.get("delta") or "")
        elif kind == "thinking":
            thinking.append(str(data.get("summary") or ""))
        elif kind == "error":
            errors.append(data)
        elif kind == "done":
            done = data
    raw_places = [places[item] for item in order if item in places]
    from app.constraints.place_identity import deduplicate_places

    canonical_places = deduplicate_places(raw_places)
    return {
        "places": canonical_places,
        "raw_place_count": len(raw_places),
        "canonical_duplicate_count": len(raw_places) - len(canonical_places),
        "text": text,
        "thinking": thinking,
        "errors": errors,
        "done": done,
    }


def retrieval_integrity_checks(result: dict[str, Any]) -> dict[str, Any]:
    """A formal live baseline must be provably free of fixture/fallback data."""
    places = result.get("places") or []
    done = result.get("done") or {}
    audits = done.get("retrieval_audits") or []
    failures: list[str] = []
    modes = Counter(str(place.get("execution_mode") or "missing") for place in places)
    audit_modes = Counter(str(audit.get("execution_mode") or "missing") for audit in audits)
    fixture_places = sum(count for mode, count in modes.items() if mode == "fixture")
    fallback_places = sum(count for mode, count in modes.items() if mode == "fallback")
    non_live_places = [
        str(place.get("name") or place.get("place_id") or "<unknown>")
        for place in places
        if place.get("execution_mode") != "live"
    ]
    if non_live_places:
        failures.append("存在非 live 地点：" + "、".join(non_live_places))
    missing_provenance = [
        str(place.get("name") or place.get("place_id") or "<unknown>")
        for place in places
        if not place.get("retrieval_provider") or not place.get("retrieval_response_hash")
    ]
    if missing_provenance:
        failures.append("地点缺少 provider/response_hash：" + "、".join(missing_provenance))
    if not audits:
        failures.append("缺少 retrieval_audits，无法证明本案例执行了 live 高德检索")
    impure_audits = [
        audit for audit in audits
        if audit.get("provider") != "amap"
        or audit.get("execution_mode") != "live"
        or not audit.get("retrieved_at")
        or (
            audit.get("status") in {"ok", "empty"}
            and not audit.get("response_hash")
        )
    ]
    if impure_audits:
        failures.append(f"存在 {len(impure_audits)} 条来源不完整的检索审计记录")
    amap_failures = [
        failure for failure in done.get("tool_failures") or []
        if failure.get("tool") == "search_places"
    ]
    tool_failure_categories = Counter(
        str(failure.get("reason") or "unknown") for failure in amap_failures
    )
    event_failure_categories = Counter(
        str(error.get("error_category") or "unknown")
        for error in (result.get("errors") or [])
    )
    failure_categories = tool_failure_categories + event_failure_categories
    infrastructure_categories = sorted(
        category for category in failure_categories
        if category in INFRASTRUCTURE_FAILURE_CATEGORIES
    )
    application_categories = sorted(
        category for category in failure_categories
        if category in APPLICATION_FAILURE_CATEGORIES
    )
    business_categories = sorted(
        category for category in failure_categories
        if category in BUSINESS_RETRIEVAL_FAILURE_CATEGORIES
    )
    unknown_failure_categories = sorted(
        category for category in failure_categories
        if category not in (
            INFRASTRUCTURE_FAILURE_CATEGORIES
            | APPLICATION_FAILURE_CATEGORIES
            | BUSINESS_RETRIEVAL_FAILURE_CATEGORIES
        )
    )
    data_purity_passed = not failures
    provider_available = not infrastructure_categories
    all_retrievals_succeeded = not amap_failures and not event_failure_categories and bool(audits) and all(
        audit.get("status") in {"ok", "empty"} for audit in audits
    )
    return {
        "passed": data_purity_passed,
        "data_purity_passed": data_purity_passed,
        "all_retrievals_succeeded": all_retrievals_succeeded,
        "provider_available": provider_available,
        "quality_eligible": data_purity_passed and all_retrievals_succeeded,
        "infrastructure_failure": bool(infrastructure_categories),
        "application_failure": bool(application_categories or unknown_failure_categories),
        "business_retrieval_failure": bool(business_categories),
        "failure_categories": dict(failure_categories),
        "event_failure_categories": dict(event_failure_categories),
        "infrastructure_failure_categories": infrastructure_categories,
        "application_failure_categories": application_categories,
        "business_failure_categories": business_categories,
        "unknown_failure_categories": unknown_failure_categories,
        "failures": failures,
        "place_execution_modes": dict(modes),
        "audit_execution_modes": dict(audit_modes),
        "retrieval_audit_count": len(audits),
        "fixture_places": fixture_places,
        "fallback_places": fallback_places,
        "canonical_duplicate_count": int(result.get("canonical_duplicate_count") or 0),
        "amap_failure_count": len(amap_failures),
    }


def _contains_alias(text: str, aliases: list[str]) -> bool:
    compact = "".join(text.lower().split())
    return any("".join(alias.lower().split()) in compact for alias in aliases)


def high_risk_honesty_checks(result: dict[str, Any]) -> dict[str, Any]:
    """Audit visible pending evidence without treating a confirmation notice as proof."""
    places = result.get("places") or []
    pending_places = [
        place for place in places
        if place.get("selection_evidence_status") in {"UNKNOWN", "REQUIRES_CONFIRMATION"}
    ]
    missing_actions = [
        str(place.get("name") or "")
        for place in pending_places
        if not (place.get("confirmation_actions") or [])
    ]
    unsupported_claims: list[str] = []
    response_text = str(result.get("text") or "")
    response_sentences = [
        sentence.strip()
        for sentence in re.split(r"[。！？\n]", response_text)
        if sentence.strip()
    ]
    negation_markers = (
        "未", "不", "无法", "不能", "尚无", "待确认", "需确认", "请确认", "确认：", "核实",
    )
    for place in pending_places:
        card_text = " ".join([
            str(place.get("description") or ""),
            *[str(tag) for tag in (place.get("tags") or [])],
        ])
        for evidence in place.get("constraint_evidence") or []:
            if evidence.get("status") == "VERIFIED":
                continue
            key = str(evidence.get("constraint") or "")
            for pattern in _PENDING_AFFIRMATIVE_PATTERNS.get(key, ()):
                if pattern in card_text:
                    unsupported_claims.append(f"{place.get('name')}卡片声称{pattern}")
                for sentence in response_sentences:
                    if pattern in sentence and not any(marker in sentence for marker in negation_markers):
                        unsupported_claims.append(f"回复声称{pattern}")
    unsupported_claims = list(dict.fromkeys(unsupported_claims))
    pending_count = len(pending_places)
    action_covered = pending_count - len(missing_actions)
    return {
        "pending_place_count": pending_count,
        "pending_with_action_count": action_covered,
        "confirmation_action_coverage": action_covered / pending_count if pending_count else 1.0,
        "missing_confirmation_action_places": missing_actions,
        "unsupported_affirmative_claim_count": len(unsupported_claims),
        "unsupported_affirmative_claims": unsupported_claims,
        "passed": not missing_actions and not unsupported_claims,
    }


def deterministic_checks(
    case: dict[str, Any],
    result: dict[str, Any],
    *,
    require_live_integrity: bool = False,
) -> dict[str, Any]:
    expected = _expected_for(case)
    places = result["places"]
    allowed = set(expected.get("allowed_categories") or [])
    categories = [str(place.get("category") or "") for place in places]
    names = [str(place.get("name") or "") for place in places]
    attraction_names = [
        evidence for place in places
        if place.get("category") == "attraction"
        for evidence in [
            str(place.get("name") or ""),
            *(str(item) for item in (place.get("canonical_entity_names") or [])),
            str(place.get("address") or ""),
        ]
    ]
    failures: list[str] = []
    coverage_warnings: list[str] = []
    integrity = retrieval_integrity_checks(result)
    honesty = high_risk_honesty_checks(result)
    if require_live_integrity:
        failures.extend(integrity["failures"])

    if result["errors"]:
        failures.append("SSE 返回 error 事件")
    required_categories = list(expected.get("required_category_coverage") or [])
    missing_required_categories = [
        category for category in required_categories if category not in categories
    ]
    safe_coverage_degradation = bool(
        missing_required_categories
        and "安全降级回执：" in result.get("text", "")
        and "未用错误品类或明显远距离地点凑数" in result.get("text", "")
    )
    if len(places) < int(expected.get("min_places", 1)):
        message = f"地点数 {len(places)} 小于最低要求 {expected.get('min_places', 1)}"
        (coverage_warnings if safe_coverage_degradation else failures).append(message)
    max_places = expected.get("max_places")
    if max_places is not None and len(places) > int(max_places):
        failures.append(f"地点数 {len(places)} 超过上限 {max_places}")
    wrong_categories = [
        f"{place.get('name')}({place.get('category')})"
        for place in places
        if allowed and place.get("category") not in allowed
    ]
    if wrong_categories:
        failures.append("存在意图外品类：" + "、".join(wrong_categories))
    if expected.get("require_city"):
        expected_city = str(case["city"]).removesuffix("市")
        wrong_city = [
            str(place.get("name") or "")
            for place in places
            if str(place.get("city") or "").removesuffix("市") != expected_city
        ]
        if wrong_city:
            failures.append(f"存在非{case['city']}地点：" + "、".join(wrong_city))
    for category in missing_required_categories:
        message = f"缺少必需品类：{category}"
        (coverage_warnings if safe_coverage_degradation else failures).append(message)

    min_per_category = expected.get("min_per_category")
    max_per_category = expected.get("max_per_category")
    for category in required_categories:
        count = categories.count(category)
        if min_per_category is not None and count < int(min_per_category):
            message = f"品类 {category} 只有 {count} 个，少于 {min_per_category}"
            if safe_coverage_degradation and category in missing_required_categories:
                coverage_warnings.append(message)
            else:
                failures.append(message)
        if max_per_category is not None and count > int(max_per_category):
            failures.append(f"品类 {category} 有 {count} 个，超过 {max_per_category}")

    if expected.get("unique_names") and len(names) != len(set(names)):
        failures.append("存在重复地点名称")
    if expected.get("require_descriptions"):
        missing_descriptions = [
            str(place.get("name") or "") for place in places
            if not str(place.get("description") or "").strip()
        ]
        if missing_descriptions:
            failures.append("缺少一句话特色描述：" + "、".join(missing_descriptions))
    missing_confirmation_actions = [
        str(place.get("name") or "")
        for place in places
        if place.get("selection_evidence_status") in {"UNKNOWN", "REQUIRES_CONFIRMATION"}
        and not (place.get("confirmation_actions") or [])
    ]
    if missing_confirmation_actions:
        failures.append(
            "未验证约束缺少确认动作：" + "、".join(missing_confirmation_actions)
        )
    if honesty["unsupported_affirmative_claims"]:
        failures.append(
            "未验证高风险属性被肯定声称："
            + "、".join(honesty["unsupported_affirmative_claims"])
        )
    for category in expected.get("require_district_categories") or []:
        missing_district = [
            str(place.get("name") or "") for place in places
            if place.get("category") == category and not str(place.get("district") or "").strip()
        ]
        if missing_district:
            failures.append(f"品类 {category} 缺少所在行政区：" + "、".join(missing_district))
    for category in expected.get("require_price_categories") or []:
        missing_price = [
            str(place.get("name") or "") for place in places
            if place.get("category") == category
            and not isinstance(place.get("amap_price"), (int, float))
        ]
        if missing_price:
            failures.append(f"品类 {category} 缺少大致价位：" + "、".join(missing_price))

    for keyword in expected.get("forbidden_text_keywords") or []:
        if keyword in result.get("text", ""):
            failures.append(f"回复文本包含禁止内容：{keyword}")
    thinking_text = "\n".join(result.get("thinking") or [])
    for keyword in expected.get("forbidden_thinking_keywords") or []:
        if keyword in thinking_text:
            failures.append(f"思考链包含错误降级提示：{keyword}")

    district = expected.get("district")
    if district:
        outside = [
            place.get("name")
            for place in places
            if district not in f"{place.get('district', '')} {place.get('address', '')}"
        ]
        if outside:
            failures.append(f"不在{district}：" + "、".join(str(item) for item in outside))

    bad_names = [
        str(place.get("name") or "") for place in places
        if (
            place.get("category") in {"transport", "attraction"}
            and any(term in str(place.get("name") or "") for term in TRANSIT_OR_NON_DESTINATION)
        )
        or str(place.get("name") or "") in {case["city"], district, f"{case['city']}市"}
    ]
    forbidden = expected.get("forbidden_name_keywords") or []
    bad_names.extend(name for name in names if any(term in name for term in forbidden))
    bad_names = list(dict.fromkeys(bad_names))
    if bad_names:
        failures.append("包含交通设施、行政地名或明确排除项：" + "、".join(bad_names))

    required_groups = expected.get("required_place_groups") or []
    for aliases in required_groups:
        if not any(_contains_alias(name, aliases) for name in attraction_names):
            failures.append("缺少指定地点：" + "/".join(aliases))

    ordered_groups = expected.get("ordered_place_groups") or []
    if ordered_groups:
        indices: list[int] = []
        for aliases in ordered_groups:
            index = next(
                (
                    i for i, place in enumerate(places)
                    if place.get("category") == "attraction"
                    and any(_contains_alias(evidence, aliases) for evidence in [
                        str(place.get("name") or ""),
                        *(str(item) for item in (place.get("canonical_entity_names") or [])),
                        str(place.get("address") or ""),
                    ])
                ),
                -1,
            )
            indices.append(index)
        if -1 not in indices and indices != sorted(indices):
            failures.append(f"指定地点顺序错误：索引 {indices}")

    return {
        "passed": not failures,
        "failures": failures,
        "coverage_warnings": coverage_warnings,
        "missing_required_categories": missing_required_categories,
        "safe_coverage_degradation": safe_coverage_degradation,
        "wrong_category_items": wrong_categories,
        "place_count": len(places),
        "categories": categories,
        "names": names,
        "retrieval_integrity": integrity,
        "high_risk_honesty": honesty,
    }


def _retrieval_blocker(integrity: dict[str, Any]) -> tuple[str, str] | None:
    """Return an evaluation status/reason when semantic judging is invalid."""
    if integrity.get("infrastructure_failure"):
        categories = integrity.get("infrastructure_failure_categories") or []
        return "infrastructure_error", ",".join(categories) or "provider_unavailable"
    if integrity.get("application_failure"):
        categories = integrity.get("application_failure_categories") or []
        return "system_error", ",".join(categories) or "application_failure"
    if not integrity.get("quality_eligible"):
        categories = integrity.get("business_failure_categories") or []
        return "retrieval_failed", ",".join(categories) or "invalid_retrieval"
    return None


def _skipped_judge(status: str, reason: str) -> dict[str, Any]:
    return {
        "passed": False,
        "skipped": True,
        "skip_reason": reason,
        "evaluation_status": status,
    }


JUDGE_PROMPT = """你是严格的旅行地点推荐质量评审。只根据给出的用户请求、游客画像、验收语义和系统输出评分，不补充系统没有提供的事实。

用户请求：{query}
游客画像：{persona}
多样化维度：{dimensions}
验收语义：{semantic_requirement}
候选地点：{places}
回复文本：{response_text}

产品呈现说明：候选地点数组中的每一项都会作为独立、完整、可见的地点卡片展示给用户；回复文本只是摘要，
不要求重复列出每张卡片的名称。评分时必须把全部候选卡片视为系统已交付的推荐，不能因为摘要只突出一个地点，
就声称系统只推荐了一个。只有明确指定/隐喻的实体才影响 entity_coverage；开放式请求不得因为没有出现你偏好的某个具体品牌而扣实体覆盖分。
当用户明确说“候选”、“先给”或回复明确说同类卡片为备选时，同一品类的卡片是互斥备选，不是要全部串起来的行程。
此时对景餐住复合请求，应评估回复指出的一个“每类选一”组合是否有证据支持，不得因其他同类备选没有彼此路线而扣分。但用户明确要求“两个景点”或顺序时，仍必须同时验证这些实体及顺序。

分别给出 0-5 整数分：
- intent_relevance：地点是否直接满足用户想吃、想玩、住宿或指定地标的意图
- geographic_fit：是否处于用户表达的行政区、附近或语义区域
- entity_coverage：明确指定或隐喻的地点是否被正确解析和覆盖
- constraint_adherence：预算、饮食、同行人、时间、体力、交通和排除项等约束是否真正改变推荐，而不只是口头复述
- persona_fit：推荐组合与游客画像是否匹配，例如老人、儿童、学生、轮椅使用者、商务旅客或独行游客
- practical_usefulness：结果是否是可实际到访的地点，信息是否足够帮助选择
- groundedness：回复是否只使用候选数据可支持的事实，没有臆造

公交站、地铁站、行政区名称、泛化商圈不能作为餐馆或景点凑数。明确指定地点缺失、顺序相反、品类错误、地理范围明显错误，或高风险约束（过敏、无障碍、婴幼儿）在没有证据时被肯定承诺，都属于 critical_violations。若数据不足，诚实说明待确认比臆造通过更好。

证据不足时的统一评分口径：价格、具体房型、接驳、宠物、隔音、过敏原、无障碍设施等动态或高风险属性，
如果候选先满足可验证的品类/实体/行政区或语义半径约束，系统又逐卡标记 UNKNOWN/REQUIRES_CONFIRMATION、没有作肯定承诺，
并给出电话、官方页面或地图路线等具体确认动作，则 constraint_adherence 应给 4（诚实且可执行，但因尚未证实不能给 5），
persona_fit 和 practical_usefulness 也不得仅因动态属性尚未证实而低于 4。若只写泛泛的“建议确认”、没有具体动作，仍应低于 4。
这个口径不适用于可由当前卡片和检索直接验证的静态硬约束：指定实体、顺序、品类、行政区、明确语义区域和排除项不满足时仍必须低分。

5 分锚点（用于避免把所有正常结果机械评为 4）：
- constraint_adherence：请求中所有相关约束都由当前卡片证据验证；只要仍有相关动态属性待确认，仍按上述口径给 4。
- geographic_fit：行政区/语义区域直接命中，或附近关系有可回读距离证据且满足语义半径。
- persona_fit：候选集确实因同行人/预算/体力/商务等画像发生了可见改变，且所有相关静态条件已验证。
- practical_usefulness：卡片为可到访实体，已给出足以区分选择的具体信息（如地址、营业时间、价格、电话、距离或评分中与请求相关的项），并给出明确的首选/备选或可逆的下一步。
- groundedness：所有事实性描述都能逐项对应卡片证据，未将 UNKNOWN/REQUIRES_CONFIRMATION 扩大为事实。
4 分表示整体正确可用但仍缺一项非致命的决策信息；3 分及以下表示核心关系、必需品类/实体或可执行动作缺失。
只返回 JSON，不要 Markdown：
{{"scores":{{"intent_relevance":0,"geographic_fit":0,"entity_coverage":0,"constraint_adherence":0,"persona_fit":0,"practical_usefulness":0,"groundedness":0}},"critical_violations":[],"root_cause_hint":"失败时指出最可能的环节，成功时写none","summary":"一句话结论"}}
"""


def _generation_chain_metadata() -> dict[str, Any]:
    """Describe only the product generation chain used by the live service."""
    provider = "deepseek" if settings.deepseek_api_key else "openai_compatible"
    base_url = settings.effective_llm_api_url.rstrip("/")
    return {
        "provider": provider,
        "base_url": base_url,
        "router_model": settings.llm_model_router,
        "synthesizer_model": settings.llm_model_synthesizer,
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_metadata() -> dict[str, Any]:
    repo = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True,
        ).stdout.strip())
        return {"commit": commit, "dirty": dirty}
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"commit": None, "dirty": None, "error": type(exc).__name__}


def _execution_tree_metadata() -> dict[str, Any]:
    """Hash the exact backend source consumed by the live eval, including untracked files."""
    backend = Path(__file__).resolve().parents[1]
    files = sorted([
        *backend.joinpath("app").rglob("*.py"),
        Path(__file__).resolve(),
    ], key=lambda path: path.relative_to(backend).as_posix())
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(backend).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return {"sha256": digest.hexdigest(), "file_count": len(files)}


def _runner_metadata(dataset_path: Path) -> dict[str, Any]:
    return {
        "runner_version": RUNNER_VERSION,
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "dataset_path": str(dataset_path.resolve()),
        "dataset_sha256": _sha256_file(dataset_path),
        "git": _git_metadata(),
        "backend_execution_tree": _execution_tree_metadata(),
        "python": platform.python_version(),
        "environment": {
            "runtime_profile_required": "local_real|public",
            "demo_mode_required": False,
            "amap_mock_required": False,
        },
    }


def _judge_stage_metadata(
    report_path: Path,
    dataset_path: Path,
    source_report: dict[str, Any],
) -> dict[str, Any]:
    """Legacy metadata helper retained for reading historical API-Judge reports."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_report_path": str(report_path.resolve()),
        "source_report_sha256": _sha256_file(report_path),
        "source_report_generated_at": source_report.get("generated_at"),
        "source_report_reproducibility": source_report.get("reproducibility"),
        "judge_reproducibility": _runner_metadata(dataset_path),
        "judge_policy": {
            "kind": "disabled_api_judge",
            "network_calls_allowed": False,
            "replacement": "gpt-5.6-sol independent subagent panel",
        },
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    """Durable checkpoint write that never exposes a partially-written JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # Windows antivirus/indexers can hold the destination for a few
    # milliseconds. Retry only that transient sharing violation; the temp
    # file stays complete and the published path is never partially written.
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (2 ** attempt))


def verify_live_server(base: str) -> dict[str, Any]:
    response = requests.get(f"{base}/health", timeout=20)
    response.raise_for_status()
    health = response.json()
    failures = []
    if health.get("runtime_profile") not in {"local_real", "public"}:
        failures.append(f"runtime_profile={health.get('runtime_profile')!r}")
    if health.get("demo_mode") is not False:
        failures.append(f"demo_mode={health.get('demo_mode')!r}")
    if health.get("amap_mock") is not False:
        failures.append(f"amap_mock={health.get('amap_mock')!r}")
    if health.get("amap_configured") is not True:
        failures.append("amap_configured=false")
    if failures:
        raise RuntimeError("服务不满足 live eval 前置门禁：" + ", ".join(failures))
    return health


def _read_service_metrics(base: str) -> dict[str, Any]:
    response = requests.get(f"{base}/metrics", timeout=20)
    response.raise_for_status()
    return response.json()


def _numeric_mapping_delta(before: dict[str, Any], after: dict[str, Any], key: str) -> dict[str, int | float]:
    earlier = before.get(key) or {}
    later = after.get(key) or {}
    labels = set(earlier) | set(later)
    return {
        label: later.get(label, 0) - earlier.get(label, 0)
        for label in sorted(labels)
        if later.get(label, 0) - earlier.get(label, 0)
    }


def _judge_payload_places(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose every user-visible evidence field to the semantic judge."""

    return [
        {
            "name": place.get("name"),
            "category": place.get("category"),
            "district": place.get("district"),
            "address": place.get("address"),
            "amap_rating": place.get("amap_rating"),
            "amap_price": place.get("amap_price"),
            "opening_hours": place.get("opening_hours"),
            "description": place.get("description"),
            "tags": place.get("tags"),
            "constraint_evidence": place.get("constraint_evidence") or [],
            "geo_evidence": place.get("geo_evidence") or [],
            "selection_evidence_status": place.get("selection_evidence_status"),
            "confirmation_actions": place.get("confirmation_actions") or [],
            "phone": place.get("phone"),
        }
        for place in result["places"]
    ]


def llm_judge(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    del case, result
    raise RuntimeError(
        "API LLM-as-Judge 已禁用；请导出 blind bundle，并使用 GPT-5.6-sol 子 Agent 评审组"
    )


def _register(base: str) -> tuple[str, str]:
    response = requests.post(
        f"{base}/api/auth/email-register",
        json={
            "email": f"e2e+daily-{uuid4().hex[:12]}@breezetravel.local",
            "password": f"E2e-{uuid4().hex}!",
            "nickname": "DailyQueryEval",
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    return data["user_id"], data["token"]


def _run_case(base: str, user_id: str, token: str, case: dict[str, Any]) -> dict[str, Any]:
    room_id = f"daily-{case['id']}-{uuid4().hex[:8]}"
    thread_id = f"thread-{uuid4().hex[:12]}"
    headers = {"Authorization": f"Bearer {token}"}
    room = requests.post(
        f"{base}/api/room",
        headers=headers,
        json={
            "room_id": room_id,
            "thread_id": thread_id,
            "trip_city": case["city"],
            "trip_days": 1,
            "user_id": user_id,
            "nickname": "DailyQueryEval",
        },
        timeout=20,
    )
    room.raise_for_status()
    with requests.post(
        f"{base}/api/chat",
        headers=headers,
        json={
            "thread_id": thread_id,
            "room_id": room_id,
            "user_id": user_id,
            # Keep the real authenticated identity for room authorization, but
            # isolate every evaluation case from persistent user preferences.
            "use_long_term_memory": False,
            "message": case["query"],
            "trip_city": case["city"],
            "selected_place_ids": [],
        },
        stream=True,
        timeout=150,
    ) as response:
        response.raise_for_status()
        return _materialize_response(_read_sse(response))


def _summarize(rows: list[dict[str, Any]], cases_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    passed_count = sum(bool(row.get("passed")) for row in rows)
    judged_count = sum(bool((row.get("judge") or {}).get("scores")) for row in rows)
    judged_passed_count = sum(
        bool(row.get("passed"))
        for row in rows
        if (row.get("judge") or {}).get("scores")
    )
    judge_error_count = sum(bool((row.get("judge") or {}).get("error")) for row in rows)
    system_error_count = sum(row.get("evaluation_status") == "system_error" for row in rows)
    infrastructure_error_count = sum(
        row.get("evaluation_status") == "infrastructure_error" for row in rows
    )
    by_city: dict[str, dict[str, Any]] = {}
    by_intent: dict[str, dict[str, Any]] = {}
    by_intent_group: dict[str, dict[str, Any]] = {}
    score_totals: defaultdict[str, list[int]] = defaultdict(list)
    failure_reasons: Counter[str] = Counter()
    failure_types: Counter[str] = Counter()
    judge_providers: Counter[str] = Counter()
    judge_models: Counter[str] = Counter()
    fixture_places = 0
    fallback_places = 0
    canonical_duplicates = 0
    retrieval_audits = 0
    amap_failures = 0
    amap_tool_calls = 0
    pending_place_count = 0
    pending_with_action_count = 0
    unsupported_affirmative_claims: list[str] = []
    provider_available_count = 0
    quality_eligible_count = 0
    quality_passed_count = 0
    quality_by_city: dict[str, dict[str, Any]] = {}
    quality_by_intent: dict[str, dict[str, Any]] = {}
    quality_by_intent_group: dict[str, dict[str, Any]] = {}
    missing_category_case_ids: list[str] = []
    wrong_category_case_ids: list[str] = []
    safe_degradation_case_ids: list[str] = []

    def add_quality_bucket(bucket: dict[str, dict[str, Any]], key: str, passed: bool) -> None:
        stats = bucket.setdefault(key, {"eligible": 0, "passed": 0, "pass_rate": 0.0})
        stats["eligible"] += 1
        stats["passed"] += int(passed)

    for row in rows:
        case = cases_by_id.get(row.get("id"), {})
        for bucket, key in ((by_city, str(case.get("city") or "unknown")), (by_intent, str(case.get("intent") or "unknown"))):
            stats = bucket.setdefault(key, {"total": 0, "passed": 0, "pass_rate": 0.0})
            stats["total"] += 1
            stats["passed"] += int(bool(row.get("passed")))
        intent_group = str(case.get("intent") or "unknown")
        if intent_group in {"mixed", "all"}:
            intent_group = "compound"
        group_stats = by_intent_group.setdefault(intent_group, {"total": 0, "passed": 0, "pass_rate": 0.0})
        group_stats["total"] += 1
        group_stats["passed"] += int(bool(row.get("passed")))

        deterministic = row.get("deterministic") or {}
        if deterministic.get("missing_required_categories"):
            missing_category_case_ids.append(str(row.get("id") or "<unknown>"))
        if deterministic.get("wrong_category_items"):
            wrong_category_case_ids.append(str(row.get("id") or "<unknown>"))
        if deterministic.get("safe_coverage_degradation"):
            safe_degradation_case_ids.append(str(row.get("id") or "<unknown>"))
        integrity = deterministic.get("retrieval_integrity") or {}
        honesty = deterministic.get("high_risk_honesty") or {}
        pending_place_count += int(honesty.get("pending_place_count") or 0)
        pending_with_action_count += int(honesty.get("pending_with_action_count") or 0)
        unsupported_affirmative_claims.extend(
            str(item) for item in (honesty.get("unsupported_affirmative_claims") or [])
        )
        fixture_places += int(integrity.get("fixture_places") or 0)
        fallback_places += int(integrity.get("fallback_places") or 0)
        canonical_duplicates += int(integrity.get("canonical_duplicate_count") or 0)
        retrieval_audits += int(integrity.get("retrieval_audit_count") or 0)
        amap_failures += int(integrity.get("amap_failure_count") or 0)
        amap_tool_calls += sum(
            1 for receipt in (((row.get("output") or {}).get("done") or {}).get("tool_receipts") or [])
            if receipt and receipt.get("tool") == "search_places"
        )
        provider_available = (
            bool(integrity.get("provider_available"))
            if integrity
            else row.get("evaluation_status") != "infrastructure_error"
        )
        quality_eligible = (
            bool(integrity.get("quality_eligible"))
            if integrity
            else row.get("evaluation_status") in {"completed", "judge_error", "judge_skipped"}
        )
        provider_available_count += int(provider_available)
        if quality_eligible:
            quality_eligible_count += 1
            quality_passed_count += int(bool(row.get("passed")))
            city_key = str(case.get("city") or "unknown")
            intent_key = str(case.get("intent") or "unknown")
            intent_group_key = "compound" if intent_key in {"mixed", "all"} else intent_key
            add_quality_bucket(quality_by_city, city_key, bool(row.get("passed")))
            add_quality_bucket(quality_by_intent, intent_key, bool(row.get("passed")))
            add_quality_bucket(quality_by_intent_group, intent_group_key, bool(row.get("passed")))
        judge = row.get("judge") or {}
        deterministic_failed = bool(deterministic) and not bool(deterministic.get("passed"))
        semantic_scored = bool(judge.get("scores"))
        semantic_failed = semantic_scored and not bool(judge.get("passed"))
        if row.get("evaluation_status") == "infrastructure_error":
            failure_types["infrastructure_error"] += 1
        elif row.get("evaluation_status") == "system_error":
            failure_types["system_error"] += 1
        elif row.get("evaluation_status") == "retrieval_failed":
            failure_types["retrieval_failed"] += 1
        elif row.get("evaluation_status") == "judge_error" or judge.get("error"):
            failure_types["judge_error"] += 1
        elif deterministic_failed and semantic_failed:
            failure_types["deterministic_and_llm"] += 1
        elif deterministic_failed:
            failure_types["deterministic_only"] += 1
        elif semantic_failed:
            failure_types["llm_semantic_only"] += 1
        elif row.get("passed"):
            failure_types["passed"] += 1
        if deterministic_failed:
            failure_types["deterministic_failed_total"] += 1
        if semantic_failed:
            failure_types["llm_semantic_failed_total"] += 1

        for reason in (row.get("deterministic") or {}).get("failures") or []:
            failure_reasons[reason.split("：", 1)[0]] += 1
        if judge.get("judge_provider"):
            judge_providers[str(judge["judge_provider"])] += 1
        if judge.get("judge_model"):
            judge_models[str(judge["judge_model"])] += 1
        for name, score in (judge.get("scores") or {}).items():
            score_totals[name].append(int(score))
            if int(score) < 4:
                failure_reasons[f"LLM低分:{name}"] += 1
        for reason in judge.get("critical_violations") or []:
            failure_reasons[f"LLM:{reason}"] += 1
        if judge.get("error"):
            failure_reasons[f"JUDGE_ERROR:{str(judge['error']).split(':', 1)[0]}"] += 1
        if row.get("error"):
            failure_reasons[f"ERROR:{str(row['error']).split(':', 1)[0]}"] += 1
    for bucket in (by_city, by_intent, by_intent_group):
        for stats in bucket.values():
            stats["pass_rate"] = stats["passed"] / stats["total"] if stats["total"] else 0.0
    for bucket in (quality_by_city, quality_by_intent, quality_by_intent_group):
        for stats in bucket.values():
            stats["pass_rate"] = stats["passed"] / stats["eligible"] if stats["eligible"] else 0.0
    return {
        "total": len(rows),
        "passed": passed_count,
        "pass_rate": passed_count / len(rows) if rows else 0.0,
        "judged": judged_count,
        "judge_errors": judge_error_count,
        "system_errors": system_error_count,
        "infrastructure_errors": infrastructure_error_count,
        "judged_pass_rate": judged_passed_count / judged_count if judged_count else 0.0,
        "end_to_end_success": {
            "total": len(rows),
            "passed": passed_count,
            "pass_rate": passed_count / len(rows) if rows else 0.0,
        },
        "retrieval_availability": {
            "total": len(rows),
            "available": provider_available_count,
            "availability_rate": provider_available_count / len(rows) if rows else 0.0,
            "infrastructure_failures": infrastructure_error_count,
        },
        "recommendation_quality_under_valid_retrieval": {
            "eligible": quality_eligible_count,
            "passed": quality_passed_count,
            "pass_rate": quality_passed_count / quality_eligible_count if quality_eligible_count else 0.0,
            "excluded": len(rows) - quality_eligible_count,
            "by_city": quality_by_city,
            "by_intent": quality_by_intent,
            "by_intent_group": quality_by_intent_group,
        },
        "by_city": by_city,
        "by_intent": by_intent,
        "by_intent_group": by_intent_group,
        "failure_type_counts": dict(failure_types),
        "judge_provider_distribution": dict(judge_providers),
        "judge_model_distribution": dict(judge_models),
        "average_judge_scores": {
            name: round(sum(values) / len(values), 3) for name, values in score_totals.items() if values
        },
        "failure_reasons": dict(failure_reasons.most_common()),
        "category_coverage": {
            "missing_category_case_count": len(missing_category_case_ids),
            "missing_category_case_rate": (
                len(missing_category_case_ids) / len(rows) if rows else 0.0
            ),
            "missing_category_case_ids": missing_category_case_ids,
            "wrong_category_case_count": len(wrong_category_case_ids),
            "wrong_category_case_rate": (
                len(wrong_category_case_ids) / len(rows) if rows else 0.0
            ),
            "wrong_category_case_ids": wrong_category_case_ids,
            "safe_degradation_case_ids": safe_degradation_case_ids,
            "passed": (
                (len(missing_category_case_ids) / len(rows) if rows else 0.0) < 0.02
                and (len(wrong_category_case_ids) / len(rows) if rows else 0.0) < 0.01
                and set(missing_category_case_ids) == set(safe_degradation_case_ids)
            ),
        },
        "retrieval_integrity": {
            "fixture_places": fixture_places,
            "fallback_places": fallback_places,
            "canonical_duplicate_count": canonical_duplicates,
            "retrieval_audit_count": retrieval_audits,
            "data_purity_passed": fixture_places == 0 and fallback_places == 0 and all(
                bool((row.get("deterministic") or {}).get("retrieval_integrity", {}).get("data_purity_passed"))
                for row in rows
            ),
            "all_retrievals_succeeded": amap_failures == 0,
            "amap_tool_call_count": amap_tool_calls,
            "amap_failure_count": amap_failures,
            "amap_tool_failure_rate": (
                amap_failures / amap_tool_calls if amap_tool_calls else 0.0
            ),
        },
        "high_risk_honesty": {
            "pending_place_count": pending_place_count,
            "pending_with_action_count": pending_with_action_count,
            "confirmation_action_coverage": (
                pending_with_action_count / pending_place_count if pending_place_count else 1.0
            ),
            "unsupported_affirmative_claim_count": len(unsupported_affirmative_claims),
            "unsupported_affirmative_claims": unsupported_affirmative_claims,
            "passed": (
                pending_with_action_count == pending_place_count
                and not unsupported_affirmative_claims
            ),
        },
    }


def _build_report(
    base: str,
    rows: list[dict[str, Any]],
    cases_by_id: dict[str, dict[str, Any]],
    skip_judge: bool,
    server_health: dict[str, Any],
    reproducibility: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "5.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base,
        "mode": {"demo_mode": False, "amap_mock": False, "llm_judge": False},
        "generation_chain": _generation_chain_metadata(),
        "judge_chain": {
            "kind": "none",
            "network_calls": 0,
            "api_judge_disabled": True,
        },
        "reproducibility": reproducibility,
        "server_health": server_health,
        "summary": _summarize(rows, cases_by_id),
        "cases": rows,
    }


def build_candidate_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    """Extract provider-call inputs and candidates from a live evaluation."""
    cases = []
    missing_case_ids: list[str] = []
    non_live_audits = 0
    failed_receipts = 0
    for row in report.get("cases", []):
        snapshots = list(((row.get("output") or {}).get("done") or {}).get("retrieval_snapshots") or [])
        if not snapshots:
            missing_case_ids.append(str(row.get("id") or "<unknown>"))
        for snapshot in snapshots:
            non_live_audits += sum(
                1 for audit in snapshot.get("audits") or []
                if audit.get("execution_mode") != "live" or audit.get("provider") != "amap"
            )
            failed_receipts += int((snapshot.get("receipt") or {}).get("status") == "error")
        cases.append({
            "id": row.get("id"),
            "city": row.get("city"),
            "intent": row.get("intent"),
            "persona": row.get("persona"),
            "dimensions": row.get("dimensions") or [],
            "query": row.get("query"),
            "execution_tree_sha256": row.get("execution_tree_sha256"),
            # Freeze the post-filter, post-route-evidence candidates as well as
            # raw provider snapshots. Replay may regenerate prose and judge the
            # same cards, but must not refresh route evidence from current traffic.
            "selected_places": list((row.get("output") or {}).get("places") or []),
            "retrieval_snapshots": snapshots,
        })
    return {
        "schema_version": "1.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_report": {
            "generated_at": report.get("generated_at"),
            "reproducibility": report.get("reproducibility"),
            "server_health": report.get("server_health"),
        },
        "integrity": {
            "case_count": len(cases),
            "missing_snapshot_case_ids": missing_case_ids,
            "non_live_audit_count": non_live_audits,
            "failed_receipt_count": failed_receipts,
            "passed": not missing_case_ids and non_live_audits == 0 and failed_receipts == 0,
        },
        "cases": cases,
    }


def replay_candidate_snapshot(
    snapshot_path: Path,
    dataset_path: Path,
    skip_judge: bool,
    workers: int = 1,
) -> dict[str, Any]:
    """Rerun deterministic selection, synthesis and judging without Amap."""
    if not skip_judge:
        raise RuntimeError("冻结重放禁止 API Judge；请使用子 Agent blind bundle")
    from langchain_core.messages import HumanMessage

    from app.agents.nodes import synthesizer
    from app.agents.state import default_working_context
    from app.constraints.location import extract_district_constraint
    from app.constraints.place_identity import deduplicate_places
    from app.constraints.recommendation_plan import bind_geo_anchor_evidence, build_recommendation_plan
    from app.schemas.place import Place

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not (snapshot.get("integrity") or {}).get("passed"):
        raise RuntimeError("候选快照完整性未通过，拒绝把故障样本当作稳定候选重放")
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    dataset_cases = validate_dataset(dataset)
    cases_by_id = {case["id"]: case for case in dataset_cases}
    snapshot_cases = snapshot.get("cases") or []
    reproducibility = _runner_metadata(dataset_path)
    execution_tree_sha256 = reproducibility["backend_execution_tree"]["sha256"]

    def replay_one(index: int, frozen: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        case = cases_by_id[str(frozen["id"])]
        snapshots = list(frozen.get("retrieval_snapshots") or [])
        raw_places = [
            Place.model_validate(place)
            for item in snapshots
            for place in item.get("places") or []
        ]
        places = deduplicate_places(raw_places)
        selected_places = [
            Place.model_validate(place)
            for place in frozen.get("selected_places") or []
        ]
        has_frozen_selection = "selected_places" in frozen
        audits = [audit for item in snapshots for audit in item.get("audits") or []]
        failures = [
            {
                "tool": "search_places",
                "reason": str((item.get("receipt") or {}).get("error_category") or "tool_exception"),
            }
            for item in snapshots
            if (item.get("receipt") or {}).get("status") == "error"
        ]
        district = extract_district_constraint(case["query"]) or ""
        plan = bind_geo_anchor_evidence(
            build_recommendation_plan(case["query"], case["city"], district),
            audits,
        )
        state = {
            "messages": [HumanMessage(content=case["query"])],
            "thread_id": f"snapshot-{case['id']}",
            "user_id": "eval",
            "room_id": None,
            "trace_id": f"snapshot-{case['id']}",
            "deadline_monotonic": time.monotonic() + settings.chat_deadline_seconds,
            "trip_city": case["city"],
            "trip_district": district or None,
            "amap_places": places,
            "eligible_amap_places": selected_places,
            "eligible_candidates_computed": has_frozen_selection,
            "rag_chunks": [],
            "citations": [],
            "tool_failures": failures,
            "tool_receipts": [item.get("receipt") for item in snapshots if item.get("receipt")],
            "retrieval_audits": audits,
            "retrieval_snapshots": snapshots,
            "recommendation_plan": plan.model_dump(mode="json"),
            "slot_coverage": {},
            "missing_slot_ids": [],
            "working_context": default_working_context(),
            "user_long_term_prefs": None,
            "react_iterations": 0,
            "critic_iterations": 0,
        }
        if has_frozen_selection:
            synthesized = synthesizer.synthesize_frozen_places(
                selected_places,
                case["city"],
                state["working_context"],
                district,
                case["query"],
            )
        else:
            # Backward compatibility for v1.0 snapshots only. New snapshots
            # freeze selected_places and never refresh LLM or route evidence.
            synthesized = asyncio.run(synthesizer.run(state))
        delivered = synthesized.get("synthesized_places") or []
        result = {
            "places": [place.model_dump(mode="json") for place in delivered],
            "raw_place_count": len(delivered),
            "canonical_duplicate_count": 0,
            "candidate_pool_duplicate_count": len(raw_places) - len(places),
            "text": synthesized.get("final_response") or "",
            "thinking": [],
            "errors": [],
            "done": {
                "retrieval_audits": audits,
                "tool_failures": failures,
                "tool_receipts": state["tool_receipts"],
                "retrieval_snapshots": snapshots,
            },
        }
        deterministic = deterministic_checks(case, result, require_live_integrity=True)
        blocker = _retrieval_blocker(deterministic["retrieval_integrity"])
        if blocker:
            status, reason = blocker
            judge = _skipped_judge(status, reason)
        else:
            status = "judge_skipped" if skip_judge else "completed"
            judge = {"passed": True, "skipped": True} if skip_judge else llm_judge(case, result)
        passed = bool(deterministic["passed"] and judge.get("passed"))
        return index, {
            "id": case["id"], "city": case["city"], "intent": case["intent"],
            "persona": case["persona"], "dimensions": case.get("dimensions") or [],
            "query": case["query"], "execution_tree_sha256": execution_tree_sha256,
            "passed": passed, "evaluation_status": status,
            "deterministic": deterministic, "judge": judge, "output": result,
        }

    rows_by_index: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [
            executor.submit(replay_one, index, frozen)
            for index, frozen in enumerate(snapshot_cases)
        ]
        for future in as_completed(futures):
            index, row = future.result()
            rows_by_index[index] = row
    rows = [rows_by_index[index] for index in sorted(rows_by_index)]
    report = _build_report(
        f"snapshot://{snapshot_path.resolve()}",
        rows,
        cases_by_id,
        skip_judge,
        (snapshot.get("source_report") or {}).get("server_health") or {},
        reproducibility,
    )
    report["mode"]["retrieval"] = "frozen_snapshot"
    report["snapshot_source"] = str(snapshot_path.resolve())
    report["api_usage"] = {
        "provider_calls": 0,
        "generation_llm_calls": 0,
        "judge_api_calls": 0,
        "external_calls_total": 0,
        "paid_generation_authorized": False,
    }
    return report


def run(
    base_url: str,
    dataset_path: Path,
    case_ids: set[str],
    limit: int | None,
    skip_judge: bool,
    *,
    workers: int = 1,
    checkpoint_path: Path | None = None,
    resume_path: Path | None = None,
    allow_paid_generation: bool = False,
) -> dict[str, Any]:
    if not skip_judge:
        raise RuntimeError("API Judge 已禁用；真实链路必须使用 --skip-judge")
    if not allow_paid_generation:
        raise RuntimeError(
            "真实产品链路可能调用 DeepSeek；必须显式传入 --allow-paid-generation"
        )
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    cases = validate_dataset(dataset)
    if case_ids:
        cases = [case for case in cases if case["id"] in case_ids]
    if limit is not None:
        cases = cases[:limit]
    base = base_url.rstrip("/")
    server_health = verify_live_server(base)
    metrics_before = _read_service_metrics(base)
    reproducibility = _runner_metadata(dataset_path)
    execution_tree_sha256 = reproducibility["backend_execution_tree"]["sha256"]
    user_id, token = _register(base)
    rows_by_index: dict[int, dict[str, Any]] = {}
    cases_by_id = {case["id"]: case for case in cases}
    resumed_by_id: dict[str, dict[str, Any]] = {}
    if resume_path and resume_path.exists():
        resumed_report = json.loads(resume_path.read_text(encoding="utf-8"))
        resumed_by_id = {row["id"]: row for row in resumed_report.get("cases", []) if row.get("id")}

    def evaluate(index: int, case: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        print(f"[{index + 1}/{len(cases)}] {case['id']}: {case['query']}", flush=True)
        previous = resumed_by_id.get(case["id"]) or {}
        if (
            previous.get("output")
            and previous.get("execution_tree_sha256") == execution_tree_sha256
            and retrieval_integrity_checks(previous["output"]).get("quality_eligible")
        ):
            result = previous["output"]
            deterministic = deterministic_checks(case, result, require_live_integrity=True)
            previous["deterministic"] = deterministic
            try:
                judge = {"passed": True, "skipped": True} if skip_judge else llm_judge(case, result)
                previous["judge"] = judge
                previous["passed"] = bool(deterministic["passed"] and judge.get("passed"))
                previous["evaluation_status"] = "completed" if not skip_judge else "judge_skipped"
                previous["execution_tree_sha256"] = execution_tree_sha256
                previous.pop("error", None)
            except Exception as exc:
                previous["judge"] = {"passed": False, "error": str(exc)}
                previous["passed"] = False
                previous["evaluation_status"] = "judge_error"
            return index, previous
        try:
            result = _run_case(base, user_id, token, case)
            deterministic = deterministic_checks(case, result, require_live_integrity=True)
        except Exception as exc:
            print(f"  SYSTEM ERROR {type(exc).__name__}: {exc}", flush=True)
            return index, {
                "id": case["id"], "city": case["city"], "intent": case["intent"],
                "persona": case["persona"], "dimensions": case.get("dimensions") or [],
                "query": case["query"], "passed": False, "evaluation_status": "system_error",
                "execution_tree_sha256": execution_tree_sha256,
                "error": str(exc),
            }
        integrity = deterministic.get("retrieval_integrity") or {}
        blocker = _retrieval_blocker(integrity)
        if blocker:
            status, reason = blocker
            print(f"  {status.upper()} {reason}", flush=True)
            return index, {
                "id": case["id"],
                "city": case["city"],
                "intent": case["intent"],
                "persona": case["persona"],
                "dimensions": case.get("dimensions") or [],
                "query": case["query"],
                "execution_tree_sha256": execution_tree_sha256,
                "passed": False,
                "evaluation_status": status,
                "deterministic": deterministic,
                "judge": _skipped_judge(status, reason),
                "output": result,
            }
        try:
            judge = {"passed": True, "skipped": True} if skip_judge else llm_judge(case, result)
            passed = deterministic["passed"] and judge.get("passed", False)
            print(
                f"  {'PASS' if passed else 'FAIL'} places={len(result['places'])} "
                f"deterministic={deterministic['passed']} judge={judge.get('passed')}",
                flush=True,
            )
            row = {
                "id": case["id"],
                "city": case["city"],
                "intent": case["intent"],
                "persona": case["persona"],
                "dimensions": case.get("dimensions") or [],
                "query": case["query"],
                "execution_tree_sha256": execution_tree_sha256,
                "passed": passed,
                "evaluation_status": "completed" if not skip_judge else "judge_skipped",
                "deterministic": deterministic,
                "judge": judge,
                "output": result,
            }
        except Exception as exc:
            print(f"  JUDGE ERROR {type(exc).__name__}: {exc}", flush=True)
            row = {
                "id": case["id"], "city": case["city"], "intent": case["intent"],
                "persona": case["persona"], "dimensions": case.get("dimensions") or [],
                "query": case["query"], "passed": False, "evaluation_status": "judge_error",
                "execution_tree_sha256": execution_tree_sha256,
                "deterministic": deterministic, "judge": {"passed": False, "error": str(exc)},
                "output": result,
            }
        return index, row

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = []
        for index, case in enumerate(cases):
            previous = resumed_by_id.get(case["id"]) or {}
            if (
                (previous.get("judge") or {}).get("scores")
                and previous.get("output")
                and previous.get("execution_tree_sha256") == execution_tree_sha256
                and retrieval_integrity_checks(previous["output"]).get("quality_eligible")
            ):
                previous["deterministic"] = deterministic_checks(
                    case, previous["output"], require_live_integrity=True,
                )
                previous["passed"] = bool(
                    previous["deterministic"]["passed"]
                    and (previous.get("judge") or {}).get("passed")
                )
                previous["evaluation_status"] = "completed"
                rows_by_index[index] = previous
                continue
            futures.append(executor.submit(evaluate, index, case))
        for future in as_completed(futures):
            index, row = future.result()
            rows_by_index[index] = row
            if checkpoint_path:
                rows = [rows_by_index[item] for item in sorted(rows_by_index)]
                _write_report(
                    checkpoint_path,
                    _build_report(base, rows, cases_by_id, skip_judge, server_health, reproducibility),
                )

    rows = [rows_by_index[item] for item in sorted(rows_by_index)]
    metrics_after = _read_service_metrics(base)
    report = _build_report(base, rows, cases_by_id, skip_judge, server_health, reproducibility)
    provider_calls = int((report.get("summary") or {}).get("retrieval_integrity", {}).get("amap_tool_call_count") or 0)
    model_calls = _numeric_mapping_delta(metrics_before, metrics_after, "model_calls")
    model_usage = _numeric_mapping_delta(metrics_before, metrics_after, "model_usage")
    generation_llm_calls = int(sum(model_calls.values()))
    report["api_usage"] = {
        "provider_calls": provider_calls,
        "generation_llm_calls": generation_llm_calls,
        "generation_call_distribution": model_calls,
        "generation_token_usage": model_usage,
        "generation_call_count_source": "server_metrics_delta",
        "judge_api_calls": 0,
        "external_calls_total": provider_calls + generation_llm_calls,
        "paid_generation_authorized": True,
    }
    return report


def judge_existing(report_path: Path, dataset_path: Path, workers: int = 1) -> dict[str, Any]:
    del report_path, dataset_path, workers
    raise RuntimeError("--judge-existing 已禁用；请使用 scripts.agent_judge_panel")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--skip-judge", action="store_true", help="兼容参数；API Judge 始终禁用")
    parser.add_argument(
        "--allow-paid-generation",
        action="store_true",
        help="显式授权真实产品链路调用已配置的 DeepSeek/高德；冻结重放不需要",
    )
    parser.add_argument("--judge-existing", type=Path)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--snapshot-output", type=Path)
    parser.add_argument("--replay-snapshot", type=Path)
    args = parser.parse_args()
    report = (
        replay_candidate_snapshot(args.replay_snapshot, args.dataset, True, args.workers)
        if args.replay_snapshot
        else judge_existing(args.judge_existing, args.dataset, args.workers)
        if args.judge_existing
        else run(
            args.base_url,
            args.dataset,
            set(args.case_id),
            args.limit,
            args.skip_judge,
            workers=args.workers,
            checkpoint_path=args.output,
            resume_path=args.resume_from,
            allow_paid_generation=args.allow_paid_generation,
        )
    )
    _write_report(args.output, report)
    if args.snapshot_output:
        _write_report(args.snapshot_output, build_candidate_snapshot(report))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if report["summary"]["passed"] != report["summary"]["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
