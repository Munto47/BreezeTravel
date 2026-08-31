from datetime import datetime
from pathlib import Path

from evals.g05_knowledge import evaluate_admission_manifest, load_admission_manifest


MANIFEST = Path(__file__).parents[1] / "eval_data/g05_knowledge/admission_v1.json"
AS_OF = datetime.fromisoformat("2026-08-31T09:00:00+08:00")


def test_g05_source_admission_manifest_passes_before_migration() -> None:
    report = evaluate_admission_manifest(load_admission_manifest(MANIFEST), as_of=AS_OF)

    assert report.passed, report.errors
    assert report.place_count == 18
    assert report.available_place_count == 3
    assert report.explicit_gap_count == 15
    assert report.admitted_source_count == 4
    assert report.not_ready_source_count == 1
    assert report.claim_count == 4
    assert report.available_claim_type_count == 4
    assert report.explicit_gap_claim_type_count == 1
    assert report.required_field_coverage == 1.0
    assert report.source_binding_coverage == 1.0
    assert report.unauthorized_claim_count == 0
    assert report.expired_claim_count == 0


def test_g05_source_admission_rejects_not_ready_source_claim() -> None:
    manifest = load_admission_manifest(MANIFEST)
    manifest["claims"][0]["source_key"] = "palace-museum-direct-site"

    report = evaluate_admission_manifest(manifest, as_of=AS_OF)

    assert not report.passed
    assert report.unauthorized_claim_count == 1


def test_g05_source_admission_rejects_silent_gap_fill() -> None:
    manifest = load_admission_manifest(MANIFEST)
    manifest["places"][1]["claim_keys"] = ["beijing-palace-reservation"]

    report = evaluate_admission_manifest(manifest, as_of=AS_OF)

    assert not report.passed
    assert any("EXPLICIT_GAP cannot reference claims" in error for error in report.errors)


def test_g05_source_admission_rejects_expired_claim() -> None:
    manifest = load_admission_manifest(MANIFEST)
    manifest["claims"][0]["expires_at"] = "2026-08-31T08:30:00+08:00"

    report = evaluate_admission_manifest(manifest, as_of=AS_OF)

    assert not report.passed
    assert report.expired_claim_count == 1


def test_g05_source_admission_rejects_expired_source() -> None:
    manifest = load_admission_manifest(MANIFEST)
    manifest["sources"][0]["expires_at"] = "2026-08-31T08:30:00+08:00"

    report = evaluate_admission_manifest(manifest, as_of=AS_OF)

    assert not report.passed
    assert any("admitted source is not current" in error for error in report.errors)


def test_g05_required_coverage_counts_each_missing_field() -> None:
    manifest = load_admission_manifest(MANIFEST)
    manifest["claims"][0]["suggestion_text"] = ""
    manifest["claims"][0]["short_evidence"] = ""

    report = evaluate_admission_manifest(manifest, as_of=AS_OF)

    assert not report.passed
    assert report.required_field_coverage < 1.0


def test_g05_source_admission_forbids_full_page_storage() -> None:
    manifest = load_admission_manifest(MANIFEST)
    manifest["sources"][0]["raw_html"] = "<html>not allowed</html>"

    report = evaluate_admission_manifest(manifest, as_of=AS_OF)

    assert not report.passed
    assert any("full source content is forbidden" in error for error in report.errors)


def test_g05_source_admission_requires_honest_five_type_dispositions() -> None:
    manifest = load_admission_manifest(MANIFEST)
    manifest["claim_type_dispositions"][0]["disposition"] = "CLAIM_AVAILABLE"

    report = evaluate_admission_manifest(manifest, as_of=AS_OF)

    assert not report.passed
    assert any(
        "TYPICAL_DURATION is marked available without a claim" in error
        for error in report.errors
    )
