"""Fast, offline checks for public evidence and citation contracts."""

import asyncio
from pathlib import Path

from app.agents.nodes.tool_executor import _citations_from_chunks
from app.api.evidence import latest_evidence
from scripts.ingest_public_notes import load_records


def test_public_source_example_is_explicitly_not_a_real_corpus():
    example = Path(__file__).parents[1] / "data" / "public_sources.example.jsonl"
    record = load_records(example)[0]
    assert record["source_url"] == "https://example.org/canonical-source"
    assert "do not claim it is a live public corpus" in record["content"]


def test_citations_are_display_safe_and_keep_provenance():
    citations = _citations_from_chunks([{
        "note_id": "public-hz-1", "chunk_idx": 2, "title": "Official source",
        "source_url": "https://example.org/source", "content": "x" * 400,
        "rrf_score": 0.12, "retrieval_sources": ["dense", "sparse"],
        "source_license": "CC BY 4.0", "corpus_kind": "public",
    }])
    assert citations[0]["source_id"] == "public-hz-1:2"
    assert len(citations[0]["excerpt"]) == 320
    assert citations[0]["corpus_kind"] == "public"


def test_latest_evidence_discloses_candidate_boundary():
    evidence = asyncio.run(latest_evidence())
    assert evidence["status"] == "three_city_local_rc1_candidate"
    assert evidence["three_city_candidate"]["overall_rc1_passed"] is False
    assert evidence["three_city_candidate"]["model_panel_quality_passed"] is False
    assert evidence["historical_metrics"]["router_both_accuracy"] == 0.6
