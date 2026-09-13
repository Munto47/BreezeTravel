"""Fixed user-added visits and HTTP routes; actual comparator and public commands.

This validates UI comparison/adoption mechanics, not live route quality or source extraction.
"""
import asyncio
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime

from pydantic import TypeAdapter

from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.demo import build_demo_pipeline, DEMO_SOURCE_TEXT
from app.trip_understanding.models import UserFacingTripResult, TripUnderstandingCommand
from app.trip_understanding.relative_route_previews import PublicRelativeRouteOptions, issue_route_preview, verify_route_preview
from tests.test_relative_route_options import compare, visits, FixedAmap


def items_for(result):
    cards = result.days[0].activities
    return tuple(replace(visit, source_kind='USER_ADDED', stop=visit.stop.model_copy(update={
        'activity_token': cards[i].activity_token, 'name': cards[i].name,
    })) for i, visit in enumerate(visits(len(cards))))


async def run(data):
    if data['operation'] == 'initial':
        result = (await build_demo_pipeline().run(DEMO_SOURCE_TEXT)).public_result
        prototype = result.days[0].activities[0]
        result.days[0].activities = [prototype.model_copy(update={'name': f'固定景点{i}', 'activity_token': f'fixed-route-card-{i:024d}', 'source_details': [], 'city': '北京'}) for i in range(4)]
        result.days[0].meal_slots = []
        return result.model_dump(mode='json')
    result = UserFacingTripResult.model_validate(data['result'])
    now = datetime.now(UTC)
    if data['operation'] == 'compare':
        items = items_for(result)
        comparison, _ = await compare(items, fixture=FixedAmap(minutes={(2, 1, 'walking'): 45, (2, 1, 'transit'): 14}))
        return PublicRelativeRouteOptions(status='AVAILABLE', message='固定路线响应得到可比较方案；尚未修改行程。', day_index=1,
            options=[issue_route_preview(option, items, public_resource_id=data['resource'], expected_etag=data['etag'], now=now) for option in comparison.options]).model_dump(mode='json')
    if data['operation'] == 'adopt':
        verified = verify_route_preview(data['token'], public_resource_id=data['resource'], expected_etag=data['etag'], now=now, current_result=result)
        command = verified.command
    else:
        command = TypeAdapter(TripUnderstandingCommand).validate_python(data['command'])
    kwargs = {}
    for name in ('undo', 'redo'):
        if data.get(name):
            kwargs[name + '_result'] = UserFacingTripResult.model_validate(data[name])
    changed = apply_public_command(result, command, **kwargs).result
    return UserFacingTripResult.model_validate_json(changed.model_dump_json()).model_dump(mode='json')


if __name__ == '__main__':
    print(json.dumps(asyncio.run(run(json.load(sys.stdin))), ensure_ascii=False))
