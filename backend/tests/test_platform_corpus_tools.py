"""Corpus provenance, family isolation and independent metrics; zero network."""
import hashlib
import json
from pathlib import Path
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
    with pytest.raises(ValueError, match="reviewed annotations"):
        compare_annotations(invalid, [])
    report = summarize_measurements([{"status": "COMPLETED", "elapsed_ms": 10}])
    assert report["annotated_runs"] == 0
    assert report["semantic_recall"] is None and report["semantic_precision"] is None


def test_visit_occurrence_and_parent_are_required_even_when_names_and_poi_match():
    gold = {"annotation_status": "reviewed", "annotator_type": "owner", "activities": [
        {"id": "first", "name": "东馆", "day_index": 1, "span_start": 0, "span_end": 2, "parent_id": None},
        {"id": "exhibit", "name": "青铜器展", "day_index": 1, "span_start": 4, "span_end": 8,
         "parent_id": "first", "relation_type": "INTERNAL_DETAIL", "detail_kind": "VISIT"},
        {"id": "return", "name": "东馆", "day_index": 1, "span_start": 10, "span_end": 12, "parent_id": None},
    ]}
    observations = [
        {"mention_id": "a", "name": "东馆", "day_index": 1, "role": "PLANNED", "span_start": 0, "span_end": 2},
        {"mention_id": "b", "name": "青铜器展", "day_index": 1, "role": "PLANNED", "span_start": 4, "span_end": 8,
         "relation_type": "INTERNAL_DETAIL", "detail_kind": "VISIT", "parent_mention_id": "a"},
        {"mention_id": "c", "name": "东馆", "day_index": 1, "role": "PLANNED", "span_start": 10, "span_end": 12},
    ]
    assert compare_annotations(gold, observations)["semantic_exact"]
    wrong_parent = [{**row, **({"parent_mention_id": "c"} if row["mention_id"] == "b" else {})} for row in observations]
    result = compare_annotations(gold, wrong_parent)
    assert result["matched_places"] == 2 and result["missing_places"] == 1
    assert result["semantic_exact"] is False
    duplicate = [*observations[:2], {**observations[0], "mention_id": "c"}]
    assert compare_annotations(gold, duplicate)["matched_places"] == 2


def test_failed_and_incomplete_documents_stay_in_structure_denominator():
    perfect = {"status": "COMPLETED", "elapsed_ms": 1, **compare_annotations(annotation(), [
        {"name": "公园甲", "day_index": 1, "role": "PLANNED"},
        {"name": "公园乙", "day_index": 2, "role": "OPTIONAL", "branch_label": "方案A"}])}
    failed = {"status": "FAILED", "elapsed_ms": 2, **compare_annotations(annotation(), [])}
    partial = {**perfect, "unprocessed_count": 1}
    result = summarize_measurements([perfect, partial, failed])
    assert result["semantic_recall"] == 4 / 6
    assert result["structure_correct_rate"] == 1 / 3


@pytest.mark.asyncio
async def test_internal_storage_role_purpose_and_conditional_target_compare_as_business_units():
    from scripts.platform_corpus_metrics import semantic_observations
    from tests.corpus_projection_case import replay
    from tests.corpus_projection_case import SOURCE, demo_annotation
    sample = {"source": SOURCE}
    output = await replay()
    rows = semantic_observations(output.proposal)
    assert len(rows) == 9  # Pickup is a qualifier, not a tenth visit.
    result = compare_annotations(demo_annotation(sample["source"]), rows)
    assert result["matched_places"] == result["expected_places"] == result["observed_places"] == 9
    assert result["semantic_exact"]
    purpose = next(row for row in rows if row["purposes"])
    purpose["purposes"] = []
    assert compare_annotations(demo_annotation(sample["source"]), rows)["matched_places"] == 8


def test_wrong_identity_or_hidden_correct_item_prevents_whole_document_success():
    label = annotation()
    label["activities"] = label["activities"][:1]
    row = dict(name="公园甲", day_index=1, role="PLANNED", poi_id="wrong", visible_correct=True)
    assert not compare_annotations(label, [row])["semantic_exact"]
    row.update(poi_id="amap:poi-a", visible_correct=False)
    assert not compare_annotations(label, [row])["semantic_exact"]


def test_authorized_reference_inputs_use_literal_source_and_keep_families_isolated(tmp_path):
    from scripts.measure_platform_corpus import read_corpus
    path = tmp_path / "cases.json"
    cases = [dict(id="dev", family_id="first", split="development", text="Day1：故宫。", source_kind="owner_authorized"),
             dict(id="holdout", family_id="second", split="holdout", text="Day1：天坛。", source_kind="synthetic_independent_scenario")]
    def write():
        path.write_text(json.dumps(dict(provenance="authorized_reference", cases=cases)), encoding="utf-8")
    write()
    assert read_corpus(path, split="holdout")[0]["text"] == "Day1：天坛。"
    cases[1]["family_id"] = "first"
    write()
    with pytest.raises(ValueError, match="family"):
        read_corpus(path, split="holdout")
    cases[1].update(family_id="second", text=cases[0]["text"])
    write()
    with pytest.raises(ValueError, match="Identical"):
        read_corpus(path, split="holdout")


def test_worker_and_measurement_use_identical_kimi_options(monkeypatch):
    from app.config import Settings
    from app.trip_understanding import worker
    seen = []
    monkeypatch.setattr(worker, "ExperienceQwenProvider", lambda **options: seen.append(options) or options)
    monkeypatch.setattr(worker, "AmapPlaceResolver", lambda **options: None)
    settings = Settings(_env_file=None, trip_understanding_provider_mode="live", kimi_for_code="test-only",
                        kimi_model="kimi-for-coding", kimi_parse_deadline_seconds=180,
                        kimi_parse_max_output_tokens=8192)
    worker.build_configured_inference_provider(settings)
    worker.build_configured_full_pipeline(settings)
    assert seen[0] == seen[1]
    assert seen[0]["execution_config"].reasoning_effort == "high"
    assert seen[0]["model"] == "k3-256k" and seen[0]["max_output_tokens"] == 8192
    assert seen[0]["enable_source_visits"] and seen[0]["relative_only"]


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
    assert result["order_correct"] is True and result["semantic_exact"] is True
    assert result["planned_order_correct"] is True and result["planned_exact"] is True
    assert result["poi_identity_correct"] == 1 and result["wrong_auto_confirmations"] == 0


def test_development_review_is_named_honestly_and_extra_relations_are_errors():
    gold = {"annotation_status": "reviewed", "annotator_type": "implementation_agent", "activities": [
        {"name": "公园甲", "day_index": 1, "role": "PLANNED", "parent_id": None, "replaces_id": None, "purpose": None}]}
    actual = {"name": "公园甲", "day_index": 1, "role": "PLANNED"}
    good = compare_annotations(gold, [actual])
    assert good["gold_status"] == "DEVELOPMENT_REVIEWED"
    for extra in ({"replaces_mention_id": "another"}, {"purposes": ["PICKUP_ONLY"]}):
        bad = compare_annotations(gold, [{**actual, **extra}])
        assert bad["matched_places"] == 0 and not bad["semantic_exact"]
    summary = summarize_measurements([{**good, "status": "COMPLETED", "elapsed_ms": 1}])
    assert summary["annotation_review_types"] == {"DEVELOPMENT_REVIEWED": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [None, "child", "purpose", "alternative", "pending", "day", "replacement", "duplicate"])
async def test_full_measurement_checks_public_projection_after_save(damage):
    from scripts.platform_corpus_metrics import public_projection_observations, read_public_projection, semantic_observations
    from tests.corpus_projection_case import replay
    from tests.corpus_projection_case import SOURCE, demo_annotation

    sample = {"source": SOURCE}
    output = await replay()
    label = demo_annotation(sample["source"])
    day = output.public_result.days[0]
    if damage == "child":
        day.activities[2].source_details += day.activities[0].source_details
        day.activities[0].source_details = []
    elif damage == "purpose":
        day.activities[2].source_details = []
    elif damage == "alternative":
        output.public_result.days[1].alternatives = []
    elif damage == "pending":
        day.activities[0].status = "NEEDS_CONFIRMATION"
    elif damage == "day":
        output.public_result.days[1].activities.append(day.activities.pop())
    elif damage == "replacement":
        day.alternatives[0].replaces_visit_id = day.activities[0].visit_id
    elif damage == "duplicate":
        day.activities.append(day.activities[0].model_copy(deep=True))
    # Semantic correctness alone cannot establish public projection correctness.
    assert compare_annotations(label, semantic_observations(output.proposal))["semantic_exact"]
    if damage == "duplicate":
        # The real storage contract already rejects this before readback.
        with pytest.raises(ValueError, match="unique authoritative positions"):
            await read_public_projection(output, sample["source"])
        observations = public_projection_observations(output)
    else:
        observations = await read_public_projection(output, sample["source"])
    result = compare_annotations(label, observations)
    assert result["expected_places"] == result["matched_places"] == 9
    assert result["visibility_assessed"]
    assert result["semantic_exact"] == (damage is None)
    if damage == "duplicate":
        assert result["extra_or_misassigned_places"] == 1
        assert result["semantic_precision"] == 9 / 10
    else:
        assert (result["visible_correct"] == 9) == (damage is None)


def test_missing_identity_cannot_pass_assessed_public_result():
    label = annotation()
    label["activities"] = label["activities"][:1]
    result = compare_annotations(label, [dict(name="公园甲", day_index=1, role="PLANNED", visible_correct=True)])
    assert result["wrong_auto_confirmations"] == 0
    assert result["visible_correct"] == 0
    assert not result["semantic_exact"]


def test_unknown_failed_request_spend_is_not_reported_as_zero():
    known = dict(status="COMPLETED", elapsed_ms=1, usage=dict(external_calls=2, input_tokens=100, output_tokens=20))
    unknown = dict(status="FAILED", elapsed_ms=2, usage={})
    report = summarize_measurements([known, unknown])
    assert report["model_calls"] is report["input_tokens"] is report["output_tokens"] is None
    assert report["known_model_calls"] == 2 and report["unknown_model_call_runs"] == 1
    assert report["known_input_tokens"] == 100 and report["known_output_tokens"] == 20
    assert report["usage_complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("readback_failure", [False, True])
@pytest.mark.parametrize("unprocessed", [0, 1])
async def test_full_runner_records_projection_and_keeps_spend_on_readback_failure(tmp_path, monkeypatch, readback_failure, unprocessed):
    from types import SimpleNamespace
    from app.trip_understanding import worker
    from scripts import measure_platform_corpus as runner
    from tests.corpus_projection_case import replay
    from tests.corpus_projection_case import SOURCE, demo_annotation

    sample = {"source": SOURCE}
    output = await replay()
    # Control completion separately from content to exercise both runner outcomes.
    output.proposal.unprocessed_count = unprocessed
    output.proposal.binding.update(external_calls=2, input_tokens=100, output_tokens=20)
    model = SimpleNamespace(prompt="controlled replay", model="controlled", max_output_tokens=4096,
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=None))))
    class ReplayPipeline:
        inference_provider = model
        place_resolver = object()
        async def run(self, text):
            assert text == sample["source"]
            return output
        async def aclose(self):
            pass
    monkeypatch.setattr(worker, "build_configured_full_pipeline", lambda settings: ReplayPipeline())
    if readback_failure:
        async def fail(*args):
            raise RuntimeError("controlled readback failure")
        monkeypatch.setattr(runner, "read_public_projection", fail)
    manifest, labels, env, result = [tmp_path / name for name in ("cases.json", "labels.json", "config.env", "result.json")]
    manifest.write_text(json.dumps(dict(provenance="authorized_reference", cases=[dict(
        id="demo", family_id="demo", split="development", text=sample["source"], source_kind="synthetic_independent_scenario")]), ensure_ascii=False), encoding="utf-8")
    labels.write_text(json.dumps(dict(annotations=[dict(demo_annotation(sample["source"]), case_id="demo", source_text=sample["source"])]), ensure_ascii=False), encoding="utf-8")
    env.write_text("TRIP_UNDERSTANDING_PROVIDER_MODE=live\n", encoding="utf-8")
    args = SimpleNamespace(output=result, runtime_root=Path(__file__).resolve().parents[2], manifest=manifest,
        limit=None, case_ids=None, split="development", labels=labels, config_env=env, mode="full", prompt_path=None,
        record_raw_calls=False, thinking_budget=None, repeat=1)
    assert await runner.measure(args) == int(readback_failure)
    report = json.loads(result.read_text(encoding="utf-8"))
    assert report["summary"]["model_calls"] == 2
    assert report["summary"]["input_tokens"] == 100 and report["summary"]["output_tokens"] == 20
    assert report["summary"]["expected_visits"] == 9
    assert report["summary"]["structure_correct_rate"] == (0 if readback_failure or unprocessed else 1)
    assert report["by_source_kind"]["synthetic_independent_scenario"] == report["summary"]


def test_equivalent_references_do_not_collapse_distinct_return_visits():
    label = dict(annotation_status="reviewed", annotator_type="implementation_agent", activities=[
        dict(id="first", name="公园甲", day_index=1, role="PLANNED", span_start=0, span_end=3,
             equivalent_reference_spans=[dict(span_start=10, span_end=13)]),
        dict(id="return", name="公园甲", day_index=1, role="PLANNED", span_start=20, span_end=23)])
    actual = [dict(name="公园甲", day_index=1, role="PLANNED", span_start=10, span_end=13),
              dict(name="公园甲", day_index=1, role="PLANNED", span_start=20, span_end=23)]
    assert compare_annotations(label, actual)["matched_places"] == 2
    actual[1].update(span_start=0, span_end=3)
    result = compare_annotations(label, actual)
    assert result["matched_places"] == 1 and not result["semantic_exact"]
