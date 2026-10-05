"""Independent-label comparison; corpus generation never supplies its own gold."""
from __future__ import annotations

from collections import Counter
from math import ceil
from scripts.visit_matching import match_occurrences, validate_formal_annotation, whole_article_passed


def semantic_observations(proposal, places=None) -> list[dict]:
    """Compare business meaning without requiring identical storage enums.

    Mandatory internal details use REFERENCE in the current runtime so they do
    not become route stops. Their adoption is PLANNED for semantic comparison.
    Pickup/exterior purposes qualify their parent; they aren't extra visits.
    """
    places = places or {}
    purposes = {}
    for item in proposal.mentions:
        if item.parent_mention_id and item.detail_kind in {"PICKUP_ONLY", "EXTERIOR_ONLY"}:
            purposes.setdefault(item.parent_mention_id, []).append(item.detail_kind)
    return [{"mention_id": item.mention_id, "name": item.atomic_place_name, "day_index": item.day_index,
             "role": "PLANNED" if item.parent_mention_id and item.relation_type == "INTERNAL_DETAIL"
                     and item.role.value == "REFERENCE" else item.role.value,
             "storage_role": item.role.value, "sequence_index": item.sequence_index,
             "span_start": item.span_start, "span_end": item.span_end,
             "parent_mention_id": item.parent_mention_id, "relation_type": item.relation_type,
             "replaces_mention_id": item.replaces_mention_id, "replacement_condition": item.replacement_condition,
             "detail_kind": item.detail_kind, "category": item.category_hint,
             "choice_group_id": item.choice_group_id, "branch_id": item.branch_id,
             "choice_group_selectable": item.choice_group_selectable,
             "lodging_event": item.lodging_event, "lodging_scope": item.lodging_scope,
             "lodging_excluded_nights": item.lodging_excluded_nights,
             "branch_label": item.branch_label, "purposes": purposes.get(item.mention_id, []),
             "poi_id": places[item.mention_id].canonical_place_id if places.get(item.mention_id) else None}
            for item in proposal.mentions if item.detail_kind not in {"PICKUP_ONLY", "EXTERIOR_ONLY"}]


def _amap_id(value: str) -> str:
    return value.removeprefix("amap:")


def public_projection_observations(output, supplementary=None) -> list[dict]:
    """Inspect actual public fields; semantic mentions alone do not prove display.

    This checks the public projection, not DOM rendering or PNG. Missing
    supplementary readback is unverified and cannot pass cancelled items.
    """
    places = {item.compiled.mention.mention_id: item.place for item in output.activities}
    tokens = {item.compiled.mention.mention_id: item.compiled.public_activity_token for item in output.activities}
    rows = semantic_observations(output.proposal, places)
    cards = {card.activity_token: (day_index, card) for day_index, day in enumerate(output.public_result.days, 1)
             for card in day.activities if card.status == "READY"}
    alternatives = {card.activity_token: (day_index, card) for day_index, day in enumerate(output.public_result.days, 1)
                    for card in day.alternatives}
    lodgings = {card.activity_token: card for card in output.public_result.lodging_constraints if card.status == 'READY'}
    cancelled = Counter((day.day_index, item.name) for day in supplementary.days for item in day.items
                        if item.role == "EXCLUDED") if supplementary and supplementary.status == "AVAILABLE" else Counter()
    details = Counter((token, detail.name, detail.optional)
                      for token, (_, card) in {**cards, **alternatives}.items() for detail in card.source_details)
    purpose_labels = {"PICKUP_ONLY": "仅取物，不参观", "EXTERIOR_ONLY": "仅看外观，不入内部"}
    for row in rows:
        token = tokens.get(row["mention_id"])
        parent = row["parent_mention_id"]
        visible = False
        if parent:
            parent_view = cards.get(tokens.get(parent)) or alternatives.get(tokens.get(parent))
            if parent_view and parent_view[0] == row["day_index"]:
                name = {"ENTRY": "入口：", "EXIT": "出口："}.get(row["detail_kind"], "") + (row["name"] or "")
                key = (tokens.get(parent), name, row["role"] == "OPTIONAL")
                visible = details[key] > 0
                if visible:
                    details[key] -= 1
        elif row["role"] == "EXCLUDED":
            key = (row["day_index"], row["name"])
            visible = cancelled[key] > 0
            if visible:
                cancelled[key] -= 1
        else:
            view = (cards if row["role"] == "PLANNED" else alternatives).get(token)
            if view and view[0] == row["day_index"]:
                visible = True
                detail_names = {detail.name for detail in view[1].source_details if not detail.optional}
                visible = all(purpose_labels[purpose] in detail_names for purpose in row["purposes"])
                if row["replaces_mention_id"]:
                    target = cards.get(tokens.get(row["replaces_mention_id"]))
                    visible = visible and bool(target and getattr(view[1], "replaces_visit_id", None) == target[1].visit_id
                                               and getattr(view[1], "replacement_condition", None) == row["replacement_condition"])
            elif (row['role'] == 'PLANNED' and row['category'] == '住宿' and row['lodging_event'] == 'OVERNIGHT'
                  and token in lodgings):
                lodging = lodgings[token]
                nights = (set(range(1, len(output.public_result.days))) if row['lodging_scope'] == 'WHOLE_TRIP'
                          else {row['day_index']}) - set(row['lodging_excluded_nights'])
                visible = (bool(nights) and set(lodging.overnight_days) == nights
                           and lodging.scope == ('WHOLE_TRIP' if row['lodging_scope'] == 'WHOLE_TRIP' else 'NIGHTS'))
        row["visible_correct"] = visible
    # A projection can also invent/duplicate a main stop even when the semantic
    # plan is correct. Include those additions in the same precision denominator.
    known = {tokens.get(row["mention_id"]): row for row in rows if not row["parent_mention_id"]}
    seen = set()
    for day_index, day in enumerate(output.public_result.days, 1):
        for role, views in (("PLANNED", day.activities), ("OPTIONAL", day.alternatives)):
            for view in views:
                token = view.activity_token
                row = known.get(token)
                key = (role, token)
                if key in seen or row is None or row["role"] != role:
                    rows.append(dict(name=view.name, day_index=day_index, role=role,
                                     visible_correct=False, projection_extra=True))
                seen.add(key)
    return rows


async def read_public_projection(output, source_text):
    """Use existing save/readback projection without touching any user's DB."""
    from datetime import datetime, timezone
    from app.trip_understanding.repository import InMemoryTripUnderstandingRepository

    repo = InMemoryTripUnderstandingRepository()
    now = datetime.now(timezone.utc)
    created = await repo.create_full(owner_user_id="measurement", source_text=source_text,
        idempotency_key="measurement", request_hash=output.source_hash, now=now, retention_days=1)
    job = await repo.claim_next(worker_id="measurement", now=now, lease_seconds=60)
    await repo.complete_job(job, output, now=now)
    resource = await repo.authorize(created.accepted.public_resource_id, capability_hash=None, user_id="measurement", now=now)
    stored = await repo.get_result(resource)
    supplementary = await repo.get_supplementary_view(resource, now=now)
    projected = output.model_copy(update={"public_result": stored.result})
    return public_projection_observations(projected, supplementary)


def _matches_visit(gold: dict, actual: dict) -> bool:
    names = set(gold.get("acceptable_names", [])) | {gold["name"]}
    if (actual.get("name") not in names or actual.get("day_index") != gold["day_index"]
            or actual.get("role") != gold.get("role", "PLANNED")):
        return False
    # A model may quote more context, but must cover this particular occurrence.
    if "span_start" in gold:
        spans = [(gold["span_start"], gold["span_end"])] + [
            (span["span_start"], span["span_end"]) for span in gold.get("equivalent_reference_spans", [])]
        if not (isinstance(actual.get("span_start"), int) and isinstance(actual.get("span_end"), int)
                and any(actual["span_start"] <= start < end <= actual["span_end"] for start, end in spans)):
            return False
    for field in ("branch_label", "detail_kind", "relation_type", "category", "replacement_condition", "choice_group_selectable"):
        if field in gold and actual.get(field) != gold[field]:
            return False
    if "purpose" in gold and set(actual.get("purposes", [])) != ({gold["purpose"]} if gold["purpose"] else set()):
        return False
    if "parent_id" in gold and gold["parent_id"] is None and actual.get("parent_mention_id") is not None:
        return False
    if "replaces_id" in gold and gold["replaces_id"] is None and actual.get("replaces_mention_id") is not None:
        return False
    for field in ('choice_group_id', 'branch_id'):
        if field in gold and bool(gold[field]) != bool(actual.get(field)):
            return False
    return True


def _choice_membership_conflicts(expected, actual, pairs):
    """Compare group partitions without requiring identical generated IDs."""
    invalid = set()
    for field in ('choice_group_id', 'branch_id'):
        forward, reverse = {}, {}
        for i, j in pairs:
            if field not in expected[i] or expected[i][field] is None:
                continue
            # A branch name is scoped to its own choice, never globally.
            gold = (expected[i].get('choice_group_id'), expected[i][field]) if field == 'branch_id' else expected[i][field]
            observed = (actual[j].get('choice_group_id'), actual[j].get(field)) if field == 'branch_id' else actual[j].get(field)
            forward.setdefault(gold, {}).setdefault(observed, []).append((i, j))
            reverse.setdefault(observed, {}).setdefault(gold, []).append((i, j))
        for mapping in (forward, reverse):
            for destinations in mapping.values():
                if len(destinations) > 1:
                    invalid.update(pair for group in destinations.values() for pair in group)
    return invalid


def compare_annotations(label: dict, observations: list[dict]) -> dict:
    if label.get("annotation_status") != "reviewed" or label.get("annotator_type") not in {"human", "owner", "independent_agent", "implementation_agent"}:
        raise ValueError("Only reviewed annotations with a declared reviewer support comparison")
    scope = set(label.get("roles_in_scope", ["PLANNED", "OPTIONAL"]))
    expected = [item for item in label["activities"] if item.get("role", "PLANNED") in scope]
    actual = [item for item in observations if item["role"] in scope and item.get("name")]
    if label.get("acceptance_eligible"):
        validate_formal_annotation(label)
    visit_pairs = match_occurrences(expected, actual, _matches_visit)
    pairs = [(i, j) for i, j in visit_pairs if _matches_visit(expected[i], actual[j])]
    available = set(range(len(actual))) - {j for _, j in pairs}
    # Parent identity is a visit, never just its POI or display name. Repeatedly
    # reject orphaned dependencies so a missing parent cannot validate a child.
    while True:
        identities = {expected[i].get("id"): actual[j].get("mention_id") for i, j in pairs if expected[i].get("id")}
        invalid = {(i, j) for i, j in pairs if any(expected[i].get(gold_key) is not None
                   and (identities.get(expected[i][gold_key]) is None
                        or actual[j].get(actual_key) != identities[expected[i][gold_key]])
                   for gold_key, actual_key in (("parent_id", "parent_mention_id"), ("replaces_id", "replaces_mention_id")))}
        invalid.update(_choice_membership_conflicts(expected, actual, pairs))
        if not invalid:
            break
        pairs = [pair for pair in pairs if pair not in invalid]
        available.update(j for _, j in invalid)
    identity_total = sum(bool(item.get("expected_poi_ids")) for item in expected)
    correct_identity = 0
    wrong_identity = 0
    identity_assessed = set()
    # A wrong role/day/parent does not exempt an auto-confirmed identity from
    # assessment. Locate it independently by its literal visit/name evidence.
    for actual_index, item in enumerate(observations):
        if item.get('poi_id') is None:
            continue
        candidates = []
        for gold in label["activities"]:
            if not gold.get('expected_poi_ids'):
                continue
            identity_gold = {key: gold[key] for key in ('name', 'acceptable_names', 'span_start', 'span_end', 'equivalent_reference_spans') if key in gold}
            identity_gold.update(day_index=item.get('day_index') if 'span_start' in gold else gold['day_index'], role=item['role'])
            if _matches_visit(identity_gold, item):
                candidates.append(gold)
        if len(candidates) != 1:
            continue
        identity_assessed.add(actual_index)
        equal = _amap_id(item['poi_id']) in {_amap_id(value) for value in candidates[0]['expected_poi_ids']}
        correct_identity += int(equal)
        wrong_identity += int(not equal)
    groups = {}
    for i, j in pairs:
        key = (expected[i]["day_index"], expected[i].get("parent_id"), expected[i].get("role", "PLANNED"),
               expected[i].get("choice_group_id"), expected[i].get("branch_id"))
        groups.setdefault(key, []).append((i, j))
    order_errors = set()
    for group in groups.values():
        group.sort(key=lambda pair: expected[pair[0]].get("order_index", pair[0]))
        for offset, left in enumerate(group):
            for right in group[offset + 1:]:
                if actual[left[1]].get("sequence_index", left[1]) >= actual[right[1]].get("sequence_index", right[1]):
                    order_errors.update((left, right))
    ordered = not order_errors
    planned_pairs = [(i, j) for i, j in pairs if expected[i].get("role", "PLANNED") == "PLANNED"
                     and expected[i].get("parent_id") is None and expected[i].get("relation_type") != "INTERNAL_DETAIL"]
    planned_ordered = not any(pair in order_errors for pair in planned_pairs)
    planned_expected = sum(item.get("role", "PLANNED") == "PLANNED" and item.get("parent_id") is None
                           and item.get("relation_type") != "INTERNAL_DETAIL" for item in expected)
    planned_observed = sum(item["role"] == "PLANNED" and not item.get("parent_mention_id")
                           and item.get("relation_type") != "INTERNAL_DETAIL" for item in actual)
    pairs = [pair for pair in pairs if pair not in order_errors]
    available.update(j for _, j in order_errors)
    by_role = {}
    for role in sorted(scope):
        role_expected = sum(item.get("role", "PLANNED") == role for item in expected)
        role_observed = sum(item["role"] == role for item in actual)
        role_matched = sum(expected[i].get("role", "PLANNED") == role for i, _j in pairs)
        by_role[role] = {"expected": role_expected, "observed": role_observed, "matched": role_matched,
            "recall": role_matched / role_expected if role_expected else None,
            "precision": role_matched / role_observed if role_observed else None}
    by_hierarchy = {}
    for hierarchy in sorted({item.get("hierarchy_level", "unspecified") for item in expected}):
        total = sum(item.get("hierarchy_level", "unspecified") == hierarchy for item in expected)
        matched = sum(expected[i].get("hierarchy_level", "unspecified") == hierarchy for i, _j in pairs)
        by_hierarchy[hierarchy] = {"expected": total, "matched": matched, "recall": matched / total}
    visible = sum(actual[j].get("visible_correct") is True and
                  (not expected[i].get("expected_poi_ids") or
                   _amap_id(actual[j].get("poi_id") or "") in {_amap_id(value) for value in expected[i]["expected_poi_ids"]})
                  for i, j in pairs)
    unassessed = sum(item.get('poi_id') is not None and j not in identity_assessed for j, item in enumerate(observations))
    return {"visit_matches": len(visit_pairs), "visit_recall": len(visit_pairs) / len(expected) if expected else None,
        "gold_status": "DEVELOPMENT_REVIEWED" if label.get("annotator_type") == "implementation_agent" else "INDEPENDENT_REVIEWED",
        "expected_places": len(expected), "observed_places": len(actual),
        "visible_correct": visible, "visibility_assessed": any("visible_correct" in item for item in observations),
        "matched_places": len(pairs), "missing_places": len(expected) - len(pairs), "extra_or_misassigned_places": len(available),
        "semantic_recall": len(pairs) / len(expected) if expected else None,
        "semantic_precision": len(pairs) / len(actual) if actual else None,
        "order_correct": len(pairs) == len(expected) and ordered,
        "retained_order_correct": ordered,
        "semantic_exact": len(pairs) == len(expected) == len(actual) and ordered and not wrong_identity and not unassessed
            and (not any("visible_correct" in item for item in observations)
                 or (visible == len(expected) and correct_identity == identity_total)),
        "planned_order_correct": len(planned_pairs) == planned_expected and planned_ordered,
        "retained_planned_order_correct": planned_ordered,
        "planned_exact": len(planned_pairs) == planned_expected == planned_observed and planned_ordered,
        "poi_labeled_count": identity_total, "poi_identity_correct": correct_identity, "wrong_auto_confirmations": wrong_identity,
        "unassessed_auto_confirmations": unassessed,
        "by_role": by_role, "by_hierarchy": by_hierarchy}


def summarize_measurements(rows: list[dict]) -> dict:
    annotated = [row for row in rows if row.get("gold_status") in {"INDEPENDENT_REVIEWED", "DEVELOPMENT_REVIEWED"}]
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
    by_hierarchy = {}
    for hierarchy in sorted({hierarchy for row in annotated for hierarchy in row.get("by_hierarchy", {})}):
        total = sum(row.get("by_hierarchy", {}).get(hierarchy, {}).get("expected", 0) for row in annotated)
        hierarchy_matched = sum(row.get("by_hierarchy", {}).get(hierarchy, {}).get("matched", 0) for row in annotated)
        by_hierarchy[hierarchy] = {"expected": total, "matched": hierarchy_matched, "recall": hierarchy_matched / total if total else None}
    def percentile(fraction):
        return elapsed[max(0, ceil(len(elapsed) * fraction) - 1)] if elapsed else None
    def known_usage(key):
        return sum(row.get("usage", {}).get(key) or 0 for row in rows)
    def complete_usage(key):
        return bool(rows) and all(isinstance(row.get("usage", {}).get(key), int)
            and not row.get("usage", {}).get(key + "_unknown_calls", 0) for row in rows)
    return {"runs": len(rows), "whole_article_correct": sum(whole_article_passed(row) for row in rows), "completed": sum(row["status"] == "COMPLETED" for row in rows),
        "expected_visits": expected, "observed_visits": observed, "matched_visits": matched,
        "public_projection_assessed_runs": sum(row.get("visibility_assessed", False) for row in annotated),
        "public_projection_visible_visits": sum(row.get("visible_correct", 0) for row in annotated),
        "public_projection_recall": (sum(row.get("visible_correct", 0) for row in annotated) / expected
            if expected and any(row.get("visibility_assessed") for row in annotated) else None),
        "errors": sum(row["status"] != "COMPLETED" for row in rows), "annotated_runs": len(annotated), "by_role": by_role,
        "annotation_review_types": dict(Counter(row["gold_status"] for row in annotated)),
        "by_hierarchy": by_hierarchy,
        "semantic_exact_runs": sum(row.get("semantic_exact") is True and row.get("status") == "COMPLETED"
                                    and not row.get("unprocessed_count", 0) for row in annotated) if annotated else None,
        "structure_correct_rate": (sum(row.get("semantic_exact") is True and row.get("status") == "COMPLETED"
                                       and not row.get("unprocessed_count", 0) for row in annotated) / len(annotated)) if annotated else None,
        "planned_exact_runs": sum(row.get("planned_exact") is True for row in annotated)
            if any(isinstance(row.get("planned_exact"), bool) for row in annotated) else None,
        "planned_order_correct_runs": sum(row.get("planned_order_correct") is True for row in annotated)
            if any(isinstance(row.get("planned_order_correct"), bool) for row in annotated) else None,
        "semantic_recall": matched / expected if expected else None,
        "semantic_precision": matched / observed if observed else None,
        "missing_places": sum(row["missing_places"] for row in annotated) if annotated else None,
        "extra_or_misassigned_places": sum(row["extra_or_misassigned_places"] for row in annotated) if annotated else None,
        "poi_labeled_count": sum(row["poi_labeled_count"] for row in annotated),
        "wrong_auto_confirmations": (sum(row["wrong_auto_confirmations"] for row in annotated)
            if any(row["poi_labeled_count"] for row in annotated) else None),
        "unassessed_auto_confirmations": (sum(row['unassessed_auto_confirmations'] for row in annotated)
            if annotated and all('unassessed_auto_confirmations' in row for row in annotated) else None),
        "latency_p50_ms": percentile(0.5), "latency_p95_ms": percentile(0.95),
        "latency_percentile_method": "nearest_rank", "latency_count": len(elapsed),
        "error_categories": dict(Counter(row.get("error_category", "UNKNOWN") for row in rows if row["status"] != "COMPLETED")),
        "model_calls": known_usage("external_calls") if complete_usage("external_calls") else None,
        "known_model_calls": known_usage("external_calls"),
        "unknown_model_call_runs": sum(row.get("usage", {}).get("external_calls") is None for row in rows),
        "usage_complete": complete_usage("input_tokens") and complete_usage("output_tokens"),
        "input_tokens": known_usage("input_tokens") if complete_usage("input_tokens") else None,
        "output_tokens": known_usage("output_tokens") if complete_usage("output_tokens") else None,
        "known_input_tokens": known_usage("input_tokens"), "known_output_tokens": known_usage("output_tokens"),
        "reasoning_tokens": known_usage("reasoning_tokens") if complete_usage("reasoning_tokens") else None,
        "known_reasoning_tokens": known_usage("reasoning_tokens"),
        "unknown_input_token_calls": sum(row.get("usage", {}).get("input_tokens_unknown_calls", 0) for row in rows),
        "unknown_output_token_calls": sum(row.get("usage", {}).get("output_tokens_unknown_calls", 0) for row in rows),
        "unknown_reasoning_token_calls": sum(row.get("usage", {}).get("reasoning_tokens_unknown_calls", 0) for row in rows)}
