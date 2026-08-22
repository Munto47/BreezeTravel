from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from app.route_priors import PriorContribution, RoutePriorIntegrityError, RoutePriorLoader, RouteSequenceKind
from app.route_priors.models import OfficialPriorAvailability
from scripts.validate_dual_entry_testset import DATASET_ROOT


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows) + "\n", encoding="utf-8")


def _mutable_dataset(tmp_path: Path) -> Path:
    target = tmp_path / "dual_entry_v1"
    shutil.copytree(DATASET_ROOT, target)
    return target


def _mutate_extract(dataset_root: Path, source_id: str, mutate) -> None:
    registry_path = dataset_root / "source_registry.jsonl"
    rows = _jsonl(registry_path)
    row = next(item for item in rows if item["source_document_id"] == source_id)
    extract_path = dataset_root / row["extract_archive_path"]
    extract = json.loads(extract_path.read_text(encoding="utf-8"))
    mutate(extract)
    _write_json(extract_path, extract)
    row["extract_hash"] = hashlib.sha256(extract_path.read_bytes()).hexdigest()
    _write_jsonl(registry_path, rows)


def test_archived_wikivoyage_sources_are_schema_valid_and_attributed():
    schema = json.loads((DATASET_ROOT / "source.schema.json").read_text(encoding="utf-8"))
    sources = [row for row in _jsonl(DATASET_ROOT / "source_registry.jsonl") if row.get("source_kind") == "wikivoyage_community"]
    assert {row["city"] for row in sources} == {"北京", "上海", "杭州"}
    for source in sources:
        assert list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(source)) == []
        assert source["license_spdx"] == "CC-BY-SA-4.0"
        assert source["license_url"] == "https://creativecommons.org/licenses/by-sa/4.0/"
        assert "Wikivoyage contributors" in source["attribution"]
        assert source["source_revision"] in source["revision_url"]
        assert "FACT" not in source["usage_modes"]


def test_loader_returns_three_city_minimal_priors_with_fixed_revisions():
    priors = RoutePriorLoader().load_all()
    by_city = {prior.city: prior for prior in priors}
    assert set(by_city) == {"北京", "上海", "杭州"}
    assert by_city["北京"].source_revision == "5331911"
    assert by_city["上海"].source_revision == "5306138"
    assert by_city["杭州"].source_revision == "5265453"
    assert by_city["北京"].route_sequences[0].sequence_kind is RouteSequenceKind.EXPLICIT_DIRECTIONAL_SEQUENCE
    assert by_city["杭州"].route_sequences[0].sequence_kind is RouteSequenceKind.ARTICLE_CLUSTER_ORDER
    assert all(prior.identity_policy == "UNRESOLVED_QUERY_HINTS_ONLY_PROVIDER_RECEIPT_REQUIRED" for prior in priors)


@pytest.mark.parametrize(
    ("city", "anchor", "expected"),
    [
        ("北京", "Forbidden City", {"Drum and Bell Towers", "Tiananmen Square", "Temple of Heaven"}),
        ("上海", "The Bund", {"Nanjing Road East", "People's Square", "Yuyuan Gardens"}),
        ("杭州", "West Lake", {"Spring Dawn at Su Causeway", "Lingering Snow on Broken Bridge", "Leifeng Pagoda in Evening Glow"}),
    ],
)
def test_three_city_anchor_queries_offer_several_unresolved_prior_hints(city: str, anchor: str, expected: set[str]):
    hints = RoutePriorLoader().candidate_hints(city, anchor, limit=6)
    assert len(hints) >= 3
    assert expected <= {hint.query_hint for hint in hints}
    assert all(hint.requires_provider_resolution for hint in hints)
    assert all(hint.license_spdx == "CC-BY-SA-4.0" for hint in hints)
    assert any(PriorContribution.ROUTE_ADJACENCY in hint.contributions for hint in hints)
    assert all(not hasattr(hint, "coordinates") and not hasattr(hint, "canonical_place_id") for hint in hints)


def test_loader_rejects_tampered_archive_hash(tmp_path: Path):
    dataset = _mutable_dataset(tmp_path)
    path = dataset / "archives/open-wikivoyage-beijing-5331911/extract.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(RoutePriorIntegrityError, match="ARCHIVE_HASH_MISMATCH"):
        RoutePriorLoader(dataset).load_all()


def test_loader_rejects_missing_attribution(tmp_path: Path):
    dataset = _mutable_dataset(tmp_path)
    registry_path = dataset / "source_registry.jsonl"
    rows = _jsonl(registry_path)
    next(row for row in rows if row["source_document_id"] == "open-wikivoyage-beijing-5331911")["attribution"] = ""
    _write_jsonl(registry_path, rows)
    with pytest.raises(RoutePriorIntegrityError, match="PROVENANCE_INCOMPLETE"):
        RoutePriorLoader(dataset).load_all()


def test_loader_rejects_license_mismatch(tmp_path: Path):
    dataset = _mutable_dataset(tmp_path)
    registry_path = dataset / "source_registry.jsonl"
    rows = _jsonl(registry_path)
    next(row for row in rows if row["source_document_id"] == "open-wikivoyage-shanghai-5306138")["license_url"] = "https://example.invalid/license"
    _write_jsonl(registry_path, rows)
    with pytest.raises(RoutePriorIntegrityError, match="LICENSE_MISMATCH"):
        RoutePriorLoader(dataset).load_all()


def test_loader_rejects_fact_usage_mode(tmp_path: Path):
    dataset = _mutable_dataset(tmp_path)
    registry_path = dataset / "source_registry.jsonl"
    rows = _jsonl(registry_path)
    next(row for row in rows if row["source_document_id"] == "open-wikivoyage-hangzhou-5265453")["usage_modes"].append("FACT")
    _write_jsonl(registry_path, rows)
    with pytest.raises(RoutePriorIntegrityError, match="FACT_USAGE_FORBIDDEN"):
        RoutePriorLoader(dataset).load_all()


@pytest.mark.parametrize("forbidden_field", ["coordinates", "current_opening", "current_route_time", "current_popularity"])
def test_loader_rejects_current_fact_or_identity_fields_even_when_archive_hash_is_updated(tmp_path: Path, forbidden_field: str):
    dataset = _mutable_dataset(tmp_path)
    _mutate_extract(
        dataset,
        "open-wikivoyage-beijing-5331911",
        lambda extract: extract.__setitem__(forbidden_field, "fabricated-value"),
    )
    with pytest.raises(RoutePriorIntegrityError, match="SCHEMA_INVALID"):
        RoutePriorLoader(dataset).load_all()


def test_loader_has_no_persistence_or_acceptance_surface():
    loader = RoutePriorLoader()
    assert not hasattr(loader, "save")
    assert not hasattr(loader, "upsert")
    assert not hasattr(loader, "accept")


@pytest.mark.parametrize(
    ("city", "anchor", "expected"),
    [
        ("北京", "故宫博物院", {"景山公园"}),
        ("上海", "遇见·南昌路", {"淮海中路街道新天地商圈党群服务站", "思南书局诗歌店"}),
    ],
)
def test_verified_official_archives_only_emit_unresolved_adjacent_query_hints(
    city: str,
    anchor: str,
    expected: set[str],
):
    signals = RoutePriorLoader().signals_for_city(city, anchor)
    assert signals.official_status.availability is OfficialPriorAvailability.AVAILABLE
    assert expected == {hint.query_hint for hint in signals.official_hints}
    assert all(hint.requires_provider_resolution for hint in signals.official_hints)
    assert all(
        hint.contributions == frozenset({PriorContribution.ROUTE_ADJACENCY})
        for hint in signals.official_hints
    )
    for hint in signals.official_hints:
        assert not hasattr(hint, "canonical_place_id")
        assert not hasattr(hint, "coordinates")
        assert not hasattr(hint, "opening_hours")
        assert not hasattr(hint, "route_time")
        assert not hasattr(hint, "popularity")
        assert hint.official_prior_refs[0].allowed_use == ("STRUCTURE", "EVAL_ONLY")
        assert hint.official_prior_refs[0].establishes_current_facts is False


def test_unified_projection_keeps_community_and_official_provenance_separate():
    signals = RoutePriorLoader().signals_for_city("北京", "Forbidden City")
    assert signals.community_hints
    assert signals.official_hints == ()
    assert signals.official_status.available_source_refs
    assert all(hasattr(hint, "license_spdx") for hint in signals.community_hints)
    assert all(not hasattr(hint, "official_prior_refs") for hint in signals.community_hints)


def test_shanghai_unarchived_vote_is_reported_but_never_used():
    status = RoutePriorLoader().official_status("上海")
    assert status.availability is OfficialPriorAvailability.AVAILABLE
    assert {ref.source_document_id for ref in status.available_source_refs} == {
        "official-shanghai-citywalk-20240616"
    }
    assert status.unavailable_source_ids == ("official-shanghai-route-vote-20241001",)


def test_hangzhou_official_prior_is_explicitly_unavailable_and_community_is_not_promoted():
    signals = RoutePriorLoader().signals_for_city("杭州", "West Lake")
    assert signals.community_hints
    assert signals.official_hints == ()
    assert signals.official_status.availability is OfficialPriorAvailability.UNAVAILABLE
    assert signals.official_status.available_source_refs == ()
    assert signals.official_status.unavailable_source_ids == ("official-hangzhou-route-pdf-20260821",)
    assert signals.official_status.reason_code == "OFFICIAL_ARCHIVE_UNAVAILABLE"


def test_official_loader_rejects_tampered_extract_hash(tmp_path: Path):
    dataset = _mutable_dataset(tmp_path)
    path = dataset / "archives/official-beijing-route-library-20260821/extract.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(RoutePriorIntegrityError, match="OFFICIAL_PRIOR_ARCHIVE_HASH_MISMATCH"):
        RoutePriorLoader(dataset).official_status("北京")


def test_official_loader_rejects_canonical_url_mismatch_even_with_updated_extract_hash(tmp_path: Path):
    dataset = _mutable_dataset(tmp_path)
    _mutate_extract(
        dataset,
        "official-shanghai-citywalk-20240616",
        lambda extract: extract.__setitem__("canonical_url", "https://example.invalid/route"),
    )
    with pytest.raises(RoutePriorIntegrityError, match="OFFICIAL_PRIOR_CANONICAL_URL_MISMATCH"):
        RoutePriorLoader(dataset).official_status("上海")


def test_official_loader_rejects_broken_remote_body_derivation(tmp_path: Path):
    dataset = _mutable_dataset(tmp_path)
    _mutate_extract(
        dataset,
        "official-beijing-route-library-20260821",
        lambda extract: extract["derivation"].__setitem__("source_body_sha256", "0" * 64),
    )
    with pytest.raises(RoutePriorIntegrityError, match="OFFICIAL_PRIOR_DERIVATION_BROKEN"):
        RoutePriorLoader(dataset).official_status("北京")


def test_official_loader_rejects_usage_expansion(tmp_path: Path):
    dataset = _mutable_dataset(tmp_path)
    _mutate_extract(
        dataset,
        "official-beijing-route-library-20260821",
        lambda extract: extract["allowed_use"].append("RETRIEVAL"),
    )
    with pytest.raises(RoutePriorIntegrityError, match="OFFICIAL_PRIOR_USAGE_FORBIDDEN"):
        RoutePriorLoader(dataset).official_status("北京")


def test_official_loader_rejects_removed_prohibited_claim(tmp_path: Path):
    dataset = _mutable_dataset(tmp_path)
    _mutate_extract(
        dataset,
        "official-shanghai-citywalk-20240616",
        lambda extract: extract["prohibited_claims"].remove("current_poi_identity"),
    )
    with pytest.raises(RoutePriorIntegrityError, match="OFFICIAL_PRIOR_FACT_BOUNDARY_INCOMPLETE"):
        RoutePriorLoader(dataset).official_status("上海")


@pytest.mark.parametrize("forbidden_field", ["canonical_place_id", "coordinates", "current_route_time", "popularity"])
def test_official_loader_rejects_product_fact_overreach_even_when_extract_hash_is_updated(
    tmp_path: Path,
    forbidden_field: str,
):
    dataset = _mutable_dataset(tmp_path)
    _mutate_extract(
        dataset,
        "official-beijing-route-library-20260821",
        lambda extract: extract.__setitem__(forbidden_field, "fabricated"),
    )
    with pytest.raises(RoutePriorIntegrityError, match="OFFICIAL_PRIOR_SCHEMA_OVERREACH"):
        RoutePriorLoader(dataset).official_status("北京")
