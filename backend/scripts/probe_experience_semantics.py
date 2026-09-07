"""Private actual semantic diagnostics for explicitly supplied development inputs."""
import argparse
import asyncio
import json
import time
from pathlib import Path

from dotenv import dotenv_values

from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.pipeline import _model_activity_cities
from scripts.collect_platform_corpus import ROOT, save_json
from scripts.measure_platform_corpus import source_fingerprint


async def run(args):
    if args.output.exists():
        raise FileExistsError("Existing real diagnostics must be preserved")
    inputs = json.loads(args.inputs.read_text(encoding="utf-8"))
    values = dotenv_values(ROOT / ".local-artifacts/experience/experience.env", interpolate=False)
    model = ExperienceQwenProvider(api_key=values.get("QWEN_API_KEY") or "", base_url=values.get("QWEN_API_URL") or "",
        model=values.get("TRIP_UNDERSTANDING_QWEN_MODEL") or "", deadline_seconds=60, max_output_tokens=4096)
    original = model.client.chat.completions.create
    calls = []
    async def record(**kwargs):
        call = {"request": kwargs, "status": "STARTED"}
        calls.append(call)
        try:
            response = await original(**kwargs)
            call.update(status="COMPLETED", choices=[{"content": value.message.content, "finish_reason": value.finish_reason} for value in response.choices],
                usage=response.usage.model_dump() if response.usage else None)
            return response
        except BaseException as error:
            call.update(status="FAILED", error_category=type(error).__name__)
            raise
    model.client.chat.completions.create = record
    report = {"provenance": "ACTUAL_MODEL_DIAGNOSTIC_AGENT_AUTHORED_INPUT", "acceptance_claim": False,
        "source_before": source_fingerprint(), "model": model.model, "cases": []}
    try:
        for case in inputs["cases"]:
            calls.clear()
            started = time.perf_counter()
            row = {"id": case["id"], "input_text": case["input_text"]}
            try:
                proposal = await model.propose(case["input_text"])
                row.update(status="COMPLETED", proposal=proposal.model_dump(mode="json"),
                    selected_cities=[{"name": item.atomic_place_name, "day": item.day_index,
                        "cities": _model_activity_cities(case["input_text"], proposal, item)} for item in proposal.mentions])
            except Exception as error:
                row.update(status="FAILED", error_category=type(error).__name__, binding=getattr(error, "provider_binding", {}))
            row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            row["raw_calls"] = list(calls)
            report["cases"].append(row)
            save_json(args.output, report)
            print(json.dumps({"id": case["id"], "status": row["status"],
                "unprocessed": row.get("proposal", {}).get("unprocessed_count"),
                "diagnostics": [item["category"] for item in row.get("proposal", {}).get("diagnostics", [])]}), flush=True)
    finally:
        await model.aclose()
        report["source_after"] = source_fingerprint()
        report["source_unchanged"] = report["source_after"] == report["source_before"]
        save_json(args.output, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
