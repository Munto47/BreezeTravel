"""Saved supplier failure and explicit connected-segment controls; zero network."""
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest

from app.trip_understanding.amap_route import AmapRouteProvider
from app.trip_understanding.daily_dining import build_daily_meals, project_daily_meals
from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.map_render import (
    ROUTE_CONFIG_SHA256, InternalRouteModeFact, MapRenderPlan, MapRenderer,
    MapStop, PlanRevisionRef, RouteGeometryPoint, mode_public_view,
)
from app.trip_understanding.map_repository import InMemoryMapRenderRepositoryMixin, _mode_view_from_row
from app.trip_understanding.relative_route_previews import (
    PublicRelativeRouteOptions, ROUTE_PREVIEW_PREFIX, _cipher,
    issue_route_preview, verify_route_preview,
)
from app.trip_understanding.route_connection import compared_path_scope, connection_evidence, route_segment
from app.trip_understanding.stay import assess_stay_commute, load_stay_commute_assessment
from app.trip_understanding.stay_repository import _candidate_view
from tests.test_daily_dining import card, day_context
from tests.test_dining_recommendations import restaurant
from tests.test_relative_route_options import FixedAmap, compare, current_edges, visits

SAVED = json.loads((Path(__file__).parent / "fixtures/live_poi_route_access_points.json").read_text(encoding="utf-8"))


def stop(coords, index=0):
    return MapStop(activity_token=f"point-{index}", day_index=1, day_label="Day 1", sequence_index=index,
        name=f"固定地点{index}", canonical_place_id=f"poi-{index}", city="北京", resolution_status="AUTO_MATCHED",
        longitude=coords[0], latitude=coords[1])


def fact(a, b, *, returned=None, minutes=10):
    geometry = [RouteGeometryPoint(longitude=p[0], latitude=p[1]) for p in (returned or [(a.longitude,a.latitude),(b.longitude,b.latitude)])]
    now = datetime.now(UTC)
    return InternalRouteModeFact(mode="walking", status="AVAILABLE", duration_minutes=minutes, distance_meters=500,
        geometry=geometry, response_hash="a"*64, request_hash="b"*64,
        provider_binding={"route_connection": connection_evidence(a,b,geometry)}, external_call_count=0,
        observed_at=now, expires_at=now+timedelta(hours=1))


@pytest.mark.asyncio
async def test_saved_poi_access_route_keeps_137m_and_line_but_public_is_limited():
    row = SAVED["routes"][0]
    a = stop(tuple(map(float,row["query"]["origin"].split(","))))
    b = stop(tuple(map(float,row["query"]["destination"].split(","))),1)
    a.canonical_place_id, b.canonical_place_id = row["query"]["origin_id"], row["query"]["destination_id"]
    seen = []
    def respond(request):
        seen.append(request.url.path)
        if request.url.path.endswith("walking"):
            query = dict(request.url.params)
            query.pop("key")
            assert query == row["query"]  # Preserve POI IDs; no request workaround.
            return httpx.Response(200,json=row["response"])
        return httpx.Response(200,json={"status":"1","route":{"transits":[]}})
    plan = MapRenderPlan(understanding_id="fixed", plan_ref=PlanRevisionRef(kind="UNDERSTANDING",aggregate_id="fixed",revision=1,stop_set_hash="a"*64),route_config_hash=ROUTE_CONFIG_SHA256,stops=[a,b])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        output = await MapRenderer(AmapRouteProvider(api_key="fixed",client=client)).render(plan)
    result = InMemoryMapRenderRepositoryMixin()._memory_snapshot_view(output)
    route = result.days[0].routes[0]
    assert len(seen) == 2 and output.status == "READY" and result.status == "LIMITED"
    assert output.failure['unverified_connection_count'] == 1
    assert route.walking.status == "AVAILABLE" and route.walking.distance_meters == 137 and route.walking.duration_minutes == 2
    assert route.walking.connection_status == "UNVERIFIED" and len(route.walking.geometry) == 14
    assert route.walking.geometry_break_indices == [4,10]
    assert route.walking.geometry[0].longitude == 116.396914
    assert "衔接未核实" in route.message
    assert "provider_binding" not in result.model_dump_json() and "requested_origin" not in result.model_dump_json()


@pytest.mark.parametrize("offset,expected", [(0,"VERIFIED"),(.0000004,"VERIFIED"),(.000002,"UNVERIFIED"),(.001,"UNVERIFIED")])
def test_connection_uses_wire_coordinate_precision_not_a_meter_radius(offset,expected):
    a,b = stop((116.3,39.9)),stop((116.31,39.91),1)
    value = fact(a,b,returned=[(116.3+offset,39.9),(116.31,39.91)])
    assert mode_public_view(value).connection_status == expected
    assert value.duration_minutes == 10 and value.distance_meters == 500


@pytest.mark.parametrize("partial,break_at,expected", [
    (False,None,"REQUESTED_POINTS"),(True,None,"RETURNED_SEGMENTS"),
    (True,"origin",None),(True,"destination",None),(True,"middle",None),(False,"missing",None),
])
def test_real_returned_endpoints_define_whether_a_detour_is_comparable(partial,break_at,expected):
    a,m,b = stop((116.3,39.9)),stop((116.31,39.9),1),stop((116.32,39.9),2)
    origin = (116.301,39.9) if partial else (a.longitude,a.latitude)
    destination = (116.321,39.9) if partial else (b.longitude,b.latitude)
    middle = (m.longitude,m.latitude)
    base = fact(a,b,returned=[origin,destination])
    left = fact(a,m,returned=[(116.302,39.9) if break_at == "origin" else origin,middle])
    right = fact(m,b,returned=[(116.312,39.9) if break_at == "middle" else middle,(116.322,39.9) if break_at == "destination" else destination])
    if break_at == "missing":
        left = left.model_copy(update={"provider_binding":{}})
    assert compared_path_scope([base],[left,right]) == expected
    assert route_segment(base,stop((116.9,39.9)),b) is None


@pytest.mark.asyncio
async def test_actual_saved_dining_routes_cannot_subtract_different_poi_access_points():
    values = {}
    for row in SAVED["routes"]:
        q = row["query"]
        a,b = stop(tuple(map(float,q["origin"].split(",")))),stop(tuple(map(float,q["destination"].split(","))),1)
        a.canonical_place_id,b.canonical_place_id=q["origin_id"],q["destination_id"]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,json=row["response"]))) as client:
            values[(q["origin_id"],q["destination_id"])] = await AmapRouteProvider(api_key="fixed",client=client).route(a,b,"walking",observed_at=datetime.now(UTC))
    base=values[("B000A8UIN8","B000A7I1OL")]
    for (origin,mid),left in values.items():
        if origin == "B000A8UIN8" and mid != "B000A7I1OL":
            right=values[(mid,"B000A7I1OL")]
            assert compared_path_scope([base],[left,right]) is None
            assert left.geometry[0] != base.geometry[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind",["complete","shared_segment","mismatched_segment"])
async def test_dining_builder_preserves_only_scoped_real_differences(kind):
    day,stops=day_context([card("前站",0),card("后站",1)])
    stops[0].longitude=116.3
    stops[1].longitude=116.32
    candidate=restaurant().model_copy(update={"position":{"longitude":116.31,"latitude":stops[0].latitude}})
    # model_copy does not validate nested types.
    candidate=type(candidate).model_validate(candidate.model_dump())
    async def search(**_):return [candidate]
    class Routes:
        async def route(self,a,b,mode,**_):
            returned=[(a.longitude,a.latitude),(b.longitude,b.latitude)]
            if kind != "complete":
                if a.canonical_place_id == stops[0].canonical_place_id:
                    returned[0]=(a.longitude+.001,a.latitude)
                if kind == "mismatched_segment" and b.canonical_place_id == candidate.canonical_place_id:
                    returned[0]=(a.longitude+.002,a.latitude)
            return fact(a,b,returned=returned,minutes=10 if b.canonical_place_id==stops[1].canonical_place_id and a.canonical_place_id==stops[0].canonical_place_id else 8)
    rows=await build_daily_meals(NS(days=[day]),NS(stops=stops),search=search,routes=Routes(),area_search=None)
    item=rows[0]["candidates"][0]
    assert item["extra_minutes"] == (None if kind == "mismatched_segment" else 6)
    assert item["route_coverage_scope"] == {"complete":"REQUESTED_POINTS","shared_segment":"RETURNED_SEGMENTS","mismatched_segment":None}[kind]


def test_old_meal_cache_clears_delta_claim_and_recommendation_without_rewriting_rows():
    old={"place":restaurant().model_dump(mode="json"),"extra_minutes":29,"reason":"经此店前往下一站约多29分钟"}
    row={"day_index":1,"label":"Day 1","status":"AVAILABLE","message":"中途用餐建议","after_activity_token":"a","candidates":[old]}
    output=project_daily_meals([row],public_resource_id="fixed",etag="fixed")[0].candidates[0]
    assert output.extra_minutes is None and output.route_coverage_scope is None and not output.recommended
    assert "29" not in output.reason and old["extra_minutes"] == 29


@pytest.mark.asyncio
async def test_old_stay_route_rows_have_unknown_commute_instead_of_37_minute_total():
    now=datetime.now(UTC)
    row={"selected_mode":"walking","mode":"walking","status":"AVAILABLE","duration_minutes":37,"transfer_count":0,"observed_at":now-timedelta(minutes=1),"expires_at":now+timedelta(hours=1),"provider_receipt_json":{}}
    class Connection:
        async def fetch(self,query,*_):
            assert "provider_receipt_json" in query
            return [row]
    assessment=await load_stay_commute_assessment(Connection(),"fixed",now=now)
    assert not assessment.complete and assessment.maximum_minutes is None
    candidate={"missing_leg_count":0,"public_candidate_token":"fixed"*5,"name":"固定酒店","brand":"","area_or_address":"固定地址","provider_binding_json":{}}
    projected=_candidate_view(candidate,assessment=assessment)
    assert projected.max_single_leg_minutes is None and "37" not in projected.commute_summary and "前列" not in projected.reason
    a,b=stop((116.3,39.9)),stop((116.31,39.9),1)
    value=fact(a,b,minutes=37)
    current=assess_stay_commute([{"selected_mode":"walking","walking":value.model_dump()}],now=datetime.now(UTC))
    assert current.complete and current.maximum_minutes==37


@pytest.mark.asyncio
async def test_relative_preview_preserves_full_connection_and_rejects_old_unscoped_token():
    items=visits()
    output,_=await compare(items)
    assert output.options and output.options[0].route_coverage_scope=="REQUESTED_POINTS"
    public=issue_route_preview(output.options[0],items,public_resource_id="fixed",expected_etag="e",now=datetime.now(UTC))
    assert public.comparison_scope=="CHANGED_EDGES_ONLY" and public.route_coverage_scope=="REQUESTED_POINTS"
    payload=json.loads(_cipher().decrypt(public.change_token[len(ROUTE_PREVIEW_PREFIX):].encode()))
    payload.pop("route_coverage_scope")
    token=ROUTE_PREVIEW_PREFIX+_cipher().encrypt(json.dumps(payload).encode()).decode()
    with pytest.raises(CommandTargetChangedError):
        verify_route_preview(token,public_resource_id="fixed",expected_etag="e",now=datetime.now(UTC))
    old=public.model_dump()
    old.pop("route_coverage_scope")
    result=PublicRelativeRouteOptions(status="AVAILABLE",day_index=1,message="old",options=[old])
    assert result.options==[] and result.status=="UNAVAILABLE"
    edges=current_edges(items)
    edges=[replace(edge,walking=edge.walking.model_copy(update={"provider_binding":{}})) for edge in edges]
    result,calls=await compare(items,edges=edges)
    assert not result.options and not calls


def test_old_map_rows_keep_local_numbers_without_promoting_missing_scope():
    public=_mode_view_from_row({"status":"AVAILABLE","duration_minutes":2,"distance_meters":137,"transfer_count":0})
    assert public.status=="AVAILABLE" and public.connection_status=="UNVERIFIED" and public.distance_meters==137


def test_single_geometry_point_cannot_supply_a_comparable_route():
    a,b=stop((116.3,39.9)),stop((116.31,39.9),1)
    only=[RouteGeometryPoint(longitude=116.3,latitude=39.9)]
    value=fact(a,b).model_copy(update={'provider_binding':{'route_connection':connection_evidence(a,b,only)}})
    assert route_segment(value) is None and compared_path_scope([value],[value]) is None


@pytest.mark.asyncio
async def test_relative_real_adapter_keeps_comparable_returned_segments_without_center_claim():
    items=visits()
    edges=[]
    for edge in current_edges(items):
        modes={}
        for mode in ('walking','transit'):
            original=getattr(edge,mode)
            geometry=[RouteGeometryPoint(longitude=s.longitude+.0001,latitude=s.latitude) for s in (edge.origin,edge.destination)]
            modes[mode]=InternalRouteModeFact.model_validate({**original.model_dump(),
                'provider_binding':{'route_connection':connection_evidence(edge.origin,edge.destination,geometry)},'geometry':geometry})
        edges.append(replace(edge,**modes))
    class Shifted(FixedAmap):
        async def respond(self,request):
            response=await super().respond(request)
            payload=response.json()
            points=[]
            for endpoint in ('origin','destination'):
                lon,lat=map(float,request.url.params[endpoint].split(','))
                points.append(f'{lon+.0001:.6f},{lat:.6f}')
            steps=[{'polyline':';'.join(points)}]
            if request.url.path.endswith('walking'):
                payload['route']['paths'][0]['steps']=steps
            else:
                payload['route']['transits'][0]['segments']=[{'walking':{'steps':steps}}]
            return httpx.Response(200,json=payload)
    result,_=await compare(items,edges=edges,fixture=Shifted())
    assert result.options and result.options[0].minutes_saved>0
    public=issue_route_preview(result.options[0],items,public_resource_id='fixed',expected_etag='e',now=datetime.now(UTC))
    assert public.route_coverage_scope=='RETURNED_SEGMENTS'
    assert public.comparison_scope=='CHANGED_EDGES_ONLY'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,kind,expected_breaks',[
    ('walking','continuous',[]),('walking','gap',[2]),('walking','missing',[2]),
    ('walking','missing_first',[]),('walking','missing_last',[]),
    ('transit','continuous',[]),('transit','gap',[2]),('transit','missing',[2]),
    ('transit','empty_optional',[]),('transit','missing_segment',[2]),
])
async def test_original_step_and_transport_boundaries_are_not_flattened_into_false_connections(mode,kind,expected_breaks):
    a,b=stop((116.3,39.9)),stop((116.33,39.9),1)
    left={'polyline':'116.3,39.9;116.31,39.9','distance':'100','duration':'100'}
    right={'polyline':('116.32' if kind=='gap' else '116.31')+',39.9;116.33,39.9','distance':'100','duration':'100'}
    if mode=='walking':
        steps=[left,*([{'distance':'100','duration':'100'}] if kind=='missing' else []),right]
        if kind=='missing_first':
            steps.insert(0,{'distance':'100','duration':'100'})
        if kind=='missing_last':
            steps.append({'distance':'100','duration':'100'})
        response={'status':'1','route':{'paths':[{'distance':'500','duration':'600','steps':steps}]}}
    else:
        segments=[{'walking':{'steps':[left]}}]
        if kind=='missing':
            segments.append({'bus':{'buslines':[{'distance':'100','duration':'100'}]}})
        if kind=='missing_segment':
            segments.append({'distance':'100','walking':{'distance':'0','steps':[]},'bus':{'buslines':[]}})
        segments.append({'walking':{'distance':'0','duration':'0','steps':[]} if kind=='empty_optional' else {},'bus':{'buslines':[right]}})
        response={'status':'1','route':{'transits':[{'distance':'500','duration':'600','segments':segments}]}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=response))) as client:
        value=await AmapRouteProvider(api_key='fixed',client=client).route(a,b,mode,observed_at=datetime.now(UTC))
    public=mode_public_view(value)
    assert public.status=='AVAILABLE' and public.duration_minutes==10 and public.distance_meters==500
    assert public.geometry_break_indices==expected_breaks
    if kind=='gap':
        expected_points=[(116.3,39.9),(116.31,39.9),(116.32,39.9),(116.33,39.9)]
    elif kind in ('missing','missing_segment'):
        expected_points=[(116.3,39.9),(116.31,39.9),(116.31,39.9),(116.33,39.9)]
    else:
        expected_points=[(116.3,39.9),(116.31,39.9),(116.33,39.9)]
    assert [(p.longitude,p.latitude) for p in public.geometry]==expected_points
    assert value.provider_binding['route_connection']['geometry_complete'] == (not kind.startswith('missing'))
    if expected_breaks==[] and not kind.startswith('missing'):
        assert public.connection_status=='VERIFIED' and compared_path_scope([value],[value])=='REQUESTED_POINTS'
    else:
        assert public.connection_status=='UNVERIFIED' and compared_path_scope([value],[value]) is None


def test_old_flat_geometry_keeps_data_but_has_no_boundary_proof():
    a,b=stop((116.3,39.9)),stop((116.31,39.9),1)
    value=fact(a,b)
    old=value.model_dump()
    old['provider_binding']['route_connection'].pop('geometry_break_indices')
    value=InternalRouteModeFact.model_validate(old)
    public=mode_public_view(value)
    assert len(public.geometry)==2 and public.geometry_break_indices is None and public.connection_status=='UNVERIFIED'
    assert compared_path_scope([value],[value]) is None


def test_break_list_without_completeness_does_not_prove_a_full_route():
    a,b=stop((116.3,39.9)),stop((116.31,39.9),1)
    old=fact(a,b).model_dump()
    old['provider_binding']['route_connection'].pop('geometry_complete')
    value=InternalRouteModeFact.model_validate(old)
    assert value.geometry_break_indices==[] and len(value.geometry)==2
    assert value.connection_status=='UNVERIFIED' and compared_path_scope([value],[value]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['walking','transit'])
@pytest.mark.parametrize('bad_at',['first','middle','last'])
async def test_invalid_polyline_coordinate_preserves_known_pieces_without_bridging(mode,bad_at):
    a,b=stop((116.3,39.9)),stop((116.33,39.9),1)
    coords=['116.3,39.9','116.31,39.9','116.32,39.9','116.33,39.9']
    coords.insert({'first':0,'middle':2,'last':4}[bad_at],'bad')
    step={'polyline':';'.join(coords),'distance':'500','duration':'600'}
    body={'distance':'500','duration':'600'}
    body.update({'steps':[step]} if mode=='walking' else {'segments':[{'bus':{'buslines':[step]}}]})
    response={'status':'1','route':{'paths' if mode=='walking' else 'transits':[body]}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=response))) as client:
        value=await AmapRouteProvider(api_key='fixed',client=client).route(a,b,mode,observed_at=datetime.now(UTC))
    public=mode_public_view(value)
    assert public.status=='AVAILABLE' and public.duration_minutes==10 and public.distance_meters==500
    assert [p.longitude for p in public.geometry]==[116.3,116.31,116.32,116.33]
    assert public.geometry_break_indices==([2] if bad_at=='middle' else [])
    assert public.connection_status=='UNVERIFIED' and value.provider_binding['route_connection']['geometry_complete'] is False
    assert compared_path_scope([value],[value]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['memory','postgres'])
async def test_split_geometry_survives_worker_storage_and_read_only_refresh(kind, monkeypatch):
    from app.trip_understanding.map_worker import MapRenderWorker
    from app.trip_understanding.repository import PostgresTripUnderstandingRepository
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from tests.test_experience_v3_journey import repository_for
    from tests.test_relative_route_flow import create_trip

    class Broken(FixedAmap):
        async def respond(self,request):
            response=await super().respond(request)
            payload=response.json()
            if request.url.path.endswith('walking'):
                left=request.url.params['origin']
                right=request.url.params['destination']
                lon,lat=map(float,left.split(','))
                payload['route']['paths'][0]['steps']=[
                    {'polyline':left+f';{lon+.001:.6f},{lat:.6f}'},
                    {'polyline':f'{lon+.002:.6f},{lat:.6f};'+right},
                ]
            return httpx.Response(200,json=payload)

    async with repository_for(kind) as repo:
        resource,stored=await create_trip(repo,key='boundary-storage',render=False)
        service=TripUnderstandingApplicationService(repo)
        await service.request_map_render(resource,expected_etag=stored.opaque_etag,idempotency_key='boundary-map',now=datetime.now(UTC))
        errors=[]
        complete=repo.complete_map_job
        async def checked_complete(*args,**kwargs):
            try:
                return await complete(*args,**kwargs)
            except Exception as error:
                errors.append(f'{type(error).__name__}: {error}')
                raise
        monkeypatch.setattr(repo,'complete_map_job',checked_complete)
        transport=Broken()
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport.respond)) as client:
            assert await MapRenderWorker(repo,renderer=MapRenderer(AmapRouteProvider(api_key='fixed',client=client))).run_once('boundary-worker')
        assert not errors, errors
        calls=len(transport.calls)
        first=await repo.get_map_view(resource,now=datetime.now(UTC))
        reader=PostgresTripUnderstandingRepository(pool=repo._pool,geometry_cache=repo._geometry_cache) if kind=='postgres' else repo
        second=await reader.get_map_view(resource,now=datetime.now(UTC))
        assert first==second and first.status=='LIMITED'
        assert len(transport.calls)==calls==8
        for day in first.days:
            for route in day.routes:
                assert route.walking.geometry_break_indices==[2] and len(route.walking.geometry)==4
                assert route.walking.connection_status=='UNVERIFIED' and route.walking.duration_minutes==10
                assert route.transit.geometry_break_indices==[]
