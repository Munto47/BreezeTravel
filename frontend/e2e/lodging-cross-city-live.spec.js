const {test,expect}=require('@playwright/test')
const fs=require('node:fs'),path=require('node:path')
const {execFileSync}=require('node:child_process')
const {installMapObservation,watchMapResources}=require('./helpers/real-map-observation')
const root=path.resolve(__dirname,'../..')
const sourceReport=process.env.LODGING_LIVE_SOURCE_REPORT||'D:/CODEX/BreezeTravel-five-city/.local-artifacts/evaluation/complex-lodging-real-ui-v4.json'
const caseId='beijing-shanghai-partly-booked'
const liveEnabled=process.env.RUN_LODGING_LIVE==='1'
test.skip(!liveEnabled,'Run only through the dedicated local lodging live configuration')
const input=liveEnabled?JSON.parse(fs.readFileSync(sourceReport,'utf8')).cases.find(c=>c.id===caseId):null
if(liveEnabled&&!input?.input_text)throw new Error('The existing real failure source is required')
const out=path.resolve(process.env.LODGING_LIVE_OUTPUT||path.join(root,'.local-artifacts/evaluation/lodging-cross-city-live-v1'))
const hotel='汉庭酒店(北京天安门广场前门店)'
const normalize=name=>name.replaceAll('（','(').replaceAll('）',')').replace(/^上海豫园$/,'豫园')
const summarize=result=>({status:result.status,coverage:result.coverage,is_demo:result.is_demo,
 days:result.days.map(day=>({label:day.label,activities:day.activities.map(({name,city,status,category,lodging_event,lodging_scope,lodging_role_uncertain})=>
  ({name,city,status,category,lodging_event,lodging_scope,lodging_role_uncertain}))})),map:result.map,stay:result.stay})
function privateFacts(resource){
 return JSON.parse(execFileSync('D:/CODEX/BreezeTravel/.venv/Scripts/python.exe',
  [path.join(root,'scripts/read_private_lodging_measurement.py'),resource],{cwd:root,encoding:'utf8',windowsHide:true,timeout:20000}))
}

test('existing cross-city failure: preserve the booked night, select Shanghai nights, reload, undo and explicitly update map',async({page,context})=>{
 fs.mkdirSync(out,{recursive:true})
 const report={case_id:caseId,source_report:sourceReport,input_provenance:'EXISTING_AGENT_AUTHORED_DEVELOPMENT_CASE',
  execution:'REAL_LOCAL_MODEL_PLACE_SERVICES_AND_BROWSER',started_at:new Date().toISOString(),status:'RUNNING',
  expected:input.expected,known_previous_failure:input.checks,phases:[],writes:[],errors:[]}
 const save=()=>fs.writeFileSync(path.join(out,'result.json'),JSON.stringify(report,null,2))
 let resource=null
 await installMapObservation(page)
 const mapResources=watchMapResources(page)
 page.on('request',request=>{
  const u=new URL(request.url())
  if(u.origin!==new URL(page.url()||'http://127.0.0.1:3168').origin||!u.pathname.startsWith('/api/v3/')||request.method()!=='POST')return
  let command;try{command=request.postDataJSON()?.command_type}catch{}
  report.writes.push({action:u.pathname.split('/').pop(),command_type:command||null})
 })
 page.on('pageerror',error=>report.errors.push({kind:'PAGE',name:error.name}))
 const read=async suffix=>{const response=await page.request.get(`/api/v3/trip-understandings/${resource}/${suffix}`);expect(response.ok()).toBe(true);return response.json()}
 const snapshot=async phase=>{
  const result=await read('result'),stay=await read('stay-suggestions')
  report.phases.push({phase,result:summarize(result),stay,private_facts:privateFacts(resource),visible_main_names:await page.getByTestId('activity-card').getByRole('heading').allTextContents()})
  save();return {result,stay}
 }
 const openMap=async()=>{
  await page.getByTestId('desktop-nav-map_stay').click()
  const panel=page.getByTestId('stay-panel')
  if(!await panel.evaluate(node=>node.open))await panel.locator('summary').click()
  return panel
 }
 try{
  if(process.env.LODGING_LIVE_RESUME_DIR){
   const previous=JSON.parse(fs.readFileSync(path.join(process.env.LODGING_LIVE_RESUME_DIR,'result.json'),'utf8'))
   resource=previous.resource
   report.resume_of=process.env.LODGING_LIVE_RESUME_DIR
   await page.goto(`/trip/result#trip=${resource}`)
  }else{
   await page.goto('/')
   await page.getByTestId('trip-source-text').fill(input.input_text)
   await page.getByTestId('create-full-trip').click()
  }
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible({timeout:120000})
  resource=resource||new URLSearchParams(new URL(page.url()).hash.slice(1)).get('trip')
  expect(resource).toBeTruthy()
  report.resource=resource
  await context.storageState({path:path.join(out,'browser-state.json')})
  let initial=await snapshot('INITIAL_RECOGNITION')
  expect.soft(initial.result.is_demo).toBe(false)
  expect.soft(initial.result.days.length).toBe(5)
  expect.soft(initial.result.days.map(day=>day.activities.map(card=>normalize(card.name)))).toEqual(input.expected.source_visits.map(day=>day.map(normalize)))
  const sourceHotel=initial.result.days[0].activities.find(card=>normalize(card.name)===normalize(hotel))
  const identityRows=facts=>facts.stops.filter(stop=>!stop.generated_overnight_endpoint).map(stop=>({day:stop.day,id:stop.canonical_place_id,city:stop.city}))
  expect.soft(identityRows(report.phases[0].private_facts)).toEqual(identityRows(input.overnight_projection))
  expect.soft(sourceHotel?.status).toBe('READY')
  expect.soft(sourceHotel?.lodging_event).toBe('OVERNIGHT')
  expect.soft(sourceHotel?.lodging_scope).toBe('DAY')
  expect.soft(sourceHotel?.lodging_role_uncertain).not.toBe(true)
  await page.screenshot({path:path.join(out,'01-recognized-itinerary.png'),fullPage:true})
  let panel=await openMap()
  await expect.poll(async()=>{const stay=await read('stay-suggestions');return stay.status},{timeout:100000}).not.toBe('PREPARING')
  initial=await snapshot('INITIAL_STAY_READY')
  const nights=report.phases.at(-1).private_facts.nights.map(n=>({days:n.overnight_days,city:n.city,preserved:n.preserved_hotels,...(n.uncertain?{uncertain:true}:{})}))
  expect.soft(nights).toEqual(input.expected.nights)
  const shanghai=initial.stay.segments?.find(segment=>segment.city==='上海'&&segment.overnight_days.length===2)
  expect(shanghai?.candidates.length,'The real Shanghai segment needs verified candidates before selection').toBeGreaterThan(0)
  await expect(panel.getByTestId('stay-segment').filter({hasText:'上海'})).toContainText(initial.result.days[2].label)
  await expect(panel.getByTestId('stay-segment').filter({hasText:'上海'})).toContainText(initial.result.days[3].label)
  const refreshResponse=page.waitForResponse(response=>response.request().method()==='POST'&&new URL(response.url()).pathname.endsWith('/stay-suggestions'))
  await panel.getByTestId('retry-stay').click()
  expect((await refreshResponse).ok()).toBe(true)
  await expect.poll(async()=>(await read('stay-suggestions')).status,{timeout:100000}).not.toBe('PREPARING')
  const refreshed=await snapshot('EXPLICIT_STAY_REFRESH')
  const choice=refreshed.stay.segments.find(segment=>segment.city==='上海'&&segment.overnight_days.length===2).candidates[0]
  const article=panel.getByTestId('stay-segment').filter({hasText:'上海'}).locator('article').filter({has:page.getByRole('heading',{name:choice.name,exact:true})})
  await expect(article.getByTestId('choose-stay')).toBeEnabled({timeout:15000})
  const beforeWriteCount=report.writes.length
  const selection=page.waitForResponse(response=>response.request().method()==='POST'&&new URL(response.url()).pathname.endsWith('/stay-selection'))
  await article.getByTestId('choose-stay').click()
  expect((await selection).ok()).toBe(true)
  await expect(article.getByTestId('choose-stay')).toHaveText('已选择',{timeout:20000})
  await page.reload()
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  panel=await openMap()
  const selected=await snapshot('SELECTED_AND_RELOADED')
  expect(selected.stay.segments.find(segment=>segment.city==='上海').candidates.filter(c=>c.selected).map(c=>c.name)).toEqual([choice.name])
  expect(selected.stay.segments.find(segment=>segment.city==='北京').preserved_hotels.map(normalize)).toEqual([normalize(hotel)])
  expect(selected.result.days.map(day=>day.activities.map(card=>card.name))).toEqual(refreshed.result.days.map(day=>day.activities.map(card=>card.name)))
  expect(selected.result.map.status).toBe('NEEDS_UPDATE')
  expect(report.writes.slice(beforeWriteCount).filter(w=>['map-renders','stay-suggestions','daily-dining'].includes(w.action))).toEqual([])
  await page.screenshot({path:path.join(out,'02-selected-shanghai-nights.png'),fullPage:true})
  await page.getByTestId('desktop-nav-itinerary').click()
  const undoResponse=page.waitForResponse(response=>response.request().method()==='POST'&&new URL(response.url()).pathname.endsWith('/commands')&&response.request().postDataJSON()?.command_type==='UNDO')
  await page.getByTestId('undo-trip-command').click()
  expect((await undoResponse).ok()).toBe(true)
  await expect(page.getByTestId('undo-trip-command')).toBeDisabled({timeout:20000})
  panel=await openMap()
  const undone=await snapshot('UNDO_SELECTION')
  expect(undone.stay.segments.flatMap(segment=>segment.candidates).some(c=>c.selected)).toBe(false)
  expect(undone.stay.segments.find(segment=>segment.city==='北京').preserved_hotels.map(normalize)).toEqual([normalize(hotel)])
  expect(undone.result.days.map(day=>day.activities.map(card=>card.name))).toEqual(refreshed.result.days.map(day=>day.activities.map(card=>card.name)))
  const routesBefore=report.writes.filter(w=>w.action==='map-renders').length
  await page.getByTestId('render-map').click()
  await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status',/^(AVAILABLE|LIMITED)$/,{timeout:100000})
  expect(report.writes.filter(w=>w.action==='map-renders').length).toBe(routesBefore+1)
  const map=await read('map-renders/latest')
  const geometries=map.days.flatMap(day=>day.routes.filter(route=>route.selected_mode&&route[route.selected_mode]?.status==='AVAILABLE'&&route[route.selected_mode]?.geometry?.length>=2))
  expect(geometries.length).toBeGreaterThan(0)
  await expect(page.getByTestId('route-map')).toBeVisible()
  await page.getByRole('button',{name:initial.result.days[2].label,exact:true}).click()
  await expect(page.locator('.e-map-marker')).toHaveCount(2,{timeout:20000})
  await expect.poll(()=>page.evaluate(()=>window.__lodgingMapObservation.snapshot().some(s=>s.complete_events>0&&s.fit_count>0&&s.canvas_visible&&s.active_paths.length===1)),{timeout:45000}).toBe(true)
  await expect.poll(()=>mapResources().pending===0&&mapResources().idle_ms>=2000,{timeout:45000}).toBe(true)
  await expect.poll(()=>page.evaluate(()=>window.__lodgingMapObservation.snapshot().some(s=>{
   const extent=s.fit_calls.at(-1)?.extent
   return extent&&s.center[0]>=extent[0]&&s.center[0]<=extent[2]&&s.center[1]>=extent[1]&&s.center[1]<=extent[3]
  })),{timeout:15000}).toBe(true)
  await page.screenshot({path:path.join(out,'03-shanghai-day-map.png'),fullPage:true})
  await page.getByRole('button',{name:'全部行程',exact:true}).click()
  await snapshot('FINAL_MAP_UPDATED')
  report.map_summary={status:map.status,verified_geometry_count:geometries.length}
  expect(report.errors).toEqual([])
  report.status=test.info().errors.length?'FAILED_EXPECTATIONS':'PASSED'
 }catch(error){report.status='FAILED';report.failure={name:error.name,message:error.message};throw error}
 finally{
  if(resource)report.resource=resource
  report.finished_at=new Date().toISOString();save()
  await page.screenshot({path:path.join(out,'final-page.png'),fullPage:true}).catch(()=>{})
 }
})

test('same saved resource: selected Shanghai hotel appears on the correct overnight map and undo preserves the original hotel',async({page})=>{
 test.skip(!process.env.LODGING_LIVE_RESUME_DIR,'A previously created local test trip is required; this check never creates a new model request')
 fs.mkdirSync(out,{recursive:true})
 const previous=JSON.parse(fs.readFileSync(path.join(process.env.LODGING_LIVE_RESUME_DIR,'result.json'),'utf8'))
 const resource=previous.resource,writes=[]
 const read=async suffix=>{const response=await page.request.get(`/api/v3/trip-understandings/${resource}/${suffix}`);expect(response.ok()).toBe(true);return response.json()}
 await installMapObservation(page)
 const resources=watchMapResources(page)
 page.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname.startsWith('/api/v3/'))writes.push(new URL(r.url()).pathname.split('/').pop())})
 await page.goto(`/trip/result#trip=${resource}`)
 await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
 const original=await read('result'),originalStay=await read('stay-suggestions')
 const priorSelected=originalStay.segments.find(s=>s.city==='上海').candidates.find(c=>c.selected)
 const choice=priorSelected||originalStay.segments.find(s=>s.city==='上海').candidates.find(c=>!c.selected)
 expect(choice).toBeTruthy()
 await page.getByTestId('desktop-nav-map_stay').click()
 const panel=page.getByTestId('stay-panel')
 await panel.locator('summary').click()
 const article=panel.getByTestId('stay-segment').filter({hasText:'上海'}).locator('article').filter({has:page.getByRole('heading',{name:choice.name,exact:true})})
 if(!priorSelected)await article.getByTestId('choose-stay').click()
 await expect(article.getByTestId('choose-stay')).toHaveText('已选择',{timeout:20000})
 expect(writes.filter(w=>['map-renders','stay-suggestions','daily-dining'].includes(w))).toEqual([])
 await panel.locator('summary').click()
 if(!priorSelected)await page.getByTestId('render-map').click()
 await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status',/^(AVAILABLE|LIMITED)$/,{timeout:100000})
 const selectedMap=await read('map-renders/latest')
 const selectedLabels=[...new Set(selectedMap.lodging_points.filter(p=>p.name===choice.name).map(p=>p.day_label))].sort()
 expect(selectedLabels).toEqual(original.days.slice(2).map(day=>day.label).sort())
 await page.getByRole('button',{name:original.days[3].label,exact:true}).click()
 await expect(page.locator('.e-map-marker:not(.e-map-lodging-marker)')).toHaveCount(2)
 await expect(page.locator('.e-map-lodging-marker')).toHaveCount(1)
 await expect.poll(()=>page.evaluate(()=>window.__lodgingMapObservation.snapshot().some(s=>s.complete_events>0&&s.fit_count>0&&s.canvas_visible&&s.active_paths.length===3)),{timeout:45000}).toBe(true)
 await expect.poll(()=>resources().pending===0&&resources().idle_ms>=2000,{timeout:45000}).toBe(true)
 const readViewport=()=>page.evaluate(()=>window.__lodgingMapObservation.snapshot())
 const initialViewport=await readViewport()
 fs.writeFileSync(path.join(out,'map-viewport-before-undo.json'),JSON.stringify({initial:initialViewport},null,2))
 await expect.poll(async()=>(await readViewport()).some(s=>{
  const extent=s.fit_calls.at(-1)?.extent
  return extent&&s.center[0]>=extent[0]&&s.center[0]<=extent[2]&&s.center[1]>=extent[1]&&s.center[1]<=extent[3]
 }),{timeout:15000,message:'The real map center must enter the current day bounds after fitting'}).toBe(true)
 fs.writeFileSync(path.join(out,'map-viewport-before-undo.json'),JSON.stringify({initial:initialViewport,settled:await readViewport()},null,2))
 await page.screenshot({path:path.join(out,'selected-hotel-day-four-map.png'),fullPage:true})
 const selectedFacts=privateFacts(resource)
 await page.reload()
 await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
 const reloaded=await read('stay-suggestions')
 expect(reloaded.segments.find(s=>s.city==='上海').candidates.find(c=>c.selected).name).toBe(choice.name)
 const undoResponse=page.waitForResponse(response=>response.request().method()==='POST'&&new URL(response.url()).pathname.endsWith('/commands')&&response.request().postDataJSON()?.command_type==='UNDO')
 await page.getByTestId('undo-trip-command').click()
 expect((await undoResponse).ok()).toBe(true)
 await expect(page.getByTestId('undo-trip-command')).toBeDisabled({timeout:20000})
 const undone=await read('result'),undoneStay=await read('stay-suggestions')
 expect(undone.days.map(day=>day.activities.map(card=>card.name))).toEqual(original.days.map(day=>day.activities.map(card=>card.name)))
 expect(undoneStay.segments.find(s=>s.city==='北京').preserved_hotels.map(normalize)).toEqual([normalize(hotel)])
 expect(undoneStay.segments.flatMap(s=>s.candidates).some(c=>c.selected)).toBe(false)
 expect(undone.map.status).toBe('NEEDS_UPDATE')
 expect(writes.filter(w=>w==='map-renders')).toHaveLength(priorSelected?0:1)
 expect(writes.filter(w=>['trip-understandings','stay-suggestions','daily-dining'].includes(w))).toEqual([])
 fs.writeFileSync(path.join(out,'selected-map-readback.json'),JSON.stringify({status:'PASSED',provenance:'SAME_REAL_LOCAL_RESOURCE_NO_NEW_INFERENCE',
  resumed_after_selected_map:Boolean(priorSelected),selected_name:choice.name,selected_night_labels:original.days.slice(2,4).map(day=>day.label),selected_map_day_labels:selectedLabels,
  selected_private_facts:selectedFacts,undone_private_facts:privateFacts(resource),map_status:selectedMap.status,visible_day_four_real_paths:3,
  final_result:summarize(undone),writes,sdk_resources:resources()},null,2))
})
