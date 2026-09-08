// Controlled UI contract scenarios. These do not measure live model or map quality.
const {test, expect} = require('@playwright/test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/lib/confirmed-trip-view.ts'), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020},
}).outputText
const view = {}; vm.runInNewContext(code, {exports: view})
const R = 'synthetic-source-lodging-only'
const P = `/api/v3/trip-understandings/${R}`
const hotelName = '星河全程酒店'
const card = (id, name, extra={}) => ({activity_token:`synthetic-lodging-${id}-0000000000`, name, category:'景点',
  city:'北京',status:'READY',area_or_address:'合成测试地址',time_hint:null,photo_url:null,
  available_actions:['VIEW_DETAILS','REPLACE','DELETE','MOVE'], ...extra})
function fixtureResult(unresolved=false) {
  return {status:'PARTIAL_RESULT',ownership:'ANONYMOUS',is_demo:false,can_undo:false,
    assumptions:[{key:'destination',label:'目的地',value:'北京',editable:true}],
    days:[{label:'Day 1',activities:[
      card('whole',hotelName,{category:'住宿',lodging_event:'OVERNIGHT',lodging_scope:'WHOLE_TRIP',status:unresolved?'NEEDS_CONFIRMATION':'READY'}),
      card('park','星河公园'),card('day-hotel','湖畔当晚酒店',{category:'住宿',lodging_event:'OVERNIGHT',lodging_scope:'DAY'}),
    ],alternatives:[]}, {label:'Day 2',activities:[card('museum','星河博物馆')],alternatives:[]}],
    map:{status:'NEEDS_UPDATE',message:'路线需手动更新',available_actions:['RENDER_MAP']},
    stay:{status:'UNAVAILABLE',message:'尚未生成建议',candidates:[],searched_scopes:[],area_summary:null,available_actions:[]},
    available_actions:['EDIT_ASSUMPTIONS','EDIT_CARDS']}
}

test('whole-trip lodging is independent of visit numbers and stored command positions', () => {
  const result=fixtureResult(), original=JSON.stringify(result)
  expect(view.confirmedSourceLodgings(result).map(c=>c.name)).toEqual([hotelName])
  expect(view.confirmedTripView(result).days[0].activities.map(c=>c.name)).toEqual(['星河公园','湖畔当晚酒店'])
  expect(view.storedPositionCommand({command_type:'ACTIVITY_INSERT',day_index:1,position:0,name:'新地点'},result).position).toBe(1)
  expect(view.storedPositionCommand({command_type:'ACTIVITY_MOVE',activity_token:result.days[1].activities[0].activity_token,target_day_index:1,target_position:1},result).target_position).toBe(2)
  expect(JSON.stringify(result)).toBe(original)
  for (const event of [undefined,'DEPARTURE','CHECK_OUT','LUGGAGE_PICKUP']) {
    const changed=fixtureResult();changed.days[0].activities[0].lodging_event=event
    expect(view.confirmedSourceLodgings(changed)).toEqual([])
    expect(view.confirmedTripView(changed).days[0].activities).toHaveLength(3)
  }
  const pending=fixtureResult(true)
  expect(view.confirmedSourceLodgings(pending)).toEqual([])
  expect(pending.days[0].activities[0].status).toBe('NEEDS_CONFIRMATION')
})

async function fixture(page,{unresolved=false,mapView=null,dayTwoActivities=null}={}) {
  const original=fixtureResult(unresolved)
  if(dayTwoActivities)original.days[1].activities=dayTwoActivities
  if(mapView)original.map={status:mapView.status,message:mapView.message,available_actions:[]}
  const state={result:structuredClone(original),version:0,commands:[],searches:[],mapPosts:0,mapView}
  await page.route('**/webapi.amap.com/**',route=>route.abort())
  await page.route('**/restapi.amap.com/**',route=>route.abort())
  await page.route('**/api/user/me',route=>route.fulfill({status:401,json:{}}))
  await page.route('**/api/v3/trip-understandings/**',async route=>{
    const request=route.request(),action=new URL(request.url()).pathname.slice(P.length)
    const reply=(json,status=200)=>route.fulfill({status,json,headers:{ETag:`"source-lodging-${state.version}"`}})
    if(action==='/result')return reply(state.result)
    if(action==='/map-renders/latest')return reply(state.mapView||{...state.result.map,points:[],days:[]})
    if(action==='/stay-suggestions')return reply(state.result.stay)
    if(action==='/daily-dining')return reply({status:'UNAVAILABLE',message:'未配置合成餐饮',days:[]})
    if(action==='/supplementary')return reply({status:'AVAILABLE',days:[]})
    if(action==='/materialize')return reply({status:'READY',message:'已准备',calendar:'按日期',party_size:2,checks_available:true})
    if(action==='/checks')return reply({status:'STILL_NEEDS_CONFIRMATION',message:'路线尚未更新',items:[],remaining_must_adjust:0,available_actions:[]})
    if(action==='/place-candidates') {
      state.searches.push(request.postDataJSON())
      return reply({status:'AVAILABLE',candidates:[{candidate_token:'synthetic-new-hotel-candidate',name:'晴川全程酒店',category:'住宿',area_or_address:'合成新酒店地址',position:{longitude:116.4,latitude:39.9,coordinate_system:'GCJ02'}}]})
    }
    if(action==='/commands') {
      const command=request.postDataJSON();state.commands.push(command)
      if(command.command_type==='UNDO')state.result=structuredClone(original)
      else if(command.command_type==='PLACE_CONFIRM') {
        const target=state.result.days.flatMap(d=>d.activities).find(c=>c.activity_token===command.activity_token)
        if(!target)return reply({},409)
        Object.assign(target,{name:'晴川全程酒店',area_or_address:'合成新酒店地址',status:'READY'})
      } else if(command.command_type==='ACTIVITY_DELETE') {
        if(!state.result.days.some(d=>d.activities.some(c=>c.activity_token===command.activity_token)))return reply({},409)
        state.result.days.forEach(d=>{d.activities=d.activities.filter(c=>c.activity_token!==command.activity_token)})
      } else return reply({},400)
      state.version++
      state.result.days.forEach(d=>d.activities.forEach((c,index)=>{c.activity_token=`synthetic-v${state.version}-${d.label}-${index}-0000000000`}))
      state.result.can_undo=command.command_type!=='UNDO'
      return reply({status:'APPLIED',changed_days:['Day 1'],map_readiness:'NEEDS_UPDATE'})
    }
    if(action==='/map-renders')state.mapPosts++
    return reply({},404)
  })
  await page.goto(`/trip/result#trip=${R}`)
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  return state
}

for(const width of [1440,390])test(`confirmed hotel purpose is visible and accessible in both layouts at ${width}px`,async({page})=>{
  await page.setViewportSize({width,height:950})
  const cases=[
    ['取件酒店','LUGGAGE_PICKUP','取行李'],
    ['当晚酒店','OVERNIGHT','入住'],
    ['离店酒店','CHECK_OUT','退房'],
    ['出发酒店','DEPARTURE','酒店出发'],
    ['到访酒店','VISIT_ONLY','仅到访'],
    ['未知用途酒店',null,'住宿'],
    ['用途待确认酒店','LUGGAGE_PICKUP','住宿',true],
  ]
  const activities=cases.map(([name,event,,uncertain],index)=>card(`purpose-${index}`,name,{
    category:'住宿',lodging_event:event,lodging_scope:'DAY',lodging_role_uncertain:!!uncertain,
  }))
  const state=await fixture(page,{dayTwoActivities:activities})
  for(const [name,,purpose] of cases){
    const mainCard=page.getByTestId('activity-card').filter({has:page.getByRole('heading',{name,exact:true})})
    const details=mainCard.getByRole('button',{name:new RegExp(`${name} ${purpose} 已确认`)})
    await details.scrollIntoViewIfNeeded()
    await expect(details).toBeVisible()
    await expect(details.getByText(purpose,{exact:true})).toBeVisible()
  }
  await page.getByRole('button',{name:'切换为列表',exact:true}).click()
  const list=page.getByRole('list',{name:'Day 2 地点列表',exact:true})
  for(const [name,,purpose] of cases){
    const details=list.getByRole('button',{name:`${name} ${purpose} · 已确认 · 可更改`,exact:true})
    await details.scrollIntoViewIfNeeded()
    await expect(details).toBeVisible()
  }
  expect(state.result.days[1].activities).toEqual(activities)
  expect(state.commands).toEqual([])
  expect(state.mapPosts).toBe(0)
})

for(const width of [1440,390])test(`whole-trip hotel can be viewed, changed, deleted and undone without route generation at ${width}px`,async({page})=>{
  await page.setViewportSize({width,height:950})
  const state=await fixture(page)
  const lodging=page.getByTestId('source-lodging')
  await expect(lodging.getByRole('heading',{name:'全程住宿',exact:true})).toBeVisible()
  await expect(page.getByTestId('activity-card').getByRole('heading',{name:hotelName,exact:true})).toHaveCount(0)
  await expect(page.getByTestId('activity-card').getByRole('heading',{name:'湖畔当晚酒店',exact:true})).toHaveCount(1)
  await lodging.getByText('查看住宿',{exact:true}).click()
  await expect(lodging.getByText('北京 · 合成测试地址',{exact:true})).toBeVisible()
  await page.screenshot({path:test.info().outputPath('source-lodging.png'),fullPage:true})
  await lodging.getByRole('button',{name:`修改全程住宿 ${hotelName}`,exact:true}).click()
  expect(state.searches).toHaveLength(0)
  await lodging.getByRole('button',{name:'搜索',exact:true}).click()
  await lodging.getByRole('button',{name:/晴川全程酒店/}).click()
  await lodging.getByRole('button',{name:'使用这个地点',exact:true}).click()
  await expect(lodging.getByRole('heading',{name:'晴川全程酒店',exact:true})).toBeVisible()
  expect(state.commands[0].command_type).toBe('PLACE_CONFIRM')
  await expect(page.getByTestId('activity-card').getByRole('heading',{name:'晴川全程酒店',exact:true})).toHaveCount(0)
  await lodging.getByRole('button',{name:'移除全程住宿 晴川全程酒店',exact:true}).click()
  await lodging.getByRole('button',{name:'确认移除住宿',exact:true}).click()
  await expect(lodging).toHaveCount(0)
  expect(state.commands[1].command_type).toBe('ACTIVITY_DELETE')
  expect(state.commands[1].activity_token).not.toBe(state.commands[0].activity_token)
  await page.getByTestId('undo-trip-command').click()
  await expect(lodging.getByRole('heading',{name:hotelName,exact:true})).toBeVisible()
  await page.reload()
  await expect(lodging.getByRole('heading',{name:hotelName,exact:true})).toBeVisible()
  expect(state.mapPosts).toBe(0)
})

test('unconfirmed whole-trip hotel is recoverable and moves to its separate block after confirmation',async({page})=>{
  const state=await fixture(page,{unresolved:true})
  await expect(page.getByTestId('source-lodging')).toHaveCount(0)
  await page.getByTestId('unresolved-places').locator('summary').click()
  await page.getByRole('button',{name:`${hotelName} · 北京 · 确认地点`,exact:true}).click()
  await page.getByTestId('pending-place-dropdown').getByRole('button',{name:'搜索',exact:true}).click()
  await page.getByRole('button',{name:/晴川全程酒店/}).click()
  await page.getByRole('button',{name:'使用这个地点',exact:true}).click()
  await expect(page.getByTestId('source-lodging').getByRole('heading',{name:'晴川全程酒店',exact:true})).toBeVisible()
  await expect(page.getByTestId('activity-card').getByRole('heading',{name:'晴川全程酒店',exact:true})).toHaveCount(0)
  await expect(page.getByTestId('unresolved-places')).toHaveCount(0)
  expect(state.mapPosts).toBe(0)
})

for(const width of [1440,390])test(`selected hotel has an independent map marker and late route bounds are fitted at ${width}px`,async({page})=>{
  await page.setViewportSize({width,height:950})
  await page.addInitScript(()=>{
    window.__mapFits=[]
    window.AMap={
      Map:class {
        constructor(container){this.container=container}
        on(event,callback){if(event==='complete')setTimeout(callback,0)}
        add(items){for(const item of items)if(item.options.content)this.container.append(item.options.content)}
        remove(items){for(const item of items)item.options.content?.remove()}
        destroy(){this.container.replaceChildren()}
        setCenter(point){window.__lodgingCenter=point}
        setFitView(items){window.__mapFits.push(items.filter(i=>i.kind==='line').flatMap(i=>i.options.path))}
        resize(){} zoomIn(){} zoomOut(){}
      },
      Marker:class {constructor(options){this.options=options;this.kind='marker'}},
      Polyline:class {constructor(options){this.options=options;this.kind='line'}},
    }
  })
  const result=fixtureResult()
  const points=result.days.flatMap(day=>day.activities.filter(c=>c.lodging_scope!=='WHOLE_TRIP').map((c,index)=>({
    activity_token:c.activity_token,day_label:day.label,sequence_index:index,name:c.name,
    position:{longitude:116.4+index/100,latitude:39.9,coordinate_system:'GCJ02'},
  })))
  const lodging={point_token:'opaque-synthetic-lodging-1',day_label:'Day 1',name:hotelName,
    position:{longitude:116.42,latitude:39.91,coordinate_system:'GCJ02'}}
  const state=await fixture(page,{mapView:{status:'PREPARING',message:'正在准备路线',points,
    lodging_points:[lodging,{...lodging,point_token:'opaque-synthetic-lodging-2',day_label:'Day 2'}],days:[],available_actions:[]}})
  await page.getByTestId(`${width<1024?'mobile':'desktop'}-nav-map_stay`).click()
  const map=page.getByTestId('result-view-map-stay').getByTestId('route-map')
  await expect(map.locator('.e-map-marker:not(.e-map-lodging-marker)')).toHaveCount(3)
  await expect(map.locator('.e-map-lodging-marker')).toHaveCount(1)
  expect(await map.locator('.e-map-marker:not(.e-map-lodging-marker)').allTextContents()).toEqual(['1','2','1'])
  await map.getByRole('button',{name:`查看住宿位置 ${hotelName}`,exact:true}).click()
  await expect(map.getByText(`住宿位置：${hotelName}`,{exact:true})).toBeVisible()
  expect(state.commands).toEqual([])
  const available={status:'AVAILABLE',duration_minutes:20,distance_meters:2000,transfer_count:0,
    geometry:[{longitude:116.4,latitude:39.9},{longitude:116.8,latitude:40.2}]}
  state.mapView={...state.mapView,status:'AVAILABLE',days:[{day_index:1,label:'Day 1',routes:[{
    from_name:'星河公园',to_name:hotelName,selected_mode:'transit',message:'合成路线',walking:available,transit:available,
  }]}]}
  await expect.poll(()=>page.evaluate(()=>window.__mapFits.some(path=>path.some(p=>p[0]===116.8&&p[1]===40.2))),{timeout:12000}).toBe(true)
  await expect(map.locator('.e-map-lodging-marker')).toHaveCount(1)
  expect(state.mapPosts).toBe(0)
})
