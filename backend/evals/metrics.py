from __future__ import annotations

import math
import random
from collections import defaultdict
from statistics import mean
from typing import Callable, Iterable


def exact_set_match(expected, actual) -> float:
    return float(set(expected) == set(actual))


def precision_recall_f1(expected, actual):
    expected, actual = set(expected), set(actual)
    tp = len(expected & actual)
    precision = tp / len(actual) if actual else float(not expected)
    recall = tp / len(expected) if expected else float(not actual)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def reciprocal_rank(expected_id: str, ranked_ids: list[str]) -> float:
    try:
        return 1.0 / (ranked_ids.index(expected_id) + 1)
    except ValueError:
        return 0.0


def ndcg_at_k(relevant_ids: set[str], ranked_ids: list[str], k: int = 10) -> float:
    dcg = sum((1.0 if item in relevant_ids else 0.0) / math.log2(index + 2) for index, item in enumerate(ranked_ids[:k]))
    ideal = sum(1.0 / math.log2(index + 2) for index in range(min(k, len(relevant_ids))))
    return dcg / ideal if ideal else 0.0


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def bootstrap_ci(values: list[float], *, seed: int = 42, iterations: int = 1000, confidence: float = 0.95):
    if not values:
        return {"mean": 0.0, "low": 0.0, "high": 0.0, "n": 0}
    rng = random.Random(seed)
    estimates = [mean(rng.choice(values) for _ in values) for _ in range(iterations)]
    alpha = (1 - confidence) / 2
    return {
        "mean": mean(values),
        "low": percentile(estimates, alpha),
        "high": percentile(estimates, 1 - alpha),
        "n": len(values),
    }


def bucketed(rows: Iterable[dict], key: str, metric: Callable[[dict], float]):
    buckets = defaultdict(list)
    for row in rows:
        buckets[str(row.get(key, "unknown"))].append(metric(row))
    return {name: bootstrap_ci(values) for name, values in sorted(buckets.items())}
