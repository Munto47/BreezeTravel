from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "frozen-suggestion-ranking-oracle-v1"
RUBRIC_VERSION = "deterministic-route-category-receipt-v1"
LABEL_AUTHORITY = "AUTOMATED_DETERMINISTIC_PROXY_NOT_HUMAN"
_FORBIDDEN_PRODUCT_FIELDS = {
    "rank_position",
    "score_components",
    "total_score",
    "classification",
    "explanation_codes",
    "source_prior_refs",
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def _require_hash(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(code)
    return value


def _route_component(route: Mapping[str, Any]) -> tuple[int, str]:
    if route.get("status") != "AVAILABLE":
        return 0, "ROUTE_UNKNOWN_OR_UNAVAILABLE"
    minutes = route.get("previous_to_candidate_minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
        raise ValueError("ORACLE_ROUTE_MINUTES_INVALID")
    if minutes <= 15:
        return 3, "ON_ROUTE_LE_15_MIN"
    if minutes <= 30:
        return 2, "ACCEPTABLE_DETOUR_LE_30_MIN"
    if minutes <= 60:
        return 1, "REMOTE_31_TO_60_MIN"
    return 0, "REMOTE_OVER_60_MIN"


def _intent_component(
    category: str,
    intents: list[str],
    route_minutes: int | None,
) -> tuple[int, str]:
    if "FOOD" in intents and category == "food":
        return 1, "FOOD_INTENT_CATEGORY_MATCH"
    if "FUN" in intents and category == "attraction":
        return 1, "FUN_INTENT_CATEGORY_MATCH"
    if "NEARBY" in intents and route_minutes is not None and route_minutes <= 15:
        return 1, "NEARBY_INTENT_ROUTE_MATCH"
    return 0, "INTENT_CATEGORY_SUBJECTIVE_OR_NOT_ESTABLISHED"


def _receipt_refs(candidate: Mapping[str, Any], anchor: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    place = candidate.get("canonical_place")
    entity = candidate.get("provider_receipt")
    route = candidate.get("route_times")
    if not isinstance(place, dict) or not isinstance(entity, dict) or not isinstance(route, dict):
        raise ValueError("ORACLE_CANDIDATE_RECEIPTS_MISSING")
    place_id = place.get("place_id")
    if (
        entity.get("canonical_place_id") != place_id
        or entity.get("provider_place_id") != place_id
        or entity.get("city") != place.get("city")
        or entity.get("name") != place.get("name")
    ):
        raise ValueError("ORACLE_ENTITY_RECEIPT_IDENTITY_MISMATCH")
    _require_hash(entity.get("request_hash"), "ORACLE_ENTITY_REQUEST_HASH_INVALID")
    _require_hash(entity.get("response_hash"), "ORACLE_ENTITY_RESPONSE_HASH_INVALID")
    entity_ref = {
        "canonical_place_id": place_id,
        "provider": entity.get("provider"),
        "request_hash": entity["request_hash"],
        "response_hash": entity["response_hash"],
        "observed_at": entity.get("observed_at"),
        "execution_mode": entity.get("execution_mode"),
        "source_url": entity.get("source_url"),
        "receipt_sha256": _sha256(entity),
    }

    route_refs: list[dict[str, Any]] = []
    for item in route.get("route_receipts", []):
        if not isinstance(item, dict):
            raise ValueError("ORACLE_ROUTE_RECEIPT_INVALID")
        _require_hash(item.get("request_hash"), "ORACLE_ROUTE_REQUEST_HASH_INVALID")
        _require_hash(item.get("response_hash"), "ORACLE_ROUTE_RESPONSE_HASH_INVALID")
        if (
            item.get("origin_place_id") != anchor.get("place_id")
            or item.get("destination_place_id") != place_id
            or item.get("duration_minutes") != route.get("previous_to_candidate_minutes")
        ):
            raise ValueError("ORACLE_ROUTE_RECEIPT_ENDPOINT_OR_DURATION_MISMATCH")
        route_refs.append({
            "leg": item.get("leg"),
            "origin_place_id": item.get("origin_place_id"),
            "destination_place_id": item.get("destination_place_id"),
            "duration_minutes": item.get("duration_minutes"),
            "provider": item.get("provider"),
            "request_hash": item["request_hash"],
            "response_hash": item["response_hash"],
            "snapshot_id": item.get("snapshot_id"),
            "observed_at": item.get("observed_at"),
            "receipt_sha256": _sha256(item),
        })
    if route.get("status") == "AVAILABLE" and not route_refs:
        raise ValueError("ORACLE_AVAILABLE_ROUTE_RECEIPT_MISSING")

    current_refs: list[dict[str, Any]] = []
    operational = candidate.get("operational_evidence")
    if not isinstance(operational, dict):
        raise ValueError("ORACLE_CURRENT_FACT_CONTAINER_MISSING")
    for fact in operational.get("facts", []):
        if not isinstance(fact, dict):
            raise ValueError("ORACLE_CURRENT_FACT_RECEIPT_INVALID")
        _require_hash(fact.get("request_hash"), "ORACLE_CURRENT_FACT_REQUEST_HASH_INVALID")
        _require_hash(fact.get("response_hash"), "ORACLE_CURRENT_FACT_RESPONSE_HASH_INVALID")
        if fact["request_hash"] != entity["request_hash"] or fact["response_hash"] != entity["response_hash"]:
            raise ValueError("ORACLE_CURRENT_FACT_ENTITY_RECEIPT_MISMATCH")
        current_refs.append({
            "fact_type": fact.get("fact_type"),
            "provider": fact.get("provider"),
            "observed_at": fact.get("observed_at"),
            "request_hash": fact["request_hash"],
            "response_hash": fact["response_hash"],
            "source_url": fact.get("source_url"),
            "fact_receipt_sha256": _sha256(fact),
            "rubric_use": "HARD_CHECK_ONLY_IF_QUERY_CONTEXT_ESTABLISHES_APPLICABILITY",
        })
    return entity_ref, route_refs, current_refs


def _grade_candidate(
    candidate: Mapping[str, Any],
    *,
    city: str,
    anchor: Mapping[str, Any],
    seen_ids: set[str],
) -> dict[str, Any]:
    polluted = sorted(_FORBIDDEN_PRODUCT_FIELDS & set(candidate))
    if polluted:
        raise ValueError(f"ORACLE_PRODUCT_RANKING_FIELD_POLLUTION:{','.join(polluted)}")
    place = candidate.get("canonical_place")
    intents = candidate.get("matched_intents")
    route = candidate.get("route_times")
    if not isinstance(place, dict) or not isinstance(intents, list) or not isinstance(route, dict):
        raise ValueError("ORACLE_CANDIDATE_SHAPE_INVALID")
    place_id = place.get("place_id")
    if not isinstance(place_id, str) or not place_id:
        raise ValueError("ORACLE_CANONICAL_ID_MISSING")
    wrong_city = place.get("city") != city
    duplicate = place_id == anchor.get("place_id") or place_id in seen_ids
    seen_ids.add(place_id)
    hard_codes = candidate.get("hard_block_codes", [])
    if not isinstance(hard_codes, list):
        raise ValueError("ORACLE_HARD_CODES_INVALID")
    route_unknown = route.get("status") != "AVAILABLE"
    entity_ref, route_refs, current_refs = _receipt_refs(candidate, anchor)
    route_points, route_reason = _route_component(route)
    minutes = route.get("previous_to_candidate_minutes") if route.get("status") == "AVAILABLE" else None
    intent_points, intent_reason = _intent_component(str(place.get("category")), intents, minutes)
    receipt_points = int(bool(route_refs) and bool(entity_ref))
    exclusion_codes: list[str] = []
    if wrong_city:
        exclusion_codes.append("WRONG_CITY")
    if duplicate:
        exclusion_codes.append("CANONICAL_OR_ANCHOR_DUPLICATE")
    if hard_codes:
        exclusion_codes.extend(f"HARD:{code}" for code in hard_codes)
    if route_unknown:
        exclusion_codes.append("ROUTE_UNKNOWN")
    grade = 0 if exclusion_codes else route_points + intent_points + receipt_points
    return {
        "canonical_candidate_id": place_id,
        "canonical_name": place.get("name"),
        "city": place.get("city"),
        "category": place.get("category"),
        "matched_intents": intents,
        "relevance_grade": grade,
        "relevant_at_4": grade >= 4,
        "grade_components": {
            "route_suitability": {"points": route_points, "reason_code": route_reason},
            "intent_category_coverage": {"points": intent_points, "reason_code": intent_reason},
            "receipt_completeness": {"points": receipt_points},
        },
        "eligibility_checks": {
            "wrong_city": wrong_city,
            "canonical_or_anchor_duplicate": duplicate,
            "explicit_hard_block_codes": hard_codes,
            "route_status": route.get("status"),
            "exclusion_codes": exclusion_codes,
        },
        "subjective_boundaries": {
            "human_preference_fit": "UNKNOWN",
            "current_popularity_quality": "UNKNOWN",
            "opening_fit_without_visit_slot": "N_A",
            "reservation_fit_without_requirement": "N_A",
            "accessibility_fit_without_member_requirement": "N_A",
            "community_or_official_prior_used_as_current_fact": False,
        },
        "entity_receipt_ref": entity_ref,
        "route_receipt_refs": route_refs,
        "current_fact_receipt_refs": current_refs,
        "source_candidate_projection_sha256": _sha256(candidate),
    }


def build_oracle(snapshot: Mapping[str, Any], *, source_path: str, source_sha256: str) -> dict[str, Any]:
    _require_hash(source_sha256, "ORACLE_SOURCE_SHA256_INVALID")
    schema_version = snapshot.get("schema_version")
    if (
        schema_version not in {"1.0", "1.1"}
        or snapshot.get("evidence_class") != "real_provider_local_authorized"
    ):
        raise ValueError("ORACLE_SOURCE_SNAPSHOT_UNSUPPORTED")
    source_rows: list[Mapping[str, Any]] = []
    if schema_version == "1.0":
        source_rows = [row for row in snapshot.get("cities", []) if isinstance(row, dict)]
    else:
        if snapshot.get("overall_status") != "PASSED" or snapshot.get("capture_status") != "COMPLETE":
            raise ValueError("ORACLE_CHAIN_CAPTURE_NOT_PASSED")
        for city_row in snapshot.get("cities", []):
            rounds = city_row.get("rounds") if isinstance(city_row, dict) else None
            if (
                not isinstance(rounds, list)
                or len(rounds) != 3
                or city_row.get("chain_status") != "COMPLETE"
                or any(not isinstance(item, dict) or item.get("status") != "COMPLETE" for item in rounds)
            ):
                raise ValueError("ORACLE_CHAIN_CAPTURE_INCOMPLETE")
            # Ranking quality is evaluated at the fixed initial Anchor.  G2's
            # deterministic product checks independently cover all three
            # visible sets, their Top-3 safety, acceptance and Anchor advance.
            source_rows.append({"city": city_row.get("city"), **rounds[0]})
    cities: list[dict[str, Any]] = []
    for row in source_rows:
        if not isinstance(row, dict) or not isinstance(row.get("anchor"), dict):
            raise ValueError("ORACLE_CITY_SHAPE_INVALID")
        city = str(row.get("city") or "")
        seen_ids: set[str] = set()
        candidates = [
            _grade_candidate(candidate, city=city, anchor=row["anchor"], seen_ids=seen_ids)
            for candidate in row.get("candidates", [])
            if isinstance(candidate, dict)
        ]
        city_oracle = {
            "city": city,
            "anchor": copy.deepcopy(row["anchor"]),
            "provider_snapshot_id": row.get("provider_snapshot_id"),
            "candidate_universe_size": len(candidates),
            "candidates": candidates,
            "relevant_candidate_ids": [
                item["canonical_candidate_id"] for item in candidates if item["relevant_at_4"]
            ],
        }
        city_oracle["city_oracle_sha256"] = _sha256(city_oracle)
        cities.append(city_oracle)
    source = {
        "snapshot_path": source_path,
        "snapshot_sha256": source_sha256,
        "evidence_class": snapshot.get("evidence_class"),
        "evidence_subtype": snapshot.get("evidence_subtype"),
        "claim_boundary": copy.deepcopy(snapshot.get("claim_boundary")),
    }
    if schema_version == "1.1":
        source["ranking_scope"] = "INITIAL_FIXED_ANCHOR_ONLY"
        source["three_round_product_safety_scored_by"] = "G2_DETERMINISTIC_HTTP_CHECKS"
    content = {
        "artifact_kind": "FROZEN_SUGGESTION_GRADED_RANKING_ORACLE",
        "label_authority": LABEL_AUTHORITY,
        "is_human_label": False,
        "is_release_approval": False,
        "rubric": {
            "rubric_version": RUBRIC_VERSION,
            "grade_range": [0, 5],
            "route_points": {"0-15": 3, "16-30": 2, "31-60": 1, "over-60-or-UNKNOWN": 0},
            "intent_category_points": 1,
            "receipt_completeness_points": 1,
            "forced_zero": ["WRONG_CITY", "CANONICAL_OR_ANCHOR_DUPLICATE", "EXPLICIT_HARD", "ROUTE_UNKNOWN"],
            "relevant_threshold": 4,
            "subjective_policy": "UNKNOWN_OR_N_A_NEVER_INFERRED",
            "product_ranking_fields_read": False,
        },
        "source": source,
        "cities": cities,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "content_sha256": _sha256(content),
        "content": content,
    }


def validate_oracle(
    artifact: Mapping[str, Any],
    *,
    artifact_sha256: str,
    source_snapshot: Mapping[str, Any],
    source_sha256: str,
) -> dict[str, Any]:
    _require_hash(artifact_sha256, "ORACLE_ARTIFACT_SHA256_INVALID")
    if artifact.get("schema_version") != SCHEMA_VERSION or not isinstance(artifact.get("content"), dict):
        raise ValueError("ORACLE_SCHEMA_INVALID")
    if _sha256(artifact) != artifact_sha256:
        raise ValueError("ORACLE_ARTIFACT_HASH_MISMATCH")
    if artifact.get("content_sha256") != _sha256(artifact["content"]):
        raise ValueError("ORACLE_CONTENT_HASH_MISMATCH")
    expected = build_oracle(
        source_snapshot,
        source_path=str(artifact["content"].get("source", {}).get("snapshot_path") or ""),
        source_sha256=source_sha256,
    )
    if expected != artifact:
        raise ValueError("ORACLE_SOURCE_RECOMPUTE_MISMATCH")
    return copy.deepcopy(dict(artifact))


def load_bound_oracle(binding: Mapping[str, Any], repo_root: Path) -> dict[str, Any]:
    oracle_path = binding.get("path")
    source_path = binding.get("source_snapshot_path")
    if not isinstance(oracle_path, str) or not isinstance(source_path, str):
        raise ValueError("GRADED_RANKING_ORACLE_PATHS_REQUIRED")
    oracle_bytes = (repo_root / oracle_path).read_bytes()
    source_bytes = (repo_root / source_path).read_bytes()
    if _sha256(oracle_bytes) != binding.get("sha256"):
        raise ValueError("GRADED_RANKING_ORACLE_FILE_HASH_MISMATCH")
    if _sha256(source_bytes) != binding.get("source_snapshot_sha256"):
        raise ValueError("GRADED_RANKING_ORACLE_SOURCE_HASH_MISMATCH")
    artifact = json.loads(oracle_bytes)
    snapshot = json.loads(source_bytes)
    return validate_oracle(
        artifact,
        # The binding above verifies the exact checked-in bytes.  The inner
        # validator separately verifies the canonical decoded object.
        artifact_sha256=_sha256(artifact),
        source_snapshot=snapshot,
        source_sha256=str(binding["source_snapshot_sha256"]),
    )


def overlay_case_oracles(
    label: Mapping[str, Any],
    artifact: Mapping[str, Any],
    *,
    city: str,
    requested_intents: list[str] | None = None,
) -> dict[str, Any]:
    rows = [row for row in artifact["content"]["cities"] if row.get("city") == city]
    if len(rows) != 1:
        raise ValueError("GRADED_RANKING_ORACLE_CITY_MISSING_OR_DUPLICATE")
    row = rows[0]
    intent_set = set(requested_intents or ["NEARBY", "POPULAR", "FUN", "FOOD"])
    candidates = [
        item
        for item in row["candidates"]
        if intent_set.intersection(item.get("matched_intents", []))
    ]
    if not candidates:
        raise ValueError("GRADED_RANKING_ORACLE_REQUESTED_INTENT_UNIVERSE_EMPTY")
    output = copy.deepcopy(dict(label))
    metric_oracles = output.setdefault("metric_oracles", {})
    metric_oracles["builder_ndcg_at_5"] = {
        "applicability": "APPLICABLE",
        "metric_version": RUBRIC_VERSION,
        "k": 5,
        "identity_key": "canonical_place.place_id",
        "label_authority": LABEL_AUTHORITY,
        "city_oracle_sha256": row["city_oracle_sha256"],
        "relevance_items": [
            {
                "candidate_id": item["canonical_candidate_id"],
                "relevance_grade": item["relevance_grade"],
            }
            for item in candidates
        ],
    }
    metric_oracles["builder_recall_at_5"] = {
        "applicability": "APPLICABLE",
        "metric_version": RUBRIC_VERSION,
        "k": 5,
        "identity_key": "canonical_place.place_id",
        "label_authority": LABEL_AUTHORITY,
        "city_oracle_sha256": row["city_oracle_sha256"],
        "relevant_candidate_ids": [
            item["canonical_candidate_id"] for item in candidates if item["relevant_at_4"]
        ],
    }
    return output


def canonical_builder_actuals(output: Mapping[str, Any]) -> dict[str, Any]:
    rounds = output.get("rounds")
    if not isinstance(rounds, list) or not rounds or not isinstance(rounds[0], dict):
        return {}
    candidates = rounds[0].get("suggestion_set", {}).get("candidates")
    if not isinstance(candidates, list):
        return {}
    ids: list[str] = []
    for candidate in candidates:
        place = candidate.get("canonical_place") if isinstance(candidate, dict) else None
        place_id = place.get("place_id") if isinstance(place, dict) else None
        if not isinstance(place_id, str) or not place_id:
            return {}
        ids.append(place_id)
    return {"ranked_candidate_ids": ids}


def write_generated_oracle(source_path: Path, destination_path: Path, *, repo_root: Path) -> None:
    source_bytes = source_path.read_bytes()
    snapshot = json.loads(source_bytes)
    artifact = build_oracle(
        snapshot,
        source_path=source_path.resolve().relative_to(repo_root.resolve()).as_posix(),
        source_sha256=_sha256(source_bytes),
    )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
