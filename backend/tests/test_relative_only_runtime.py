"""New text/checking runtime is relative; historical timing readers stay usable.

All responses and place identities are fixed. No model or map HTTP is made.
"""
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.audit.models import EvidenceSnapshot
from app.itineraries.models import RevisionSource
from app.trip_understanding.experience_inference import ExperienceQwenProvider, SemanticDraft, proposal_from_draft
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.g03 import build_itinerary_revision, run_g03_audit, public_checks, check_route_basis, command_for_finding
from app.trip_understanding.models import MealSlotView
from app.trip_understanding.pipeline import TripUnderstandingPipeline


SOURCE = "北京。\nDay1（9月12日）：09:00到故宫博物院游览60分钟，已经预约。午餐吃面。然后到景山公园。\nDay2（9月13日）：若有余力去北海公园。"
PAYLOAD = dict(destination="北京", day_labels=["9月12日", "9月13日"], unprocessed_quotes=[], activities=[
    dict(source_quote="故宫博物院", place_name="故宫博物院", role="PLANNED", day_index=1,
         category="景点", start_time="09:00", visit_duration_minutes=60, timing_source="TEXT",
         fixed_commitment=True, time_evidence="09:00到故宫博物院游览60分钟，已经预约"),
    dict(source_quote="午餐吃面", place_name=None, role="PLANNED", day_index=1, category="餐饮", meal_role="LUNCH"),
    dict(source_quote="景山公园", place_name="景山公园", role="PLANNED", day_index=1, category="景点"),
    dict(source_quote="北海公园", place_name="北海公园", role="OPTIONAL", day_index=2, category="景点"),
])
TEMPORAL_FIELDS = {"start_time", "end_time", "visit_duration_minutes", "timing_source", "locked", "fixed_commitment", "time_evidence"}


class Client:
    def __init__(self, *values):
        self.values, self.calls = list(values), []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        assert self.values, "unexpected additional model call"
        value = self.values.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
            content=json.dumps(value, ensure_ascii=False)))], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10))


def provider(client, **kwargs):
    return ExperienceQwenProvider(api_key="fixed", base_url="https://fixed.invalid", model="fixed",
                                  client=client, enable_day_sections=False, **kwargs)


async def result_for(payload=PAYLOAD, source=SOURCE):
    client = Client(payload, payload)
    output = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run(source)
    return output, client


@pytest.mark.asyncio
async def test_new_default_wire_has_required_semantics_without_temporal_fields():
    _output, client = await result_for()
    schema = client.calls[0]["response_format"]["json_schema"]["schema"]
    activity = schema["$defs"]["SemanticActivity"]
    assert not TEMPORAL_FIELDS.intersection(activity["properties"])
    assert {"source_quote", "place_name", "day_index", "role"} <= set(activity["required"])
    assert {"day_labels", "activities", "unprocessed_quotes"} <= set(schema["required"])
    assert {"meal_role", "lodging_event", "lodging_scope", "city", "city_evidence"} <= set(activity["properties"])
    assert activity["additionalProperties"] is False
    assert schema["properties"]["activities"]["maxItems"] == 160
    assert schema["properties"]["day_labels"]["items"] == {"type": "null"}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_old_time", [False, True])
async def test_new_pipeline_ignores_saved_temporal_fields_without_losing_visit_or_meal(bad_old_time):
    value = copy.deepcopy(PAYLOAD)
    if bad_old_time:
        value["activities"][0].update(start_time="25:80", visit_duration_minutes=-20, time_evidence="无依据时间")
    output, client = await result_for(value)
    assert len(client.calls) == 1
    assert [(m.atomic_place_name, m.day_index, m.role.value) for m in output.proposal.mentions] == [
        ("故宫博物院", 1, "PLANNED"), (None, 1, "PLANNED"), ("景山公园", 1, "PLANNED"), ("北海公园", 2, "OPTIONAL")]
    assert output.proposal.unprocessed_count == 0
    assert [d.label for d in output.public_result.days] == ["Day 1", "Day 2"]
    assert [m.meal_role for m in output.public_result.days[0].meal_slots] == ["LUNCH"]
    for mention in output.proposal.mentions:
        assert (mention.start_time, mention.end_time, mention.visit_duration_minutes, mention.time_hint) == (None, None, None, None)
        assert not mention.locked and not mention.fixed_commitment
    for day in output.public_result.days:
        for item in [*day.activities, *day.alternatives]:
            assert item.start_time is item.end_time is item.visit_duration_minutes is None


@pytest.mark.asyncio
async def test_new_boundary_does_not_ignore_missing_place_field_or_invented_name():
    value = copy.deepcopy(PAYLOAD)
    del value["activities"][0]["place_name"]
    value["activities"][2]["place_name"] = "原文不存在的门店"
    output, client = await result_for(value)
    assert len(client.calls) == 2
    assert output.proposal.unprocessed_count > 0
    assert not output.public_result.coverage.complete
    assert all(m.atomic_place_name != "原文不存在的门店" for m in output.proposal.mentions)


@pytest.mark.asyncio
async def test_calendar_only_headings_become_relative_days_without_parsing_dates():
    source = "北京。\n## 9月12日\n故宫博物院。\n## 9月13日\n景山公园。"
    value = dict(destination="北京", day_labels=["9月12日", "9月13日"], unprocessed_quotes=[], activities=[
        dict(source_quote=name, place_name=name, role="PLANNED", day_index=day, category="景点")
        for day, name in [(1, "故宫博物院"), (2, "景山公园")]])
    output, _client = await result_for(value, source)
    assert [d.label for d in output.public_result.days] == ["Day 1", "Day 2"]
    assert [[a.name for a in d.activities] for d in output.public_result.days] == [["故宫博物院"], ["景山公园"]]
    assert output.proposal.unprocessed_count == 0


def test_historical_draft_reader_keeps_timing_and_labels():
    old = proposal_from_draft(SOURCE, SemanticDraft.model_validate(PAYLOAD))
    assert old.mentions[0].start_time == "09:00"
    assert old.mentions[0].visit_duration_minutes == 60
    assert old.day_labels == {1: "9月12日", 2: "9月13日"}


@pytest.mark.asyncio
async def test_explicit_legacy_provider_contract_is_still_replayable():
    client = Client(PAYLOAD)
    old = await provider(client, relative_only=False).propose(SOURCE)
    assert old.mentions[0].start_time == "09:00" and old.day_labels[1] == "9月12日"
    assert TEMPORAL_FIELDS <= set(client.calls[0]["response_format"]["json_schema"]["schema"]["$defs"]["SemanticActivity"]["properties"])


@pytest.mark.asyncio
async def test_semantic_repair_stays_relative_and_preserves_other_semantics():
    bad = copy.deepcopy(PAYLOAD)
    bad["activities"][0]["place_name"] = "未出现的分店"
    client = Client(bad, PAYLOAD)
    output = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run(SOURCE)
    assert len(client.calls) == 2
    assert [m.atomic_place_name for m in output.proposal.mentions] == ["故宫博物院", None, "景山公园", "北海公园"]
    assert output.proposal.unprocessed_count == 0
    assert [d.label for d in output.public_result.days] == ["Day 1", "Day 2"]
    for request in client.calls:
        assert not TEMPORAL_FIELDS.intersection(request["response_format"]["json_schema"]["schema"]["$defs"]["SemanticActivity"]["properties"])


@pytest.mark.asyncio
async def test_real_day_section_orchestrator_uses_relative_contract_for_every_day():
    source = "北京。\n" + "这段仅介绍攻略的阅读方法，不另加到访。\n" * 55 + (
        "Day1（9月12日）：09:00到故宫博物院游览60分钟。\nDay2（9月13日）：到景山公园。")
    client = Client(dict(cross_day_dependencies=False, sections=[
        dict(day_index=1, start_quote="Day1", occurrence=1), dict(day_index=2, start_quote="Day2", occurrence=1)]),
        dict(destination="北京", day_labels=["9月12日"], unprocessed_quotes=[], activities=[PAYLOAD["activities"][0]]),
        dict(destination="北京", day_labels=[None, "9月13日"], unprocessed_quotes=[], activities=[
            {**PAYLOAD["activities"][2], "day_index": 2}]))
    model = ExperienceQwenProvider(api_key="fixed", base_url="https://fixed.invalid", model="fixed", client=client)
    output = await TripUnderstandingPipeline(model, ControlledSnapshotPlaceResolver()).run(source)
    assert len(client.calls) == 3
    assert [(m.atomic_place_name, m.day_index, m.start_time) for m in output.proposal.mentions] == [
        ("故宫博物院", 1, None), ("景山公园", 2, None)]
    assert [d.label for d in output.public_result.days] == ["Day 1", "Day 2"]
    assert output.proposal.unprocessed_count == 0
    for request in client.calls[1:]:
        assert not TEMPORAL_FIELDS.intersection(request["response_format"]["json_schema"]["schema"]["$defs"]["SemanticActivity"]["properties"])


@pytest.mark.asyncio
async def test_prepared_saved_plan_gets_new_projection_without_mutating_old_plan():
    old = proposal_from_draft(SOURCE, SemanticDraft.model_validate(PAYLOAD))
    before = old.model_dump_json()
    client = Client()
    output = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver(), relative_only=True).run(SOURCE, prepared_plan=old)
    assert client.calls == []
    assert old.model_dump_json() == before
    assert output.proposal.mentions[0].start_time is None
    assert [d.label for d in output.public_result.days] == ["Day 1", "Day 2"]
    assert [(m.atomic_place_name, m.day_index, m.role) for m in output.proposal.mentions] == [
        (m.atomic_place_name, m.day_index, m.role) for m in old.mentions]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["optional", "unprocessed", "empty"])
@pytest.mark.parametrize("stored_day_count", [0, 3])
async def test_prepared_plan_keeps_later_day_extent_when_only_display_labels_are_removed(kind, stored_day_count):
    from app.trip_understanding.models import SemanticDiagnostic

    source = SOURCE + "\nDay3（9月14日）。"
    old = proposal_from_draft(source, SemanticDraft.model_validate({**PAYLOAD, "day_labels": ["9月12日", "9月13日", "9月14日"]}))
    mentions = list(old.mentions) if kind == "optional" else [m for m in old.mentions if m.day_index == 1]
    old = old.model_copy(update={"mentions": mentions, "day_count": stored_day_count,
        "day_labels": {1: "9月12日", 2: "9月13日", 3: "9月14日"},
        "unprocessed_count": 1 if kind == "unprocessed" else 0,
        "unprocessed_by_day": {2: 1} if kind == "unprocessed" else {},
        "diagnostics": [SemanticDiagnostic(category="DAY_SECTION_UNPROCESSED", field="days[2]")]
            if kind == "unprocessed" else []})
    before = old.model_dump_json()
    output = await TripUnderstandingPipeline(provider(Client()), ControlledSnapshotPlaceResolver(), relative_only=True).run(source, prepared_plan=old)
    public = output.public_result
    assert len(public.days) == output.proposal.day_count == 3
    assert [d.label for d in public.days] == ["Day 1", "Day 2", "Day 3"]
    assert public.days[2].activities == [] and public.days[2].alternatives == []
    assert old.model_dump_json() == before
    if kind == "optional":
        assert [a.name for a in public.days[1].alternatives] == ["北海公园"]
    elif kind == "unprocessed":
        assert public.days[1].activities == [] and public.days[1].unprocessed_count == 1
        assert public.coverage.unprocessed_count == 1 and not public.coverage.complete


def test_production_worker_explicitly_uses_relative_provider_and_prepared_plan_projection(monkeypatch):
    from app.trip_understanding import worker
    seen = {}
    monkeypatch.setattr(worker, "ExperienceQwenProvider", lambda **kwargs: seen.update(kwargs) or SimpleNamespace())
    monkeypatch.setattr(worker, "AmapPlaceResolver", lambda **kwargs: SimpleNamespace())
    settings = SimpleNamespace(trip_understanding_provider_mode="live", qwen_api_key="fixed",
        qwen_api_url="https://fixed.invalid", trip_understanding_qwen_model="fixed", trip_understanding_qwen_deadline_seconds=60,
        trip_understanding_qwen_max_output_tokens=4096, trip_understanding_qwen_input_cny_per_million=None,
        trip_understanding_qwen_output_cny_per_million=None, amap_api_key="fixed", trip_understanding_amap_place_deadline_seconds=3,
        trip_understanding_amap_place_max_concurrency=4)
    pipeline = worker.build_configured_full_pipeline(settings)
    assert seen["relative_only"] is True and seen["enable_source_visits"] is True
    assert pipeline.relative_only is True


def empty_snapshot():
    return EvidenceSnapshot(snapshot_id="relative-snapshot", itinerary_id="relative-itinerary", itinerary_revision=1,
                            workspace_id="relative-workspace", policy_version="fixed", facts=[], created_at=datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_new_check_materialization_ignores_old_card_timing_and_calendar_without_mutating_record():
    output, _ = await result_for()
    result = output.public_result
    result.days[0].activities[0].start_time = "09:00"
    result.days[0].activities[0].visit_duration_minutes = 60
    result.days[0].activities[0].fixed_commitment = True
    before = result.model_dump_json()
    revision, profile = build_itinerary_revision(result=result, bindings={}, assumptions=[
        {"key": "calendar", "value": "2026-10-01 至 2026-10-02", "source": "USER_EDIT"}], city="北京",
        workspace_id="relative-workspace", itinerary_id="relative-itinerary", revision=1,
        parent_revision=None, source_type=RevisionSource.IMPORT)
    assert profile.mode == "DAY_INDEX_ONLY" and profile.start is profile.end is None
    assert revision.date_range.start is revision.date_range.end is None
    assert all(day.date is None for day in revision.days)
    assert all(s.start_time is s.end_time is s.visit_duration_minutes is None and not s.fixed_commitment
               for day in revision.days for s in day.stops)
    assert result.model_dump_json() == before


@pytest.mark.asyncio
async def test_relative_checks_respect_anonymous_meal_slot_and_never_request_clock_or_calendar():
    output, _ = await result_for()
    result = output.public_result
    assert isinstance(result.days[0].meal_slots[0], MealSlotView)
    revision, profile = build_itinerary_revision(result=result, bindings={}, assumptions=[], city="北京",
        workspace_id="relative-workspace", itinerary_id="relative-itinerary", revision=1,
        parent_revision=None, source_type=RevisionSource.IMPORT)
    snapshot = empty_snapshot()
    report = run_g03_audit(revision=revision, profile=profile, room_id="relative-room", snapshot=snapshot)
    assert not any(f.reason_code.startswith("SCHEDULE_") or "CALENDAR" in f.reason_code
                   or f.reason_code in {"DAY_INDEX_HAS_NO_DATE_HARD_CONCLUSION", "MEAL_BREAK_MISSING"} for f in report.findings)
    assert any(f.reason_code == "ROUTE_CONFIRMATION_REQUIRED" for f in report.findings)
    view = public_checks(report, snapshot, check_tokens={f.finding_id: "x" * 24 for f in report.findings}, result=result)
    assert "活动时间" not in view.message and "具体日期" not in view.message


@pytest.mark.asyncio
async def test_stored_clock_report_remains_readable_but_cannot_authorize_new_clock_change():
    from app.audit.models import AuditFinding, AuditSeverity, AuditStatus
    output, _ = await result_for()
    revision, profile = build_itinerary_revision(result=output.public_result, bindings={}, assumptions=[], city="北京",
        workspace_id="relative-workspace", itinerary_id="relative-itinerary", revision=1,
        parent_revision=None, source_type=RevisionSource.IMPORT)
    snapshot = empty_snapshot()
    report = run_g03_audit(revision=revision, profile=profile, room_id="relative-room", snapshot=snapshot)
    old = AuditFinding(finding_id="old-clock", rule_id="experience.schedule_feasibility", rule_version="1.2.0",
        status=AuditStatus.VIOLATED, severity=AuditSeverity.HIGH, reason_code="SCHEDULE_CONFLICT", message="旧时刻冲突",
        affected_days=[0], repairable=True, input_values={"shift_changes": []})
    old_report = report.model_copy(update={"findings": [old]})
    view = public_checks(old_report, snapshot, check_tokens={old.finding_id: "x" * 24}, result=output.public_result)
    assert view.items == [] and view.available_actions == []
    assert check_route_basis(old, snapshot) == (True, False)
    with pytest.raises(ValueError, match="absolute timing"):
        command_for_finding(old, output.public_result)
    assert old_report.findings[0].message == "旧时刻冲突"


def test_actual_v3_worker_result_materialize_and_checks_use_relative_contract():
    import asyncio
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import trip_understandings_v3 as api
    from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
    from app.trip_understanding.worker import TripUnderstandingWorker

    repository = InMemoryTripUnderstandingRepository()
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.get_trip_understanding_repository] = lambda: repository
    fixed_model = Client(PAYLOAD)
    pipeline = TripUnderstandingPipeline(provider(fixed_model), ControlledSnapshotPlaceResolver(), relative_only=True)
    with TestClient(app) as browser:
        accepted = browser.post("/api/v3/trip-understandings", json={"mode": "FULL", "source": {"type": "TEXT", "text": SOURCE}},
                                headers={"Idempotency-Key": "relative-source-create"})
        assert accepted.status_code == 202
        base = "/api/v3/trip-understandings/" + accepted.json()["public_resource_id"]
        assert asyncio.run(TripUnderstandingWorker(repository, full_pipeline=pipeline).run_once("relative-worker"))
        result = browser.get(base + "/result")
        assert result.status_code == 200
        assert [day["label"] for day in result.json()["days"]] == ["Day 1", "Day 2"]
        assert [slot["meal_role"] for slot in result.json()["days"][0]["meal_slots"]] == ["LUNCH"]
        materialized = browser.post(base + "/materialize", headers={"If-Match": result.headers["etag"], "Idempotency-Key": "relative-materialize"})
        assert materialized.status_code == 200
        assert materialized.json()["calendar"] == "按 Day 编号安排"
        checked = browser.get(base + "/checks")
        assert checked.status_code == 200
        assert not {"活动时间尚未核对", "这段时间来不及", "两处活动的时间重叠", "确认活动时长", "补一个用餐停留"}.intersection(
            item["title"] for item in checked.json()["items"])
        assert "具体日期" not in checked.json()["message"]
        assert browser.get(base + "/result").json() == result.json()
    assert len(fixed_model.calls) == 1


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_rechecking_previously_materialized_calendar_keeps_old_record_but_uses_relative_context(kind, monkeypatch):
    from functools import partial
    from app.audit.engine import AuditEngine
    from app.trip_understanding import g03, g03_repository
    from app.trip_understanding.models import ActivityTimeSetCommand, AssumptionSetCommand, MaterializedTripView
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from tests.test_experience_v3_journey import create, finish, refresh, repository_for

    async with repository_for(kind) as repository:
        now = datetime.now(timezone.utc)
        resource = await finish(repository, await create(repository, "old-dated-trip", now), now)
        service = TripUnderstandingApplicationService(repository)
        stored = await repository.get_result(resource)
        for key, command in [
            ("legacy-calendar", AssumptionSetCommand(command_type="ASSUMPTION_SET", key="calendar", value="2026-10-01 至 2026-10-02")),
            ("legacy-clock", ActivityTimeSetCommand(command_type="ACTIVITY_TIME_SET", activity_token=stored.result.days[0].activities[0].activity_token,
                                                   start_time="09:00", visit_duration_minutes=60)),
        ]:
            # Historical direct commands remain readable; the active HTTP guard
            # is separately verified to reject creating these fields today.
            if key == "legacy-clock":
                command = command.model_copy(update={"activity_token": stored.result.days[0].activities[0].activity_token})
            await service.apply_command(resource, command, expected_etag=stored.opaque_etag, idempotency_key=key, now=now)
            resource, stored = await refresh(repository, resource, now)
        with monkeypatch.context() as legacy:
            legacy.setattr(g03_repository, "build_itinerary_revision", partial(g03.build_itinerary_revision, relative_only=False))
            legacy.setattr(g03_repository, "run_g03_audit", partial(g03.run_g03_audit, relative_only=False))
            legacy.setattr(g03_repository, "_materialized_view", lambda profile: MaterializedTripView(
                message="历史已保存结果", calendar=profile.public_calendar, party_size=profile.party_size))
            old = await service.materialize_trip(resource, expected_etag=stored.opaque_etag, idempotency_key="old-materialized", now=now)
        assert old.view.calendar == "2026-10-01 至 2026-10-02"

        async def saved_legacy_state():
            if kind == "memory":
                state = repository.g03_materialized[resource.understanding_id]
                return state["itinerary"].model_dump(mode="json"), state["profile"]
            rows = await repository._pool.fetch("SELECT * FROM itinerary_revisions ORDER BY workspace_id, revision")
            profile = await repository._pool.fetchrow("SELECT trip_start_date, trip_end_date, calendar_mode FROM trip_workspaces")
            return [dict(row) for row in rows], dict(profile)

        old_state = await saved_legacy_state()
        seen_starts = []
        engine_run = AuditEngine.run

        def record_check_context(self, **kwargs):
            seen_starts.append(kwargs["task_spec"].date_range.start)
            return engine_run(self, **kwargs)

        monkeypatch.setattr(AuditEngine, "run", record_check_context)
        current = await service.materialize_trip(resource, expected_etag=stored.opaque_etag, idempotency_key="new-relative-check", now=now)
        assert current.view.calendar == "按 Day 编号安排"
        assert seen_starts == [None]
        assert await saved_legacy_state() == old_state
        resource, after = await refresh(repository, resource, now)
        assert after.opaque_etag == stored.opaque_etag and after.result == stored.result
        assert after.result.days[0].activities[0].start_time == "09:00"
        checks = await repository.get_trip_checks(resource)
        assert not {"活动时间尚未核对", "这段时间来不及", "两处活动的时间重叠", "确认活动时长"}.intersection(item.title for item in checks.items)
        replay = await service.materialize_trip(resource, expected_etag=stored.opaque_etag, idempotency_key="old-materialized", now=now)
        assert replay.replayed and replay.view.calendar == "按 Day 编号安排"
        assert old.view.calendar == "2026-10-01 至 2026-10-02"
        assert seen_starts == [None] and await saved_legacy_state() == old_state
