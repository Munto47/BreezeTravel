"""Confirming an unresolved parent must not erase its already understood visits."""
import pytest
from datetime import datetime, timezone

from app.trip_understanding.candidates import CandidatePlace, GCJ02Position
from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.models import PlaceConfirmCommand
from tests.test_source_details_readback import build_source_details_result, cards, details, DETAILS


@pytest.mark.asyncio
@pytest.mark.parametrize('source_city, selected_name, old_id, selected_id, retained', [
    (None, None, None, 'new-identity', True),
    ('北京', None, None, 'new-identity', True),
    ('上海', None, None, 'new-identity', False),
    (None, '北海公园', None, 'new-identity', False),
    (None, None, 'previous-identity', 'different-identity', False),
    (None, None, 'previous-identity', 'previous-identity', True),
])
async def test_first_identity_confirmation_retains_source_visits_but_replacement_does_not(
        source_city, selected_name, old_id, selected_id, retained):
    original = (await build_source_details_result()).public_result
    parent = cards(original)[0]
    parent.city = source_city
    parent.status = 'NEEDS_CONFIRMATION'
    selected = CandidatePlace(canonical_place_id=selected_id, name=selected_name or parent.name,
        city='北京', category='景点', area_or_address='固定测试地点',
        position=GCJ02Position(longitude=116.4, latitude=39.9))
    result = apply_public_command(original, PlaceConfirmCommand(command_type='PLACE_CONFIRM',
        activity_token=parent.activity_token, candidate_token='fixed-candidate-validated-at-repository-boundary'),
        confirmed_place=selected, current_place_id=old_id).result
    changed = cards(result)[0]
    assert changed.status == 'READY' and changed.city == '北京'
    assert details(changed) == (DETAILS if retained else [])
    assert details(parent) == DETAILS
    assert [[card.name for card in day.activities[1:]] for day in result.days] == [
        [card.name for card in day.activities[1:]] for day in original.days]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['memory', 'postgres'])
async def test_unknown_city_parent_confirmation_survives_persistence_and_undo(kind):
    from app.trip_understanding.candidates import issue_candidate
    from app.trip_understanding.models import CreateFullRequest, UndoCommand
    from app.trip_understanding.pipeline import TripUnderstandingPipeline
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from app.trip_understanding.worker import TripUnderstandingWorker
    from tests.test_experience_v3_journey import repository_for
    from tests.test_source_details_readback import SOURCE, raw_response, CapturedClient, provider, RecordingPlaces

    source = SOURCE.removeprefix('北京一日游。\n')
    raw = raw_response()
    raw['destination'] = '目的地待确认'
    for row in raw['activities']:
        row['city'] = row['city_evidence'] = None
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repo)
        created = await service.create_full(CreateFullRequest.model_validate({
            'mode': 'FULL', 'source': {'type': 'TEXT', 'text': source}}),
            owner_user_id='experience-owner', idempotency_key='unknown-city-parent', now=now)
        worker = TripUnderstandingWorker(repo, full_pipeline=TripUnderstandingPipeline(
            provider(CapturedClient(raw)), RecordingPlaces()))
        assert await worker.run_once('fixed-details-worker', now=now)
        reference = created.accepted.public_resource_id
        resource = await repo.authorize(reference, capability_hash=None, user_id='experience-owner', now=now)
        stored = await repo.get_result(resource)
        parent = stored.result.days[0].activities[0]
        assert parent.city is None and parent.status == 'NEEDS_CONFIRMATION'
        assert details(parent) == DETAILS
        candidate = issue_candidate(CandidatePlace(canonical_place_id='fixed-palace-id', name=parent.name,
            city='北京', category='景点', area_or_address='固定地点地址',
            position=GCJ02Position(longitude=116.4, latitude=39.9)),
            public_resource_id=reference, activity_token=parent.activity_token,
            expected_etag=stored.opaque_etag, now=now)
        await service.apply_command(resource, PlaceConfirmCommand(command_type='PLACE_CONFIRM',
            activity_token=parent.activity_token, candidate_token=candidate.candidate_token),
            expected_etag=stored.opaque_etag, idempotency_key='confirm-city', now=now)
        updated_resource = await repo.authorize(reference, capability_hash=None, user_id='experience-owner', now=now)
        updated = await repo.get_result(updated_resource)
        assert details(updated.result.days[0].activities[0]) == DETAILS
        assert updated.result.days[0].activities[0].status == 'READY'
        assert updated.result.days[0].activities[0].city == '北京'
        await service.apply_command(updated_resource, UndoCommand(command_type='UNDO'),
            expected_etag=updated.opaque_etag, idempotency_key='undo-confirmation', now=now)
        undone_resource = await repo.authorize(reference, capability_hash=None, user_id='experience-owner', now=now)
        undone = await repo.get_result(undone_resource)
        assert details(undone.result.days[0].activities[0]) == DETAILS
        assert undone.result.days[0].activities[0].status == 'NEEDS_CONFIRMATION'
        assert undone.result.days[0].activities[0].city is None
