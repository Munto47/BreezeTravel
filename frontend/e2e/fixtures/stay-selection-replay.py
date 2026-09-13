"""Portable fixed identity/route fixture; real in-memory select and undo commands."""
import asyncio
import json
from datetime import datetime, timezone

from app.trip_understanding.models import ResolvedPlace, UndoCommand
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.stay import StayRecommendationEngine, ControlledStayRouteProvider
from tests.test_experience_text_fidelity import DraftProvider, activity
from tests.test_experience_v3_journey import repository_for
from tests.test_g02_map_stay import _test_registry
from tests.test_stay_overnight_segments import CityHotels


async def build_stay_selection_states():
    names = ['故宫博物院', '景山公园', '天坛公园', '前门大街']
    source = '北京两日行程。\nDay1：故宫博物院、景山公园。\nDay2：天坛公园、前门大街。'
    class FixedPlaces:
        async def resolve(self, *, city, atomic_place_name, category_hint=None):
            assert city == '北京' and atomic_place_name in names
            index = names.index(atomic_place_name)
            return ResolvedPlace(canonical_place_id=f'fixed-place-{index}', name=atomic_place_name,
                category='景点', area_or_address=f'固定演示地址{index}', provider_binding={
                    'provider':'CONTROLLED_FIXTURE','external_calls':0,'city':city,
                    'coordinates':{'longitude':116.39+index*.001,'latitude':39.91-index*.001}})
    output = await TripUnderstandingPipeline(DraftProvider([
        activity(name,1 if index<2 else 2) for index,name in enumerate(names)
    ]), FixedPlaces()).run(source)
    async with repository_for('memory') as repo:
        now = datetime.now(timezone.utc)
        created = await repo.create_demo(capability_hash='a'*64,source_text=source,
            idempotency_key='fixed-nightly-stay',request_hash=canonical_sha256({'source':source}),now=now,ttl_hours=24)
        job = await repo.claim_next(worker_id='fixed-trip',now=now,lease_seconds=60)
        await repo.complete_job(job,output,now=now)
        async def current():
            resource = await repo.authorize(created.accepted.public_resource_id,capability_hash='a'*64,now=now)
            return resource,await repo.get_result(resource)
        resource, _ = await current()
        job = await repo.claim_next_stay(worker_id='fixed-hotel',now=now,lease_seconds=60)
        plan = await repo.load_stay_plan(job)
        hotels = await StayRecommendationEngine(CityHotels(),ControlledStayRouteProvider(),
            brand_registry=_test_registry()).recommend(plan,observed_at=now)
        await repo.complete_stay_job(job,hotels,now=now)
        resource, before = await current()
        chosen = before.result.stay.segments[0].candidates[0]
        service = TripUnderstandingApplicationService(repo)
        await service.select_stay(resource,candidate_token=chosen.candidate_token,
            expected_etag=before.opaque_etag,idempotency_key='fixed-select',now=now)
        resource, selected = await current()
        await service.apply_command(resource,UndoCommand(command_type='UNDO'),expected_etag=selected.opaque_etag,
            idempotency_key='fixed-undo',now=now)
        _, undone = await current()
        return {'before':before.result.model_dump(mode='json'),'selected':selected.result.model_dump(mode='json'),
            'undo':undone.result.model_dump(mode='json')}


if __name__ == '__main__':
    print(json.dumps(asyncio.run(build_stay_selection_states()),ensure_ascii=False))
