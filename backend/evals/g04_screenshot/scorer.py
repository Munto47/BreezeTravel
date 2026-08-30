from __future__ import annotations

import math
import unicodedata
from collections.abc import Iterable
from typing import Any

from .contracts import (
    G04ScreenshotParityCaseV1,
    G04ScreenshotParityManifestV1,
    G04ScreenshotScoreReportV1,
)


KEY_FIELD_F1_MIN = 0.95
ADJACENCY_F1_MIN = 0.97
LOW_CONFIDENCE_RECALL_MIN = 1.0
PLACE_DROP_MAX = 0.01
SERIOUS_ERROR_MAX = 0
THREE_IMAGE_P95_MAX_MS = 12_000.0


class G04ScreenshotManifestError(ValueError):
    pass


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip().casefold()


def _namespaced(case: G04ScreenshotParityCaseV1, values: Iterable[str]) -> set[str]:
    return {f"{case.case_id}:{_normalize(value)}" for value in values}


def _precision_recall_f1(
    expected: set[str],
    observed: set[str],
) -> tuple[float, float, float]:
    true_positive = len(expected & observed)
    precision = true_positive / len(observed) if observed else float(not expected)
    recall = true_positive / len(expected) if expected else 1.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return precision, recall, f1


def _adjacencies(case: G04ScreenshotParityCaseV1, values: tuple[str, ...]) -> set[str]:
    normalized = [_normalize(value) for value in values]
    return {
        f"{case.case_id}:{left}->{right}"
        for left, right in zip(normalized, normalized[1:])
    }


def _nearest_rank_p95(values: tuple[float, ...]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def score_g04_screenshot_manifest(
    manifest: G04ScreenshotParityManifestV1 | dict[str, Any],
) -> G04ScreenshotScoreReportV1:
    if not isinstance(manifest, G04ScreenshotParityManifestV1):
        manifest = G04ScreenshotParityManifestV1.model_validate(manifest)
    real_cases = tuple(
        case
        for case in manifest.cases
        if case.source.evidence_tier == "LICENSED_REAL_SOURCE"
    )
    if not real_cases:
        raise G04ScreenshotManifestError(
            "licensed real-source cases are required for screenshot quality metrics"
        )

    expected_fields: set[str] = set()
    observed_fields: set[str] = set()
    expected_adjacencies: set[str] = set()
    observed_adjacencies: set[str] = set()
    expected_low_confidence: set[str] = set()
    observed_confirmations: set[str] = set()
    reference_places: set[str] = set()
    text_places: set[str] = set()
    screenshot_places: set[str] = set()
    serious_error_count = 0

    for case in real_cases:
        expected_fields |= _namespaced(case, case.expected_key_fields)
        observed_fields |= _namespaced(case, case.observed_key_fields)
        expected_adjacencies |= _adjacencies(case, case.expected_reading_order)
        observed_adjacencies |= _adjacencies(case, case.observed_reading_order)
        expected_low_confidence |= _namespaced(
            case,
            case.expected_low_confidence_fields,
        )
        observed_confirmations |= _namespaced(
            case,
            case.observed_confirmation_fields,
        )
        reference_places |= _namespaced(case, case.reference_executable_places)
        text_places |= _namespaced(case, case.text_executable_places)
        screenshot_places |= _namespaced(case, case.screenshot_executable_places)
        serious_error_count += len(case.serious_errors)

    key_precision, key_recall, key_f1 = _precision_recall_f1(
        expected_fields,
        observed_fields,
    )
    adjacency_precision, adjacency_recall, adjacency_f1 = _precision_recall_f1(
        expected_adjacencies,
        observed_adjacencies,
    )
    _, low_confidence_recall, _ = _precision_recall_f1(
        expected_low_confidence,
        observed_confirmations,
    )
    text_precision, text_recall, _ = _precision_recall_f1(
        reference_places,
        text_places,
    )
    screenshot_precision, screenshot_recall, _ = _precision_recall_f1(
        reference_places,
        screenshot_places,
    )
    precision_drop = max(0.0, text_precision - screenshot_precision)
    recall_drop = max(0.0, text_recall - screenshot_recall)
    p95_ms = _nearest_rank_p95(manifest.performance.durations_ms)

    failures: list[str] = []
    if key_f1 < KEY_FIELD_F1_MIN:
        failures.append("KEY_FIELD_F1_BELOW_95_PERCENT")
    if adjacency_f1 < ADJACENCY_F1_MIN:
        failures.append("ADJACENCY_F1_BELOW_97_PERCENT")
    if low_confidence_recall < LOW_CONFIDENCE_RECALL_MIN:
        failures.append("LOW_CONFIDENCE_CONFIRMATION_RECALL_BELOW_100_PERCENT")
    if precision_drop > PLACE_DROP_MAX + 1e-12:
        failures.append("SCREENSHOT_PLACE_PRECISION_DROP_EXCEEDS_1PP")
    if recall_drop > PLACE_DROP_MAX + 1e-12:
        failures.append("SCREENSHOT_PLACE_RECALL_DROP_EXCEEDS_1PP")
    if serious_error_count > SERIOUS_ERROR_MAX:
        failures.append("SERIOUS_PLACE_ERROR_COUNT_NONZERO")
    if p95_ms > THREE_IMAGE_P95_MAX_MS:
        failures.append("THREE_IMAGE_P95_EXCEEDS_12_SECONDS")

    def rounded(value: float) -> float:
        return round(value, 6)

    return G04ScreenshotScoreReportV1(
        licensed_real_case_count=len(real_cases),
        synthetic_format_case_count=len(manifest.cases) - len(real_cases),
        key_field_precision=rounded(key_precision),
        key_field_recall=rounded(key_recall),
        key_field_f1=rounded(key_f1),
        adjacency_precision=rounded(adjacency_precision),
        adjacency_recall=rounded(adjacency_recall),
        adjacency_f1=rounded(adjacency_f1),
        low_confidence_confirmation_recall=rounded(low_confidence_recall),
        text_place_precision=rounded(text_precision),
        text_place_recall=rounded(text_recall),
        screenshot_place_precision=rounded(screenshot_precision),
        screenshot_place_recall=rounded(screenshot_recall),
        place_precision_drop_pp=round(precision_drop * 100, 4),
        place_recall_drop_pp=round(recall_drop * 100, 4),
        serious_error_count=serious_error_count,
        performance_sample_count=len(manifest.performance.durations_ms),
        three_image_p95_ms=round(p95_ms, 3),
        failures=tuple(failures),
        gate_pass=not failures,
    )
