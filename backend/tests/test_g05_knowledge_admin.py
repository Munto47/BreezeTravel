from pathlib import Path

import pytest

from app.trip_understanding.knowledge_admin import compile_knowledge_bundle
from evals.g05_knowledge import load_admission_manifest


MANIFEST = Path(__file__).parents[1] / "eval_data/g05_knowledge/admission_v1.json"


def test_g05_bundle_compilation_is_deterministic_and_fact_only() -> None:
    manifest = load_admission_manifest(MANIFEST)

    first = compile_knowledge_bundle(manifest)
    second = compile_knowledge_bundle(manifest)

    assert first == second
    assert first["bundle_id"] == "g05-three-city-g01-journeys-v1"
    assert len(first["sources"]) == 4
    assert len(first["claims"]) == 4
    assert {source["admission_status"] for source in first["sources"]} == {"ADMITTED"}
    assert "palace-museum-direct-site" not in {
        source["source_key"] for source in first["sources"]
    }
    serialized = str(first).lower()
    assert "raw_html" not in serialized
    assert "page_text" not in serialized


def test_g05_bundle_rejects_claim_with_missing_source_version() -> None:
    manifest = load_admission_manifest(MANIFEST)
    manifest["claims"][0]["source_version"] = 99

    with pytest.raises(ValueError, match="absent source version"):
        compile_knowledge_bundle(manifest)
