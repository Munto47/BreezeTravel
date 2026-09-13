"""Old records remain readable; the active HTTP entry rejects new clock edits."""
import asyncio

import pytest

from app.trip_understanding.worker import TripUnderstandingWorker
from tests.test_trip_understanding_v3_api import _client


@pytest.mark.parametrize('payload', [
    {'command_type': 'ACTIVITY_TIME_SET', 'start_time': '09:00'},
    {'command_type': 'ACTIVITY_TIMES_SHIFT', 'minutes': 30},
    {'command_type': 'ACTIVITY_TIMES_APPLY'},
    {'command_type': 'ACTIVITY_TEXT_EDIT', 'time_hint': '09:00'},
    {'command_type': 'ACTIVITY_INSERT', 'name': '新地点', 'day_index': 1, 'position': 1, 'visit_duration_minutes': 60},
    {'command_type': 'ASSUMPTION_SET', 'key': 'calendar', 'value': '2026-09-13'},
])
def test_clock_edit_is_rejected_without_losing_or_changing_saved_trip(payload):
    client, repository, _app = _client()
    created = client.post('/api/v3/trip-understandings', json={'mode': 'DEMO'}, headers={'Idempotency-Key': 'relative-api'})
    assert created.status_code == 202
    asyncio.run(TripUnderstandingWorker(repository).run_once('fixed-demo'))
    before = client.get(created.json()['result_url'])
    token = before.json()['days'][0]['activities'][0]['activity_token']
    payload = dict(payload)
    if payload['command_type'] == 'ACTIVITY_TIMES_SHIFT':
        payload['activity_tokens'] = [token]
    elif payload['command_type'] == 'ACTIVITY_TIMES_APPLY':
        payload['changes'] = [{'activity_token': token, 'start_time': '09:30'}]
    elif payload['command_type'] in {'ACTIVITY_TIME_SET', 'ACTIVITY_TEXT_EDIT'}:
        payload['activity_token'] = token
    endpoint = f"/api/v3/trip-understandings/{created.json()['public_resource_id']}/commands"
    headers = {'If-Match': before.headers['etag'], 'Idempotency-Key': 'attempt-time'}
    rejected = client.post(endpoint, json=payload, headers=headers)
    assert rejected.status_code == 422
    assert rejected.json()['detail']['code'] == 'TIMING_EDIT_UNSUPPORTED'
    after = client.get(created.json()['result_url'])
    assert after.headers['etag'] == before.headers['etag']
    assert after.json() == before.json()
    # The same current record is still editable in relative order.
    moved = client.post(endpoint, headers={**headers, 'Idempotency-Key': 'relative-move'}, json={
        'command_type': 'ACTIVITY_MOVE', 'activity_token': token, 'target_day_index': 2, 'target_position': 0})
    assert moved.status_code == 200
    latest = client.get(created.json()['result_url']).json()
    expected = [[card['name'] for card in day['activities']] for day in before.json()['days']]
    moved_name = expected[0].pop(0)
    expected[1].insert(0, moved_name)
    # Public activity tokens rotate with revisions; compare the complete order.
    assert [[card['name'] for card in day['activities']] for day in latest['days']] == expected
