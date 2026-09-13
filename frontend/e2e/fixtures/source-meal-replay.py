"""Fixed external providers; actual source context, signed candidates and commands.

No server, user account, database, model or map request is used by this helper.
"""
import asyncio
import json
import sys
from datetime import datetime, timezone

from pydantic import TypeAdapter

from app.trip_understanding.candidates import issue_candidate, CandidatePlace, GCJ02Position
from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.dining import SourceMealSearchRequest, DiningCandidateView, source_meal_context, source_meal_binding, verify_command_candidate
from app.trip_understanding.models import UserFacingTripResult, TripUnderstandingCommand, ActivityTextEditCommand
from tests.test_source_meal_selection import build_source_meal_result, fixed_plan, place


def run(data):
    if data['operation'] == 'initial':
        result = asyncio.run(build_source_meal_result()).public_result
        if data.get('pending'):
            card = result.days[0].activities[-1]
            result = apply_public_command(result, ActivityTextEditCommand(command_type='ACTIVITY_TEXT_EDIT', activity_token=card.activity_token, name=card.name)).result
        if data.get('no_anchor'):
            result.days[0].meal_slots[1].after_activity_token = None
            result.days[0].meal_slots[1].before_activity_token = None
        return result.model_dump(mode='json')
    result = UserFacingTripResult.model_validate(data['result'])
    plan = fixed_plan(result)
    now = datetime.now(timezone.utc)
    if data['operation'] == 'search':
        body = SourceMealSearchRequest.model_validate(data['body'])
        view = source_meal_context(result, plan, body.meal_slot, body.position)
        if view.status == 'AVAILABLE':
            candidate = issue_candidate(place(), public_resource_id=data['resource'], expected_etag=data['etag'], now=now,
                activity_token=source_meal_binding(view.after_activity_token, before=view.insert_before, meal_slot=body.meal_slot, meal_role=view.meal_role))
            view.candidates = [DiningCandidateView(**candidate.model_dump(), reason='固定供应商响应；按手动词对应标签，未核对动态菜品供应。')]
        return view.model_dump(mode='json')
    if data['operation'] == 'confirm-search':
        card = next(card for day in result.days for card in day.activities if card.activity_token == data['body']['activity_token'])
        candidate = CandidatePlace(canonical_place_id='amap:fixed-jingshan', name=card.name, city=card.city,
            category=card.category, area_or_address='固定地点地址', position=GCJ02Position(longitude=116.398, latitude=39.918))
        return {'status': 'AVAILABLE', 'candidates': [issue_candidate(candidate, public_resource_id=data['resource'],
            activity_token=card.activity_token, expected_etag=data['etag'], now=now).model_dump(mode='json')]}
    command = TypeAdapter(TripUnderstandingCommand).validate_python(data['command'])
    kwargs = {'dining_plan': plan}
    if command.command_type in ('DINING_INSERT', 'PLACE_CONFIRM'):
        kwargs['confirmed_place'] = verify_command_candidate(command, public_resource_id=data['resource'], expected_etag=data['etag'], now=now)
    for key in ('undo', 'redo'):
        if data.get(key):
            kwargs[key + '_result'] = UserFacingTripResult.model_validate(data[key])
    changed = apply_public_command(result, command, **kwargs).result
    return UserFacingTripResult.model_validate_json(changed.model_dump_json()).model_dump(mode='json')


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin)), ensure_ascii=False))
