"""Offline contract tests for the automated public-corpus pipeline."""

import json
from pathlib import Path

import pytest

from scripts import build_public_corpus as corpus
from scripts.ingest_public_notes import load_records


def test_clean_wikitext_removes_markup_without_losing_link_label():
    assert corpus._clean_wikitext("== See == [[West Lake|西湖]] {{template}}") == "西湖"


def test_write_outputs_is_hashed_and_auditable(tmp_path: Path):
    record = {
        "id": "wikivoyage-a", "title": "x", "city": "杭州", "content": "x" * 300,
        "source_url": "https://en.wikivoyage.org/w/index.php?oldid=1", "source_license": "CC BY-SA 4.0",
        "source_revision": "1", "source_attribution": "Wikivoyage contributors", "source_retrieved_at": "2026-01-01T00:00:00Z",
    }
    output, manifest = tmp_path / "sources.jsonl", tmp_path / "manifest.json"
    corpus.write_outputs([record], [], output, manifest)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["records"] == 1 and len(payload["sha256"]) == 64
    loaded = load_records(output)[0]
    assert loaded["source_revision"] == "1" and len(loaded["source_content_hash"]) == 64


def test_importer_rejects_unapproved_or_nonpublic_source(tmp_path: Path):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({
        "id": "bad", "title": "x", "city": "北京", "content": "x" * 100,
        "source_url": "https://example.com", "source_license": "unknown", "source_revision": "1",
        "source_attribution": "x", "corpus_kind": "synthetic",
    }) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_records(path)
