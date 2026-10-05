import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from app.config import Settings
from app.trip_understanding.example_preprocessing import catalog, exact_plan, provisional_plan, select_example
from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.model_adapter import execution_config
from app.trip_understanding.models import PlaceResolutionOutcome
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_short_streaming import Resolver


def settings():
    return Settings(trip_short_stream_enabled=True, trip_example_preprocessing_enabled=True)


def provider(source, reference=None, create=None):
    async def forbidden(**options):
        raise AssertionError("exact public example must not request a model")
    config = execution_config(settings(), source_text=source, example_reference=reference)
    return ExperienceQwenProvider(api_key="controlled", model=config.model, base_url=config.base_url,
        execution_config=config, client=NS(chat=NS(completions=NS(create=create or forbidden))))


@pytest.mark.parametrize("example", catalog(), ids=lambda x: x["id"])
@pytest.mark.asyncio
async def test_exact_example_recompiles_independent_visits_and_preserves_semantics(example):
    class Places(Resolver):
        async def resolve(self, **query):
            if query["atomic_place_name"] == "大芬油画村":
                return PlaceResolutionOutcome(receipt={"status": "AMBIGUOUS", "external_calls": 1})
            return await super().resolve(**query)
    text = " \r\n" + example["text"].replace("\n", "\r\n") + "\n "
    updates = []
    async def progress(update):
        updates.append(update)
    model = provider(text)
    pipeline = TripUnderstandingPipeline(model, Places(), relative_only=True)
    first = await pipeline.run(text, progress_callback=progress)
    second = await pipeline.run(text)
    cards = [c for d in first.public_result.days for c in d.activities]
    assert [[c.name for c in d.activities] for d in first.public_result.days] == example["days"]
    assert len(cards) == 12 and not model.adapter.calls
    assert first.public_result.coverage.unprocessed_count == 0
    assert {c.activity_token for c in cards}.isdisjoint(c.activity_token for d in second.public_result.days for c in d.activities)
    assert updates[0].progress.semantic_complete is True
    if example["id"] == "shenzhen":
        assert cards[10].name == "大芬油画村" and cards[10].status == "NEEDS_CONFIRMATION"
        assert any(c.name == "深圳市当代艺术与城市规划馆" for d in first.public_result.days for c in d.alternatives)
    else:
        assert {x.name for c in cards for x in c.source_details} == {"太和殿", "御花园", "长廊"}


def test_selection_is_new_task_configuration_and_requires_a_reliable_baseline():
    example = catalog()[0]
    reference = {"id": example["id"], "version": example["version"]}
    assert select_example(example["text"], {**reference, "version": 900}) is None
    assert select_example(example["text"].replace("北京", "上海"), reference) is None
    assert select_example(example["text"].replace("故宫博物院", "天坛公园")) is None
    config = execution_config(settings(), source_text=example["text"])
    disabled = settings().model_copy(update={"trip_example_preprocessing_enabled": False})
    assert execution_config(disabled, source_text=example["text"]).example_preprocessing is None
    assert config.example_preprocessing["mode"] == "exact"
    assert execution_config(settings(), source_text=example["text"] + "。" * 501,
                            example_reference=reference).example_preprocessing is None


@pytest.mark.parametrize("change", [
    lambda s: s.replace("接着去大芬油画村，", ""),
    lambda s: s.replace("大芬油画村", "深圳美术馆"),
    lambda s: s.replace("Day 3：", "Day 3：先去梧桐山，再"),
    lambda s: s.replace("如果下雨", "如果太热"),
    lambda s: s.replace("接着去莲花山公园，在园内风筝广场活动；", ""),
    lambda s: s.replace("先逛东门老街", "先逛东门老街，再去东门老街"),
    lambda s: s.replace("最后去仙湖植物园", "仙湖植物园取消"),
])
def test_changed_paragraphs_never_reappear_as_old_confirmed_arrangements(change):
    example = catalog()[1]
    source = change(example["text"])
    selection = select_example(source, {"id": example["id"], "version": 1})
    assert selection["mode"] == "incremental"
    plan = provisional_plan(source, selection)
    assert plan.unprocessed_count > 0
    assert all(m.semantic_review == "PENDING" and source[m.span_start:m.span_end] == m.raw_text for m in plan.mentions)
    changed_day = 1 if "Day 1：" + example["text"].split("Day 1：")[1].split("\n")[0] not in source else 3
    assert all(m.day_index != changed_day for m in plan.mentions)


@pytest.mark.asyncio
async def test_model_timeout_keeps_provisional_cards_without_place_queries():
    example = catalog()[0]
    source = example["text"].replace("景山公园", "香山公园")
    saw_draft = asyncio.Event()
    async def timeout(**options):
        await asyncio.wait_for(saw_draft.wait(), 2)
        raise TimeoutError()
    model = provider(source, {"id": "beijing", "version": 1}, timeout)
    resolver = Resolver()
    async def progress(update):
        if any(d.activities for d in update.snapshot.days):
            assert all(c.semantic_review == "PENDING" and c.status != "READY" and not c.photo_url
                       for d in update.snapshot.days for c in d.activities)
            saw_draft.set()
    result = await TripUnderstandingPipeline(model, resolver, relative_only=True).run(source, progress_callback=progress)
    assert saw_draft.is_set() and len(model.adapter.calls) == 1 and not resolver.calls
    assert result.public_result.coverage.unprocessed_count > 0
    assert len([c for d in result.public_result.days for c in d.activities]) == 8


def beijing_delta(source):
    from app.trip_understanding.example_preprocessing import seed_plan
    example, plan = seed_plan("beijing", 1)
    roots = [m for m in plan.mentions if not m.parent_mention_id]
    rows = []
    for m in roots:
        rows.append(dict(source_quote=m.raw_text, place_name=m.atomic_place_name, role=m.role.value,
            day_index=m.day_index, city=m.city_hint, city_evidence=m.city_evidence,
            operation="UPDATE" if m.raw_text == "景山公园" else "KEEP", base_visit_id=m.mention_id,
            source_details=[dict(kind=x.detail_kind, source_quote=x.raw_text, optional=False, evidence=x.role_evidence)
                            for x in plan.mentions if x.parent_mention_id == m.mention_id]))
    value = dict(destination=plan.destination_name, day_labels=[None] * 3, activities=rows,
        choice_groups=[], unprocessed_quotes=[], deleted_base_ids=[],
        order_groups=[dict(kind=g.kind, activity_indices=[next(i for i, m in enumerate(roots) if m.mention_id == key)
                      for key in g.member_mention_ids], scope_quote=example["text"][g.scope_start:g.scope_end])
                      for g in plan.order_assessment.groups], source_inventory=plan.binding["_source_inventory"])
    return json.loads(json.dumps(value, ensure_ascii=False).replace("景山公园", "香山公园"))


@pytest.mark.asyncio
async def test_reviewed_delta_updates_in_place_and_uses_current_source():
    example = catalog()[0]
    source = example["text"].replace("景山公园", "香山公园")
    seen = asyncio.Event()
    class Stream:
        async def __aiter__(self):
            await asyncio.wait_for(seen.wait(), 2)
            raw = json.dumps(beijing_delta(source), ensure_ascii=False)
            for offset in range(0, len(raw), 40):
                yield NS(model="controlled", usage=None, choices=[NS(index=0, finish_reason=None, delta=NS(content=raw[offset:offset+40]))])
                await asyncio.sleep(0)
            yield NS(model="controlled", usage=None, choices=[NS(index=0, finish_reason="stop", delta=NS(content=None))])
        async def close(self):
            pass
    async def create(**options):
        return Stream()
    model = provider(source, {"id": "beijing", "version": 1}, create)
    updates = []
    async def progress(update):
        updates.append(update)
        if any(d.activities for d in update.snapshot.days):
            seen.set()
    output = await TripUnderstandingPipeline(model, Resolver(), relative_only=True).run(source, progress_callback=progress)
    cards = [c for d in output.public_result.days for c in d.activities]
    assert len(model.adapter.calls) == 1
    assert output.public_result.coverage.complete
    assert cards[1].name == "香山公园" and "景山公园" not in [c.name for c in cards]
    initial = {c.name: c.activity_token for d in updates[0].snapshot.days for c in d.activities}
    assert initial and all(c.activity_token == initial[c.name] for c in cards if c.name in initial)
    assert all(c.semantic_review == "CONFIRMED" for c in cards)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_task_persists_selection_and_idempotency_without_exposing_seed(kind, monkeypatch):
    from datetime import datetime, timezone
    from app.trip_understanding.models import CreateFullRequest
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from tests.test_experience_v3_journey import repository_for
    configured = settings()
    monkeypatch.setattr("app.trip_understanding.repository.get_settings", lambda: configured)
    monkeypatch.setattr("app.trip_understanding.execution_repository.get_settings", lambda: configured)
    source = catalog()[0]["text"].replace("景山公园", "香山公园")
    body = CreateFullRequest(mode="FULL", source={"type": "TEXT", "text": source},
                             example_reference={"id": "beijing", "version": 1})
    now = datetime.now(timezone.utc)
    async with repository_for(kind) as repo:
        service = TripUnderstandingApplicationService(repo)
        first = await service.create_full(body, owner_user_id=None, capability_hash="a" * 64,
                                           idempotency_key="example-submit", now=now)
        repeated = await service.create_full(body, owner_user_id=None, capability_hash="a" * 64,
                                              idempotency_key="example-submit", now=now)
        assert repeated.replayed and repeated.accepted.public_resource_id == first.accepted.public_resource_id
        configured.trip_example_preprocessing_enabled = False
        job = await repo.claim_next(worker_id="example-worker", now=now, lease_seconds=30)
        config, dispatched = await repo.load_execution(job, now=now)
        assert config.example_preprocessing == {"id": "beijing", "version": 1, "mode": "incremental"}
        assert not dispatched
        public = first.accepted.model_dump_json()
        assert "example_preprocessing" not in public and "source_inventory" not in public


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_cached_trip_move_delete_undo_and_reopen_do_not_change_the_seed(kind):
    from datetime import datetime, timezone
    from app.trip_understanding.models import CreateFullRequest, ActivityMoveCommand, ActivityDeleteCommand, UndoCommand
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from tests.test_experience_v3_journey import repository_for, refresh
    example = catalog()[0]
    original = exact_plan(example["text"], select_example(example["text"]))
    model = provider(example["text"])
    output = await TripUnderstandingPipeline(model, Resolver(), relative_only=True).run(example["text"])
    now = datetime.now(timezone.utc)
    async with repository_for(kind) as repo:
        service = TripUnderstandingApplicationService(repo)
        accepted = await service.create_full(CreateFullRequest(mode="FULL", source={"type": "TEXT", "text": example["text"]}),
            owner_user_id=None, capability_hash="a" * 64, idempotency_key="cached-edit", now=now)
        job = await repo.claim_next(worker_id="cached-editor", now=now, lease_seconds=30)
        await repo.complete_job(job, output, now=now)
        resource = await repo.authorize(accepted.accepted.public_resource_id, capability_hash="a" * 64, now=now)
        resource, stored = await refresh(repo, resource, now)
        await service.apply_command(resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
            activity_token=stored.result.days[0].activities[0].activity_token, target_day_index=2, target_position=1),
            expected_etag=stored.opaque_etag, idempotency_key="move", now=now)
        resource, stored = await refresh(repo, resource, now)
        assert stored.result.days[1].activities[1].name == "故宫博物院"
        await service.apply_command(resource, ActivityDeleteCommand(command_type="ACTIVITY_DELETE",
            activity_token=stored.result.days[1].activities[1].activity_token), expected_etag=stored.opaque_etag,
            idempotency_key="delete", now=now)
        resource, stored = await refresh(repo, resource, now)
        assert sum(len(d.activities) for d in stored.result.days) == 11
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=stored.opaque_etag,
            idempotency_key="undo", now=now)
        resource, stored = await refresh(repo, resource, now)
        assert stored.result.days[1].activities[1].name == "故宫博物院"
        assert sum(len(d.activities) for d in stored.result.days) == 12 and not model.adapter.calls
    assert exact_plan(example["text"], select_example(example["text"])) == original
