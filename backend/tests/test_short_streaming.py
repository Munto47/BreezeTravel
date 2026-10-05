import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from app.config import Settings
from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.model_adapter import ExecutionConfig, execution_config
from app.trip_understanding.models import ResolvedPlace
from app.trip_understanding.pipeline import TripUnderstandingPipeline


SOURCE = "北京。Day1：故宫博物院，然后到景山公园。"


def payload():
    return dict(destination="北京", day_labels=[None], activities=[
        dict(source_quote=name, place_name=name, role="PLANNED", day_index=1, source_details=[])
        for name in ("故宫博物院", "景山公园")], choice_groups=[],
        order_groups=[dict(kind="INITIAL_ORDER", activity_indices=[0, 1], scope_quote=SOURCE)],
        unprocessed_quotes=[], source_inventory=dict(segments=[
            dict(segment_index=0, classification="CONTEXT", items=[], unresolved=False),
            dict(segment_index=1, classification="ARRANGEMENTS", unresolved=False, items=[
                dict(quote=name, role="PLANNED", day_index=1) for name in ("故宫博物院", "景山公园")])]))


class GatedStream:
    def __init__(self, value, visible, finish="stop"):
        text = json.dumps(value, ensure_ascii=False)
        first = json.dumps(value["activities"][0], ensure_ascii=False)
        split = text.index(first) + len(first)
        self.parts = (text[:split], text[split:])
        self.visible = visible
        self.finish = finish
        self.ended = False
        self.closed = False

    async def __aiter__(self):
        yield NS(model="controlled", usage=None, choices=[NS(index=0, finish_reason=None, delta=NS(content=self.parts[0]))])
        await asyncio.wait_for(self.visible.wait(), 2)
        if self.finish == "stop":
            yield NS(model="controlled", usage=None, choices=[NS(index=0, finish_reason=None, delta=NS(content=self.parts[1]))])
        self.ended = True
        yield NS(model="controlled", usage=None, choices=[NS(index=0, finish_reason=self.finish, delta=NS(content=None))])

    async def close(self):
        self.closed = True


class Resolver:
    def __init__(self):
        self.calls = []

    async def resolve(self, **query):
        self.calls.append(query)
        await asyncio.sleep(0)
        return ResolvedPlace(canonical_place_id=query["atomic_place_name"], name=query["atomic_place_name"],
            category="景点", area_or_address=query["city"], provider_binding={"city": query["city"]})


@pytest.mark.parametrize("finish", ["stop", "length", None, "invalid_item"])
@pytest.mark.asyncio
async def test_real_incremental_delivery_precedes_model_end_and_retains_identity(finish):
    visible = asyncio.Event()
    value = payload()
    if finish == "invalid_item":
        value['activities'][1].pop('day_index')
    stream = GatedStream(value, visible, "stop" if finish == "invalid_item" else finish)
    requests = []
    async def create(**options):
        requests.append(options)
        return stream
    config = ExecutionConfig(provider="KIMI_CODE", model="k3-256k", base_url="https://example.invalid/v1",
        credential_ref="kimi_for_code", processing_mode="short_stream")
    provider = ExperienceQwenProvider(api_key="controlled", model=config.model, base_url=config.base_url,
        execution_config=config, client=NS(chat=NS(completions=NS(create=create))))
    resolver = Resolver()
    updates = []
    async def progress(update):
        updates.append(update)
        if any(card.status == "READY" for day in update.snapshot.days for card in day.activities):
            visible.set()
    result = await TripUnderstandingPipeline(provider, resolver, relative_only=True).run(SOURCE, progress_callback=progress)
    first = next(u for u in updates if any(c.status == "READY" for d in u.snapshot.days for c in d.activities))
    assert first.progress.semantic_complete is False
    assert first.progress.places_total_final is False
    assert len(requests) == 1 and requests[0]["stream"] is True and stream.closed
    names = [c.name for d in result.public_result.days for c in d.activities]
    assert names == (["故宫博物院", "景山公园"] if finish == "stop" else ["故宫博物院"])
    assert len(resolver.calls) == len(names)
    assert result.public_result.days[0].activities[0].activity_token == first.snapshot.days[0].activities[0].activity_token
    assert result.public_result.coverage.complete is (finish == "stop")
    assert result.inference_binding["stream_complete"] is (finish == "stop")


def test_rollout_is_saved_only_for_new_short_text_and_old_snapshots_keep_legacy():
    settings = Settings(trip_short_stream_enabled=True)
    short = execution_config(settings, source_text="字" * 500)
    assert short.processing_mode == "short_stream" and short.max_calls <= 2
    assert execution_config(settings, source_text="字" * 501).processing_mode == "legacy"
    assert execution_config(settings).processing_mode == "legacy"
    old = short.model_dump(exclude={"processing_mode", "stream_protocol_version"})
    assert ExecutionConfig.model_validate(old).processing_mode == "legacy"


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_equal_counts_changed_content_persist_and_old_sequences_cannot_overwrite(kind):
    from tests.test_experience_v3_journey import repository_for, create
    from app.trip_understanding.demo import build_demo_pipeline, DEMO_SOURCE_TEXT
    now = datetime.now(timezone.utc)
    updates = []
    async def progress(update):
        updates.append(update)
    await build_demo_pipeline().run(DEMO_SOURCE_TEXT, progress_callback=progress)
    async with repository_for(kind) as repo:
        await create(repo, "stream-progress", now)
        job = await repo.claim_next(worker_id="stream-test", now=now, lease_seconds=30)
        first = updates[0].model_copy(update={"update_sequence": 1})
        second = first.model_copy(update={"update_sequence": 2, "snapshot": first.snapshot.model_copy(update={
            "days": [day.model_copy(update={"unprocessed_count": 1}) for day in first.snapshot.days]})})
        assert await repo.record_progress(job, first, now=now)
        assert await repo.record_progress(job, second, now=now)
        assert await repo.record_progress(job, first, now=now)
        assert await repo.record_progress(job, second, now=now)
        if kind == "memory":
            events = repo.events[job.understanding_id]
            snapshots = [e.payload.snapshot for e in events if e.payload.snapshot]
        else:
            rows = await repo._pool.fetch("SELECT public_payload_json FROM trip_understanding_events WHERE understanding_id=$1 ORDER BY event_id", job.understanding_id)
            snapshots = [json.loads(row['public_payload_json'])["snapshot"] for row in rows if json.loads(row['public_payload_json']).get('snapshot')]
            assert await repo._pool.fetchval("SELECT progress_sequence FROM trip_understanding_jobs WHERE job_id=$1", job.job_id) == 2
        assert len(snapshots) == 2
        last = snapshots[-1] if isinstance(snapshots[-1], dict) else snapshots[-1].model_dump()
        assert last["days"][0]["unprocessed_count"] == 1


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("alternatives_only", [False, True])
@pytest.mark.asyncio
async def test_stop_preserves_verified_identity_or_alternatives_and_rejects_late_update(kind, alternatives_only):
    from tests.test_experience_v3_journey import repository_for, create
    from app.trip_understanding.demo import build_demo_pipeline, DEMO_SOURCE_TEXT
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from app.trip_understanding.models import ActivityAlternativeView
    now = datetime.now(timezone.utc)
    updates = []
    async def progress(update):
        updates.append(update)
    await build_demo_pipeline().run(DEMO_SOURCE_TEXT, progress_callback=progress, progressive_places=True)
    update = updates[-1].model_copy(update={"update_sequence": 1})
    if alternatives_only:
        day = update.snapshot.days[0].model_copy(update={"activities": [], "meal_slots": [],
            "alternatives": [ActivityAlternativeView(name="备选园", category="景点")]})
        update = update.model_copy(update={"snapshot": update.snapshot.model_copy(update={"days": [day]})})
    async with repository_for(kind) as repo:
        created = await create(repo, "stop-stream", now)
        service = TripUnderstandingApplicationService(repo)
        resource = await service.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)
        job = await repo.claim_next(worker_id="stop-stream", now=now, lease_seconds=30)
        assert await repo.record_progress(job, update, now=now)
        stopped = await service.cancel_understanding(resource, idempotency_key="stop", now=now)
        assert stopped.cancelled.status == "STOPPED_WITH_DRAFT"
        resource = await service.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)
        stored = await repo.get_result(resource)
        assert stored and "EDIT_CARDS" in stored.result.available_actions
        assert stored.result.coverage and not stored.result.coverage.complete
        if alternatives_only:
            assert stored.result.days[0].alternatives[0].name == "备选园"
            assert not stored.result.days[0].activities
        else:
            before = update.snapshot.days[0].activities[0]
            after = stored.result.days[0].activities[0]
            assert after.status == "READY" and after.area_or_address == before.area_or_address
            assert not after.verification_pending
            assert after.activity_token == before.activity_token
            expected = update.internal_binding["preview_places"][before.activity_token]["canonical_place_id"]
            if kind == "postgres":
                place_id = await repo._pool.fetchval("SELECT canonical_place_id FROM trip_understanding_activities WHERE understanding_id=$1 AND revision=$2 AND public_activity_token=$3",
                    job.understanding_id, job.revision + 1, before.activity_token)
                assert place_id == expected
            else:
                assert repo.g03_pipeline_inputs[(job.understanding_id, job.revision + 1)]["bindings"][before.activity_token]["canonical_place_id"] == expected
        assert not await repo.record_progress(job, update.model_copy(update={"update_sequence": 2}), now=now)


@pytest.mark.asyncio
async def test_cancelled_projection_does_not_cancel_shared_query_or_mix_cities():
    from app.trip_understanding.streaming_pipeline import SharedPlaceQueries
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    class Places:
        async def resolve(self, **query):
            calls.append(query)
            started.set()
            await release.wait()
            return query["city"]
    shared = SharedPlaceQueries(Places(), 2)
    first = asyncio.create_task(shared.resolve(city="北京", atomic_place_name="公园", category_hint="景点"))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    successor = asyncio.create_task(shared.resolve(city="北京", atomic_place_name="公园", category_hint="景点"))
    changed = asyncio.create_task(shared.resolve(city="上海", atomic_place_name="公园", category_hint="景点"))
    release.set()
    assert await successor == "北京"
    assert await changed == "上海"
    assert len(calls) == 2
    await shared.close()


@pytest.mark.parametrize('venue_suffix', ['', '，看室内展览'])
def test_long_condition_and_explicit_default_reference_share_validated_relation(venue_suffix):
    from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
    from app.trip_understanding.conditional_replacement import is_replacement_reference
    from app.trip_understanding.source_inventory import inventory_covers, source_segments
    replacement = '如果下雨，就把这次青溪公园的安排替换成星河博物馆' + venue_suffix
    source = '北京。Day1：青溪公园。' + replacement + '。晴天仍按原来的公园计划走。'
    draft = SemanticDraft.model_validate(dict(destination='北京', activities=[
        dict(source_quote='青溪公园', place_name='青溪公园', role='PLANNED', day_index=1),
        dict(source_quote='星河博物馆', place_name='星河博物馆', role='OPTIONAL', day_index=1,
            conditional_replacement=dict(target_quote='青溪公园', condition_quote=replacement, evidence=replacement))]))
    plan = _proposal_from_live_draft(source, draft)
    repeated = source.index('青溪公园', source.index('如果'))
    assert is_replacement_reference(source, plan, repeated, repeated + 4)
    segments = source_segments(source)
    assert len(segments) == 4
    inventory = {'segments': [
        dict(segment_index=0, classification='CONTEXT', items=[]),
        dict(segment_index=1, classification='ARRANGEMENTS', items=[dict(quote='青溪公园', role='PLANNED', day_index=1)]),
        dict(segment_index=2, classification='ARRANGEMENTS', items=[dict(quote='青溪公园', role='REFERENCE', day_index=1,
            refers_to_occurrence=1), dict(quote='星河博物馆', role='OPTIONAL', day_index=1)]),
        dict(segment_index=3, classification='REFERENCES', reference_target_quote='青溪公园', items=[]),
    ]}
    assert inventory_covers(source, plan, inventory)
    from app.trip_understanding.source_inventory import bind_implicit_references
    inventory['segments'][-1] = dict(segment_index=3, classification='ARRANGEMENTS', items=[])
    bound = bind_implicit_references(source, plan, inventory)
    assert bound['segments'][-1]['classification'] == 'REFERENCES'
    assert bound['segments'][-1]['reference_target_quote'] == '青溪公园'
    assert inventory_covers(source, plan, bound)
    inventory['segments'][-1]['unresolved'] = True
    assert not inventory_covers(source, plan, bind_implicit_references(source, plan, inventory))


@pytest.mark.asyncio
async def test_internal_role_repair_only_rereads_conflicting_fragment():
    from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
    from app.trip_understanding.inline_source_details import apply_inline_source_details
    from app.trip_understanding.source_inventory import inventory_covers
    from app.trip_understanding.streaming_repair import repair_roles, role_conflicts

    fragment = '去青溪公园，在园内的迎春广场活动一下，不要求一定爬到山顶。'
    source = '北京。Day1：' + fragment + '接着到星河博物馆。'
    value = dict(activities=[dict(source_quote='青溪公园', place_name='青溪公园', role='PLANNED', day_index=1,
        source_details=[dict(kind='VISIT', source_quote='迎春广场', optional=True, evidence=fragment)]),
        dict(source_quote='星河博物馆', place_name='星河博物馆', role='PLANNED', day_index=1)],
        source_inventory=dict(segments=[
            dict(segment_index=0, classification='CONTEXT', items=[]),
            dict(segment_index=1, classification='ARRANGEMENTS', items=[
                dict(quote='青溪公园', role='PLANNED', day_index=1),
                dict(quote='迎春广场', role='PLANNED', day_index=1, kind='INTERNAL', parent_quote='青溪公园')]),
            dict(segment_index=2, classification='ARRANGEMENTS', items=[dict(quote='星河博物馆', role='PLANNED', day_index=1)])]))
    def project(data):
        draft = SemanticDraft.model_validate({'activities': data['activities']})
        return apply_inline_source_details(source, draft, _proposal_from_live_draft(source, draft))
    plan = project(value)
    assert not inventory_covers(source, plan, value['source_inventory'])
    assert len(role_conflicts(source, value, plan)) == 1
    calls = []
    async def complete(client, **kwargs):
        calls.append(kwargs)
        return NS(choices=[NS(finish_reason='stop', message=NS(content='{"repairs":[{"problem_index":0,"optional":false}]}'))])
    provider = NS(client=None, adapter=NS(config=NS(max_calls=2), complete=complete))
    corrected = await repair_roles(provider, source, value, plan)
    assert len(calls) == 1
    assert '星河博物馆' not in calls[0]['messages'][1]['content']
    assert value['activities'][0]['source_details'][0]['optional'] is True
    assert corrected['activities'][1] == value['activities'][1]
    assert inventory_covers(source, project(corrected), corrected['source_inventory'])
    assert role_conflicts(source, corrected, project(corrected)) == []


@pytest.mark.parametrize('kind', ['memory', 'postgres'])
@pytest.mark.asyncio
async def test_interrupted_worker_recovers_saved_preview_without_inference_and_old_lease_cannot_write(kind):
    from datetime import timedelta
    from tests.test_experience_v3_journey import repository_for, create
    from app.trip_understanding.demo import build_demo_pipeline, DEMO_SOURCE_TEXT
    from app.trip_understanding.errors import JobLeaseLostError
    updates = []
    async def progress(update):
        updates.append(update)
    await build_demo_pipeline().run(DEMO_SOURCE_TEXT, progress_callback=progress, progressive_places=True)
    now = datetime.now(timezone.utc)
    async with repository_for(kind) as repo:
        created = await create(repo, 'interrupted', now)
        original = await repo.claim_next(worker_id='old-worker', now=now, lease_seconds=2)
        assert await repo.record_progress(original, updates[-1].model_copy(update={'update_sequence': 1}), now=now)
        recovered_at = now + timedelta(seconds=3)
        successor = await repo.claim_next(worker_id='new-worker', now=recovered_at, lease_seconds=30)
        with pytest.raises(JobLeaseLostError):
            await repo.recover_interrupted_progress(original, now=recovered_at)
        assert await repo.recover_interrupted_progress(successor, now=recovered_at)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash='a' * 64, now=recovered_at)
        stored = await repo.get_result(resource)
        assert resource.state == 'PARTIAL'
        assert stored.result.days[0].activities[0].status == 'READY'
        assert stored.result.days[0].activities[0].activity_token == updates[-1].snapshot.days[0].activities[0].activity_token
        assert 'EDIT_CARDS' in stored.result.available_actions
        assert not await repo.record_progress(original, updates[-1].model_copy(update={'update_sequence': 2}), now=recovered_at)


def test_internal_day_can_only_follow_its_source_anchored_parent():
    from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
    from app.trip_understanding.inline_source_details import apply_inline_source_details
    from app.trip_understanding.source_inventory import inventory_covers
    fragment = '去青溪公园，在园内的迎春广场活动。'
    source = '北京。Day1：' + fragment
    draft = SemanticDraft.model_validate(dict(activities=[dict(source_quote='青溪公园',
        place_name='青溪公园', role='PLANNED', day_index=1, source_details=[
            dict(kind='VISIT', source_quote='迎春广场', optional=False, evidence=fragment)])]))
    plan = apply_inline_source_details(source, draft, _proposal_from_live_draft(source, draft))
    child = dict(quote='迎春广场', role='PLANNED', kind='INTERNAL', parent_quote='青溪公园')
    inventory = dict(segments=[dict(segment_index=0, classification='CONTEXT', items=[]),
        dict(segment_index=1, classification='ARRANGEMENTS', items=[
            dict(quote='青溪公园', role='PLANNED', day_index=1), child])])
    assert inventory_covers(source, plan, inventory)
    child['day_index'] = 2
    assert not inventory_covers(source, plan, inventory)
    child.pop('day_index')
    child['parent_quote'] = '不存在的公园'
    assert not inventory_covers(source, plan, inventory)


@pytest.mark.asyncio
async def test_cancelled_visit_without_day_is_visible_in_result_and_share():
    from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
    from app.trip_understanding.memory_share import build_share_projection
    source = '北京。Day1：故宫博物院。取消北海公园。'
    draft = SemanticDraft.model_validate(dict(destination='北京', activities=[
        dict(source_quote='故宫博物院', place_name='故宫博物院', role='PLANNED', day_index=1),
        dict(source_quote='北海公园', place_name='北海公园', role='EXCLUDED')]))
    plan = _proposal_from_live_draft(source, draft)
    pipeline = TripUnderstandingPipeline(NS(), Resolver(), relative_only=True)
    result = await pipeline.run(source, prepared_plan=plan)
    assert [c.name for d in result.public_result.days for c in d.activities] == ['故宫博物院']
    notes = [n.text for d in result.public_result.days for n in d.source_notes]
    assert any('北海公园' in n and '未指定 Day' in n for n in notes)
    assert [n for d in build_share_projection(result.public_result).days for n in d.notes] == notes
