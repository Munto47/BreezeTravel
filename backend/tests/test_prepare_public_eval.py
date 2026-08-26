from scripts.prepare_public_eval import QUESTIONS_PER_SOURCE, build_cases


def _record(city: str, source_id: str) -> dict:
    return {"id": source_id, "city": city, "title": source_id, "corpus_kind": "public", "source_revision": "r1", "content": "A source-backed fact with enough detail for deterministic retrieval evaluation."}


def test_source_disjoint_blind_split_has_required_coverage():
    result = build_cases([_record(city, f"{city}-{i}") for city in ("北京", "上海", "杭州") for i in range(3)])
    assert all(value["blind"] >= QUESTIONS_PER_SOURCE for value in result["counts"].values())
    source_splits = {}
    for case in result["cases"]:
        source_splits.setdefault(case["expected_source_id"], set()).add(case["split"])
    assert all(len(splits) == 1 for splits in source_splits.values())
