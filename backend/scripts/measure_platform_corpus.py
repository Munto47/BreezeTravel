"""Opt-in layered live measurement of platform-generated inputs.

The collector's response is an input, never a gold answer. Without reviewed
independent annotations this reports operational coverage only, not accuracy.
``full`` measures semantic extraction and real POI resolution; it does not
claim browser, database, routes, dining, hotel or human usability validation.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values

from scripts.collect_platform_corpus import ROOT, save_json
from scripts.platform_corpus_metrics import compare_annotations, summarize_measurements


def runtime_file_hashes(runtime_root: Path = ROOT) -> dict[str, str]:
    base = runtime_root / "backend/app/trip_understanding"
    unrelated = {"daily_dining.py", "dining_jobs.py", "dining.py", "stay.py", "stay_repository.py", "map_repository.py",
        "map_render.py", "repository.py", "commands.py", "overnight_context.py", "hotel_brand_registry_v1.json"}
    files = sorted(path for path in base.rglob("*") if path.is_file() and path.suffix in {".py", ".json", ".jsonl", ".md"}
        and path.name not in unrelated)
    return {path.relative_to(base).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def source_fingerprint(runtime_root: Path = ROOT) -> str:
    digest = hashlib.sha256()
    for name, file_hash in runtime_file_hashes(runtime_root).items():
        digest.update(name.encode())
        digest.update(file_hash.encode())
    return digest.hexdigest()


def freeze_runtime(runtime_root: Path, artifact_directory: Path) -> Path:
    """Copy only runtime source/data, never dotenv files, databases or caches."""
    expected = runtime_file_hashes(runtime_root)
    fingerprint = source_fingerprint(runtime_root)
    target = artifact_directory.resolve() / ("runtime-" + fingerprint[:20] + "-" + uuid4().hex[:8])
    if target.exists():
        raise FileExistsError("A runtime snapshot must never overwrite an existing directory")
    source_app = runtime_root / "backend/app"
    for source_file in source_app.rglob("*"):
        if not source_file.is_file() or source_file.suffix not in {".py", ".json", ".jsonl", ".md"}:
            continue
        if any(part.startswith(".") or part == "__pycache__" for part in source_file.relative_to(source_app).parts):
            continue
        destination = target / "backend/app" / source_file.relative_to(source_app)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination)
    if runtime_file_hashes(runtime_root) != expected or runtime_file_hashes(target) != expected:
        raise RuntimeError("Source changed during freezing; no model calls were made")
    save_json(target / "runtime-source.json", {"origin": str(runtime_root), "source_fingerprint": fingerprint,
        "copied_content": "APP_SOURCE_AND_VERSIONED_DATA_ONLY", "dotenv_copied": False})
    return target


def read_corpus(manifest_path: Path, *, limit: int | None = None, case_ids: set[str] | None = None,
                split: str = "development") -> list[dict]:
    folder = manifest_path.resolve().parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("provenance") != "platform_generated":
        raise ValueError("Expected a recorded platform-generated corpus")
    if case_ids:
        selected = [row for row in manifest["cases"] if row["id"] in case_ids]
        if len(selected) != len(case_ids) or any(row.get("split") != split for row in selected):
            raise ValueError("Every requested case must exist in the explicitly selected split")
    rows = []
    for row in sorted(manifest["cases"], key=lambda row: row["id"]):
        if row.get("split") != split or row["status"] != "COMPLETED" or (case_ids and row["id"] not in case_ids):
            continue
        artifact = (folder / row["artifact"]).resolve()
        if not artifact.is_relative_to(folder):
            raise ValueError("Corpus artifact must remain within its collection directory")
        record = json.loads(artifact.read_text(encoding="utf-8"))
        text = record.get("output")
        if not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != row["output_sha256"]:
            raise ValueError("Recorded platform output does not match its immutable fingerprint")
        rows.append({**row, "text": text})
        if limit and len(rows) >= limit:
            break
    return rows


async def measure(args) -> int:
    if args.output.exists():
        raise FileExistsError("Existing measurements must be preserved")
    runtime_root = args.runtime_root.resolve()
    if not (runtime_root / "backend/app/trip_understanding/experience_inference.py").is_file():
        raise ValueError("Selected runtime root has no experience inference implementation")
    runtime_origin = runtime_root
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=runtime_origin, capture_output=True, text=True, check=True).stdout.strip()
    if args.freeze_runtime:
        runtime_root = freeze_runtime(runtime_root, args.output.parent / "runtime-snapshots")
    sys.path.insert(0, str(runtime_root / "backend"))
    from app.trip_understanding.amap_place import AmapPlaceResolver
    from app.trip_understanding.experience_inference import ExperienceQwenProvider
    from app.trip_understanding.pipeline import TripUnderstandingPipeline

    cases = read_corpus(args.manifest, limit=args.limit, case_ids=set(args.case_ids.split(",")) if args.case_ids else None,
        split=args.split)
    if not cases:
        raise ValueError("No completed corpus records match this measurement")
    labels = {}
    labels_sha256 = None
    labels_source_sha256 = None
    if args.labels:
        label_bytes = args.labels.read_bytes()
        labels_source_sha256 = hashlib.sha256(label_bytes).hexdigest()
        selected_ids = {case["id"] for case in cases}
        labels = {label["case_id"]: label for label in json.loads(label_bytes)["annotations"] if label["case_id"] in selected_ids}
        label_snapshot = args.output.with_suffix(".labels.json")
        save_json(label_snapshot, {"annotations": list(labels.values())})
        labels_sha256 = hashlib.sha256(label_snapshot.read_bytes()).hexdigest()
    for case in cases:
        label = labels.get(case["id"])
        if label and (label.get("source_sha256") != case["output_sha256"] or
            label.get("annotation_status") != "reviewed" or label.get("annotator_type") not in {"human", "owner", "independent_agent"}):
            raise ValueError("Gold annotations must match the unchanged source and be independently reviewed")
    values = dotenv_values(args.config_env, interpolate=False)
    model = ExperienceQwenProvider(api_key=values.get("QWEN_API_KEY") or "", base_url=values.get("QWEN_API_URL") or "",
        model=values.get("TRIP_UNDERSTANDING_QWEN_MODEL") or "",
        deadline_seconds=float(values.get("TRIP_UNDERSTANDING_QWEN_DEADLINE_SECONDS") or 60),
        max_output_tokens=int(values.get("TRIP_UNDERSTANDING_QWEN_MAX_OUTPUT_TOKENS") or 4096),
        input_cny_per_million=float(values["TRIP_UNDERSTANDING_QWEN_INPUT_CNY_PER_MILLION"]) if values.get("TRIP_UNDERSTANDING_QWEN_INPUT_CNY_PER_MILLION") else None,
        output_cny_per_million=float(values["TRIP_UNDERSTANDING_QWEN_OUTPUT_CNY_PER_MILLION"]) if values.get("TRIP_UNDERSTANDING_QWEN_OUTPUT_CNY_PER_MILLION") else None)
    original_prompt_sha256 = hashlib.sha256(model.prompt.encode()).hexdigest()
    if args.prompt_path:
        model.prompt = args.prompt_path.read_text(encoding="utf-8")
        if not model.prompt.strip():
            raise ValueError("A private experimental prompt cannot be empty")
    prompt_snapshot = args.output.with_suffix(".prompt.md")
    if prompt_snapshot.exists():
        raise FileExistsError("Existing prompt snapshots must be preserved")
    prompt_snapshot.parent.mkdir(parents=True, exist_ok=True)
    prompt_snapshot.write_text(model.prompt, encoding="utf-8")
    # Private opt-in evidence for attributing extraction vs validator errors.
    # Record message bodies and provider outputs, never authentication headers.
    raw_directory = args.output.with_suffix("").with_name(args.output.stem + "-raw-calls")
    capture_context = {"case_id": None, "repeat": 0, "call": 0}
    original_create = model.client.chat.completions.create
    thinking_budget = getattr(args, "thinking_budget", None)
    async def recorded_create(**kwargs):
        if thinking_budget is not None:
            # Explicit private experiment, with the runtime answer cap and deadline
            # unchanged. No account, service or production model configuration changes.
            kwargs["extra_body"] = {**(kwargs.get("extra_body") or {}),
                "enable_thinking": True, "thinking_budget": thinking_budget}
        capture_context["call"] += 1
        path = raw_directory / f"{capture_context['case_id']}-r{capture_context['repeat']}-call{capture_context['call']:02d}.json"
        if path.exists():
            raise ValueError("Refusing to overwrite an existing raw model call")
        record = {"case_id": capture_context["case_id"], "repeat": capture_context["repeat"], "call": capture_context["call"],
            "provenance": "platform_generated_input_actual_runtime_response", "messages": kwargs.get("messages"),
            "model": kwargs.get("model"), "max_tokens": kwargs.get("max_tokens"), "status": "STARTED",
            "temperature": kwargs.get("temperature"), "extra_body": kwargs.get("extra_body"),
            "response_format": kwargs.get("response_format")}
        if args.record_raw_calls:
            save_json(path, record)
        try:
            response = await original_create(**kwargs)
            record.update(status="COMPLETED", choices=[{"finish_reason": choice.finish_reason,
                "content": choice.message.content} for choice in response.choices],
                usage=response.usage.model_dump() if response.usage else None)
            return response
        except BaseException as error:
            record.update(status="FAILED", error_category=type(error).__name__)
            raise
        finally:
            if args.record_raw_calls:
                save_json(path, record)
    if args.record_raw_calls or thinking_budget is not None:
        model.client.chat.completions.create = recorded_create
    resolver = AmapPlaceResolver(api_key=values.get("AMAP_API_KEY") or "") if args.mode == "full" else None
    pipeline = TripUnderstandingPipeline(model, resolver) if resolver else None
    fingerprint = source_fingerprint(runtime_root)
    report = {"schema_version": "platform-corpus-measurement-v1", "mode": args.mode, "source_commit": commit,
        "source_fingerprint": fingerprint, "started_at": datetime.now(timezone.utc).isoformat(),
        "runtime_root": str(runtime_root), "runtime_files": runtime_file_hashes(runtime_root),
        "runtime_origin": str(runtime_origin), "frozen_runtime": args.freeze_runtime, "selected_split": args.split,
        "runtime_snapshot_manifest": (runtime_root / "runtime-source.json").exists(),
        "prompt_override": bool(args.prompt_path), "prompt_sha256": hashlib.sha256(model.prompt.encode()).hexdigest(),
        "original_prompt_sha256": original_prompt_sha256,
        "raw_calls_recorded": args.record_raw_calls,
        "thinking_budget_override": thinking_budget,
        "answer_token_cap": model.max_output_tokens,
        "model": model.model, "provenance": "platform_generated", "measurement": "SEMANTIC_AND_POI_NO_API_OR_ROUTES" if resolver else "SEMANTIC_ONLY",
        "cases": [], "gold_source": "independent_annotations" if labels else "NONE", "labels_sha256": labels_sha256,
        "labels_source_sha256": labels_source_sha256}
    def save():
        report["summary"] = summarize_measurements(report["cases"])
        report["by_city"] = {city: summarize_measurements([row for row in report["cases"] if row["city"] == city])
                             for city in sorted({row["city"] for row in report["cases"]})}
        report["by_split"] = {split: summarize_measurements([row for row in report["cases"] if row["split"] == split])
                              for split in sorted({row["split"] for row in report["cases"]})}
        report["repeat_stability"] = []
        for case_id in sorted({row["case_id"] for row in report["cases"]}):
            repeated = [row for row in report["cases"] if row["case_id"] == case_id]
            signatures = {json.dumps([{key: item.get(key) for key in ("name", "day_index", "role", "branch_label")}
                for item in row.get("observations", [])], sort_keys=True, ensure_ascii=False)
                for row in repeated if row["status"] == "COMPLETED"}
            counts = [len([item for item in row.get("observations", []) if item.get("name") and item["role"] == "PLANNED"])
                      for row in repeated if row["status"] == "COMPLETED"]
            report["repeat_stability"].append({"case_id": case_id, "runs": len(repeated), "semantic_variants": len(signatures),
                "planned_count_min": min(counts) if counts else None, "planned_count_max": max(counts) if counts else None,
                "error_runs": sum(row["status"] != "COMPLETED" for row in repeated)})
        save_json(args.output, report)
    try:
        for repeat in range(1, args.repeat + 1):
            for case in cases:
                if source_fingerprint(runtime_root) != fingerprint:
                    raise RuntimeError("Runtime source changed; remaining calls stopped")
                started = time.perf_counter()
                capture_context.update(case_id=case["id"], repeat=repeat, call=0)
                row = {"case_id": case["id"], "city": case["city"], "family_id": case["family_id"], "split": case["split"],
                    "repeat": repeat, "source_sha256": case["output_sha256"], "status": "FAILED", "gold_status": "NOT_ANNOTATED"}
                try:
                    output = await pipeline.run(case["text"]) if pipeline else None
                    proposal = output.proposal if output else await model.propose(case["text"])
                    places = {activity.compiled.mention.mention_id: activity.place for activity in output.activities} if output else {}
                    observations = [{"name": item.atomic_place_name, "day_index": item.day_index, "role": item.role.value,
                        "sequence_index": item.sequence_index, "branch_label": getattr(item, "branch_label", None),
                        "span_start": item.span_start, "span_end": item.span_end,
                        "parent_mention_id": getattr(item, "parent_mention_id", None), "relation_type": getattr(item, "relation_type", None),
                        "poi_id": places[item.mention_id].canonical_place_id if places.get(item.mention_id) else None}
                        for item in proposal.mentions]
                    row.update(status="COMPLETED", observations=observations, usage={key: proposal.binding.get(key) for key in
                        ("external_calls", "repair_call_count", "input_tokens", "output_tokens", "estimated_cost_cny")},
                        unprocessed_count=proposal.unprocessed_count, semantic_diagnostic_counts=proposal.binding.get("semantic_diagnostic_counts", {}),
                        diagnostics=[item.model_dump() for item in getattr(proposal, "diagnostics", [])])
                    if output:
                        coverage = getattr(output.public_result, "coverage", None)
                        row["coverage"] = coverage.model_dump() if coverage else None
                        row["resolution"] = output.resolution_receipt
                    if case["id"] in labels:
                        row.update(compare_annotations(labels[case["id"]], observations))
                except Exception as error:
                    category = getattr(error, "category", None)
                    row["error_category"] = category if isinstance(category, str) and category.isupper() else type(error).__name__
                    binding = getattr(error, "provider_binding", {})
                    row["usage"] = {key: binding.get(key) for key in ("external_calls", "repair_call_count", "input_tokens", "output_tokens")}
                    if case["id"] in labels:
                        row.update(compare_annotations(labels[case["id"]], []))
                row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
                report["cases"].append(row)
                save()
                print(json.dumps({key: row.get(key) for key in ("case_id", "repeat", "status", "semantic_exact", "unprocessed_count", "elapsed_ms")}), flush=True)
    finally:
        if pipeline:
            await pipeline.aclose()
        else:
            await model.aclose()
        report["source_unchanged"] = source_fingerprint(runtime_root) == fingerprint
        current_files = runtime_file_hashes(runtime_root)
        report["changed_runtime_files"] = sorted(name for name in report["runtime_files"].keys() | current_files.keys()
            if report["runtime_files"].get(name) != current_files.get(name))
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
    print(json.dumps({"summary": report["summary"]}), flush=True)
    return int(report["summary"]["errors"] > 0 or not report["source_unchanged"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--config-env", type=Path, default=ROOT / ".local-artifacts/experience/experience.env")
    parser.add_argument("--runtime-root", type=Path, default=ROOT,
        help="Read-only checkout whose backend runtime is measured; no service or source modification")
    parser.add_argument("--record-raw-calls", action="store_true",
        help="Save original request messages and actual model JSON privately for validator attribution")
    parser.add_argument("--freeze-runtime", action="store_true",
        help="Measure an immutable private copy of runtime source/data while collaborators keep working")
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--prompt-path", type=Path,
        help="Private experiment only: override this provider instance's prompt without modifying runtime files")
    parser.add_argument("--thinking-budget", type=int, choices=(512, 1024, 2048),
        help="Private same-model experiment only: enable bounded thinking; retain runtime answer cap and deadline")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("semantic", "full"), default="semantic")
    parser.add_argument("--repeat", type=int, choices=(1, 2, 3, 4, 5), default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-ids")
    parser.add_argument("--split", choices=("development", "validation", "holdout", "long_tail"), default="development",
        help="Select one prespecified family split before reading any source; defaults to development")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    logging.disable(logging.CRITICAL)
    return asyncio.run(measure(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_category": type(error).__name__, "message": "Measurement stopped; private details omitted"}))
        raise SystemExit(2) from None
