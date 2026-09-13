// Controlled browser contracts; synthetic providers, no live quality claims.
const {test,expect}=require('@playwright/test')
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript')
const R='synthetic-pending-lodging',P=`/api/v3/trip-understandings/${R}`
const PRIVATE_NAME='静远待确认酒店',CONFIRMED_NAME='静远已核验酒店',TOKEN='opaque-pending-lodging-synthetic'
const card=(id,name,extra={})=>({activity_token:`synthetic-visit-${id}-000000000`,name,category:'景点',city:'北京',
  status:'READY',area_or_address:'合成地址',time_hint:null,available_actions:['VIEW_DETAILS','REPLACE','DELETE','MOVE'],...extra})
function result(){return {status:'PARTIAL_RESULT',ownership:'ANONYMOUS',is_demo:false,can_undo:false,
  assumptions:[{key:'destination',label:'目的地',value:'北京',editable:true}],
  days:[{label:'Day 1',activities:[card('one','合成公园')]},{label:'Day 2',activities:[card('two','合成博物馆')]},{label:'Day 3',activities:[card('three','合成古街')]}],
  pending_lodgings:[{pending_token:TOKEN,status:'NEEDS_CONFIRMATION',unprocessed_count:1}],lodging_constraints:[],
  coverage:{recognized_place_count:4,confirmed_place_count:3,unresolved_place_count:1,unclassified_mention_count:0,unprocessed_count:1,complete:false},
  map:{status:'NEEDS_UPDATE',message:'路线需手动更新',available_actions:['RENDER_MAP']},
  stay:{status:'UNAVAILABLE',message:'尚无建议',candidates:[],searched_scopes:[],area_summary:null,available_actions:[]},available_actions:['EDIT_ASSUMPTIONS','EDIT_CARDS']}}
const candidate=(token)=>({candidate_token:token,name:CONFIRMED_NAME,category:'住宿',area_or_address:'合成酒店地址',position:{longitude:116.4,latitude:39.9,coordinate_system:'GCJ02'}})
async function fixture(page,options={}){
  const state={result:result(),version:0,commands:[],searches:[],privateReads:0,mapPosts:0,writes:0,deleted:false,bindings:new Map(),keys:new Map()}
  if(options.multiCity){state.result.assumptions[0].value='北京、上海';state.result.days[1].activities[0].city='上海'}
  if(Object.hasOwn(options,'pendingCity'))state.pendingCity=options.pendingCity
  state.requireCity=options.requireCity||false
  await page.route('**/webapi.amap.com/**',route=>route.abort())
  await page.route('**/restapi.amap.com/**',route=>route.abort())
  await page.route('**/api/user/me',route=>route.fulfill({status:401,json:{}}))
  await page.route('**/api/v3/trip-understandings/**',async route=>{
    const request=route.request(),url=new URL(request.url()),action=url.pathname.slice(P.length)
    const reply=(json,status=200)=>route.fulfill({status,json,headers:{ETag:`"pending-v${state.version}"`,'Cache-Control':'no-store'}})
    if(action==='/result')return reply(state.result)
    if(action==='/map-renders/latest')return reply({...state.result.map,points:[],lodging_points:[],days:[]})
    if(action==='/stay-suggestions')return reply(state.result.stay)
    if(action==='/daily-dining')return reply({status:'UNAVAILABLE',message:'合成餐饮未配置',days:[]})
    if(action==='/materialize')return reply({status:'READY',message:'已准备',calendar:'按日期',party_size:2,checks_available:true})
    if(action==='/checks')return reply({status:'STILL_NEEDS_CONFIRMATION',message:'路线未更新',items:[],remaining_must_adjust:0,available_actions:[]})
    if(action==='/supplementary'){
      const include=url.searchParams.get('include_pending_lodgings')==='true'
      if(include){state.privateReads++;if(state.privateGate)await state.privateGate}
      return reply({status:state.deleted?'DELETED':'AVAILABLE',days:[],pending_lodgings:!state.deleted&&include
        ?state.result.pending_lodgings.map(x=>({...x,name:PRIVATE_NAME,city:Object.hasOwn(state,'pendingCity')?state.pendingCity:'北京'})):[]})
    }
    if(action==='/source'&&request.method()==='DELETE'){state.deleted=true;return reply({status:'DELETED'})}
    if(action==='/place-candidates'){
      const body=request.postDataJSON();state.searches.push({body,etag:request.headers()['if-match']})
      if(state.searchGate)await state.searchGate
      if(state.deleted)return reply({},410)
      if(state.requireCity&&!body.city)return reply({detail:{code:'CITY_REQUIRED'}},422)
      if(state.searchConflict){state.searchConflict=false;state.version++;return reply({},409)}
      const token=`synthetic-candidate-${state.version}-${state.searches.length}`
      state.bindings.set(token,{intent:body.intent,version:state.version,pending_token:body.pending_token})
      return reply({status:'AVAILABLE',candidates:[candidate(token)]})
    }
    if(action==='/commands'){
      const body=request.postDataJSON(),key=request.headers()['idempotency-key'];state.commands.push({body,key,etag:request.headers()['if-match']})
      if(state.keys.has(key))return reply({status:'APPLIED',changed_days:[],map_readiness:'NEEDS_UPDATE'})
      if(state.conflict){state.conflict=false;state.version++;return reply({},409)}
      if(request.headers()['if-match']!==`"pending-v${state.version}"`)return reply({},409)
      if(body.command_type==='UNDO')state.result=structuredClone(state.previous)
      else{
        state.previous=structuredClone(state.result)
        if(body.command_type==='LODGING_RECOVER'){
          const binding=state.bindings.get(body.candidate_token)
          if(state.deleted||binding?.version!==state.version||binding.pending_token!==body.pending_token||JSON.stringify(binding.intent)!==JSON.stringify(body.intent))return reply({},409)
          state.result.pending_lodgings=[]
          const hotel=card(`recovered-${state.version}`,CONFIRMED_NAME,{category:'住宿',available_actions:['VIEW_DETAILS','REPLACE','DELETE'],lodging_event:body.intent.kind==='VISIT_ONLY'?'VISIT_ONLY':'OVERNIGHT'})
          if(body.intent.kind==='VISIT_ONLY'){
            const day=state.result.days[body.intent.day_index-1],index=body.intent.before_activity_token?day.activities.findIndex(c=>c.activity_token===body.intent.before_activity_token):day.activities.length
            day.activities.splice(index,0,hotel)
          }else state.result.lodging_constraints=[{...hotel,scope:body.intent.kind,overnight_days:body.intent.kind==='WHOLE_TRIP'?[1,2]:body.intent.overnight_days}]
          state.result.coverage={...state.result.coverage,confirmed_place_count:4,unresolved_place_count:0,unprocessed_count:0,complete:true}
        }else if(body.command_type==='PLACE_CONFIRM'){
          const hotel=state.result.lodging_constraints.find(c=>c.activity_token===body.activity_token)
          if(!hotel)return reply({},409)
          hotel.name=CONFIRMED_NAME
        }else if(body.command_type==='ACTIVITY_DELETE')state.result.lodging_constraints=state.result.lodging_constraints.filter(c=>c.activity_token!==body.activity_token)
        else return reply({},400)
      }
      state.version++;state.writes++;state.result.can_undo=body.command_type!=='UNDO';state.keys.set(key,true)
      if(state.loseAck){state.loseAck=false;return route.abort('failed')}
      return reply({status:'APPLIED',changed_days:['Day 1'],map_readiness:'NEEDS_UPDATE'})
    }
    if(action==='/map-renders')state.mapPosts++
    return reply({},404)
  })
  await page.goto(`/trip/result#trip=${R}`)
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  return state
}
async function openHotel(page){
  await page.getByTestId('unresolved-places').locator('summary').click()
  await page.getByRole('button',{name:`${PRIVATE_NAME} · 住宿用途待确认`,exact:true}).click()
  await expect(page.getByLabel('酒店用途',{exact:true})).toHaveValue('')
}
async function chooseIntent(page,kind){
  await page.getByLabel('酒店用途',{exact:true}).selectOption(kind)
  if(kind==='NIGHTS'){await page.getByLabel('Day 1 后一晚',{exact:true}).check();await page.getByLabel('Day 2 后一晚',{exact:true}).check()}
  if(kind==='VISIT_ONLY'){
    await page.getByLabel('到访日期',{exact:true}).selectOption('2')
    await expect(page.getByTestId('pending-place-dropdown')).toHaveCount(0)
    await page.getByLabel('到访位置',{exact:true}).selectOption({label:'在合成博物馆之前'})
  }
}
async function chooseCandidate(page){
  const dropdown=page.getByTestId('pending-lodging-recovery').getByTestId('pending-place-dropdown')
  await dropdown.getByRole('button',{name:'搜索',exact:true}).click()
  await dropdown.getByRole('button',{name:new RegExp(CONFIRMED_NAME)}).click()
  return dropdown.getByRole('button',{name:'确认保存酒店和用途',exact:true})
}

for(const width of [1440,390])test(`missing hotel city preserves the chosen night and focuses city selection at ${width}px`,async({page})=>{
  await page.setViewportSize({width,height:950})
  const state=await fixture(page,{pendingCity:null,requireCity:true,multiCity:true})
  await openHotel(page)
  await page.getByLabel('酒店用途',{exact:true}).selectOption('NIGHTS')
  await page.getByLabel('Day 2 后一晚',{exact:true}).check()
  const dropdown=page.getByTestId('pending-lodging-recovery').getByTestId('pending-place-dropdown')
  await expect(dropdown.getByLabel('查询城市',{exact:true})).toHaveValue('')
  await dropdown.getByRole('button',{name:'搜索',exact:true}).click()
  await expect(dropdown.getByText('请先填写这家酒店所在的城市，再搜索。',{exact:true})).toBeVisible()
  await expect(dropdown.getByLabel('查询城市',{exact:true})).toBeFocused()
  await expect(page.getByLabel('酒店用途',{exact:true})).toHaveValue('NIGHTS')
  await expect(page.getByLabel('Day 1 后一晚',{exact:true})).not.toBeChecked()
  await expect(page.getByLabel('Day 2 后一晚',{exact:true})).toBeChecked()
  await expect(dropdown.getByLabel('搜索地点名称',{exact:true})).toHaveValue(PRIVATE_NAME)
  expect(state.privateReads).toBe(1);expect(state.commands).toHaveLength(0)
  await dropdown.getByLabel('查询城市',{exact:true}).selectOption('北京')
  await expect(dropdown.getByText('请先填写这家酒店所在的城市，再搜索。',{exact:true})).toHaveCount(0)
  expect(state.searches).toHaveLength(1)
  await(await chooseCandidate(page)).click()
  await expect(page.getByTestId('source-lodging-card')).toContainText('第2晚住宿')
  expect(state.searches[1].body.city).toBe('北京')
  expect(state.searches[1].body.intent).toEqual({kind:'NIGHTS',overnight_days:[2]})
  expect(state.commands).toHaveLength(1);expect(state.writes).toBe(1)
  await page.screenshot({path:test.info().outputPath('city-required-preserved-night.png'),fullPage:true})
})

for(const width of [1440,390])for(const kind of ['WHOLE_TRIP','NIGHTS','VISIT_ONLY'])test(`pending hotel ${kind} is explicit, atomic and undoable at ${width}px`,async({page})=>{
  await page.setViewportSize({width,height:950})
  const state=await fixture(page)
  expect(state.privateReads).toBe(0);expect(state.searches).toHaveLength(0)
  await expect(page.getByText(PRIVATE_NAME,{exact:true})).toHaveCount(0)
  await openHotel(page)
  await expect(page.getByTestId('pending-place-dropdown')).toHaveCount(0)
  if(kind==='NIGHTS'&&width===1440){
    await page.getByLabel('酒店用途',{exact:true}).selectOption('NIGHTS')
    await page.getByLabel('Day 2 后一晚',{exact:true}).focus();await page.keyboard.press('Space')
    await expect(page.getByLabel('Day 1 后一晚',{exact:true})).not.toBeChecked()
  }else await chooseIntent(page,kind)
  await page.screenshot({path:test.info().outputPath('pending-purpose.png'),fullPage:true})
  expect(state.searches).toHaveLength(0)
  const save=await chooseCandidate(page)
  expect(state.commands).toHaveLength(0)
  expect(state.searches[0].body.pending_token).toBe(TOKEN)
  expect(state.searches[0].etag).toBe('"pending-v0"')
  await save.focus();await page.keyboard.press('Enter')
  if(kind==='VISIT_ONLY'){
    await expect(page.getByTestId('activity-card').getByRole('heading',{name:CONFIRMED_NAME,exact:true})).toBeVisible()
    expect(state.result.days[1].activities.map(c=>c.name)).toEqual([CONFIRMED_NAME,'合成博物馆'])
    expect(state.result.days[1].activities[0].lodging_event).toBe('VISIT_ONLY')
    await expect(page.getByTestId('source-lodging')).toHaveCount(0)
  }else{
    await expect(page.getByTestId('source-lodging').getByRole('heading',{name:CONFIRMED_NAME,exact:true})).toBeVisible()
    await expect(page.getByTestId('activity-card').getByRole('heading',{name:CONFIRMED_NAME,exact:true})).toHaveCount(0)
    expect(state.result.lodging_constraints[0].overnight_days).toEqual(kind==='NIGHTS'&&width===1440?[2]:[1,2])
  }
  await page.screenshot({path:test.info().outputPath('recovered-result.png'),fullPage:true})
  expect(state.writes).toBe(1);expect(state.commands[0].body.command_type).toBe('LODGING_RECOVER')
  expect(state.commands[0].body.intent).toEqual(state.searches[0].body.intent)
  expect(await page.evaluate(()=>JSON.stringify({...sessionStorage,...localStorage}))).not.toContain(PRIVATE_NAME)
  await page.getByTestId('undo-trip-command').click()
  await expect(page.getByTestId('source-lodging')).toHaveCount(0)
  await expect(page.getByTestId('unresolved-places')).toBeVisible()
  await page.reload();await expect(page.getByTestId('unresolved-places')).toBeVisible()
  await expect(page.getByText(PRIVATE_NAME,{exact:true})).toHaveCount(0)
  expect(state.mapPosts).toBe(0)
})

test.describe('touch-only single-night choice',()=>{
  test.use({hasTouch:true,viewport:{width:390,height:950}})
  test('second night can be selected alone by touch without silently choosing the first',async({page})=>{
    const state=await fixture(page);await openHotel(page)
    await page.getByLabel('酒店用途',{exact:true}).selectOption('NIGHTS')
    await page.getByLabel('Day 2 后一晚',{exact:true}).tap()
    await expect(page.getByLabel('Day 1 后一晚',{exact:true})).not.toBeChecked()
    await(await chooseCandidate(page)).tap()
    await expect(page.getByTestId('source-lodging').getByText('第2晚住宿',{exact:true})).toBeVisible()
    expect(state.commands[0].body.intent).toEqual({kind:'NIGHTS',overnight_days:[2]})
    await page.screenshot({path:test.info().outputPath('touch-second-night-only.png'),fullPage:true})
  })
})

test('changing scope, nights, day, position, query and city clears a candidate without saving',async({page})=>{
  const state=await fixture(page);await openHotel(page);await chooseIntent(page,'WHOLE_TRIP');await chooseCandidate(page)
  await page.getByLabel('酒店用途',{exact:true}).selectOption('NIGHTS')
  await expect(page.getByTestId('pending-place-dropdown')).toHaveCount(0)
  await page.getByLabel('Day 1 后一晚',{exact:true}).check();await chooseCandidate(page)
  await page.getByLabel('Day 2 后一晚',{exact:true}).check();await expect(page.getByRole('button',{name:'确认保存酒店和用途',exact:true})).toHaveCount(0)
  await chooseIntent(page,'VISIT_ONLY');await chooseCandidate(page)
  await page.getByLabel('到访位置',{exact:true}).selectOption('__END__');await expect(page.getByRole('button',{name:'确认保存酒店和用途',exact:true})).toHaveCount(0)
  await chooseCandidate(page);await page.getByLabel('到访日期',{exact:true}).selectOption('3');await expect(page.getByTestId('pending-place-dropdown')).toHaveCount(0)
  await page.getByLabel('到访位置',{exact:true}).selectOption('__END__');await chooseCandidate(page)
  await page.getByLabel('搜索地点名称',{exact:true}).fill('合成新查询');await expect(page.getByRole('button',{name:'确认保存酒店和用途',exact:true})).toHaveCount(0)
  await chooseCandidate(page);await page.getByLabel('查询城市',{exact:true}).selectOption('上海');await expect(page.getByRole('button',{name:'确认保存酒店和用途',exact:true})).toHaveCount(0)
  await page.getByRole('button',{name:'取消补全',exact:true}).click()
  await expect(page.getByTestId('pending-lodging-recovery')).toHaveCount(0);await expect(page.getByText(PRIVATE_NAME,{exact:true})).toHaveCount(0)
  expect(state.commands).toHaveLength(0)
})

test('Escape and a late search response cannot restore private hotel names or candidates',async({page})=>{
  const state=await fixture(page);await openHotel(page);await chooseIntent(page,'WHOLE_TRIP')
  let release;state.searchGate=new Promise(resolve=>{release=resolve})
  await page.getByTestId('pending-place-dropdown').getByRole('button',{name:'搜索',exact:true}).click()
  await expect.poll(()=>state.searches.length).toBe(1)
  await page.keyboard.press('Escape');release();await page.waitForTimeout(100)
  await expect(page.getByTestId('pending-lodging-recovery')).toHaveCount(0)
  await expect(page.getByText(PRIVATE_NAME,{exact:true})).toHaveCount(0)
  await expect(page.getByRole('button',{name:'确认保存酒店和用途',exact:true})).toHaveCount(0)
  expect(state.commands).toHaveLength(0)
  await openHotel(page);expect(state.privateReads).toBe(2);expect(state.searches).toHaveLength(1)
})

test('source deletion clears a pending private read and cannot reveal the hotel after reload',async({page})=>{
  const state=await fixture(page)
  let release;state.privateGate=new Promise(resolve=>{release=resolve})
  await page.getByTestId('unresolved-places').locator('summary').click()
  await expect.poll(()=>state.privateReads).toBe(1)
  await page.getByLabel('更多行程操作',{exact:true}).click();await page.getByTestId('delete-trip-source').click()
  await page.getByRole('button',{name:'确认永久删除',exact:true}).click()
  await expect.poll(()=>state.deleted).toBe(true);release()
  await expect(page.getByText('导入文字已删除，现有行程与已确认地点仍保留。',{exact:true})).toBeVisible()
  await expect(page.getByText(PRIVATE_NAME,{exact:true})).toHaveCount(0)
  await page.reload();await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  await expect(page.getByText(PRIVATE_NAME,{exact:true})).toHaveCount(0)
  expect(state.searches).toHaveLength(0);expect(state.commands).toHaveLength(0)
})

test('stale scope search synchronizes before any save and a conflicting save keeps the pending entry',async({page})=>{
  const state=await fixture(page);await openHotel(page);await chooseIntent(page,'WHOLE_TRIP');state.searchConflict=true
  await page.getByTestId('pending-place-dropdown').getByRole('button',{name:'搜索',exact:true}).click()
  await expect(page.getByTestId('pending-lodging-recovery')).toHaveCount(0)
  await expect.poll(()=>page.evaluate(()=>sessionStorage.getItem('bt_active_trip_etag'))).toBe('"pending-v1"')
  await openHotel(page);await chooseIntent(page,'WHOLE_TRIP');const save=await chooseCandidate(page);state.conflict=true;await save.click()
  await expect(page.getByTestId('pending-lodging-recovery')).toHaveCount(0)
  await expect(page.getByTestId('unresolved-places')).toBeVisible();expect(state.writes).toBe(0)
  await openHotel(page);await chooseIntent(page,'WHOLE_TRIP');await(await chooseCandidate(page)).click()
  await expect(page.getByTestId('source-lodging').getByRole('heading',{name:CONFIRMED_NAME,exact:true})).toBeVisible();expect(state.writes).toBe(1)
})

test('lost save acknowledgement recovers one logical write without persisting the private hotel name',async({page})=>{
  const state=await fixture(page);await openHotel(page);await chooseIntent(page,'NIGHTS');const save=await chooseCandidate(page);state.loseAck=true;await save.click()
  await expect(page.getByTestId('retry-result-readback')).toBeVisible()
  await page.getByTestId('retry-result-readback').click()
  await expect(page.getByTestId('source-lodging').getByRole('heading',{name:CONFIRMED_NAME,exact:true})).toBeVisible()
  expect(state.writes).toBe(1);expect(new Set(state.commands.map(c=>c.key)).size).toBe(1)
  expect(await page.evaluate(()=>JSON.stringify({...sessionStorage,...localStorage}))).not.toContain(PRIVATE_NAME)
  expect(state.mapPosts).toBe(0)
})

test('independent confirmed constraints keep their night scope and never change visit numbering',()=>{
  const code=ts.transpileModule(fs.readFileSync(path.join(__dirname,'../src/lib/confirmed-trip-view.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText
  const view={};vm.runInNewContext(code,{exports:view})
  const source=result();source.lodging_constraints=[card('confirmed-hotel',CONFIRMED_NAME,{category:'住宿',scope:'NIGHTS',overnight_days:[2],lodging_event:'OVERNIGHT'})]
  const original=JSON.stringify(source)
  expect(view.confirmedSourceLodgings(source)[0].overnight_days).toEqual([2])
  expect(view.confirmedTripView(source).days.map(d=>d.activities.length)).toEqual([1,1,1])
  expect(view.confirmedTripView(source).pending_lodgings[0].pending_token).toBe(TOKEN)
  source.lodging_constraints[0].status='NEEDS_CONFIRMATION';expect(view.confirmedSourceLodgings(source)).toEqual([])
  source.lodging_constraints[0].status='READY';expect(JSON.stringify(source)).toBe(original)
})
