"""Corpus provenance, family isolation and independent metrics; zero network."""
import hashlib
import json
from collections import Counter

import pytest

from scripts.collect_platform_corpus import planned_cases, summary
from scripts.compare_compact_semantic import compact_observations
from scripts.measure_platform_corpus import freeze_runtime, read_corpus, runtime_file_hashes
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
    manifest = {"provenance": "platform_generated", "cases": [{"id": "test-case", "split": "development", "status": "COMPLETED", "artifact": "response.json",
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


def test_measurement_split_filters_before_opening_sources_or_applying_limit(tmp_path):
    output = "明确标示为测试夹具"
    (tmp_path / "development.json").write_text(json.dumps({"output": output}), encoding="utf-8")
    common = {"status": "COMPLETED", "output_sha256": hashlib.sha256(output.encode()).hexdigest()}
    manifest = {"provenance": "platform_generated", "cases": [
        {**common, "id": "a-holdout", "split": "holdout", "artifact": "must-not-read.json"},
        {**common, "id": "b-development", "split": "development", "artifact": "development.json"}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert [row["id"] for row in read_corpus(path, limit=1)] == ["b-development"]
    with pytest.raises(ValueError, match="selected split"):
        read_corpus(path, case_ids={"a-holdout"})
    with pytest.raises(FileNotFoundError):
        read_corpus(path, split="holdout")


def test_compact_experiment_keeps_actual_line_spans_and_rejects_invented_scope():
    source = "Day1：星河公园。\nDay2：再次去星河公园。"
    draft = {"days": [{"day": 1, "planned": [["星河公园", 1]]},
        {"day": 2, "planned": [["星河公园", 2], ["月光桥", 2], ["星河公园", 99]]}]}
    observations, issues = compact_observations(source, draft)
    assert [(row["day_index"], source[row["span_start"]:row["span_end"]]) for row in observations] == [
        (1, "星河公园"), (2, "星河公园")]
    assert observations[1]["span_start"] > observations[0]["span_end"]
    assert len(issues) == 2


@pytest.mark.parametrize("values, p50, p95", [([], None, None), ([10], 10, 10),
    ([80, 10], 10, 80), ([50, 10, 40, 20, 30], 30, 50)])
def test_small_sample_latency_uses_documented_nearest_rank(values, p50, p95):
    result = summarize_measurements([{"status": "COMPLETED", "elapsed_ms": value} for value in values])
    assert result["latency_p50_ms"] == p50
    assert result["latency_p95_ms"] == p95
    assert result["latency_count"] == len(values)
    assert result["latency_percentile_method"] == "nearest_rank"


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
    assert result["order_correct"] is False


def test_missing_or_wrong_day_main_stop_cannot_pass_complete_date_and_order_retention():
    label = {"annotation_status": "reviewed", "annotator_type": "independent_agent", "activities": [
        {"name": "公园甲", "day_index": 1, "role": "PLANNED"},
        {"name": "公园乙", "day_index": 2, "role": "PLANNED"}]}
    for second in [[], [{"name": "公园乙", "day_index": 1, "role": "PLANNED"}]]:
        result = compare_annotations(label, [{"name": "公园甲", "day_index": 1, "role": "PLANNED"}, *second])
        assert result["retained_planned_order_correct"] is True
        assert result["planned_order_correct"] is False
        assert result["planned_exact"] is False


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


def test_hierarchy_breakdown_cannot_overwrite_overall_recall():
    gold = annotation()
    gold["activities"][0]["hierarchy_level"] = "site"
    gold["activities"][1]["hierarchy_level"] = "subsite"
    actual = [{"name": "公园甲", "day_index": 1, "role": "PLANNED"},
        {"name": "公园乙", "day_index": 2, "role": "OPTIONAL", "branch_label": "方案A"}]
    row = {"status": "COMPLETED", "elapsed_ms": 1, **compare_annotations(gold, actual)}
    result = summarize_measurements([row])
    assert result["semantic_recall"] == result["semantic_precision"] == 1
    assert result["by_hierarchy"]["subsite"]["matched"] == 1


def test_freezing_runtime_never_copies_env_databases_or_mutates_existing_snapshot(tmp_path):
    origin = tmp_path / "source"
    folder = origin / "backend/app/trip_understanding"
    folder.mkdir(parents=True)
    (folder / "models.py").write_text("VALUE = 1", encoding="utf-8")
    (folder / ".env").write_text("TEST_ONLY=not-a-real-secret", encoding="utf-8")
    (folder / "user.db").write_text("test-only-database", encoding="utf-8")
    snapshot = freeze_runtime(origin, tmp_path / "artifacts")
    assert runtime_file_hashes(snapshot) == runtime_file_hashes(origin)
    assert not (snapshot / "backend/app/trip_understanding/.env").exists()
    assert not (snapshot / "backend/app/trip_understanding/user.db").exists()
    (folder / "models.py").write_text("VALUE = 2", encoding="utf-8")
    later = freeze_runtime(origin, tmp_path / "artifacts")
    assert later != snapshot
    assert (snapshot / "backend/app/trip_understanding/models.py").read_text(encoding="utf-8") == "VALUE = 1"


def test_planned_order_does_not_depend_on_optional_array_position_or_provider_id_prefix():
    gold = annotation()
    gold["activities"].append({"name": "公园丙", "day_index": 3, "role": "PLANNED"})
    actual = [{"name": "公园甲", "day_index": 1, "role": "PLANNED", "poi_id": "amap:poi-a"},
        {"name": "公园丙", "day_index": 3, "role": "PLANNED"},
        {"name": "公园乙", "day_index": 2, "role": "OPTIONAL", "branch_label": "方案A"}]
    result = compare_annotations(gold, actual)
    assert result["order_correct"] is False and result["semantic_exact"] is False
    assert result["planned_order_correct"] is True and result["planned_exact"] is True
    assert result["poi_identity_correct"] == 1 and result["wrong_auto_confirmations"] == 0
