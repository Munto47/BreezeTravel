import json
from pathlib import Path


from scripts.publish_evidence import publish, validate_bundle


def _bundle() -> dict:
    return {
        "run_id": "test-run", "metadata": {"corpus_manifest_sha256": "a" * 64},
        "corpus": {"blind_questions_by_city": {"北京": 20, "上海": 20, "杭州": 20}},
        "rag": {"citation_completeness": 1.0, "key_fact_recall": 0.8, "unsupported_assertion_rate": 0.02},
        "router": {"router_both_f1": 0.9, "router_macro_f1": 0.9},
        "public_e2e": {"status": "passed", "base_url": "https://example.test"},
    }


def test_evidence_gate_rejects_historical_or_incomplete_bundle():
    bundle = _bundle()
    bundle["rag"]["citation_completeness"] = 0.99
    assert validate_bundle(bundle)


def test_evidence_gate_requires_authenticated_public_smoke():
    bundle = _bundle()
    bundle["public_e2e"]["status"] = "skipped"
    assert validate_bundle(bundle)


def test_publish_advances_latest_only_after_all_gates(tmp_path: Path):
    source = tmp_path / "bundle.json"
    source.write_text(json.dumps(_bundle()), encoding="utf-8")
    run_dir = publish(source, tmp_path / "evidence")
    latest = json.loads((tmp_path / "evidence" / "latest.json").read_text(encoding="utf-8"))
    assert run_dir.name == "test-run"
    assert latest["status"] == "verified_public_blind_run"
