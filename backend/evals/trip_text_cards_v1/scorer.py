from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any

from evals.trip_text_cards_v1.contracts import (
    CaseAnnotation,
    TextCardInputCase,
    TextCardPrediction,
    normalized_text,
)


ROLES = ("PLANNED", "OPTIONAL", "REFERENCE", "EXCLUDED", "PASS_THROUGH")
FORBIDDEN_PLACE_KINDS = {"URL", "DESCRIPTION", "RESERVATION"}
FORBIDDEN_PUBLIC_KEYS = {
    "activity_id",
    "binding",
    "canonical_place_id",
    "confidence",
    "evidence",
    "hash",
    "mention_id",
    "model",
    "offset",
    "provider",
    "public_resource_id",
    "quote",
    "receipt",
    "revision",
    "source",
    "source_span",
    "span",
    "span_end",
    "span_start",
    "stage",
    "understanding_id",
    "uuid",
}
URL_RE = re.compile(r"https?://", re.IGNORECASE)
SENTENCE_MARKERS = set("。！？；\n")


class ScoringError(ValueError):
    pass


def _prf(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _walk_keys(value: object) -> list[str]:
    if isinstance(value, dict):
        return [*(str(key).casefold() for key in value), *(key for child in value.values() for key in _walk_keys(child))]
    if isinstance(value, list):
        return [key for child in value for key in _walk_keys(child)]
    return []


def _atomic_eligibility(role: str, day_index: int | None, candidate: str | None) -> bool:
    value = (candidate or "").strip()
    if role != "PLANNED" or day_index is None or not value or len(value) > 40:
        return False
    if URL_RE.search(value) or any(marker in value for marker in SENTENCE_MARKERS):
        return False
    return not any(word in value for word in ("预约", "说明", "网址", "链接"))


def load_predictions(path: Path) -> list[TextCardPrediction]:
    predictions = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            predictions.append(TextCardPrediction.model_validate_json(line))
        except ValueError as exc:
            raise ScoringError(f"prediction line {line_number} is invalid: {exc}") from exc
    return predictions


def score_predictions(
    *,
    source_cases: list[TextCardInputCase],
    gold_cases: list[CaseAnnotation],
    predictions: list[TextCardPrediction],
) -> dict[str, Any]:
    expected_ids = [case.case_id for case in source_cases]
    if [case.case_id for case in gold_cases] != expected_ids:
        raise ScoringError("gold cases do not exactly cover the source split")
    if [prediction.case_id for prediction in predictions] != expected_ids:
        raise ScoringError("predictions do not exactly cover the source split")

    gold_by_id = {case.case_id: case for case in gold_cases}
    prediction_by_id = {prediction.case_id: prediction for prediction in predictions}
    evidence_total = 0
    evidence_valid = 0
    eligibility_consistent = 0
    forbidden_as_place = 0
    severe_wrong_matches = 0
    wrong_city_matches = 0
    wrong_category_matches = 0
    auto_total = 0
    auto_correct = 0
    executable_tp = executable_fp = executable_fn = 0
    day_tp = day_fp = day_fn = 0
    role_counts = {role: Counter(tp=0, fp=0, fn=0) for role in ROLES}
    deep_gold_executable = 0
    deep_correct_auto = 0
    confirmations: list[float] = []
    public_forbidden_key_hits = 0
    public_source_leak_hits = 0
    public_api_ready_ms: list[float] = []
    public_api_progress_ms: list[float] = []

    for source in source_cases:
        gold = gold_by_id[source.case_id]
        prediction = prediction_by_id[source.case_id]
        text = normalized_text(source.input_text)
        if prediction.source_sha256 != source.normalized_input_sha256:
            raise ScoringError(f"{source.case_id} prediction source binding mismatch")

        predicted_spans: set[tuple[int, int]] = set()
        for mention in prediction.mentions:
            span = (mention.span_start, mention.span_end)
            if span in predicted_spans:
                raise ScoringError(f"{source.case_id} has duplicate predicted spans")
            predicted_spans.add(span)
            evidence_total += 1
            if text[mention.span_start : mention.span_end] == mention.raw_text:
                evidence_valid += 1
            if mention.eligible_for_place_search == _atomic_eligibility(
                mention.role,
                mention.day_index,
                mention.atomic_place_name,
            ):
                eligibility_consistent += 1

        gold_by_span = {(mention.span_start, mention.span_end): mention for mention in gold.mentions}
        gold_executable = {
            span: mention
            for span, mention in gold_by_span.items()
            if mention.executable_place
        }
        predicted_eligible = {
            (mention.span_start, mention.span_end): mention
            for mention in prediction.mentions
            if mention.eligible_for_place_search
        }

        executable_tp += len(set(gold_executable) & set(predicted_eligible))
        executable_fp += len(set(predicted_eligible) - set(gold_executable))
        executable_fn += len(set(gold_executable) - set(predicted_eligible))
        for span in set(gold_executable) | set(predicted_eligible):
            gold_mention = gold_executable.get(span)
            predicted_mention = predicted_eligible.get(span)
            if (
                gold_mention is not None
                and predicted_mention is not None
                and gold_mention.day_index == predicted_mention.day_index
            ):
                day_tp += 1
            else:
                if predicted_mention is not None:
                    day_fp += 1
                if gold_mention is not None:
                    day_fn += 1

        predicted_roles = {
            (mention.span_start, mention.span_end): mention.role
            for mention in prediction.mentions
        }
        gold_roles = {span: mention.role for span, mention in gold_by_span.items()}
        for role in ROLES:
            predicted_set = {span for span, value in predicted_roles.items() if value == role}
            gold_set = {span for span, value in gold_roles.items() if value == role}
            role_counts[role]["tp"] += len(predicted_set & gold_set)
            role_counts[role]["fp"] += len(predicted_set - gold_set)
            role_counts[role]["fn"] += len(gold_set - predicted_set)

        correct_auto_for_case = 0
        for mention in prediction.mentions:
            if not mention.eligible_for_place_search and mention.resolution_status != "AUTO_MATCHED":
                continue
            span = (mention.span_start, mention.span_end)
            gold_mention = gold_by_span.get(span)
            if gold_mention is not None and gold_mention.semantic_kind in FORBIDDEN_PLACE_KINDS:
                forbidden_as_place += 1
            candidate = mention.atomic_place_name or ""
            if (
                URL_RE.search(candidate)
                or any(marker in candidate for marker in SENTENCE_MARKERS)
                or any(word in candidate for word in ("预约", "说明", "网址", "链接"))
            ):
                forbidden_as_place += 1
            if mention.resolution_status != "AUTO_MATCHED":
                continue
            auto_total += 1
            if gold_mention is None or not gold_mention.executable_place or gold_mention.canonical_place is None:
                severe_wrong_matches += 1
                continue
            canonical = gold_mention.canonical_place
            if mention.canonical_city != canonical.city:
                wrong_city_matches += 1
            if mention.canonical_category != canonical.category:
                wrong_category_matches += 1
            if (
                mention.canonical_place_id == canonical.place_id
                and mention.canonical_city == canonical.city
                and mention.canonical_category == canonical.category
            ):
                auto_correct += 1
                correct_auto_for_case += 1
            else:
                severe_wrong_matches += 1

        confirmations.append(float(max(0, len(gold_executable) - correct_auto_for_case)))
        if source.cohort == "DEEP_CITY":
            deep_gold_executable += len(gold_executable)
            deep_correct_auto += correct_auto_for_case

        public_keys = _walk_keys(prediction.public_result)
        public_forbidden_key_hits += sum(key in FORBIDDEN_PUBLIC_KEYS for key in public_keys)
        serialized_public = json.dumps(prediction.public_result, ensure_ascii=False, sort_keys=True)
        public_source_leak_hits += int(text in serialized_public)
        if prediction.measurement_scope == "PUBLIC_API_BROWSER":
            if prediction.cards_ready_ms is not None:
                public_api_ready_ms.append(prediction.cards_ready_ms)
            if prediction.first_progress_ms is not None:
                public_api_progress_ms.append(prediction.first_progress_ms)

    executable = _prf(executable_tp, executable_fp, executable_fn)
    day_assignment = _prf(day_tp, day_fp, day_fn)
    role_metrics = {
        role: _prf(counts["tp"], counts["fp"], counts["fn"])
        for role, counts in role_counts.items()
    }
    role_macro_f1 = sum(float(value["f1"]) for value in role_metrics.values()) / len(ROLES)
    evidence_validity = evidence_valid / evidence_total if evidence_total else 0.0
    eligibility_consistency = eligibility_consistent / evidence_total if evidence_total else 0.0
    auto_precision = auto_correct / auto_total if auto_total else 0.0
    deep_coverage = deep_correct_auto / deep_gold_executable if deep_gold_executable else 0.0

    return {
        "schema_version": "g01-text-card-score-v1",
        "case_count": len(source_cases),
        "scoring_coverage": 1.0,
        "evidence_span_validity": evidence_validity,
        "eligibility_rule_consistency": eligibility_consistency,
        "forbidden_content_as_place_count": forbidden_as_place,
        "severe_wrong_auto_match_count": severe_wrong_matches,
        "wrong_city_auto_match_count": wrong_city_matches,
        "wrong_category_auto_match_count": wrong_category_matches,
        "auto_match": {
            "correct": auto_correct,
            "denominator": auto_total,
            "precision": auto_precision,
        },
        "executable_mentions": executable,
        "day_assignment": day_assignment,
        "role_metrics": role_metrics,
        "role_macro_f1": role_macro_f1,
        "deep_city_auto_match": {
            "correct": deep_correct_auto,
            "gold_executable": deep_gold_executable,
            "coverage": deep_coverage,
        },
        "human_confirmation_count": {
            "median": median(confirmations) if confirmations else None,
            "p90": _nearest_rank(confirmations, 0.90),
        },
        "public_projection": {
            "forbidden_key_hits": public_forbidden_key_hits,
            "full_source_leak_hits": public_source_leak_hits,
        },
        "public_api_latency": {
            "measured_case_count": len(public_api_ready_ms),
            "first_progress_p95_ms": _nearest_rank(public_api_progress_ms, 0.95),
            "cards_ready_p95_ms": _nearest_rank(public_api_ready_ms, 0.95),
        },
    }
