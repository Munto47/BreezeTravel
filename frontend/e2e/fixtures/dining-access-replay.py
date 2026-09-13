"""Fixed model/identities, saved or independent supplier rows, real validators.

The saved six rows are not new service calls. Parent identities in this page
fixture are fixed; this is a product contract replay, not live admission proof.
"""
import asyncio
from datetime import datetime, timezone
import json
import sys
from types import SimpleNamespace

from pydantic import TypeAdapter

from app.trip_understanding.candidates import issue_candidate, verify_candidate
from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.daily_dining import DailyDiningView, meal_context, project_daily_meals
from app.trip_understanding.demo import FixedBeijingPlaceResolver
from app.trip_understanding.dining import (
    DiningCandidateView, SourceMealSearchRequest, bind_dining_access, dining_binding,
    select_dining_rows, source_meal_binding, source_meal_context, verify_command_candidate,
)
from app.trip_understanding.models import TripUnderstandingCommand, UserFacingTripResult
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_dining_recommendations import anchor, row
from tests.test_dining_access_conditions import (
    SAVED, SavedParentResolver, build_dining_access_result, plan_for as saved_plan,
)
from tests.test_semantic_supplement_budget import Client, provider

def definitions(family):
    places = dict(SavedParentResolver._PLACES)
    if family == 'independent':
        places['青禾商场'] = ('BTESTQINGHE01', '商场', '固定商场身份', 116.397029, 39.917839)
        rows = [{**row(), 'id': f'independent-dining-{index}', 'name': name,
            'parent': parent, 'business': {'tag': tags}}
            for index, (name, parent, tags) in enumerate([
                ('青禾商场云台传统家常菜与手工面食餐厅', 'BTESTQINGHE01', '面条,牛肉'),
                ('陌上茶点铺', 'BTESTQINGHE01', '下午茶,点心'),
                ('南岸餐厅', 'BTESTOTHER01', '米饭,烤鸭'),
            ])]
        rows.append({**row(), 'id': 'independent-snack', 'name': '槐香茶点铺',
            'parent': 'B000A81CB2', 'location': '116.410829,39.881913', 'business': {'tag': '茶饮,点心'}})
        return places, rows, '青禾商场'
    return places, SAVED['rows'], '故宫博物院'


async def initial(family):
    if family == 'saved':
        return (await build_dining_access_result()).public_result
    places, _, parent = definitions(family)
    class FixedIdentities(FixedBeijingPlaceResolver):
        _PLACES = places
    specs = [(1, parent, None), (1, '午餐：面条', 'LUNCH'), (1, '景山公园', None),
        (2, '天坛公园', None), (2, '下午茶：茶点', 'SNACK'), (2, '前门大街', None)]
    source = f'北京。\nDay1：{parent}。午餐：面条。景山公园。\nDay2：天坛公园。下午茶：茶点。前门大街。'
    raw = {'destination': '北京', 'day_labels': ['Day1', 'Day2'], 'unprocessed_quotes': [], 'activities': [
        {'source_quote': quote, 'place_name': None if meal else quote, 'role': 'PLANNED',
            'day_index': day, 'category': '餐饮' if meal else '景点', 'meal_role': meal} for day, quote, meal in specs]}
    return (await TripUnderstandingPipeline(provider(Client(raw)), FixedIdentities()).run(source)).public_result


def plan_for(result, family):
    if family == 'saved':
        return saved_plan(result)
    places, rows, _ = definitions(family)
    for item in rows:
        longitude, latitude = map(float, item['location'].split(','))
        places[item['name']] = (f"amap:{item['id']}", '餐饮', item.get('address', ''), longitude, latitude)
    return SimpleNamespace(stops=[anchor().model_copy(update={
        'activity_token': card.activity_token, 'day_index': i+1, 'day_label': day.label,
        'sequence_index': index, 'name': card.name, 'canonical_place_id': places[card.name][0],
        'category': card.category, 'longitude': places[card.name][3], 'latitude': places[card.name][4],
    }) for i, day in enumerate(result.days) for index, card in enumerate(day.activities) if card.status == 'READY'])


def candidates(plan, rows, token, before=False):
    current = next(stop for stop in plan.stops if stop.activity_token == token)
    return [bind_dining_access(place, stops=plan.stops, activity_token=token, before=before)
        for place in select_dining_rows(rows, anchor=current, excluded_ids=set(), meal_only=True)]


def run(data):
    family = data.get('family', 'saved')
    if data['operation'] == 'initial':
        return asyncio.run(initial(family)).model_dump(mode='json')
    result = UserFacingTripResult.model_validate(data['result'])
    plan = plan_for(result, family)
    _, rows, _ = definitions(family)
    now = datetime.now(timezone.utc)
    resource, etag = data['resource'], data['etag']
    if data['operation'] == 'source':
        request = SourceMealSearchRequest.model_validate(data['body'])
        view = source_meal_context(result, plan, request.meal_slot, request.position)
        if view.status == 'AVAILABLE':
            found = candidates(plan, rows, view.after_activity_token, view.insert_before)
            view.candidates = [DiningCandidateView(**issue_candidate(place, public_resource_id=resource,
                expected_etag=etag, now=now, activity_token=source_meal_binding(view.after_activity_token,
                    before=view.insert_before, meal_slot=request.meal_slot, meal_role=view.meal_role)).model_dump(),
                reason='固定供应商地点资料，仅核对访问关系，不证明门区、营业或正餐供应。') for place in found]
        return view.model_dump(mode='json')
    if data['operation'] == 'daily':
        payload = []
        for index, day in enumerate(result.days, 1):
            view, current, _ = meal_context(day, [stop for stop in plan.stops if stop.day_index == index])
            view['day_index'] = index
            if current:
                found = candidates(plan, rows, view['after_activity_token'], view.get('insert_before', False))
                view.update(status='AVAILABLE', message='固定餐饮候选，采纳后才进入行程。', candidates=[
                    {'place': place.model_dump(mode='json'), 'reason': '固定路线未调用'} for place in found])
            payload.append(view)
        return DailyDiningView(status='AVAILABLE', message='固定候选，不含新的外部调用',
            days=project_daily_meals(payload, public_resource_id=resource, etag=etag, now=now)).model_dump(mode='json')
    if data['operation'] == 'nearby':
        token = data['body']['activity_token']
        return {'status': 'AVAILABLE', 'message': '固定附近门店，尚未指定餐别。', 'candidates': [
            {**issue_candidate(place, public_resource_id=resource, expected_etag=etag, now=now,
                activity_token=dining_binding(token)).model_dump(mode='json'), 'reason': '固定供应商资料'}
            for place in candidates(plan, rows, token)]}
    if data['operation'] == 'place':
        token = data['body']['activity_token']
        return {'status': 'AVAILABLE', 'candidates': [
            issue_candidate(place, public_resource_id=resource, expected_etag=etag, now=now,
                activity_token=token).model_dump(mode='json')
            for place in candidates(plan, rows, token, before=True)]}
    command = TypeAdapter(TripUnderstandingCommand).validate_python(data['command'])
    kwargs = {'dining_plan': plan}
    if command.command_type == 'DINING_INSERT':
        kwargs['confirmed_place'] = verify_command_candidate(command, public_resource_id=resource, expected_etag=etag, now=now)
    if command.command_type == 'PLACE_CONFIRM':
        kwargs['confirmed_place'] = verify_candidate(command.candidate_token, public_resource_id=resource,
            activity_token=command.activity_token, expected_etag=etag, now=now)
        kwargs['current_place_id'] = next(stop.canonical_place_id for stop in plan.stops
            if stop.activity_token == command.activity_token)
    for key in ('undo', 'redo'):
        if data.get(key):
            kwargs[key + '_result'] = UserFacingTripResult.model_validate(data[key])
    changed = apply_public_command(result, command, **kwargs).result
    return UserFacingTripResult.model_validate_json(changed.model_dump_json()).model_dump(mode='json')


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin)), ensure_ascii=False))
