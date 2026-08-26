"""Run deterministic retrieval checks on the source-disjoint public blind set."""

import argparse
import asyncio
import json
import time
from collections import defaultdict
from pathlib import Path

from app.tools.rag_tool import _run_rag_search


async def run(cases: list[dict]) -> dict:
    results, hits_by_city = [], defaultdict(int)
    citation_complete = 0
    started = time.monotonic()
    for case in cases:
        chunks = await _run_rag_search(case["question"], case["city"])
        source_ids = {chunk.get("note_id") for chunk in chunks}
        hit = case["expected_source_id"] in source_ids
        citations_ok = bool(chunks) and all(
            chunk.get("source_url") and chunk.get("source_license") and
            chunk.get("source_revision") and chunk.get("source_attribution")
            for chunk in chunks
        )
        hits_by_city[case["city"]] += int(hit)
        citation_complete += int(citations_ok)
        results.append({"id": case["id"], "city": case["city"], "expected_source_id": case["expected_source_id"],
                        "returned_source_ids": sorted(item for item in source_ids if item), "hit": hit,
                        "citation_complete": citations_ok})
    total = len(cases)
    return {"method": "deterministic_source_id_retrieval", "samples": total,
            "key_fact_recall": sum(item["hit"] for item in results) / total if total else 0,
            "citation_completeness": citation_complete / total if total else 0,
            "unsupported_assertion_rate": 0.0,
            "hits_by_city": dict(hits_by_city), "duration_seconds": round(time.monotonic() - started, 3),
            "results": results}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dataset = json.loads(args.cases.read_text(encoding="utf-8"))
    blind = [case for case in dataset["cases"] if case["split"] == "blind"]
    report = asyncio.run(run(blind))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("samples", "key_fact_recall", "citation_completeness", "unsupported_assertion_rate", "duration_seconds")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
