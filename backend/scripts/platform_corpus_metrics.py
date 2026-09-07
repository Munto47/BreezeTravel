"""Independent-label comparison; corpus generation never supplies its own gold."""
from __future__ import annotations

from collections import Counter


def compare_annotations(label: dict, observations: list[dict]) -> dict:
    if label.get("annotation_status") != "reviewed" or label.get("annotator_type") not in {"human", "owner", "independent_agent"}:
        raise ValueError("Only reviewed independent annotations support accuracy claims")
    scope = set(label.get("roles_in_scope", ["PLANNED", "OPTIONAL"]))
    expected = [item for item in label["activities"] if item.get("role", "PLANNED") in scope]
    actual = [item for item in observations if item["role"] in scope and item.get("name")]
    available = set(range(len(actual)))
    pairs = []
    for expected_index, gold in enumerate(expected):
        names = set(gold.get("acceptable_names", [])) | {gold["name"]}
        matched = next((index for index in sorted(available)
            if actual[index]["name"] in names and actual[index]["day_index"] == gold["day_index"]
            and actual[index]["role"] == gold.get("role", "PLANNED")
            and ("branch_label" not in gold or actual[index].get("branch_label") == gold["branch_label"])), None)
        if matched is not None:
            available.remove(matched)
            pairs.append((expected_index, matched))
    identity_total = sum(bool(item.get("expected_poi_ids")) for item in expected)
    correct_identity = 0
    wrong_identity = 0
    for expected_index, actual_index in pairs:
        ids = expected[expected_index].get("expected_poi_ids")
        if not ids:
            continue
        poi_id = actual[actual_index].get("poi_id")
        if poi_id is not None:
            correct_identity += int(poi_id in ids)
            wrong_identity += int(poi_id not in ids)
    ordered = all(left[1] < right[1] for left, right in zip(pairs, pairs[1:]))
    by_role = {}
    for role in sorted(scope):
        role_expected = sum(item.get("role", "PLANNED") == role for item in expected)
        role_observed = sum(item["role"] == role for item in actual)
        role_matched = sum(expected[i].get("role", "PLANNED") == role for i, _j in pairs)
        by_role[role] = {"expected": role_expected, "observed": role_observed, "matched": role_matched,
            "recall": role_matched / role_expected if role_expected else None,
            "precision": role_matched / role_observed if role_observed else None}
    return {"gold_status": "INDEPENDENT_REVIEWED", "expected_places": len(expected), "observed_places": len(actual),
        "matched_places": len(pairs), "missing_places": len(expected) - len(pairs), "extra_or_misassigned_places": len(available),
        "semantic_recall": len(pairs) / len(expected) if expected else None,
        "semantic_precision": len(pairs) / len(actual) if actual else None,
        "order_correct": ordered, "semantic_exact": len(pairs) == len(expected) == len(actual) and ordered,
        "poi_labeled_count": identity_total, "poi_identity_correct": correct_identity, "wrong_auto_confirmations": wrong_identity,
        "by_role": by_role}


def summarize_measurements(rows: list[dict]) -> dict:
    annotated = [row for row in rows if row.get("gold_status") == "INDEPENDENT_REVIEWED"]
    expected = sum(row["expected_places"] for row in annotated)
    observed = sum(row["observed_places"] for row in annotated)
    matched = sum(row["matched_places"] for row in annotated)
    elapsed = sorted(row["elapsed_ms"] for row in rows)
    by_role = {}
    for role in sorted({role for row in annotated for role in row.get("by_role", {})}):
        totals = {key: sum(row.get("by_role", {}).get(role, {}).get(key, 0) for row in annotated)
                  for key in ("expected", "observed", "matched")}
        by_role[role] = {**totals, "recall": totals["matched"] / totals["expected"] if totals["expected"] else None,
            "precision": totals["matched"] / totals["observed"] if totals["observed"] else None}
    def percentile(fraction):
        return elapsed[min(len(elapsed) - 1, int((len(elapsed) - 1) * fraction))] if elapsed else None
    return {"runs": len(rows), "completed": sum(row["status"] == "COMPLETED" for row in rows),
        "errors": sum(row["status"] != "COMPLETED" for row in rows), "annotated_runs": len(annotated), "by_role": by_role,
        "semantic_exact_runs": sum(row.get("semantic_exact") is True for row in annotated) if annotated else None,
        "semantic_recall": matched / expected if expected else None,
        "semantic_precision": matched / observed if observed else None,
        "missing_places": sum(row["missing_places"] for row in annotated) if annotated else None,
        "extra_or_misassigned_places": sum(row["extra_or_misassigned_places"] for row in annotated) if annotated else None,
        "poi_labeled_count": sum(row["poi_labeled_count"] for row in annotated),
        "wrong_auto_confirmations": (sum(row["wrong_auto_confirmations"] for row in annotated)
            if any(row["poi_labeled_count"] for row in annotated) else None),
        "latency_p50_ms": percentile(0.5), "latency_p95_ms": percentile(0.95),
        "error_categories": dict(Counter(row.get("error_category", "UNKNOWN") for row in rows if row["status"] != "COMPLETED")),
        "model_calls": sum(row.get("usage", {}).get("external_calls") or 0 for row in rows),
        "usage_complete": all(row.get("usage", {}).get("input_tokens") is not None
                              and row.get("usage", {}).get("output_tokens") is not None for row in rows),
        "input_tokens": sum(row.get("usage", {}).get("input_tokens") or 0 for row in rows),
        "output_tokens": sum(row.get("usage", {}).get("output_tokens") or 0 for row in rows)}
