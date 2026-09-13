"""Controlled name records exercise ingestion; no live POI or travel claims."""
import json

from app.trip_understanding.city_knowledge import (
    CityEntity, CityKnowledge, find_entity_mentions, get_city_knowledge,
    load_city_knowledge,
)
from app.trip_understanding._three_city_place_lexicon import LEXICON_PATH
from scripts.collect_city_knowledge import PublicPageParser, extract


SOURCE = {"url": "https://culture.example.gov.cn/names", "title": "Controlled name list",
          "publisher": "Controlled official publisher", "retrieved_at": "2026-09-07", "evidence": "Name only."}


def row(name="星河博物馆", city="新余", identity="one", **changes):
    return {"id": identity, "city": city, "canonical_name": name, "kind": "poi", "category": "attraction",
            "review_status": "name_verified", "sources": [dict(SOURCE)], **changes}


def write_pack(tmp_path, records, *, city="新余", extra_versions=None):
    (tmp_path / "v1.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")
    cities = {city: {"active": "v1.jsonl", "fallbacks": []}, **(extra_versions or {})}
    (tmp_path / "manifest.json").write_text(json.dumps({"schema_version": "city-knowledge-v1", "cities": cities}), encoding="utf-8")


def test_new_city_with_one_entity_is_usable_without_quota(tmp_path):
    write_pack(tmp_path, [row()])
    knowledge = load_city_knowledge(tmp_path, include_legacy=False)
    assert knowledge.versions == {"新余": "v1.jsonl"}
    assert knowledge.lookup("星河博物馆", "新余市")[0].entity_id == "one"
    assert knowledge.query_lookup(city="新余", name="星河博物馆").unique.entry_id == "one"


def test_bad_record_is_quarantined_without_losing_good_records(tmp_path):
    write_pack(tmp_path, [row(), row(identity="bad", aliases=[{"name": "星河", "relation": "nearby", "source_url": SOURCE["url"]}])])
    with (tmp_path / "v1.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('\n{"invalid private raw fragment"')
    knowledge = load_city_knowledge(tmp_path, include_legacy=False)
    assert len(knowledge.entities) == 1
    assert len(knowledge.issues) == 2
    assert "private raw fragment" not in str(knowledge.issues)


def test_duplicate_identity_removes_both_rows_not_arbitrary_first(tmp_path):
    write_pack(tmp_path, [row(identity="duplicate"), row("另一馆", identity="duplicate"), row("星河公园", identity="good")])
    knowledge = load_city_knowledge(tmp_path, include_legacy=False)
    assert [e.entity_id for e in knowledge.entities] == ["good"]
    assert knowledge.issues[0]["code"] == "DUPLICATE_ID"


def test_invalid_active_falls_back_per_city_and_other_city_keeps_loading(tmp_path):
    write_pack(tmp_path, [row()], extra_versions={"成都": {"active": "cd.jsonl", "fallbacks": None}})
    (tmp_path / "bad.jsonl").write_text("not json", encoding="utf-8")
    (tmp_path / "cd.jsonl").write_text(json.dumps(row("星河书院", "成都", "cd")), encoding="utf-8")
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    manifest["cities"]["新余"] = {"active": "bad.jsonl", "fallbacks": ["../outside.jsonl", "v1.jsonl"]}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    knowledge = load_city_knowledge(tmp_path, include_legacy=False)
    assert len(knowledge.entities) == 2
    assert knowledge.versions == {"新余": "v1.jsonl", "成都": "cd.jsonl"}
    assert any(i["code"] == "INVALID_FALLBACKS" for i in knowledge.issues)


def test_malformed_manifest_preserves_original_900_as_leads(tmp_path):
    (tmp_path / "manifest.json").write_text("[]", encoding="utf-8")
    knowledge = load_city_knowledge(tmp_path)
    original = {json.loads(line)["id"] for line in LEXICON_PATH.read_text(encoding="utf-8").splitlines()}
    retained = {e.entity_id for e in knowledge.entities if e.review_status == "lead"}
    assert original <= retained and len(original) == 900
    assert {i["code"] for i in knowledge.issues} == {"MANIFEST_UNAVAILABLE"}


def test_alias_collision_is_candidate_set_and_relation_is_not_alias(tmp_path):
    alias = {"name": "星河公园", "relation": "same_as", "source_url": SOURCE["url"]}
    write_pack(tmp_path, [row("星河风景区", identity="park", aliases=[alias]), row("星河公园", identity="other"),
                         row("星河南门", identity="gate", kind="entrance", relations=[
                             {"type": "entrance_of", "target_id": "park", "source_url": SOURCE["url"]}])])
    knowledge = load_city_knowledge(tmp_path, include_legacy=False)
    assert len(knowledge.lookup("星河公园")) == 2
    assert knowledge.query_lookup(city="新余", name="星河公园").unique is None
    assert [e.entity_id for e in knowledge.lookup("星河南门")] == ["gate"]
    assert knowledge.lookup("星河南门")[0].relations[0]["type"] == "entrance_of"


def test_leads_and_areas_never_supply_verified_identity_rewrites(tmp_path):
    write_pack(tmp_path, [row(review_status="lead"), row("星河商圈", identity="area", kind="commercial_area", category="area")])
    knowledge = load_city_knowledge(tmp_path, include_legacy=False)
    assert len(knowledge.mentions("星河博物馆与星河商圈")) == 2
    assert not knowledge.query_lookup(city="新余", name="星河博物馆").matches
    assert not knowledge.query_lookup(city="新余", name="星河商圈").matches


def test_mentions_longest_offsets_repeated_urls_city_ambiguity_and_bound():
    knowledge = CityKnowledge((
        CityEntity("parent", "新余", "星河", "poi", "attraction"),
        CityEntity("a", "新余", "星河公园", "poi", "attraction"),
        CityEntity("b", "成都", "星河公园", "poi", "attraction"),
    ))
    source = "https://example.invalid/星河公园 星河公园，星河公园；星河。"
    mentions = knowledge.mentions(source)
    assert [m.surface for m in mentions] == ["星河公园", "星河公园", "星河"]
    assert all(source[m.start:m.end] == m.surface for m in mentions)
    assert mentions[0].ambiguous and mentions[0].city is None
    assert knowledge.mentions(source, "新余")[0].candidate_ids == ("a",)
    assert len(knowledge.mentions("星河公园 " * 500, limit=900)) == 320
    assert knowledge.mentions(source, limit=0) == []


def test_duplicate_canonical_leads_stay_auditable_without_false_ambiguity():
    knowledge = CityKnowledge((
        CityEntity("old", "新余", "星河公园", "poi", "attraction", aliases=("星河老名",)),
        CityEntity("new", "新余", "星河公园", "poi", "attraction", review_status="name_verified"),
    ))
    assert len(knowledge.entities) == 2
    assert not knowledge.mentions("星河公园")[0].ambiguous
    assert knowledge.lookup("星河老名")[0].review_status == "lead"
    assert not knowledge.query_lookup(city="新余", name="星河老名").matches


def test_shipped_packs_have_sources_and_useful_search_seeds_without_live_claims():
    knowledge = get_city_knowledge()
    assert knowledge.issues == ()
    for city in ("北京", "上海", "广州", "深圳", "杭州"):
        areas = [e for e in knowledge.entities if e.city == city and e.kind == "commercial_area"]
        assert len(areas) >= 10
        assert len([e for e in areas if "stay_search" in e.uses]) >= 5
        assert all(e.review_status == "name_verified" and e.sources for e in areas)
    assert knowledge.query_lookup(city="广州", name="小蛮腰").unique.canonical_name == "广州塔"
    assert knowledge.query_lookup(city="深圳", name="深圳美术馆（新馆）").unique.canonical_name == "深圳美术馆（新馆）"
    assert {m.surface for m in find_entity_mentions("广州塔、深圳北站、福田口岸")} == {"广州塔", "深圳北站", "福田口岸"}
    assert all(not {"coordinates", "provider_id", "price", "opening_hours"} & set(s) for e in knowledge.entities for s in e.sources)


def test_public_collector_rejects_unseen_names_and_generic_park_navigation():
    import pytest
    page = PublicPageParser()
    page.feed("<a>自然公园城市公园社区公园</a><a>特色公园</a><a>星河公园</a><script>不存在公园</script>")
    source = {"city": "新余", "url": SOURCE["url"], "title": SOURCE["title"], "publisher": SOURCE["publisher"], "parser": "parks_labels"}
    assert [r["canonical_name"] for r in extract(source, page)] == ["星河公园"]
    with pytest.raises(ValueError, match="absent"):
        extract({**source, "parser": "reviewed_names", "names": ["不存在公园"]}, page)
