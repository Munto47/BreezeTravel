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


RESOLUTION_OUTCOMES_V3 = {
    "AUTO_RESOLVED",
    "NEEDS_CONFIRMATION",
    "HARD_REJECTED",
    "NO_CANDIDATE",
}
_CANDIDATE_BLOCKED_MODES = {"EMPTY", "MISSING_RECEIPT"}


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
    errors: list[str] = []
    try:
        parsed = parsed_stop_projection_v3(case, materialization)
    except (TypeError, ValueError) as exc:
        return [f"{case_id}: PARSER_INPUT_INVALID: {exc}"]
    source = materialization.get("source_payload")
    if not isinstance(source, Mapping):
        return [f"{case_id}: SOURCE_PAYLOAD_MISSING"]
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
    resolution_blocked = False
    for ordinal, parsed_stop in enumerate(parsed):
        if ordinal >= len(resolutions) or not isinstance(resolutions[ordinal], Mapping):
            errors.append(f"{case_id}: RESOLUTION_ENTRY_MISSING ordinal={ordinal}")
            continue
        resolution = resolutions[ordinal]
        if resolution.get("ordinal") != ordinal:
            errors.append(f"{case_id}: RESOLUTION_ORDINAL_MISMATCH ordinal={ordinal}")
        if resolution.get("day_index") != parsed_stop["day_index"]:
            errors.append(f"{case_id}: RESOLUTION_DAY_MISMATCH ordinal={ordinal}")
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
        candidate_ids = _candidate_ids(candidates)
        selected = resolution.get("selected_place_id")
        city = case.get("city")
        candidate_rows = [item for item in candidates or [] if isinstance(item, Mapping)]
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
            resolution_blocked = True
            if selected is not None or len(candidate_rows) < 2:
                errors.append(f"{case_id}: CONFIRMATION_EVIDENCE_INSUFFICIENT ordinal={ordinal}")
        elif outcome == "HARD_REJECTED":
            resolution_blocked = True
            if selected is not None or not candidate_rows or any(item.get("city") == city for item in candidate_rows):
                errors.append(f"{case_id}: HARD_REJECTION_EVIDENCE_INVALID ordinal={ordinal}")
        else:
            resolution_blocked = True
            if selected is not None or candidate_rows:
                errors.append(f"{case_id}: NO_CANDIDATE_EVIDENCE_INVALID ordinal={ordinal}")

    source_stops = source.get("stops")
    source_place_ids = (
        [str(item.get("place_id")) for item in source_stops if isinstance(item, Mapping)]
        if isinstance(source_stops, list)
        else []
    )
    if Counter(source_place_ids) != Counter(auto_place_ids):
        errors.append(f"{case_id}: AUTO_RESOLUTION_SOURCE_STOP_SET_MISMATCH")

    oracle = case.get("oracle")
    runner_control = case.get("runner_control")
    if isinstance(oracle, Mapping) and isinstance(runner_control, Mapping):
        candidate_blocked = runner_control.get("candidate_set_mode") in _CANDIDATE_BLOCKED_MODES
        expected_resolution = resolution_blocked or candidate_blocked
        if oracle.get("requires_user_resolution") is not expected_resolution:
            errors.append(
                f"{case_id}: ORACLE_RESOLUTION_CONTRADICTION "
                f"oracle={oracle.get('requires_user_resolution')} evidence={expected_resolution}"
            )
    return errors


def validate_dataset_semantics_v3(
    cases: Sequence[Mapping[str, Any]], materializations: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Validate an exact case/materialization set without reading blind labels."""

    materialization_by_case = {
        str(item.get("case_id")): item for item in materializations if isinstance(item, Mapping)
    }
    errors: list[str] = []
    for case in cases:
        case_id = str(case.get("case_id"))
        materialization = materialization_by_case.get(case_id)
        if materialization is None:
            errors.append(f"{case_id}: MATERIALIZATION_MISSING")
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
]
