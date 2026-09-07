"""Corpus provenance, family isolation and independent metrics; zero network."""
import hashlib
import json
from collections import Counter

import pytest

from scripts.collect_platform_corpus import planned_cases, summary
from scripts.measure_platform_corpus import read_corpus
from scripts.platform_corpus_metrics import compare_annotations, summarize_measurements


def test_two_hundred_presplits_remain_fixed_when_collection_grows():
    first = planned_cases(10)
    complete = planned_cases(230)
    assert complete[:10] == first
    assert Counter(row["split"] for row in complete[:200]) == {"development": 100, "validation": 50, "holdout": 50}
    assert Counter(row["split"] for row in complete[200:]) == {"long_tail": 30}
    families = {}
    for row in complete:
        families.setdefault(row["family_id"], set()).add(row["split"])
    assert all(len(splits) == 1 for splits in families.values())
    assert len({row["id"] for row in complete}) == 230
    assert sum(row["long_text"] for row in complete[:50]) == 5
    assert any("跨城" in row["prompt"] for row in complete[:50])


def test_collection_usage_keeps_failed_and_retried_attempts():
    final = {"status": "COMPLETED", "input_tokens": 30, "output_tokens": 40, "estimated_cost_cny": 0.2}
    failed = {"status": "INCOMPLETE_RESPONSE", "input_tokens": 10, "output_tokens": 20, "estimated_cost_cny": 0.1}
    result = summary([final], [failed, final])
    assert result["cases"] == 1 and result["external_calls"] == 2
    assert result["output_tokens"] == 60 and result["estimated_cost_cny"] == 0.3


def test_corpus_reader_checks_unchanged_actual_output_and_directory(tmp_path):
    output = "平台实际输出占位测试数据，非真实模型效果证据"
    artifact = tmp_path / "response.json"
    artifact.write_text(json.dumps({"output": output}), encoding="utf-8")
    manifest = {"provenance": "platform_generated", "cases": [{"id": "test-case", "status": "COMPLETED", "artifact": "response.json",
        "output_sha256": hashlib.sha256(output.encode()).hexdigest()}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert read_corpus(path)[0]["text"] == output
    artifact.write_text(json.dumps({"output": "被改写的输出"}), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        read_corpus(path)
    manifest["cases"][0]["artifact"] = "../not-authorized.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="directory"):
        read_corpus(path)


def annotation():
    return {"annotation_status": "reviewed", "annotator_type": "independent_agent", "activities": [
        {"name": "公园甲", "day_index": 1, "role": "PLANNED", "expected_poi_ids": ["poi-a"]},
        {"name": "公园乙", "day_index": 2, "role": "OPTIONAL", "branch_label": "方案A", "expected_poi_ids": ["poi-b"]},
    ]}


def test_missing_places_remain_in_semantic_and_identity_denominators():
    actual = [{"name": "公园甲", "day_index": 1, "role": "PLANNED", "poi_id": "wrong-poi"}]
    result = compare_annotations(annotation(), actual)
    assert result["semantic_recall"] == 0.5
    assert result["poi_labeled_count"] == 2
    assert result["wrong_auto_confirmations"] == 1
    assert result["semantic_exact"] is False


def test_generation_cannot_grade_itself_and_unlabeled_reports_have_no_accuracy():
    invalid = {**annotation(), "annotator_type": "same_model"}
    with pytest.raises(ValueError, match="independent"):
        compare_annotations(invalid, [])
    report = summarize_measurements([{"status": "COMPLETED", "elapsed_ms": 10}])
    assert report["annotated_runs"] == 0
    assert report["semantic_recall"] is None and report["semantic_precision"] is None


def test_branch_day_or_role_mismatch_is_not_an_exact_semantic_match():
    actual = [{"name": "公园甲", "day_index": 1, "role": "PLANNED"},
        {"name": "公园乙", "day_index": 2, "role": "OPTIONAL", "branch_label": "方案B"}]
    result = compare_annotations(annotation(), actual)
    assert result["matched_places"] == 1 and result["extra_or_misassigned_places"] == 1
