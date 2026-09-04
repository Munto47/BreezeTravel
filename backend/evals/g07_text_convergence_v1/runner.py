from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app.api import trip_understandings_v3
from app.trip_understanding.access_log import (
    TripUnderstandingAccessLogFilter,
    redact_trip_understanding_path,
)
from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.demo import (
    DEMO_SOURCE_TEXT,
    FixedBeijingDemoInferenceProvider,
    FixedBeijingPlaceResolver,
)
from app.trip_understanding.errors import (
    InferenceProviderUnavailableError,
    JobLeaseLostError,
    PlaceProviderUnavailableError,
    ResourceAccessDeniedError,
    ResourceGoneError,
    ResourceNotFoundError,
    RouteProviderUnavailableError,
)
from app.trip_understanding.full_text import (
    ControlledSnapshotPlaceResolver,
    build_full_text_pipeline,
)
from app.trip_understanding.map_render import (
    InternalRouteModeFact,
    MapRenderPlan,
    MapRenderer,
    MapStop,
    PlanRevisionRef,
    choose_route_mode,
)
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.models import (
    ActivityRole,
    ActivityTextEditCommand,
    CreateFullRequest,
    DestinationBasis,
    InferenceProposal,
    ProposedMention,
    ResolvedPlace,
)
from app.trip_understanding.pipeline import (
    ResilientStructuredInferenceProvider,
    TripUnderstandingPipeline,
    atomic_place_rejection_reason,
)
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.worker import TripUnderstandingWorker


DATA_ROOT = Path(__file__).resolve().parents[2] / "eval_data" / "g07_text_convergence_v1"
CASES_PATH = DATA_ROOT / "compatibility_cases.json"
SCHEMA_PATH = DATA_ROOT / "compatibility_cases.schema.json"
EXPECTED_GROUPS = {
    "STRUCTURE_ROLE_DATE": 10,
    "TIME_CANCEL_RESCHEDULE": 10,
    "PLACE_CITY_CATEGORY": 8,
    "REVISION_MAP_PROVIDER": 6,
    "PRIVACY_PUBLIC_UX": 6,
}
EXPECTED_IDS = [f"BT-COMPAT-{index:03d}" for index in range(1, 41)]
FROZEN_DATASET_SHA256 = "a891e85780961f8a2dae25a0e8766dd7ae36742846e33ccda1bcf2c0dd9df299"
FROZEN_SCHEMA_SHA256 = "9797cfbd64b562ce0ecd74ea1193fb293be0848bcfeabbc7e098ab10ad5e7eb4"
FORBIDDEN_PUBLIC_KEYS = {
    "raw_text",
    "source",
    "source_id",
    "span",
    "span_start",
    "span_end",
    "offset",
    "confidence",
    "model",
    "provider",
    "uuid",
    "hash",
    "revision",
    "receipt",
    "evidence_gap",
    "run",
    "stage",
}
PUBLIC_RESULT_ALLOWED_KEYS = {
    "activity_token",
    "activities",
    "area_or_address",
    "area_summary",
    "assumptions",
    "available_actions",
    "brand",
    "candidate_token",
    "candidates",
    "category",
    "commute_summary",
    "days",
    "editable",
    "freshness",
    "key",
    "knowledge_suggestions",
    "label",
    "map",
    "max_single_leg_minutes",
    "message",
    "name",
    "reason",
    "searched_scopes",
    "selected",
    "source_name",
    "source_url",
    "status",
    "stay",
    "text",
    "time_hint",
    "transfer_count",
    "type",
    "value",
}
_FORBIDDEN_PUBLIC_TEXT_MARKERS = (
    "audit",
    "evidence",
    "model",
    "postcheck",
    "provider",
    "provider_binding",
    "repair",
    "revision",
    "source_hash",
    "source_text",
    "source span",
    "span_start",
    "span_end",
    "revision_id",
    "receipt",
    "traceback",
    "stack trace",
)
_DANGEROUS_EXACT_NAMES = {"公厕", "停车场", "充电站"}
_DANGEROUS_TEXT_MARKERS = (
    "不能生成地点卡",
    "请不要",
    "说明句",
    "预约网址",
    "旧称",
    "曾称",
    "历史说明",
    "开放时间",
    "营业时间",
)
_DANGEROUS_NAME_RE = re.compile(
    r"(?:https?://|(?:讲解|拍照|打卡|游览)$|(?:入口|出口)$|"
    r"(?:\d{1,2}(?::\d{2})?|\d+(?:小时|分钟))$)",
    re.IGNORECASE,
)
_DURATION_VALUE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:小时|分钟)")
_VALID_VISIT_TIME_HINT_RE = re.compile(
    r"(?:清晨|早上|上午|中午|午后|下午|傍晚|晚上|夜间)"
    r"|(?:[01]\d|2[0-3]):[0-5]\d"
)
_MODEL_SENTINEL = "g07-private-model-sentinel"
_PROVIDER_SENTINEL = "g07-private-provider-sentinel"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cases() -> dict[str, Any]:
    if _sha256_file(CASES_PATH) != FROZEN_DATASET_SHA256:
        raise ValueError("compatibility dataset changed without a version/hash update")
    if _sha256_file(SCHEMA_PATH) != FROZEN_SCHEMA_SHA256:
        raise ValueError("compatibility schema changed without a version/hash update")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(payload)
    case_ids = [case["case_id"] for case in payload["cases"]]
    if case_ids != EXPECTED_IDS:
        raise ValueError("compatibility case IDs must be the frozen sequence 001..040")
    group_counts = Counter(case["group"] for case in payload["cases"])
    if dict(group_counts) != EXPECTED_GROUPS:
        raise ValueError("compatibility group quotas must be 10+10+8+6+6")
    if payload["public_non_blind"] is not True:
        raise ValueError("compatibility data must remain public and non-blind")
    return payload


def _walk_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            child_key
            for child in value.values()
            for child_key in _walk_keys(child)
        }
    if isinstance(value, list):
        return {child_key for child in value for child_key in _walk_keys(child)}
    return set()


def _has_duration_field(value: object) -> bool:
    return any("duration" in key.casefold() for key in _walk_keys(value))


def _dangerous_name(name: str) -> bool:
    candidate = name.strip()
    if candidate == "地点待确认":
        return False
    return (
        candidate in _DANGEROUS_EXACT_NAMES
        or any(marker in candidate for marker in _DANGEROUS_TEXT_MARKERS)
        or _DANGEROUS_NAME_RE.search(candidate) is not None
        or atomic_place_rejection_reason(candidate) is not None
    )


def _public_payload_is_redacted(payload: object) -> bool:
    keys = {key.casefold() for key in _walk_keys(payload)}
    if not keys.issubset(PUBLIC_RESULT_ALLOWED_KEYS):
        return False
    if not FORBIDDEN_PUBLIC_KEYS.isdisjoint(keys):
        return False
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True).casefold()
    return not any(marker in serialized for marker in _FORBIDDEN_PUBLIC_TEXT_MARKERS)


def _empty_observation() -> dict[str, Any]:
    return {
        "destination_name": None,
        "public_status": None,
        "ordered_cards": [],
        "mentions": [],
        "eligible_names": [],
        "auto_matched_names": [],
        "eligible_count": 0,
        "auto_matched_count": 0,
        "facts": set(),
        "degradations": [],
        "resolution_failures": [],
    }


def _pipeline_observation(output: Any) -> dict[str, Any]:
    observation = _empty_observation()
    cards: list[dict[str, Any]] = []
    for day_index, day in enumerate(output.public_result.days, start=1):
        cards.append(
            {
                "day_index": day_index,
                "cards": [
                    {
                        "name": card.name,
                        "time_hint": card.time_hint,
                        "status": card.status,
                        "category": card.category,
                    }
                    for card in day.activities
                ],
            }
        )
    mentions: list[dict[str, Any]] = []
    eligible_names: list[str] = []
    auto_matched_names: list[str] = []
    resolution_failures: list[str] = []
    destination_cities = {
        value.strip().removesuffix("市")
        for value in re.split(r"[、，,和与/]", str(output.destination["name"]))
        if value.strip()
    }
    for activity in output.activities:
        mention = activity.compiled.mention
        name = mention.atomic_place_name or mention.raw_text
        mentions.append(
            {
                "name": name,
                "role": mention.role.value,
                "day_index": mention.day_index,
                "time_hint": mention.time_hint,
                "eligible": activity.compiled.eligible_for_place_search,
            }
        )
        if activity.compiled.eligible_for_place_search:
            eligible_names.append(name)
        if activity.place is not None:
            auto_matched_names.append(name)
            selected_city = activity.resolver_receipt.get("selected_city")
            reported_city = activity.resolver_receipt.get("city")
            place_city = activity.place.provider_binding.get("city")
            for city in (selected_city, reported_city, place_city):
                if (
                    isinstance(city, str)
                    and city.strip().removesuffix("市") not in destination_cities
                ):
                    resolution_failures.append("WRONG_CITY_AUTO_MATCH")
            category_hint = (mention.category_hint or "").strip()
            if category_hint and activity.place.category != category_hint:
                resolution_failures.append("WRONG_CATEGORY_AUTO_MATCH")
    public_payload = output.public_result.model_dump(mode="json")
    dangerous_names = [
        name
        for name in [
            *(card["name"] for day in cards for card in day["cards"]),
            *eligible_names,
            *auto_matched_names,
        ]
        if _dangerous_name(name)
    ]
    facts: set[str] = set()
    if not dangerous_names and not resolution_failures:
        facts.add("NO_DANGEROUS_OUTPUT")
    if not _has_duration_field(public_payload) and not _has_duration_field(mentions):
        facts.add("NO_DURATION_FIELD")
    observation.update(
        {
            "destination_name": output.destination["name"],
            "public_status": output.public_result.status,
            "ordered_cards": cards,
            "mentions": mentions,
            "eligible_names": eligible_names,
            "auto_matched_names": auto_matched_names,
            "eligible_count": output.resolution_receipt["eligible_count"],
            "auto_matched_count": output.resolution_receipt["auto_matched_count"],
            "facts": facts,
            "resolution_failures": resolution_failures,
        }
    )
    return observation


def _partial_dict_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


def _ordered_cards_match(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> bool:
    if len(expected) != len(actual):
        return False
    for expected_day, actual_day in zip(expected, actual, strict=True):
        if expected_day["day_index"] != actual_day["day_index"]:
            return False
        expected_cards = expected_day["cards"]
        actual_cards = actual_day["cards"]
        if len(expected_cards) != len(actual_cards):
            return False
        if not all(
            _partial_dict_matches(expected_card, actual_card)
            for expected_card, actual_card in zip(expected_cards, actual_cards, strict=True)
        ):
            return False
    return True


def _mentions_match(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> bool:
    for expected_mention in expected:
        expected_count = int(expected_mention.get("count", 1))
        expected_fields = {
            key: value for key, value in expected_mention.items() if key != "count"
        }
        matching = sum(
            _partial_dict_matches(expected_fields, actual_mention)
            for actual_mention in actual
        )
        if matching != expected_count:
            return False
    return True


def _flatten_cards(days: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    return [
        (int(day["day_index"]), card)
        for day in days
        for card in day["cards"]
    ]


def _card_counter(days: list[dict[str, Any]]) -> Counter[str]:
    return Counter(card["name"] for _day_index, card in _flatten_cards(days))


def _has_counter_excess(actual: Counter[str], expected: Counter[str]) -> bool:
    return any(count > expected[name] for name, count in actual.items())


def _unsafe_time_hint_exists(case: dict[str, Any], observation: dict[str, Any]) -> bool:
    expected_by_slot = {
        (day_index, sequence_index): card
        for day_index, day in (
            (int(item["day_index"]), item)
            for item in case["exact_expectation"]["ordered_cards"]
        )
        for sequence_index, card in enumerate(day["cards"])
    }
    for day_index, day in (
        (int(item["day_index"]), item) for item in observation["ordered_cards"]
    ):
        for sequence_index, card in enumerate(day["cards"]):
            time_hint = card.get("time_hint")
            if time_hint is None:
                continue
            if (
                not isinstance(time_hint, str)
                or _DURATION_VALUE_RE.search(time_hint)
                or _VALID_VISIT_TIME_HINT_RE.fullmatch(time_hint) is None
            ):
                return True
            expected = expected_by_slot.get((day_index, sequence_index))
            if (
                expected is not None
                and expected.get("name") == card.get("name")
                and "time_hint" in expected
                and expected["time_hint"] != time_hint
            ):
                return True
    return any(
        isinstance(mention.get("time_hint"), str)
        and _DURATION_VALUE_RE.search(mention["time_hint"]) is not None
        for mention in observation["mentions"]
    )


def _cards_are_safe_omission(
    expected_days: list[dict[str, Any]],
    actual_days: list[dict[str, Any]],
) -> bool:
    expected = _flatten_cards(expected_days)
    actual = _flatten_cards(actual_days)
    cursor = 0
    for actual_day, actual_card in actual:
        while cursor < len(expected):
            expected_day, expected_card = expected[cursor]
            cursor += 1
            if expected_day == actual_day and _partial_dict_matches(
                expected_card, actual_card
            ):
                break
        else:
            return False
    return True


def _only_time_hints_were_omitted(
    expected_days: list[dict[str, Any]],
    actual_days: list[dict[str, Any]],
) -> bool:
    expected = _flatten_cards(expected_days)
    actual = _flatten_cards(actual_days)
    if len(expected) != len(actual):
        return False
    omitted = False
    for (expected_day, expected_card), (actual_day, actual_card) in zip(
        expected, actual, strict=True
    ):
        if expected_day != actual_day:
            return False
        for key, value in expected_card.items():
            if key == "time_hint" and value is not None and actual_card.get(key) is None:
                omitted = True
                continue
            if actual_card.get(key) != value:
                return False
    return omitted


def _proven_degradation_codes(
    case: dict[str, Any],
    observation: dict[str, Any],
    exact_failures: list[str],
) -> list[str]:
    allowed = set(case["safe_expectation"]["allowed_degradations"])
    expected = case["exact_expectation"]
    proven: set[str] = set()
    missing_facts = set(expected["required_facts"]) - set(observation["facts"])
    fact_degradations = {
        "UI_COLOR_E2E_NOT_RUN": "UI_COLOR_E2E_VERIFIED",
        "HOME_AND_MOBILE_E2E_NOT_RUN": "HOME_AND_MOBILE_E2E_VERIFIED",
    }
    for code in observation["degradations"]:
        required_fact = fact_degradations.get(code)
        if (
            code in allowed
            and required_fact is not None
            and missing_facts == {required_fact}
            and set(exact_failures) == {"EXACT_FACT_MISSING"}
        ):
            proven.add(code)
    if (
        "SAFE_OMISSION" in allowed
        and set(exact_failures) == {"ORDERED_CARDS_MISMATCH"}
        and _cards_are_safe_omission(
            expected["ordered_cards"], observation["ordered_cards"]
        )
    ):
        proven.add("SAFE_OMISSION")
    if (
        "TIME_HINT_OMITTED" in allowed
        and set(exact_failures) == {"ORDERED_CARDS_MISMATCH"}
        and _only_time_hints_were_omitted(
            expected["ordered_cards"], observation["ordered_cards"]
        )
    ):
        proven.add("TIME_HINT_OMITTED")
    if (
        "REFERENCE_MENTION_OMITTED" in allowed
        and set(exact_failures) == {"MENTION_SEMANTICS_MISMATCH"}
        and all(
            mention.get("role") == "REFERENCE"
            for mention in expected["mentions"]
            if not any(
                _partial_dict_matches(
                    {key: value for key, value in mention.items() if key != "count"},
                    actual,
                )
                for actual in observation["mentions"]
            )
        )
    ):
        proven.add("REFERENCE_MENTION_OMITTED")
    return sorted(proven)


def _safe_failures(case: dict[str, Any], observation: dict[str, Any]) -> list[str]:
    expectation = case["safe_expectation"]
    card_names = _card_counter(observation["ordered_cards"])
    expected_names = _card_counter(case["exact_expectation"]["ordered_cards"])
    eligible_names = Counter(observation["eligible_names"])
    auto_matched_names = Counter(observation["auto_matched_names"])
    failures: list[str] = []
    if set(card_names).intersection(expectation["forbidden_cards"]):
        failures.append("FORBIDDEN_CARD_EMITTED")
    if set(eligible_names).intersection(expectation["forbidden_eligible_mentions"]):
        failures.append("FORBIDDEN_PLACE_BECAME_ELIGIBLE")
    if set(auto_matched_names).intersection(expectation["forbidden_auto_matches"]):
        failures.append("FORBIDDEN_PLACE_AUTO_MATCHED")
    if _has_counter_excess(card_names, expected_names):
        failures.append("UNEXPECTED_EXECUTABLE_CARD")
    if _has_counter_excess(eligible_names, expected_names):
        failures.append("UNEXPECTED_ELIGIBLE_PLACE")
    if _has_counter_excess(auto_matched_names, expected_names):
        failures.append("UNEXPECTED_AUTO_MATCH")
    if any(_dangerous_name(name) for name in (*card_names, *eligible_names, *auto_matched_names)):
        failures.append("DANGEROUS_NAME_EMITTED")
    if observation["resolution_failures"]:
        failures.extend(observation["resolution_failures"])
    if _unsafe_time_hint_exists(case, observation):
        failures.append("UNSAFE_TIME_HINT")
    allowed_degradations = set(expectation["allowed_degradations"])
    if set(observation["degradations"]) - allowed_degradations:
        failures.append("UNAUTHORIZED_DEGRADATION")
    if not set(expectation["required_facts"]).issubset(observation["facts"]):
        failures.append("REQUIRED_SAFETY_FACT_MISSING")
    return failures


def _exact_failures(case: dict[str, Any], observation: dict[str, Any]) -> list[str]:
    expectation = case["exact_expectation"]
    failures: list[str] = []
    if "destination_name" in expectation and (
        observation["destination_name"] != expectation["destination_name"]
    ):
        failures.append("DESTINATION_MISMATCH")
    if "public_status" in expectation and (
        observation["public_status"] != expectation["public_status"]
    ):
        failures.append("PUBLIC_STATUS_MISMATCH")
    if "eligible_count" in expectation and (
        observation["eligible_count"] != expectation["eligible_count"]
    ):
        failures.append("ELIGIBLE_COUNT_MISMATCH")
    if "auto_matched_count" in expectation and (
        observation["auto_matched_count"] != expectation["auto_matched_count"]
    ):
        failures.append("AUTO_MATCH_COUNT_MISMATCH")
    if not _ordered_cards_match(
        expectation["ordered_cards"], observation["ordered_cards"]
    ):
        failures.append("ORDERED_CARDS_MISMATCH")
    if (
        expectation["mentions"]
        and not _mentions_match(expectation["mentions"], observation["mentions"])
    ) or (
        not expectation["mentions"]
        and not expectation["ordered_cards"]
        and observation["mentions"]
    ):
        failures.append("MENTION_SEMANTICS_MISMATCH")
    if not set(expectation["required_facts"]).issubset(observation["facts"]):
        failures.append("EXACT_FACT_MISSING")
    return failures


def _score_case(case: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    safe_failures = _safe_failures(case, observation)
    exact_failures = _exact_failures(case, observation)
    proven_degradations = _proven_degradation_codes(
        case, observation, exact_failures
    )
    if exact_failures and not proven_degradations:
        safe_failures.append("UNPROVEN_DEGRADATION")
    if not exact_failures and observation["degradations"]:
        safe_failures.append("UNEXPECTED_DEGRADATION_REPORT")
    if safe_failures:
        status = "DANGEROUS_FAIL"
        degradation_codes: list[str] = []
        failure_codes = safe_failures + exact_failures
    elif exact_failures:
        status = "SAFE_DEGRADE"
        degradation_codes = proven_degradations
        failure_codes = exact_failures
    else:
        status = "EXACT_PASS"
        degradation_codes = []
        failure_codes = []
    return {
        "case_id": case["case_id"],
        "group": case["group"],
        "status": status,
        "failure_codes": sorted(set(failure_codes)),
        "degradation_codes": sorted(set(degradation_codes)),
    }


async def _run_text_pipeline(
    source_text: str,
    *,
    place_resolver: Any | None = None,
) -> Any:
    pipeline = build_full_text_pipeline(place_resolver=place_resolver)
    try:
        return await pipeline.run(source_text)
    finally:
        await pipeline.aclose()


async def _reschedule_contract_holds() -> bool:
    ordinary = await _run_text_pipeline(
        "北京一日游。Day 1 原计划去故宫博物院，后来改到颐和园。"
    )
    multiple = await _run_text_pipeline(
        "北京一日游。Day 1 原计划去故宫博物院，最后确定去颐和园或天坛公园，二选一。"
    )
    return not any(
        day.activities
        for output in (ordinary, multiple)
        for day in output.public_result.days
    )


async def _cancellation_contract_holds() -> bool:
    for phrase in (
        "并不取消",
        "没有取消",
        "不打算撤掉",
        "不得取消",
        "不能取消",
        "无法取消",
        "未取消",
        "取消不了",
        "不是取消",
        "并非取消",
    ):
        output = await _run_text_pipeline(
            f"北京一日游。Day 1 去景山公园；{phrase}景山公园。"
        )
        cards = [
            card.name for day in output.public_result.days for card in day.activities
        ]
        roles = [
            item.compiled.mention.role
            for item in output.activities
            if item.compiled.mention.atomic_place_name == "景山公园"
        ]
        if cards != ["景山公园"] or ActivityRole.EXCLUDED in roles:
            return False
    affirmative = await _run_text_pipeline(
        "北京一日游。Day 1 去颐和园；不得不取消颐和园。"
    )
    affirmative_cards = [
        card for day in affirmative.public_result.days for card in day.activities
    ]
    affirmative_roles = [
        item.compiled.mention.role
        for item in affirmative.activities
        if item.compiled.mention.atomic_place_name == "颐和园"
    ]
    if affirmative_cards or affirmative_roles != [
        ActivityRole.REFERENCE,
        ActivityRole.EXCLUDED,
    ]:
        return False

    restored = await _run_text_pipeline(
        "北京一日游。Day 1 去景山公园；取消景山公园；"
        "最后明确恢复原方案去景山公园。"
    )
    restored_cards = [
        card.name for day in restored.public_result.days for card in day.activities
    ]
    if restored_cards != ["景山公园"]:
        return False

    cross_day = await _run_text_pipeline(
        "北京两日游。Day 1 去故宫博物院。"
        "Day 2 不去故宫博物院，改去景山公园。"
    )
    cross_day_cards = [
        (day.label, card.name)
        for day in cross_day.public_result.days
        for card in day.activities
    ]
    if cross_day_cards != [("Day 1", "故宫博物院"), ("Day 2", "景山公园")]:
        return False

    timed = await _run_text_pipeline(
        "北京一日游。Day 1 上午去故宫博物院，下午再次去故宫博物院；"
        "取消下午的故宫博物院。"
    )
    timed_cards = [
        (card.name, card.time_hint)
        for day in timed.public_result.days
        for card in day.activities
    ]
    if timed_cards != [("故宫博物院", "上午")]:
        return False

    uncertain = await _run_text_pipeline(
        "北京一日游。Day 1 去故宫博物院；如果下雨就取消故宫博物院。"
    )
    uncertain_cards = [
        (card.name, card.status)
        for day in uncertain.public_result.days
        for card in day.activities
    ]
    if (
        uncertain_cards != [("故宫博物院", "NEEDS_CONFIRMATION")]
        or uncertain.resolution_receipt["attempted_count"] != 0
    ):
        return False

    ambiguous = await _run_text_pipeline(
        "北京一日游。Day 1 上午去故宫博物院，下午再次去故宫博物院；"
        "取消故宫博物院。"
    )
    ambiguous_cards = [
        (card.name, card.time_hint, card.status)
        for day in ambiguous.public_result.days
        for card in day.activities
    ]
    return (
        ambiguous_cards
        == [
            ("故宫博物院", "上午", "NEEDS_CONFIRMATION"),
            ("故宫博物院", "下午", "NEEDS_CONFIRMATION"),
        ]
        and ambiguous.resolution_receipt["attempted_count"] == 0
        and ambiguous.resolution_receipt["unique_resolution_count"] == 0
        and ambiguous.resolution_receipt["place_external_call_count"] == 0
    )


async def _dining_hotel_conflict_is_pending() -> bool:
    hotel = _provider_poi("CATEGORY_CONFLICT")
    hotel.update(
        {
            "id": "B0PUBLICHOTEL",
            "name": "北京饭店",
            "address": "东城区东长安街33号",
        }
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "1", "infocode": "10000", "pois": [hotel]},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await AmapPlaceResolver(
            api_key="public-fixture-only", client=client
        ).resolve(
            city="北京",
            atomic_place_name="北京饭店",
            category_hint="餐饮",
        )
    return outcome.place is None


async def _multi_city_ambiguity_is_pending(source_text: str) -> bool:
    class AmbiguousResolver:
        async def resolve(
            self,
            *,
            city: str,
            atomic_place_name: str,
            category_hint: str | None = None,
        ) -> ResolvedPlace:
            return ResolvedPlace(
                canonical_place_id=f"fixture-{city}-{atomic_place_name}",
                name=atomic_place_name,
                category=category_hint or "景点",
                area_or_address=f"{city}公开夹具地址",
                provider_binding={"city": city, "external_calls": 0},
            )

    output = await _run_text_pipeline(
        source_text,
        place_resolver=AmbiguousResolver(),
    )
    eligible = [
        activity
        for activity in output.activities
        if activity.compiled.eligible_for_place_search
    ]
    return bool(eligible) and all(
        activity.place is None
        and activity.resolution_status.value == "NEEDS_CONFIRMATION"
        for activity in eligible
    )


async def _run_pipeline_case(case: dict[str, Any]) -> dict[str, Any]:
    output = await _run_text_pipeline(case["input_text"])
    observation = _pipeline_observation(output)
    variant = case["probe"]["variant"]
    contract_holds = True
    if variant == "FINAL_RESCHEDULE":
        contract_holds = await _reschedule_contract_holds()
    elif variant == "CANCEL_RESTORE_REPEAT":
        contract_holds = await _cancellation_contract_holds()
        if contract_holds:
            observation["facts"].add("CANCELLATION_TARGET_BOUND")
    elif variant == "DINING_HOTEL_CONFLICT":
        contract_holds = await _dining_hotel_conflict_is_pending()
    elif variant == "MULTI_CITY_UNIQUE":
        contract_holds = await _multi_city_ambiguity_is_pending(case["input_text"])
    if not contract_holds:
        observation["resolution_failures"].append("VARIANT_CONTRACT_NOT_PROVEN")
        observation["facts"].discard("NO_DANGEROUS_OUTPUT")
    return observation


def _provider_poi(variant: str) -> dict[str, object]:
    poi: dict[str, object] = {
        "id": "B000A83M61",
        "name": "故宫博物院",
        "location": "116.397026,39.918058",
        "type": "风景名胜;风景名胜相关;旅游景点",
        "typecode": "110202",
        "pname": "北京市",
        "cityname": "北京市",
        "adname": "东城区",
        "address": "景山前街4号",
        "adcode": "110101",
    }
    if variant == "WRONG_CITY":
        poi.update(
            {
                "location": "121.490317,31.241701",
                "pname": "上海市",
                "cityname": "上海市",
                "adname": "黄浦区",
                "adcode": "310101",
            }
        )
    elif variant == "ADMIN_CONTRADICTION":
        poi.update({"pname": "上海市", "adname": "黄浦区", "adcode": "310101"})
    elif variant == "CATEGORY_CONFLICT":
        poi.update({"type": "住宿服务;宾馆酒店;宾馆酒店", "typecode": "100100"})
    elif variant == "CATEGORY_TYPECODE_CONFLICT":
        poi.update({"typecode": "100100"})
    elif variant == "CATEGORY_LABEL_CONFLICT":
        poi.update({"type": "住宿服务;宾馆酒店;宾馆酒店"})
    elif variant == "MISSING_PROVINCE":
        poi.pop("pname")
    elif variant == "MISSING_DISTRICT":
        poi.pop("adname")
    elif variant == "MISSING_ADCODE":
        poi.pop("adcode")
    elif variant == "BAD_PROVINCE_TYPE":
        poi["pname"] = 110000
    elif variant == "BAD_ADCODE_TYPE":
        poi["adcode"] = {"unexpected": "110101"}
    return poi


async def _run_place_safety_case(case: dict[str, Any]) -> dict[str, Any]:
    variant = case["probe"]["variant"]
    observation = _empty_observation()

    variants = {
        "WRONG_CITY": ("WRONG_CITY",),
        "ADMIN_CONTRADICTION": (
            "ADMIN_CONTRADICTION",
            "MISSING_PROVINCE",
            "MISSING_DISTRICT",
            "MISSING_ADCODE",
            "BAD_PROVINCE_TYPE",
            "BAD_ADCODE_TYPE",
        ),
        "CATEGORY_CONFLICT": (
            "CATEGORY_CONFLICT",
            "CATEGORY_TYPECODE_CONFLICT",
            "CATEGORY_LABEL_CONFLICT",
        ),
    }[variant]

    async def resolve(candidate_variant: str) -> Any:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "infocode": "10000",
                    "pois": [_provider_poi(candidate_variant)],
                },
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await AmapPlaceResolver(
                api_key="public-fixture-only", client=client
            ).resolve(
                city="北京",
                atomic_place_name="故宫博物院",
                category_hint="景点",
            )

    outcomes = [await resolve(candidate_variant) for candidate_variant in variants]
    if any(outcome.place is not None for outcome in outcomes):
        observation["auto_matched_names"] = ["故宫博物院"]
        observation["auto_matched_count"] = 1
    else:
        fact = {
            "WRONG_CITY": "WRONG_CITY_PENDING",
            "ADMIN_CONTRADICTION": "ADMIN_EVIDENCE_PENDING",
            "CATEGORY_CONFLICT": "CATEGORY_CONFLICT_PENDING",
        }[variant]
        observation["facts"].add(fact)
    return observation


async def _complete_demo(
    repository: InMemoryTripUnderstandingRepository,
    service: TripUnderstandingApplicationService,
    *,
    capability: str,
    idempotency_key: str,
    now: datetime,
) -> tuple[Any, Any]:
    created = await service.create_demo(
        capability_hash=capability,
        idempotency_key=idempotency_key,
        now=now,
    )
    job = await repository.claim_next(
        worker_id=f"{idempotency_key}-understanding",
        now=now,
        lease_seconds=30,
    )
    if job is None:
        raise RuntimeError("demo understanding job was not created")
    output = await TripUnderstandingPipeline(
        FixedBeijingDemoInferenceProvider(),
        FixedBeijingPlaceResolver(),
    ).run(DEMO_SOURCE_TEXT)
    await repository.complete_job(job, output, now=now + timedelta(seconds=1))
    resource = await service.authorize(
        created.accepted.public_resource_id,
        capability_hash=capability,
        now=now + timedelta(seconds=1),
    )
    return resource, output


async def _map_revision_facts() -> set[str]:
    facts: set[str] = set()
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    repository = InMemoryTripUnderstandingRepository()
    service = TripUnderstandingApplicationService(repository)
    resource, _output = await _complete_demo(
        repository,
        service,
        capability="d" * 64,
        idempotency_key="g07-map",
        now=now,
    )
    if repository.map_job_count == 1:
        facts.add("INITIAL_MAP_JOB_ONCE")
    if await MapRenderWorker(repository).run_once(
        "g07-map-worker", now=now + timedelta(seconds=2)
    ):
        map_view = await repository.get_map_view(resource, now=now + timedelta(seconds=3))
        routes = [route for day in map_view.days for route in day.routes]
        if routes and all(
            route.walking.status == "AVAILABLE" and route.transit.status == "AVAILABLE"
            for route in routes
        ):
            facts.add("WALKING_AND_TRANSIT_EXECUTED")
        if routes and all(
            route.selected_mode in {"walking", "transit"}
            for route in routes
        ):
            facts.add("NO_DEFAULT_DRIVING")

    stored = await repository.get_result(resource)
    if stored is None:
        raise RuntimeError("demo result was not stored")
    effects_before_edit = repository.map_provider_effect_count
    first_token = stored.result.days[0].activities[0].activity_token
    applied = await service.apply_command(
        resource,
        ActivityTextEditCommand(
            command_type="ACTIVITY_TEXT_EDIT",
            activity_token=first_token,
            time_hint="晚上",
        ),
        expected_etag=stored.opaque_etag,
        idempotency_key="g07-map-edit",
        now=now + timedelta(seconds=4),
    )
    updated = await repository.get_result(resource)
    if (
        updated is not None
        and updated.result.map.status == "NEEDS_UPDATE"
        and applied.opaque_etag != stored.opaque_etag
    ):
        facts.add("EDIT_MARKS_NEEDS_UPDATE")
    if repository.map_provider_effect_count == effects_before_edit:
        facts.add("EDIT_ROUTE_CALL_DELTA_ZERO")
        facts.add("MANUAL_RENDER_ONLY")

    accepted = await service.request_map_render(
        resource,
        expected_etag=applied.opaque_etag,
        idempotency_key="g07-map-manual",
        now=now + timedelta(seconds=5),
    )
    replayed = await service.request_map_render(
        resource,
        expected_etag=applied.opaque_etag,
        idempotency_key="g07-map-manual",
        now=now + timedelta(seconds=5),
    )
    logically_deduped = await service.request_map_render(
        resource,
        expected_etag=applied.opaque_etag,
        idempotency_key="g07-map-manual-second-key",
        now=now + timedelta(seconds=5),
    )
    requests_deduplicated = (
        accepted.accepted.status == "PREPARING"
        and replayed.replayed is True
        and logically_deduped.replayed is False
        and repository.map_job_count == 2
    )
    effects_before_manual = repository.map_provider_effect_count
    manual_ran = await MapRenderWorker(repository).run_once(
        "g07-map-manual-worker", now=now + timedelta(seconds=6)
    )
    effects_after_manual = repository.map_provider_effect_count
    duplicate_claim = await repository.claim_next_map(
        worker_id="g07-map-duplicate-check",
        now=now + timedelta(seconds=7),
        lease_seconds=30,
    )
    if (
        requests_deduplicated
        and manual_ran
        and duplicate_claim is None
        and effects_after_manual > effects_before_manual
        and repository.map_provider_effect_count == effects_after_manual
    ):
        facts.add("REQUEST_AND_LOGICAL_DEDUPE")

    stale_repository = InMemoryTripUnderstandingRepository()
    stale_service = TripUnderstandingApplicationService(stale_repository)
    stale_resource, _ = await _complete_demo(
        stale_repository,
        stale_service,
        capability="e" * 64,
        idempotency_key="g07-map-stale",
        now=now,
    )
    old_claim = await stale_repository.claim_next_map(
        worker_id="g07-reused-map-worker",
        now=now + timedelta(seconds=2),
        lease_seconds=5,
    )
    if old_claim is None:
        raise RuntimeError("initial map claim was not created")
    old_plan = await stale_repository.load_map_plan(old_claim)
    old_output = await MapRenderer().render(
        old_plan, observed_at=now + timedelta(seconds=2)
    )
    replacement = await stale_repository.claim_next_map(
        worker_id="g07-reused-map-worker",
        now=now + timedelta(seconds=8),
        lease_seconds=30,
    )
    if replacement is None:
        raise RuntimeError("expired map claim was not reclaimed")
    try:
        await stale_repository.complete_map_job(
            old_claim,
            old_output,
            now=now + timedelta(seconds=9),
        )
    except JobLeaseLostError:
        facts.add("STALE_LEASE_FENCED")
    stale_stored = await stale_repository.get_result(stale_resource)
    if stale_stored is None:
        raise RuntimeError("stale scenario result was not stored")
    await stale_service.apply_command(
        stale_resource,
        ActivityTextEditCommand(
            command_type="ACTIVITY_TEXT_EDIT",
            activity_token=stale_stored.result.days[0].activities[0].activity_token,
            name="故宫入口待确认",
        ),
        expected_etag=stale_stored.opaque_etag,
        idempotency_key="g07-late-map-edit",
        now=now + timedelta(seconds=9),
    )
    replacement_output = await MapRenderer().render(
        await stale_repository.load_map_plan(replacement),
        observed_at=now + timedelta(seconds=9),
    )
    await stale_repository.complete_map_job(
        replacement,
        replacement_output,
        now=now + timedelta(seconds=9),
    )
    current_map = await stale_repository.get_map_view(
        stale_resource, now=now + timedelta(seconds=10)
    )
    if current_map.status == "NEEDS_UPDATE":
        facts.add("OLD_REVISION_NOT_CURRENT")

    if _route_mode_rule_holds(now):
        facts.add("TEN_MINUTE_WALKING_RULE")
    facts.update(await _partial_provider_facts())
    return facts


def _route_fact(
    mode: str,
    duration_minutes: int,
    observed_at: datetime,
) -> InternalRouteModeFact:
    return InternalRouteModeFact(
        mode=mode,
        status="AVAILABLE",
        duration_minutes=duration_minutes,
        distance_meters=duration_minutes * 80,
        transfer_count=0,
        response_hash="a" * 64,
        request_hash="b" * 64,
        provider_binding={"execution_mode": "public_fixture"},
        external_call_count=0,
        observed_at=observed_at,
        expires_at=observed_at + timedelta(hours=1),
    )


def _route_mode_rule_holds(observed_at: datetime) -> bool:
    walking_at_boundary = _route_fact("walking", 20, observed_at)
    transit_at_boundary = _route_fact("transit", 10, observed_at)
    walking_too_slow = _route_fact("walking", 21, observed_at)
    return (
        choose_route_mode(walking_at_boundary, transit_at_boundary) == "walking"
        and choose_route_mode(walking_too_slow, transit_at_boundary) == "transit"
    )


async def _partial_provider_facts() -> set[str]:
    source_text = "北京一日游。Day 1 去故宫博物院，再去未知地点甲。"

    class FixedTwoPlaceProvider:
        async def propose(self, value: str) -> InferenceProposal:
            mentions = []
            for index, name in enumerate(("故宫博物院", "未知地点甲")):
                start = value.index(name)
                mentions.append(
                    ProposedMention(
                        mention_id=f"mention-{index + 1}",
                        raw_text=name,
                        span_start=start,
                        span_end=start + len(name),
                        role=ActivityRole.PLANNED,
                        day_index=1,
                        sequence_index=index,
                        atomic_place_name=name,
                        category_hint="景点",
                    )
                )
            return InferenceProposal(
                source_hash=hashlib.sha256(value.encode("utf-8")).hexdigest(),
                destination_name="北京",
                destination_basis=DestinationBasis.EXPLICIT,
                mentions=mentions,
                binding={"provider": "public_fixture", "external_calls": 0},
            )

    class PartiallyUnavailableResolver:
        def __init__(self) -> None:
            self.delegate = ControlledSnapshotPlaceResolver()

        async def resolve(
            self,
            *,
            city: str,
            atomic_place_name: str,
            category_hint: str | None = None,
        ) -> Any:
            if atomic_place_name == "未知地点甲":
                raise PlaceProviderUnavailableError(
                    "PUBLIC_FIXTURE_UNAVAILABLE",
                    provider_binding={"external_calls": 0},
                    external_call_count=0,
                )
            return await self.delegate.resolve(
                city=city,
                atomic_place_name=atomic_place_name,
                category_hint=category_hint,
            )

    class UnavailableInferenceProvider:
        def __init__(self, category: str) -> None:
            self.category = category

        async def propose(self, _value: str) -> InferenceProposal:
            raise InferenceProviderUnavailableError(
                self.category,
                provider_binding={"execution_mode": "public_fixture"},
                external_call_count=0,
            )

    async def run_inference_failure(category: str) -> Any:
        inference_pipeline = TripUnderstandingPipeline(
            ResilientStructuredInferenceProvider(
                UnavailableInferenceProvider(category), FixedTwoPlaceProvider()
            ),
            ControlledSnapshotPlaceResolver(),
        )
        try:
            return await inference_pipeline.run(source_text)
        finally:
            await inference_pipeline.aclose()

    inference_outputs = [
        await run_inference_failure("PUBLIC_FIXTURE_UNAVAILABLE"),
        await run_inference_failure("UNKNOWN"),
    ]
    place_pipeline = TripUnderstandingPipeline(
        FixedTwoPlaceProvider(),
        PartiallyUnavailableResolver(),
    )
    try:
        place_output = await place_pipeline.run(source_text)
    finally:
        await place_pipeline.aclose()

    place_statuses = [
        card.status for day in place_output.public_result.days for card in day.activities
    ]
    inference_partial = all(
        output.inference_binding.get("fallback_used") is True
        and output.public_result.status == "PARTIAL_RESULT"
        and "READY"
        in [card.status for day in output.public_result.days for card in day.activities]
        and "NEEDS_CONFIRMATION"
        in [card.status for day in output.public_result.days for card in day.activities]
        for output in inference_outputs
    )
    place_partial = (
        place_output.public_result.status == "PARTIAL_RESULT"
        and "READY" in place_statuses
        and "NEEDS_CONFIRMATION" in place_statuses
    )

    observed_at = datetime(2026, 9, 4, tzinfo=timezone.utc)
    plan = MapRenderPlan(
        understanding_id="g07-provider-partial",
        plan_ref=PlanRevisionRef(
            kind="UNDERSTANDING",
            aggregate_id="g07-provider-partial",
            revision=1,
            stop_set_hash="c" * 64,
        ),
        route_config_hash="d" * 64,
        stops=[
            MapStop(
                day_index=1,
                day_label="Day 1",
                sequence_index=0,
                name="故宫博物院",
                canonical_place_id="fixture-palace",
                resolution_status="AUTO_MATCHED",
                city="北京",
                longitude=116.397,
                latitude=39.918,
            ),
            MapStop(
                day_index=1,
                day_label="Day 1",
                sequence_index=1,
                name="景山公园",
                canonical_place_id="fixture-jingshan",
                resolution_status="AUTO_MATCHED",
                city="北京",
                longitude=116.396,
                latitude=39.925,
            ),
        ],
    )

    class PartialRouteProvider:
        async def route(
            self,
            _origin: MapStop,
            _destination: MapStop,
            mode: str,
            *,
            observed_at: datetime,
        ) -> InternalRouteModeFact:
            if mode == "transit":
                raise RouteProviderUnavailableError(
                    "PUBLIC_FIXTURE_UNAVAILABLE",
                    provider_binding={"execution_mode": "public_fixture"},
                    external_call_count=0,
                )
            return _route_fact("walking", 12, observed_at)

    class UnavailableRouteProvider:
        def __init__(self, category: str) -> None:
            self.category = category

        async def route(
            self,
            _origin: MapStop,
            _destination: MapStop,
            _mode: str,
            *,
            observed_at: datetime,
        ) -> InternalRouteModeFact:
            del observed_at
            raise RouteProviderUnavailableError(
                self.category,
                provider_binding={"execution_mode": "public_fixture"},
                external_call_count=0,
            )

    partial_map = await MapRenderer(PartialRouteProvider()).render(
        plan, observed_at=observed_at
    )
    unavailable_map = await MapRenderer(
        UnavailableRouteProvider("PUBLIC_FIXTURE_UNAVAILABLE")
    ).render(
        plan, observed_at=observed_at
    )
    unknown_map = await MapRenderer(UnavailableRouteProvider("UNKNOWN")).render(
        plan, observed_at=observed_at
    )

    public_repository = InMemoryTripUnderstandingRepository()
    public_service = TripUnderstandingApplicationService(public_repository)
    public_resource, _ = await _complete_demo(
        public_repository,
        public_service,
        capability="f" * 64,
        idempotency_key="g07-provider-public-map",
        now=observed_at,
    )
    public_map_job = await public_repository.claim_next_map(
        worker_id="g07-provider-public-map",
        now=observed_at + timedelta(seconds=2),
        lease_seconds=30,
    )
    if public_map_job is None:
        raise RuntimeError("public provider map job was not queued")
    public_map_output = await MapRenderer(
        UnavailableRouteProvider("UNKNOWN")
    ).render(
        await public_repository.load_map_plan(public_map_job),
        observed_at=observed_at + timedelta(seconds=2),
    )
    await public_repository.complete_map_job(
        public_map_job,
        public_map_output,
        now=observed_at + timedelta(seconds=3),
    )
    public_map = await public_repository.get_map_view(
        public_resource,
        now=observed_at + timedelta(seconds=4),
    )
    edge = partial_map.edges[0]
    route_partial = (
        edge.walking.status == "AVAILABLE"
        and edge.transit.status == "UNAVAILABLE"
        and edge.selected_mode == "walking"
    )
    facts: set[str] = set()
    if inference_partial and place_partial and route_partial:
        facts.add("PARTIAL_SUCCESS_PRESERVED")
    if (
        all(
            output.public_result.status == "PARTIAL_RESULT"
            for output in inference_outputs
        )
        and place_output.public_result.status == "PARTIAL_RESULT"
        and unavailable_map.status == "UNAVAILABLE"
        and all(edge.selected_mode is None for edge in unavailable_map.edges)
        and unknown_map.status == "UNAVAILABLE"
        and all(edge.selected_mode is None for edge in unknown_map.edges)
        and public_map.status in {"LIMITED", "UNAVAILABLE"}
    ):
        facts.add("UNAVAILABLE_NOT_SUCCESS")
    return facts


async def _complete_public_boundary_demo(
    repository: InMemoryTripUnderstandingRepository,
) -> None:
    class SentinelInferenceProvider:
        async def propose(self, source_text: str) -> InferenceProposal:
            proposal = await FixedBeijingDemoInferenceProvider().propose(source_text)
            return proposal.model_copy(
                update={
                    "binding": {
                        **proposal.binding,
                        "private_model": _MODEL_SENTINEL,
                    }
                }
            )

    class SentinelPlaceResolver:
        def __init__(self) -> None:
            self.delegate = FixedBeijingPlaceResolver()

        async def resolve(
            self,
            *,
            city: str,
            atomic_place_name: str,
            category_hint: str | None = None,
        ) -> ResolvedPlace | None:
            place = await self.delegate.resolve(
                city=city,
                atomic_place_name=atomic_place_name,
                category_hint=category_hint,
            )
            if place is None:
                return None
            return place.model_copy(
                update={
                    "provider_binding": {
                        **place.provider_binding,
                        "private_provider": _PROVIDER_SENTINEL,
                    }
                }
            )

    now = datetime.now(timezone.utc)
    job = await repository.claim_next(
        worker_id="g07-public-boundary",
        now=now,
        lease_seconds=30,
    )
    if job is None:
        raise RuntimeError("public boundary fixture job was not queued")
    source = await repository.load_source(job, now=now)
    pipeline = TripUnderstandingPipeline(
        SentinelInferenceProvider(),
        SentinelPlaceResolver(),
    )
    try:
        output = await pipeline.run(source.text)
    finally:
        await pipeline.aclose()
    if (
        output.inference_binding.get("private_model") != _MODEL_SENTINEL
        or not any(
            item.place is not None
            and item.place.provider_binding.get("private_provider")
            == _PROVIDER_SENTINEL
            for item in output.activities
        )
    ):
        raise RuntimeError("public boundary private sentinels were not injected")
    await repository.complete_job(
        job,
        output,
        now=datetime.now(timezone.utc),
    )


def _anonymous_boundary_facts() -> set[str]:
    repository = InMemoryTripUnderstandingRepository()
    api = FastAPI()
    api.include_router(trip_understandings_v3.router, prefix="/api")
    api.dependency_overrides[
        trip_understandings_v3.get_trip_understanding_repository
    ] = lambda: repository
    facts: set[str] = set()
    with TestClient(api) as client:
        created = client.post(
            "/api/v3/trip-understandings",
            headers={"Idempotency-Key": "g07-public-boundary"},
            json={"mode": "DEMO"},
        )
        resource_id = created.json()["public_resource_id"]
        cookie = created.headers.get("set-cookie", "")
        if "HttpOnly" in cookie and resource_id not in cookie:
            facts.add("HTTPONLY_COOKIE")
        asyncio.run(_complete_public_boundary_demo(repository))
        result = client.get(created.json()["result_url"])
        if result.status_code != 200:
            raise RuntimeError("public result endpoint did not return a completed result")
        serialized_result = json.dumps(result.json(), ensure_ascii=False).casefold()
        private_sentinels = (
            DEMO_SOURCE_TEXT.casefold(),
            "https://ticket.dpm.org.cn/",
            _MODEL_SENTINEL,
            _PROVIDER_SENTINEL,
        )
        if _public_payload_is_redacted(result.json()) and not any(
            sentinel in serialized_result for sentinel in private_sentinels
        ):
            facts.add("PUBLIC_FIELDS_REDACTED")
        etag = result.headers.get("etag", "")
        if (
            etag.startswith('"tu3_')
            and resource_id not in etag
            and "revision" not in etag.casefold()
            and "hash" not in etag.casefold()
        ):
            facts.add("OPAQUE_ETAG")
        path = f"/api/v3/trip-understandings/{resource_id}/result?x=1"
        redacted = redact_trip_understanding_path(path)
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=0,
            msg="GET %s",
            args=(path,),
            exc_info=None,
        )
        TripUnderstandingAccessLogFilter().filter(record)
        filtered_message = record.getMessage()
        if (
            resource_id not in redacted
            and resource_id not in filtered_message
            and "{public_resource_id}" in filtered_message
        ):
            facts.add("ACCESS_PATH_REDACTED")
    return facts


async def _source_deletion_facts() -> set[str]:
    repository = InMemoryTripUnderstandingRepository()
    service = TripUnderstandingApplicationService(repository)
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    source_text = "北京一日游。Day 1 去故宫博物院和景山公园。"
    body = CreateFullRequest.model_validate(
        {
            "mode": "FULL",
            "source": {
                "type": "TEXT",
                "text": source_text,
            },
        }
    )
    created = await service.create_full(
        body,
        owner_user_id="g07-source-owner",
        idempotency_key="g07-source-create",
        now=now,
    )
    await TripUnderstandingWorker(repository).run_once("g07-source-worker", now=now)
    resource = await service.authorize(
        created.accepted.public_resource_id,
        capability_hash=None,
        user_id="g07-source-owner",
        now=now + timedelta(seconds=1),
    )
    before = await repository.get_result(resource)
    await service.delete_source(
        resource,
        user_id="g07-source-owner",
        idempotency_key="g07-source-delete",
        now=now + timedelta(seconds=2),
    )
    after = await repository.get_result(resource)
    facts: set[str] = set()
    retained_state = repr(vars(repository))
    if not repository.sources and source_text not in retained_state:
        facts.add("SOURCE_DELETED")
    if before is not None and after is not None and before.result == after.result:
        facts.add("CARDS_SURVIVE_SOURCE_DELETE")
    return facts


async def _resource_deletion_facts() -> set[str]:
    facts: set[str] = set()
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    repository = InMemoryTripUnderstandingRepository()
    service = TripUnderstandingApplicationService(repository)
    created = await service.create_demo(
        capability_hash="a" * 64,
        idempotency_key="g07-trip-delete-create",
        now=now,
    )
    await TripUnderstandingWorker(repository).run_once("g07-trip-delete-worker", now=now)
    resource = await service.authorize(
        created.accepted.public_resource_id,
        capability_hash="a" * 64,
        now=now + timedelta(seconds=1),
    )
    await service.delete_trip(
        resource,
        capability_hash="a" * 64,
        user_id=None,
        idempotency_key="g07-trip-delete",
        now=now + timedelta(seconds=2),
    )
    try:
        await service.authorize(
            created.accepted.public_resource_id,
            capability_hash="a" * 64,
            now=now + timedelta(seconds=3),
        )
    except (ResourceAccessDeniedError, ResourceGoneError):
        facts.add("TRIP_DELETE_REVOKES_RESOURCE")

    account_repository = InMemoryTripUnderstandingRepository()
    account_service = TripUnderstandingApplicationService(account_repository)
    account_body = CreateFullRequest.model_validate(
        {
            "mode": "FULL",
            "source": {"type": "TEXT", "text": "北京一日游。Day 1 去故宫博物院。"},
        }
    )
    account_created = await account_service.create_full(
        account_body,
        owner_user_id="g07-account-owner",
        idempotency_key="g07-account-create",
        now=now,
    )
    await TripUnderstandingWorker(account_repository).run_once(
        "g07-account-worker", now=now
    )
    account_resource = await account_service.authorize(
        account_created.accepted.public_resource_id,
        capability_hash=None,
        user_id="g07-account-owner",
        now=now + timedelta(seconds=1),
    )
    account_stored = await account_repository.get_result(account_resource)
    if account_stored is None:
        raise RuntimeError("account result was not stored")
    share, _replayed = await account_repository.create_share(
        account_resource,
        "g07-account-owner",
        account_stored.result,
        idempotency_key="g07-account-share",
        expires_in_days=1,
        signing_key="public-fixture-signing-key",
        now=now + timedelta(seconds=1),
    )
    share_path, share_secret = share.share_url.split("#s=", maxsplit=1)
    share_ref = share_path.rsplit("/", maxsplit=1)[-1]
    share_session = await account_repository.exchange_share_secret(
        share_ref, share_secret, now=now + timedelta(seconds=1)
    )
    await account_repository.read_share(
        share_ref,
        share_session.capability,
        now=now + timedelta(seconds=1),
    )
    deletion = await account_service.delete_account_travel_data(
        user_id="g07-account-owner",
        idempotency_key="g07-account-delete",
        now=now + timedelta(seconds=2),
    )
    share_revoked = False
    try:
        await account_repository.read_share(
            share_ref,
            share_session.capability,
            now=now + timedelta(seconds=3),
        )
    except ResourceNotFoundError:
        share_revoked = True
    try:
        await account_service.authorize(
            account_created.accepted.public_resource_id,
            capability_hash=None,
            user_id="g07-account-owner",
            now=now + timedelta(seconds=3),
        )
    except (ResourceAccessDeniedError, ResourceGoneError):
        if (
            deletion.view.status == "COMPLETED"
            and not account_repository.resources
            and not account_repository.results
            and share_revoked
        ):
            facts.add("ACCOUNT_DELETE_REVOKES_RESOURCES")
    return facts


async def _neutral_failure_facts() -> tuple[set[str], list[str]]:
    class UnavailableResolver:
        async def resolve(self, **_kwargs: object) -> Any:
            raise PlaceProviderUnavailableError(
                "PRIVATE_PROVIDER_FAILURE",
                provider_binding={"private_provider": "must-not-leak"},
                external_call_count=0,
            )

    pipeline = build_full_text_pipeline(place_resolver=UnavailableResolver())
    try:
        output = await pipeline.run("北京一日游。Day 1 去故宫博物院。")
    finally:
        await pipeline.aclose()
    payload = output.public_result.model_dump(mode="json")
    serialized = json.dumps(payload, ensure_ascii=False).casefold()
    cards = [card for day in output.public_result.days for card in day.activities]
    forbidden = ("private_provider", "must-not-leak", "traceback", "stack", "receipt")
    facts: set[str] = set()
    if (
        output.public_result.status == "PARTIAL_RESULT"
        and cards
        and all(card.status == "NEEDS_CONFIRMATION" for card in cards)
        and not any(marker in serialized for marker in forbidden)
    ):
        facts.add("NEUTRAL_REDACTED_FAILURE_COPY")
    return facts, ["UI_COLOR_E2E_NOT_RUN"]


async def _text_first_flow_facts() -> tuple[set[str], list[str]]:
    repository = InMemoryTripUnderstandingRepository()
    service = TripUnderstandingApplicationService(repository)
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    body = CreateFullRequest.model_validate(
        {
            "mode": "FULL",
            "source": {"type": "TEXT", "text": "上海两日游。Day 1 去外滩。Day 2 去豫园。"},
        }
    )
    facts: set[str] = set()
    if body.source.type == "TEXT":
        facts.add("TEXT_ONLY_ACCEPTED")
    created = await service.create_full(
        body,
        owner_user_id="g07-text-owner",
        idempotency_key="g07-text-create",
        now=now,
    )
    await TripUnderstandingWorker(repository).run_once("g07-text-worker", now=now)
    resource = await service.authorize(
        created.accepted.public_resource_id,
        capability_hash=None,
        user_id="g07-text-owner",
        now=now + timedelta(seconds=1),
    )
    stored = await repository.get_result(resource)
    if stored is None:
        return facts, ["HOME_AND_MOBILE_E2E_NOT_RUN"]
    if stored.result.assumptions and all(item.editable for item in stored.result.assumptions):
        facts.add("EDITABLE_SOFT_ASSUMPTIONS")
    cards = [card for day in stored.result.days for card in day.activities]
    if cards and stored.result.map.status == "PREPARING":
        facts.add("CARDS_BEFORE_MAP")
    effects_before = repository.map_provider_effect_count
    await service.apply_command(
        resource,
        ActivityTextEditCommand(
            command_type="ACTIVITY_TEXT_EDIT",
            activity_token=cards[0].activity_token,
            time_hint="上午",
        ),
        expected_etag=stored.opaque_etag,
        idempotency_key="g07-text-edit",
        now=now + timedelta(seconds=2),
    )
    updated = await repository.get_result(resource)
    if (
        updated is not None
        and updated.result.map.status == "NEEDS_UPDATE"
        and repository.map_provider_effect_count == effects_before
    ):
        facts.add("MANUAL_MAP_UPDATE_ONLY")
    return facts, ["HOME_AND_MOBILE_E2E_NOT_RUN"]


async def _public_facts() -> dict[str, tuple[set[str], list[str]]]:
    neutral_facts, neutral_degradations = await _neutral_failure_facts()
    text_facts, text_degradations = await _text_first_flow_facts()
    anonymous_facts = await asyncio.to_thread(_anonymous_boundary_facts)
    return {
        "PUBLIC_PROJECTION": (
            {"PUBLIC_FIELDS_REDACTED"}
            if "PUBLIC_FIELDS_REDACTED" in anonymous_facts
            else set(),
            [],
        ),
        "ANONYMOUS_BOUNDARY": (anonymous_facts, []),
        "SOURCE_DELETION": (await _source_deletion_facts(), []),
        "RESOURCE_DELETION": (await _resource_deletion_facts(), []),
        "NEUTRAL_FAILURE": (neutral_facts, neutral_degradations),
        "TEXT_FIRST_FLOW": (text_facts, text_degradations),
    }


async def _evaluate_async(payload: dict[str, Any]) -> list[dict[str, Any]]:
    map_facts: set[str] | None = None
    map_error: str | None = None
    public_facts: dict[str, tuple[set[str], list[str]]] | None = None
    public_error: str | None = None
    try:
        map_facts = await _map_revision_facts()
    except Exception as exc:  # keep every case visible when one shared probe fails
        map_error = f"PROBE_EXCEPTION_{type(exc).__name__.upper()}"
    try:
        public_facts = await _public_facts()
    except Exception as exc:  # keep every case visible when one shared probe fails
        public_error = f"PROBE_EXCEPTION_{type(exc).__name__.upper()}"
    results: list[dict[str, Any]] = []
    for case in payload["cases"]:
        try:
            kind = case["probe"]["kind"]
            if kind == "PIPELINE":
                observation = await _run_pipeline_case(case)
            elif kind == "PLACE_SAFETY":
                observation = await _run_place_safety_case(case)
            else:
                observation = _empty_observation()
                if kind == "MAP_REVISION_PROVIDER":
                    if map_error is not None or map_facts is None:
                        raise RuntimeError(map_error or "MAP_PROBE_NOT_RUN")
                    observation["facts"] = set(map_facts)
                elif kind == "PUBLIC_PRIVACY_UX":
                    if public_error is not None or public_facts is None:
                        raise RuntimeError(public_error or "PUBLIC_PROBE_NOT_RUN")
                    facts, degradations = public_facts[case["probe"]["variant"]]
                    observation["facts"] = set(facts)
                    observation["degradations"] = list(degradations)
                else:
                    raise ValueError("unsupported probe kind")
            results.append(_score_case(case, observation))
        except Exception as exc:  # fail closed without exposing source or provider details
            results.append(
                {
                    "case_id": case["case_id"],
                    "group": case["group"],
                    "status": "EVALUATION_ERROR",
                    "failure_codes": [f"PROBE_EXCEPTION_{type(exc).__name__.upper()}"],
                    "degradation_codes": [],
                }
            )
    return results


def run_evaluation() -> dict[str, Any]:
    payload = load_cases()
    case_results = asyncio.run(_evaluate_async(payload))
    counts = Counter(item["status"] for item in case_results)
    exact_pass_count = counts["EXACT_PASS"]
    safe_degrade_count = counts["SAFE_DEGRADE"]
    safe_pass_count = exact_pass_count + safe_degrade_count
    hard_safety_failure_count = counts["DANGEROUS_FAIL"]
    evaluation_error_count = counts["EVALUATION_ERROR"]
    if sum(counts.values()) != 40:
        raise RuntimeError("runner must produce exactly one result for every frozen case")
    if (
        safe_pass_count == 40
        and hard_safety_failure_count == 0
        and evaluation_error_count == 0
    ):
        if exact_pass_count == 40:
            text_compatibility_level = "TEXT_COMPAT_R2"
        elif exact_pass_count >= 36:
            text_compatibility_level = "TEXT_COMPAT_R1"
        elif exact_pass_count >= 28:
            text_compatibility_level = "TEXT_COMPAT_R0"
        else:
            text_compatibility_level = "NOT_MET"
    else:
        text_compatibility_level = "NOT_MET"
    degradation_list = [
        {
            "case_id": item["case_id"],
            "codes": item["degradation_codes"],
        }
        for item in case_results
        if item["status"] == "SAFE_DEGRADE"
    ]
    return {
        "schema_version": "g07-text-convergence-result-v1",
        "dataset_version": payload["dataset_version"],
        "dataset_sha256": _sha256_file(CASES_PATH),
        "schema_sha256": _sha256_file(SCHEMA_PATH),
        "public_non_blind": True,
        "case_count": 40,
        "case_results": case_results,
        "exact_pass_count": exact_pass_count,
        "safe_degrade_count": safe_degrade_count,
        "safe_pass_count": safe_pass_count,
        "hard_safety_failure_count": hard_safety_failure_count,
        "evaluation_error_count": evaluation_error_count,
        "text_compatibility_level": text_compatibility_level,
        "degradation_list": degradation_list,
        "overall_delivery_level": "NOT_RUN",
    }


def main() -> int:
    result = run_evaluation()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return int(
        result["hard_safety_failure_count"] > 0
        or result["evaluation_error_count"] > 0
        or result["safe_pass_count"] < 40
        or result["exact_pass_count"] < 28
    )


if __name__ == "__main__":
    raise SystemExit(main())
