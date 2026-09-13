"""Offline replay must not hide omissions, invent responses, or claim live quality."""
import json
from types import SimpleNamespace

import pytest

from scripts.replay_experience_layers import (
    SavedResponses, UnrecordedRequestError, attribute_provider_omissions, expected_errors, raw_inventory,
    replay_case, stage_delta, summarize,
)


def record(source, activities, *, call=1):
    return {"call": call, "messages": [{"role": "system", "content": "old prompt"},
                                       {"role": "user", "content": source}],
            "max_tokens": 4096, "model": "saved-model", "status": "COMPLETED",
            "choices": [{"finish_reason": "stop", "content": json.dumps(
                {"destination": "北京", "activities": activities}, ensure_ascii=False)}]}


@pytest.mark.asyncio
async def test_saved_output_replay_reports_prompt_changes_but_never_answers_other_inputs():
    client = SavedResponses([record("private original", [])])
    response = await client.create(messages=[{"role": "system", "content": "new prompt"},
                                             {"role": "user", "content": "private original"}], max_tokens=4096)
    assert response.choices[0].finish_reason == "stop"
    assert client.system_message_changes == 1
    with pytest.raises(UnrecordedRequestError):
        await client.create(messages=[{"role": "user", "content": "different private original"}], max_tokens=4096)
    assert client.unrecorded_requests == 1 and client.used == [1]


def test_raw_fields_remain_missing_or_null_without_inventing_place_names():
    result = raw_inventory([record("test", [{"role": "PLANNED"}, {"role": "OPTIONAL", "place_name": None}])])
    rows = result[0]["observations"]
    assert [r["place_name_field"] for r in rows] == ["MISSING", "NULL"]
    assert result[0]["counts"]["named"] == 0


def test_omission_attribution_distinguishes_missing_field_null_and_semantic_role():
    label = {"activities": [{"name": name, "day_index": 1, "role": "PLANNED"}
                            for name in ("甲公园", "乙公园", "丙公园", "丁公园")]}
    saved = record("synthetic", [
        {"source_quote": "甲公园", "day_index": 1, "role": "PLANNED"},
        {"source_quote": "乙公园", "place_name": None, "day_index": 1, "role": "PLANNED"},
        {"source_quote": "丙公园", "place_name": "丙公园", "day_index": 1, "role": "OPTIONAL"},
        {"source_quote": "丁公园", "place_name": "丁公园", "day_index": 1, "role": "PLANNED"},
    ])
    result = attribute_provider_omissions(label, [], [saved])
    assert result["counts"] == {"RAW_NAME_FIELD_MISSING": 1, "RAW_NAME_FIELD_EXPLICIT_NULL": 1,
                                "RAW_ROLE_OR_DAY_DIFFERENCE": 1, "RAW_MATCH_REQUIRES_OCCURRENCE_OR_VALIDATOR_REVIEW": 1}


def test_layer_diff_preserves_revisits_and_separates_non_itinerary_removal():
    rows = [{"key": key, "name": "同一地点", "day_index": day, "role": "PLANNED"}
            for key, day in (("first", 1), ("revisit", 2))]
    rows.append({"key": "explanation", "name": "介绍地点", "day_index": None, "role": "REFERENCE"})
    delta = stage_delta(rows, [{**rows[0], "day_index": 2}])
    assert len(delta["lost"]) == 2
    assert [r["key"] for r in delta["named_itinerary_lost"]] == ["revisit"]
    assert delta["named_itinerary_changed"][0]["before"]["day_index"] == 1


@pytest.mark.asyncio
async def test_current_provider_pipeline_and_public_projection_keep_roles_and_report_omissions():
    source = "北京两日游。Day 1：去故宫博物院，然后去北海公园。Day 2：景山公园可选。"
    raw = record(source, [
        {"source_quote": "故宫博物院", "place_name": "故宫博物院", "role": "PLANNED", "day_index": 1, "category": "景点"},
        {"source_quote": "景山公园", "place_name": "景山公园", "role": "OPTIONAL", "day_index": 2, "category": "景点"},
    ])
    label = {"annotation_status": "reviewed", "annotator_type": "independent_agent", "activities": [
        {"name": "故宫博物院", "day_index": 1, "role": "PLANNED"},
        {"name": "北海公园", "day_index": 1, "role": "PLANNED"},
        {"name": "景山公园", "day_index": 2, "role": "OPTIONAL"},
    ]}
    result = await replay_case(source, [raw], "saved-model", label)
    assert result["replay_complete"], result.get("error_category")
    assert result["provider_to_pipeline"]["named_itinerary_changed"] == []
    assert result["pipeline_to_public"]["named_itinerary_lost"] == []
    for stage in result["stages"].values():
        assert stage["semantic"]["by_role"]["PLANNED"] == {
            "expected": 2, "observed": 1, "matched": 1, "recall": .5, "precision": 1.0}
        assert stage["semantic_errors"]["missing_or_misassigned"][0]["name"] == "北海公园"
    failed = {"replay_complete": False, "stages": result["stages"]}
    summary = summarize([result, failed])
    assert summary["completed"] == 1 and summary["cases"] == 2
    assert summary["stages"]["provider"]["by_role"]["PLANNED"]["expected"] == 4


def test_semantic_errors_do_not_pair_wrong_day_or_role_and_keep_duplicate_expected_items():
    label = {"activities": [{"name": "公园", "day_index": 1, "role": "PLANNED"},
                            {"name": "公园", "day_index": 2, "role": "PLANNED"}]}
    errors = expected_errors(label, [{"name": "公园", "day_index": 1, "role": "OPTIONAL"}])
    assert len(errors["missing_or_misassigned"]) == 2
    assert len(errors["extra_or_misassigned"]) == 1


@pytest.mark.asyncio
async def test_measurement_runs_without_source_fingerprints_or_runtime_freezing(monkeypatch, tmp_path):
    from app.trip_understanding import experience_inference
    from scripts import measure_platform_corpus as measurement

    source = "北京Day 1：故宫博物院。"
    proposal = experience_inference.proposal_from_draft(source, experience_inference.SemanticDraft.model_validate({
        "destination": "北京", "activities": [{"source_quote": "故宫博物院", "place_name": "故宫博物院",
                                               "role": "PLANNED", "day_index": 1, "category": "景点"}]}))

    class LocalProvider:
        def __init__(self, **kwargs):
            self.prompt = "offline test prompt"
            self.model = "offline-test"
            self.max_output_tokens = 4096
            self.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=None)))

        async def propose(self, text):
            assert text == source
            return proposal

        async def aclose(self):
            pass

    monkeypatch.setattr(experience_inference, "ExperienceQwenProvider", LocalProvider)
    monkeypatch.setattr(measurement, "read_corpus", lambda *_a, **_k: [
        {"id": "test", "city": "北京", "family_id": "test", "split": "development",
         "text": source, "output_sha256": proposal.source_hash}])
    for name in ("source_fingerprint", "runtime_file_hashes", "freeze_runtime"):
        monkeypatch.setattr(measurement, name, lambda *_a, **_k: pytest.fail("Process-governance prerequisite was called"))
    args = SimpleNamespace(output=tmp_path / "measurement.json", runtime_root=measurement.ROOT,
        freeze_runtime=True, manifest=tmp_path / "unused.json", limit=None, case_ids=None,
        split="development", labels=None, config_env=tmp_path / "absent.env", prompt_path=None,
        mode="semantic", record_raw_calls=False, repeat=1)
    assert await measurement.measure(args) == 0
    report = json.loads(args.output.read_text(encoding="utf-8"))
    assert report["summary"]["completed"] == 1
    assert "source_fingerprint" not in report and "runtime_files" not in report
