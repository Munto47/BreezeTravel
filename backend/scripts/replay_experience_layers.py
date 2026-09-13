"""Replay private saved model responses through current validation and projection.

No model, map, database or browser calls. The synthetic place resolver only
isolates whether meaning survives the pipeline; it cannot establish identity,
coordinates, real visibility, or the quality of a fresh model response.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from scripts.collect_platform_corpus import save_json
from scripts.measure_platform_corpus import read_corpus
from scripts.platform_corpus_metrics import compare_annotations


class UnrecordedRequestError(RuntimeError):
    pass


def request_key(messages, max_tokens):
    # Match the actual input/repair conversation, not code or prompt hashes.
    # System changes are reported separately: this is old-output replay only.
    return json.dumps([max_tokens, [m for m in messages if m.get("role") != "system"]],
                      ensure_ascii=False, sort_keys=True)


class SavedResponses:
    def __init__(self, records):
        self.pending = defaultdict(deque)
        self.used = []
        self.unrecorded_requests = 0
        self.system_message_changes = 0
        for record in records:
            self.pending[request_key(record["messages"], record.get("max_tokens"))].append(record)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        queue = self.pending[request_key(kwargs["messages"], kwargs.get("max_tokens"))]
        if not queue:
            self.unrecorded_requests += 1
            raise UnrecordedRequestError("No saved response for this input and repair conversation")
        record = queue.popleft()
        self.used.append(record["call"])
        self.system_message_changes += int(
            [m for m in kwargs["messages"] if m.get("role") == "system"] !=
            [m for m in record["messages"] if m.get("role") == "system"])
        if record["status"] != "COMPLETED":
            raise RuntimeError("Saved model call did not complete")
        return SimpleNamespace(model=record.get("model"),
            usage=SimpleNamespace(**record["usage"]) if record.get("usage") else None,
            choices=[SimpleNamespace(finish_reason=c.get("finish_reason"),
                                     message=SimpleNamespace(content=c.get("content")))
                     for c in record.get("choices", [])])

    @property
    def unused_calls(self):
        return sorted(record["call"] for queue in self.pending.values() for record in queue)


def mention_view(mention):
    return {"key": mention.mention_id, "name": mention.atomic_place_name,
            "day_index": mention.day_index, "role": mention.role.value,
            "sequence_index": mention.sequence_index, "branch_label": mention.branch_label,
            "span_start": mention.span_start, "span_end": mention.span_end}


def stage_counts(rows):
    return {"total": len(rows), "named": sum(bool(r.get("name")) for r in rows),
            "by_role": dict(Counter(r["role"] for r in rows)),
            "by_day_and_role": dict(sorted(Counter(
                f"{r.get('day_index')}:{r['role']}" for r in rows).items()))}


def stage_delta(before, after):
    old = {r["key"]: r for r in before}
    new = {r["key"]: r for r in after}
    lost = [r for key, r in old.items() if key not in new]
    changed = [{"before": old[key], "after": new[key]} for key in old.keys() & new.keys()
               if any(old[key].get(field) != new[key].get(field) for field in ("name", "day_index", "role"))]
    return {"lost": lost,
            "added": [r for key, r in new.items() if key not in old],
            "changed": changed,
            "named_itinerary_lost": [r for r in lost if r.get("name") and r["role"] in {"PLANNED", "OPTIONAL"}],
            "named_itinerary_changed": [r for r in changed if r["before"].get("name") and
                                         r["before"]["role"] in {"PLANNED", "OPTIONAL"}]}


def expected_errors(label, observations):
    """Return every unmatched expectation and prediction, preserving duplicates."""
    scope = set(label.get("roles_in_scope", ["PLANNED", "OPTIONAL"]))
    actual = [r for r in observations if r.get("name") and r["role"] in scope]
    available = set(range(len(actual)))
    missing = []
    for item in label["activities"]:
        if item.get("role", "PLANNED") not in scope:
            continue
        names = {item["name"], *item.get("acceptable_names", [])}
        match = next((i for i in sorted(available) if actual[i]["name"] in names
                      and actual[i]["day_index"] == item["day_index"]
                      and actual[i]["role"] == item.get("role", "PLANNED")
                      and ("branch_label" not in item or actual[i].get("branch_label") == item["branch_label"])), None)
        if match is None:
            missing.append({k: item.get(k) for k in ("name", "day_index", "role", "branch_label")})
        else:
            available.remove(match)
    return {"missing_or_misassigned": missing,
            "extra_or_misassigned": [actual[i] for i in sorted(available)]}


def raw_inventory(records):
    """Per-call raw fields, never union repeated attempts into an accuracy score."""
    result = []
    for record in records:
        for choice_index, choice in enumerate(record.get("choices", [])):
            try:
                payload = json.loads(choice.get("content") or "")
                activities = payload.get("activities", [])
                rows = [{"key": f"{record['call']}:{choice_index}:{index}",
                         "name": item.get("place_name"), "day_index": item.get("day_index"),
                         "role": item.get("role", "UNSPECIFIED"),
                         "place_name_field": "MISSING" if "place_name" not in item else
                             "NULL" if item["place_name"] is None else "VALUE"}
                        for index, item in enumerate(activities)]
                result.append({"call": record["call"], "choice": choice_index,
                               "kind": "ACTIVITIES" if "activities" in payload else "STRUCTURE_OR_OTHER",
                               "counts": stage_counts(rows), "observations": rows})
            except (ValueError, TypeError, AttributeError):
                result.append({"call": record["call"], "choice": choice_index, "kind": "INVALID_JSON"})
    return result


def attribute_provider_omissions(label, observations, records):
    """Candidate causes, not gold judgments: attempts/aliases may disagree."""
    raw = []
    for record in records:
        for choice in record.get("choices", []):
            try:
                payload = json.loads(choice.get("content") or "")
                raw.extend({**item, "call": record["call"]} for item in payload.get("activities", []))
            except (ValueError, TypeError, AttributeError):
                continue
    rows = []
    for missing in expected_errors(label, observations)["missing_or_misassigned"]:
        gold = next(item for item in label["activities"] if all(
            item.get(k) == missing.get(k) for k in ("name", "day_index", "role")))
        names = {gold["name"], *gold.get("acceptable_names", [])}
        named = [item for item in raw if item.get("place_name") in names]
        correct = [item for item in named if item.get("role") == gold.get("role", "PLANNED")
                   and item.get("day_index") == gold["day_index"]]
        omitted_field = [item for item in raw if "place_name" not in item
                         and any(name in (item.get("source_quote") or "") for name in names)]
        null_field = [item for item in raw if "place_name" in item and item["place_name"] is None
                      and any(name in (item.get("source_quote") or "") for name in names)]
        variants = [item for item in raw if isinstance(item.get("place_name"), str)
                    and any(name in item["place_name"] or item["place_name"] in name for name in names)]
        cause = ("RAW_MATCH_REQUIRES_OCCURRENCE_OR_VALIDATOR_REVIEW" if correct else
                 "RAW_ROLE_OR_DAY_DIFFERENCE" if named else "RAW_NAME_FIELD_MISSING" if omitted_field else
                 "RAW_NAME_FIELD_EXPLICIT_NULL" if null_field else "RAW_NAME_VARIANT_REQUIRES_REVIEW" if variants else
                 "EXPECTED_NAME_NOT_EMITTED_IN_SAVED_RAW")
        rows.append({"expected": missing, "candidate_cause": cause,
                     "candidate_calls": sorted({item["call"] for item in
                                                (correct or named or omitted_field or null_field or variants)})})
    return {"counts": dict(Counter(row["candidate_cause"] for row in rows)), "rows": rows,
            "limit": "Raw attempts may disagree; exact occurrences and semantic judgments still require source review."}


class ConservationOnlyResolver:
    """Synthetic names/IDs to inspect semantic transport, no geographic truth."""
    async def resolve(self, *, city, atomic_place_name, category_hint=None):
        from app.trip_understanding.models import ResolvedPlace
        return ResolvedPlace(canonical_place_id=f"offline:{city}:{atomic_place_name}",
            name=atomic_place_name, category=category_hint or "地点", area_or_address="离线传递检查占位",
            provider_binding={"provider": "OFFLINE_CONSERVATION_ONLY", "city": city, "external_calls": 0})


async def replay_case(source, records, model_name, label=None):
    from app.trip_understanding.experience_inference import ExperienceQwenProvider
    from app.trip_understanding.pipeline import TripUnderstandingPipeline

    client = SavedResponses(records)
    provider = ExperienceQwenProvider(api_key="offline-placeholder", base_url="https://offline.invalid",
                                      model=model_name, client=client)
    captured = []
    binding = {}

    class CaptureProvider:
        async def propose(self, text):
            proposal = await provider.propose(text)
            captured.extend(mention_view(m) for m in proposal.mentions)
            binding.update(semantic_policy=proposal.binding.get("semantic_policy"),
                           unprocessed_count=proposal.unprocessed_count)
            return proposal

    pipeline = TripUnderstandingPipeline(CaptureProvider(), ConservationOnlyResolver())
    result = {"status": "FAILED", "raw_calls": raw_inventory(records)}
    try:
        output = await pipeline.run(source)
        compiled = [mention_view(a.compiled.mention) for a in output.activities]
        by_token = {a.compiled.public_activity_token: a for a in output.activities}
        public = []
        for day_index, day in enumerate(output.public_result.days, 1):
            for role, values in (("PLANNED", day.activities), ("OPTIONAL", day.alternatives)):
                for card in values:
                    activity = by_token[card.activity_token]
                    public.append({**mention_view(activity.compiled.mention), "name": card.name,
                                   "day_index": day_index, "role": role,
                                   "status": getattr(card, "status", "OPTIONAL")})
        stages = {"provider": captured, "pipeline": compiled, "public_projection": public,
                  "confirmed_projection": [r for r in public if r["role"] == "OPTIONAL" or r["status"] == "READY"]}
        result.update(status="REPLAYED", provider_binding=binding,
                      stages={name: {"counts": stage_counts(rows), "observations": rows,
                                     **({"semantic": compare_annotations(label, rows),
                                         "semantic_errors": expected_errors(label, rows)} if label else {})}
                              for name, rows in stages.items()},
                      provider_to_pipeline=stage_delta(captured, compiled),
                      pipeline_to_public=stage_delta(compiled, public),
                      resolution_status_counts=dict(Counter(a.resolution_status.value for a in output.activities)),
                      resolution_failure_counts=dict(Counter(str(a.resolver_receipt.get("failure_category"))
                          for a in output.activities if a.resolver_receipt.get("failure_category"))),
                      public_coverage=output.public_result.coverage.model_dump() if output.public_result.coverage else None)
        if label:
            result["provider_omission_candidates"] = attribute_provider_omissions(label, captured, records)
    except Exception as error:
        result.update(error_category=type(error).__name__)
    finally:
        await pipeline.aclose()
        await provider.aclose()
    result.update(recorded_calls_consumed=client.used, unused_recorded_calls=client.unused_calls,
                  unrecorded_requests=client.unrecorded_requests, system_message_changes=client.system_message_changes,
                  replay_complete=not client.unrecorded_requests and result["status"] == "REPLAYED")
    return result


def summarize(cases):
    result = {"cases": len(cases), "completed": sum(c.get("replay_complete", False) for c in cases),
              "external_calls": 0, "stages": {}}
    for name in ("provider", "pipeline", "public_projection", "confirmed_projection"):
        rows = [c["stages"][name] for c in cases]
        totals = {role: {key: sum(r.get("semantic", {}).get("by_role", {}).get(role, {}).get(key, 0)
                                  for r in rows) for key in ("expected", "observed", "matched")}
                  for role in ("PLANNED", "OPTIONAL")}
        result["stages"][name] = {"scored_cases": len(rows), "by_role": totals,
            "complete_planned_order": sum(r.get("semantic", {}).get("planned_order_correct", False) for r in rows)}
    for name in ("provider_to_pipeline", "pipeline_to_public"):
        result[name] = {key: sum(len(c.get(name, {}).get(key, [])) for c in cases)
                        for key in ("lost", "added", "changed", "named_itinerary_lost", "named_itinerary_changed")}
    result["provider_omission_candidates"] = dict(sum((Counter(c.get("provider_omission_candidates", {}).get("counts", {}))
                                                     for c in cases), Counter()))
    return result


async def run(args):
    if ".local-artifacts" not in args.output.resolve().parts:
        raise ValueError("Replay output contains private place names; use .local-artifacts")
    if args.output.exists():
        raise FileExistsError("Keep previous measurements; choose another output")
    measurement = json.loads(args.measurement.read_text(encoding="utf-8"))
    selected = measurement["cases"]
    if any(c.get("split") != "development" for c in selected):
        raise ValueError("Repair attribution only uses development inputs")
    sources = {c["id"]: c for c in read_corpus(args.manifest, case_ids={c["case_id"] for c in selected})}
    labels = {c["case_id"]: c for c in json.loads(args.labels.read_text(encoding="utf-8"))["annotations"]}
    report = {"measurement": "SAVED_MODEL_OUTPUT_CURRENT_VALIDATION_SYNTHETIC_PLACE_CONSERVATION",
              "started_at": datetime.now(timezone.utc).isoformat(), "external_calls": 0,
              "place_identity_measured": False, "browser_validation": "NOT_RUN", "fresh_model_quality": "NOT_RUN",
              "limitations": ["Raw call inventory includes repeated attempts; it is not a combined semantic score.",
                              "Synthetic place resolution measures transport only, never real identity or location quality.",
                              "Public payload may include unresolved placeholders; confirmed_projection is a transport proxy, not browser validation.",
                              "System prompt changes are reported; saved outputs cannot predict new model responses.",
                              "Unrecorded requests are never sent; incomplete replays remain failures."], "cases": []}
    for prior in selected:
        ident = prior["case_id"]
        paths = sorted(args.raw_directory.glob(f"{ident}-r{prior.get('repeat', 1)}-call*.json"))
        records = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
        row = await replay_case(sources[ident]["text"], records, measurement["model"], labels[ident])
        if "stages" not in row:
            row["stages"] = {name: {"counts": stage_counts([]), "observations": [],
                                   "semantic": compare_annotations(labels[ident], []),
                                   "semantic_errors": expected_errors(labels[ident], [])}
                             for name in ("provider", "pipeline", "public_projection", "confirmed_projection")}
        row.update(case_id=ident, repeat=prior.get("repeat", 1), city=prior["city"])
        report["cases"].append(row)
        print(json.dumps({"case_id": ident, "status": row["status"], "replay_complete": row["replay_complete"]}), flush=True)
    report.update(summary=summarize(report["cases"]), finished_at=datetime.now(timezone.utc).isoformat())
    save_json(args.output, report)
    print(json.dumps(report["summary"], ensure_ascii=False))
    return int(report["summary"]["completed"] != len(selected))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "measurement", "labels", "raw-directory", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    logging.disable(logging.CRITICAL)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_category": type(error).__name__, "message": "Replay stopped; private details omitted"}))
        raise SystemExit(2) from None
