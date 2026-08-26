"""Semantic closure checks for P5 v3 case materializations.

The contract is deliberately independent of a scorer or oracle bundle.  It
proves that every stop produced by the product parser has an explicit,
receipt-bound entity-resolution outcome before a dataset may be sealed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from app.importing.confidence import normalize_place_name
from app.importing.parser import ItineraryTextParser
from app.importing.screenshots import ScreenshotOcrReceipt, itinerary_text_from_ocr_receipts
from app.importing.service import parse_time_range
from evals.trip_check_v1.p5.data_contract import digest


RESOLUTION_OUTCOMES_V3 = {
    "AUTO_RESOLVED",
    "NEEDS_CONFIRMATION",
    "HARD_REJECTED",
    "NO_CANDIDATE",
}
_CANDIDATE_BLOCKED_MODES = {"EMPTY", "MISSING_RECEIPT"}
_CANDIDATE_ORACLE_POLICY_V3 = {
    "VALID": ("REQUIRED", True),
    "EMPTY": ("FORBIDDEN", False),
    "MISSING_RECEIPT": ("FORBIDDEN", False),
    "NOT_APPLICABLE": ("NOT_APPLICABLE", True),
}
_MISSING = object()


def _semantic_field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, _MISSING)
    return getattr(value, name, _MISSING)


def _parser_text(case: Mapping[str, Any], materialization: Mapping[str, Any]) -> str:
    product_input = case.get("product_input")
    if not isinstance(product_input, Mapping):
        raise ValueError("product_input must be an object")
    if case.get("input_kind") == "TEXT":
        value = product_input.get("raw_text")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("text case has no raw_text")
        return value
    receipt_payload = materialization.get("ocr_baseline_receipt")
    if not isinstance(receipt_payload, Mapping):
        raise ValueError("screenshot case has no OCR receipt")
    receipt = ScreenshotOcrReceipt.model_validate(receipt_payload)
    return itinerary_text_from_ocr_receipts([receipt])


def parsed_stop_projection_v3(
    case: Mapping[str, Any], materialization: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Return the stable parser projection used by the semantic seal check."""

    draft = ItineraryTextParser().parse(
        _parser_text(case, materialization),
        import_id=f"semantic-v3-{case.get('case_id', 'unknown')}",
    )
    if draft.errors:
        raise ValueError("product parser rejected or truncated the input")
    trip_days = case.get("trip_days")
    if not isinstance(trip_days, int) or isinstance(trip_days, bool) or not 2 <= trip_days <= 5:
        raise ValueError("trip_days is outside the controlled scope")
    if any(stop.day_index < 0 or stop.day_index >= trip_days for stop in draft.raw_stops):
        raise ValueError("parsed stop is outside trip_days")
    return [
        {
            "ordinal": ordinal,
            "day_index": stop.day_index,
            "raw_name": stop.raw_name,
            "normalized_name": normalize_place_name(stop.raw_name),
            "raw_time": stop.raw_time,
        }
        for ordinal, stop in enumerate(draft.raw_stops)
    ]


def _receipt_map(materialization: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    receipts = materialization.get("receipts")
    if not isinstance(receipts, list):
        return {}
    return {
        str(item.get("receipt_id")): item
        for item in receipts
        if isinstance(item, Mapping) and isinstance(item.get("receipt_id"), str)
    }


def _candidate_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item.get("place_id"))
        for item in value
        if isinstance(item, Mapping) and isinstance(item.get("place_id"), str)
    ]


def validate_case_semantics_v3(
    case: Mapping[str, Any], materialization: Mapping[str, Any]
) -> list[str]:
    """Return fail-closed semantic errors for one label-free materialization."""

    case_id = str(case.get("case_id") or "UNKNOWN_CASE")
    forbidden_case_fields = {"oracle", "oracle_sha256", "expected", "blind_label", "label"}
    present_forbidden = sorted(forbidden_case_fields.intersection(case))
    if present_forbidden:
        return [f"{case_id}: LABEL_FIELDS_FORBIDDEN: {present_forbidden}"]
    errors: list[str] = []
    try:
        parsed = parsed_stop_projection_v3(case, materialization)
    except (TypeError, ValueError) as exc:
        return [f"{case_id}: PARSER_INPUT_INVALID: {exc}"]
    source = materialization.get("source_payload")
    if not isinstance(source, Mapping):
        return [f"{case_id}: SOURCE_PAYLOAD_MISSING"]
    provider_snapshot = materialization.get("provider_snapshot")
    if not isinstance(provider_snapshot, Mapping):
        return [f"{case_id}: PROVIDER_SNAPSHOT_MISSING"]
    for field in ("case_id", "city", "trip_days", "group_size", "input_kind"):
        if case.get(field) != source.get(field):
            errors.append(f"{case_id}: CASE_SOURCE_BINDING_MISMATCH field={field}")
    if (
        case.get("product_input") != source.get("product_input")
        or case.get("normalized_input_sha256") != source.get("normalized_input_sha256")
    ):
        errors.append(f"{case_id}: CASE_SOURCE_INPUT_BINDING_MISMATCH")
    runner_control = case.get("runner_control")
    if not isinstance(runner_control, Mapping) or provider_snapshot.get(
        "runner_control_sha256"
    ) != digest(runner_control):
        errors.append(f"{case_id}: CASE_RUNNER_CONTROL_BINDING_MISMATCH")
    resolutions = source.get("entity_resolutions")
    if not isinstance(resolutions, list):
        return [f"{case_id}: ENTITY_RESOLUTION_CONTRACT_MISSING"]
    if len(resolutions) != len(parsed):
        errors.append(
            f"{case_id}: ENTITY_RESOLUTION_COUNT_MISMATCH "
            f"parsed={len(parsed)} materialized={len(resolutions)}"
        )
    receipts = _receipt_map(materialization)
    auto_place_ids: list[str] = []
    for ordinal, parsed_stop in enumerate(parsed):
        if ordinal >= len(resolutions) or not isinstance(resolutions[ordinal], Mapping):
            errors.append(f"{case_id}: RESOLUTION_ENTRY_MISSING ordinal={ordinal}")
            continue
        resolution = resolutions[ordinal]
        if resolution.get("ordinal") != ordinal:
            errors.append(f"{case_id}: RESOLUTION_ORDINAL_MISMATCH ordinal={ordinal}")
        if resolution.get("day_index") != parsed_stop["day_index"]:
            errors.append(f"{case_id}: RESOLUTION_DAY_MISMATCH ordinal={ordinal}")
        if resolution.get("raw_name") != parsed_stop["raw_name"]:
            errors.append(f"{case_id}: RESOLUTION_RAW_NAME_MISMATCH ordinal={ordinal}")
        if resolution.get("normalized_name") != parsed_stop["normalized_name"]:
            errors.append(f"{case_id}: RESOLUTION_NAME_MISMATCH ordinal={ordinal}")
        outcome = resolution.get("outcome")
        if outcome not in RESOLUTION_OUTCOMES_V3:
            errors.append(f"{case_id}: RESOLUTION_OUTCOME_INVALID ordinal={ordinal}")
            continue
        receipt_id = resolution.get("search_receipt_id")
        receipt = receipts.get(str(receipt_id))
        if (
            receipt is None
            or receipt.get("operation") != "place.search"
            or receipt.get("status") != "SUCCEEDED"
        ):
            errors.append(f"{case_id}: RESOLUTION_SEARCH_RECEIPT_INVALID ordinal={ordinal}")
        candidates = resolution.get("candidates")
        if not isinstance(candidates, list):
            errors.append(f"{case_id}: RESOLUTION_CANDIDATES_INVALID ordinal={ordinal}")
            candidates = []
        candidate_ids = _candidate_ids(candidates)
        selected = resolution.get("selected_place_id")
        city = case.get("city")
        candidate_rows = [item for item in candidates if isinstance(item, Mapping)]
        expected_request = {"query": parsed_stop["raw_name"], "city": city}
        expected_response = {"outcome": outcome, "candidates": candidate_rows}
        if receipt is not None and (
            receipt.get("provider") != "trip-check-p5-controlled-provider-v3"
            or receipt.get("execution_mode") != "fixture"
            or receipt.get("request_hash") != digest(expected_request)
            or receipt.get("response_hash") != digest(expected_response)
            or receipt.get("source_url") != "fixture://trip-check-p5-v3/place.search"
            or receipt.get("affected_fields") != [f"entity_resolutions.{ordinal}"]
        ):
            errors.append(f"{case_id}: RESOLUTION_SEARCH_RECEIPT_BINDING_INVALID ordinal={ordinal}")
        if outcome == "AUTO_RESOLVED":
            if not isinstance(selected, str) or selected not in candidate_ids:
                errors.append(f"{case_id}: AUTO_RESOLUTION_SELECTION_INVALID ordinal={ordinal}")
            elif not any(item.get("place_id") == selected and item.get("city") == city for item in candidate_rows):
                errors.append(f"{case_id}: AUTO_RESOLUTION_CITY_INVALID ordinal={ordinal}")
            else:
                auto_place_ids.append(selected)
            if len(candidate_rows) != 1:
                errors.append(f"{case_id}: AUTO_RESOLUTION_NOT_UNIQUE ordinal={ordinal}")
        elif outcome == "NEEDS_CONFIRMATION":
            if (
                selected is not None
                or len(candidate_rows) < 2
                or len(candidate_ids) != len(set(candidate_ids))
            ):
                errors.append(f"{case_id}: CONFIRMATION_EVIDENCE_INSUFFICIENT ordinal={ordinal}")
        elif outcome == "HARD_REJECTED":
            if selected is not None or not candidate_rows or any(item.get("city") == city for item in candidate_rows):
                errors.append(f"{case_id}: HARD_REJECTION_EVIDENCE_INVALID ordinal={ordinal}")
        else:
            if selected is not None or candidate_rows:
                errors.append(f"{case_id}: NO_CANDIDATE_EVIDENCE_INVALID ordinal={ordinal}")

    source_stops = source.get("stops")
    if not isinstance(source_stops, list):
        errors.append(f"{case_id}: SOURCE_STOPS_INVALID")
        source_stops = []
    source_place_ids = [
        str(item.get("place_id")) for item in source_stops if isinstance(item, Mapping)
    ]
    if Counter(source_place_ids) != Counter(auto_place_ids):
        errors.append(f"{case_id}: AUTO_RESOLUTION_SOURCE_STOP_SET_MISMATCH")

    expected_stops: list[dict[str, Any]] = []
    for ordinal, parsed_stop in enumerate(parsed):
        if ordinal >= len(resolutions) or not isinstance(resolutions[ordinal], Mapping):
            continue
        resolution = resolutions[ordinal]
        if resolution.get("outcome") != "AUTO_RESOLVED":
            continue
        candidates = resolution.get("candidates")
        selected = resolution.get("selected_place_id")
        candidate = next(
            (
                item
                for item in candidates
                if isinstance(candidates, list)
                and isinstance(item, Mapping)
                and item.get("place_id") == selected
            ),
            None,
        )
        if candidate is None:
            continue
        start_time, end_time, _duration = parse_time_range(parsed_stop["raw_time"])
        expected_stops.append(
            {
                "stop_id": f"stop-{ordinal + 1}",
                "place_id": selected,
                "display_name": candidate.get("name"),
                "city": city,
                "day_index": parsed_stop["day_index"],
                "order_index": sum(
                    item["day_index"] == parsed_stop["day_index"] for item in expected_stops
                ),
                "start_time": start_time,
                "end_time": end_time,
                "coords": candidate.get("coords"),
            }
        )
    if source_stops != expected_stops:
        errors.append(f"{case_id}: SOURCE_STOP_PROJECTION_MISMATCH")

    candidate_mode = (
        runner_control.get("candidate_set_mode") if isinstance(runner_control, Mapping) else None
    )
    candidate_sets = materialization.get("candidate_sets")
    candidate_sets = candidate_sets if isinstance(candidate_sets, list) else []
    route_candidate_receipts = [
        item for item in receipts.values() if item.get("operation") == "route.candidate"
    ]
    unique_auto_ids = list(dict.fromkeys(auto_place_ids))
    route_candidate_ids = [
        str(item["affected_fields"][0]).removeprefix("candidate_routes.")
        for item in route_candidate_receipts
        if item.get("affected_fields")
        and len(item["affected_fields"]) == 1
        and str(item["affected_fields"][0]).startswith("candidate_routes.")
    ]
    if candidate_mode == "VALID":
        if len(candidate_sets) != 1:
            errors.append(f"{case_id}: VALID_CANDIDATE_SET_COUNT_MISMATCH")
        else:
            raw_set = candidate_sets[0]
            frozen = raw_set.get("candidate_set") if isinstance(raw_set, Mapping) else None
            rows = frozen.get("candidates") if isinstance(frozen, Mapping) else None
            actual_ids = (
                [str(item.get("canonical_place_id")) for item in rows if isinstance(item, Mapping)]
                if isinstance(rows, list)
                else []
            )
            if (
                len(actual_ids) != len(set(actual_ids))
                or sorted(actual_ids) != sorted(unique_auto_ids)
            ):
                errors.append(f"{case_id}: VALID_CANDIDATE_SET_CONTENT_MISMATCH")
        if (
            len(route_candidate_ids) != len(route_candidate_receipts)
            or len(route_candidate_ids) != len(set(route_candidate_ids))
            or sorted(route_candidate_ids) != sorted(unique_auto_ids)
        ):
            errors.append(f"{case_id}: VALID_CANDIDATE_RECEIPT_COUNT_MISMATCH")
    elif candidate_mode in {"EMPTY", "MISSING_RECEIPT", "NOT_APPLICABLE"}:
        if candidate_sets or route_candidate_receipts:
            errors.append(f"{case_id}: BLOCKED_CANDIDATE_EVIDENCE_PRESENT")
    else:
        errors.append(f"{case_id}: CANDIDATE_SET_MODE_INVALID")

    return errors


def validate_oracle_payload_compatibility_v3(
    case: Any,
    materialization: Mapping[str, Any],
    oracle: Any,
) -> list[str]:
    """Return aggregate-safe reasons when an oracle contradicts its payload."""

    embedded_oracle = _semantic_field(case, "oracle")
    embedded_oracle_hash = _semantic_field(case, "oracle_sha256")
    if (
        (embedded_oracle is not _MISSING and embedded_oracle is not None)
        or (
            embedded_oracle_hash is not _MISSING
            and embedded_oracle_hash is not None
        )
    ):
        return ["ORACLE_CASE_LABEL_FIELDS_PRESENT"]
    runner_control = _semantic_field(case, "runner_control")
    source = materialization.get("source_payload")
    if not isinstance(runner_control, Mapping) or not isinstance(source, Mapping):
        return ["ORACLE_PAYLOAD_BINDING_MISSING"]
    resolutions = source.get("entity_resolutions")
    if not isinstance(resolutions, list):
        return ["ORACLE_RESOLUTION_CONTRACT_MISSING"]

    errors: list[str] = []
    resolution_blocked = any(
        not isinstance(item, Mapping) or item.get("outcome") != "AUTO_RESOLVED"
        for item in resolutions
    )
    candidate_mode = runner_control.get("candidate_set_mode")
    candidate_blocked = candidate_mode in _CANDIDATE_BLOCKED_MODES
    expected_resolution = resolution_blocked or candidate_blocked
    if _semantic_field(oracle, "requires_user_resolution") is not expected_resolution:
        errors.append("ORACLE_REQUIRES_USER_RESOLUTION_MISMATCH")

    candidate_policy = _CANDIDATE_ORACLE_POLICY_V3.get(candidate_mode)
    if candidate_policy is None:
        errors.append("ORACLE_CANDIDATE_SET_MODE_INVALID")
        return errors
    expected_receipt_mode, expected_specific_place_allowed = candidate_policy
    if _semantic_field(oracle, "candidate_receipt_mode") != expected_receipt_mode:
        errors.append("ORACLE_CANDIDATE_RECEIPT_MODE_MISMATCH")
    if (
        _semantic_field(oracle, "specific_place_allowed")
        is not expected_specific_place_allowed
    ):
        errors.append("ORACLE_SPECIFIC_PLACE_ALLOWED_MISMATCH")
    return errors


def validate_nonblind_oracle_compatibility_v3(
    case: Mapping[str, Any], materialization: Mapping[str, Any]
) -> list[str]:
    """Compare non-blind labels in a separately declared label-reading step."""

    oracle = case.get("oracle")
    if not isinstance(oracle, Mapping):
        return ["NONBLIND_ORACLE_MISSING"]
    label_free_case = {
        key: value for key, value in case.items() if key not in {"oracle", "oracle_sha256"}
    }
    return validate_oracle_payload_compatibility_v3(
        label_free_case,
        materialization,
        oracle,
    )


def validate_dataset_semantics_v3(
    cases: Sequence[Mapping[str, Any]], materializations: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Validate an exact case/materialization set without reading blind labels."""

    errors: list[str] = []
    case_ids = [str(item.get("case_id")) for item in cases if isinstance(item, Mapping)]
    materialization_ids = [
        str(item.get("case_id")) for item in materializations if isinstance(item, Mapping)
    ]
    duplicate_cases = sorted(case_id for case_id, count in Counter(case_ids).items() if count > 1)
    duplicate_materializations = sorted(
        case_id for case_id, count in Counter(materialization_ids).items() if count > 1
    )
    if duplicate_cases:
        errors.append(f"DUPLICATE_CASE_IDS: {duplicate_cases}")
    if duplicate_materializations:
        errors.append(f"DUPLICATE_MATERIALIZATION_CASE_IDS: {duplicate_materializations}")
    case_set = set(case_ids)
    materialization_set = set(materialization_ids)
    if case_set != materialization_set:
        errors.append(
            f"CASE_MATERIALIZATION_SET_MISMATCH missing={sorted(case_set - materialization_set)} "
            f"extra={sorted(materialization_set - case_set)}"
        )
    materialization_by_case = {
        str(item.get("case_id")): item for item in materializations if isinstance(item, Mapping)
    }
    from evals.trip_check_v1.p5.evidence_materialization_v3 import (
        validate_evidence_materialization_v3,
    )

    for case in cases:
        case_id = str(case.get("case_id"))
        materialization = materialization_by_case.get(case_id)
        if materialization is None:
            errors.append(f"{case_id}: MATERIALIZATION_MISSING")
            continue
        try:
            validate_evidence_materialization_v3(materialization)
        except (TypeError, ValueError) as exc:
            errors.append(f"{case_id}: MATERIALIZATION_INVALID: {exc}")
            continue
        errors.extend(validate_case_semantics_v3(case, materialization))
    return {
        "schema_version": "trip-check-p5-semantic-validation-v3",
        "status": "PASS" if not errors else "REJECT",
        "blind_labels_read": False,
        "case_count": len(cases),
        "error_count": len(errors),
        "errors": errors,
    }


__all__ = [
    "RESOLUTION_OUTCOMES_V3",
    "parsed_stop_projection_v3",
    "validate_case_semantics_v3",
    "validate_dataset_semantics_v3",
    "validate_nonblind_oracle_compatibility_v3",
    "validate_oracle_payload_compatibility_v3",
]
