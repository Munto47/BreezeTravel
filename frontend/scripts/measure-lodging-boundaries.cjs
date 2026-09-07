/* Explicit real local model/API/browser measurement. At most five new trips.
 * No mocked providers, source prompt repair, or modification of existing trips.
 */
const fs=require('node:fs'),path=require('node:path')
const {createHash,randomUUID}=require('node:crypto')
const {execFileSync}=require('node:child_process')
const {chromium,expect}=require('@playwright/test')
const root=path.resolve(__dirname,'../..')
const [outputArg,baseURL='http://127.0.0.1:3148',measurementMode='original-five']=process.argv.slice(2)
if(!['original-five','ui-settled','original-five-settled'].includes(measurementMode))throw new Error('Unknown measurement mode')
const visualSettled=measurementMode!=='original-five'
if(!outputArg || !['127.0.0.1','localhost'].includes(new URL(baseURL).hostname))throw new Error('Supply a new private output file and local experience URL')
const output=path.resolve(outputArg)
if(fs.existsSync(output))throw new Error('Preserve previous reports; choose a new output file')
const folder=output.slice(0,-path.extname(output).length)
fs.mkdirSync(folder,{recursive:true})
const hotel='汉庭酒店(北京天安门广场前门店)'
const wholeText=`北京三日游。全程两晚都住${hotel}，已经预订。\nDay1：故宫博物院、景山公园。\nDay2：天坛公园、前门大街。\nDay3：颐和园，之后回${hotel}取行李，再返程。`
const expectedWhole={day_count:3,source_lodging:[{name:hotel,event:'OVERNIGHT',scope:'WHOLE_TRIP',storage_day:1}],
  source_visits:[['故宫博物院','景山公园'],['天坛公园','前门大街'],['颐和园',hotel]],
  source_events:[{name:hotel,day:3,event:'LUGGAGE_PICKUP'}],
  map_sequence:[['故宫博物院','景山公园',hotel],[hotel,'天坛公园','前门大街',hotel],[hotel,'颐和园',hotel]],
  nights:[{days:[1],city:'北京',preserved:[hotel]},{days:[2],city:'北京',preserved:[hotel]}]}
const originalCases=[...[1,2,3].map(repeat=>({id:`whole-trip-repeat-${repeat}`,kind:'WHOLE_TRIP',repeat,text:wholeText,expected:expectedWhole})),
  {id:'first-day-checkout',kind:'CHECK_OUT',text:`北京三日游。\nDay1：上午从${hotel}退房，然后去故宫博物院、景山公园。晚上另找一家酒店入住，具体门店尚未确定。\nDay2：天坛公园、前门大街，第二晚住宿也未确定。\nDay3：颐和园、圆明园遗址公园，游览后返程。`,
    expected:{day_count:3,source_lodging:[],source_visits:[[hotel,'故宫博物院','景山公园'],['天坛公园','前门大街'],['颐和园','圆明园遗址公园']],
      source_events:[{name:hotel,day:1,event:'CHECK_OUT'}],nights:[{days:[1,2],city:'北京',preserved:[]}],
      note:'首日原酒店仅退房访问；未指定门店不造卡，两个行程内夜晚可推荐其他酒店。'}},
  {id:'beijing-shanghai-partly-booked',kind:'MULTI_CITY',text:`北京、上海五日游。\nDay1 北京：故宫博物院、景山公园，晚上入住已预订的${hotel}，只订了这一晚。\nDay2 北京：天坛公园、前门大街，今晚酒店未确定。\nDay3 上海：从北京乘高铁到上海，游览外滩、豫园，今晚酒店未确定。\nDay4 上海：上海博物馆(人民广场馆)、南京路步行街，今晚酒店未确定。\nDay5 上海：东方明珠广播电视塔、上海中心大厦，游览后返程。`,
    expected:{day_count:5,source_lodging:[],source_visits:[['故宫博物院','景山公园',hotel],['天坛公园','前门大街'],['外滩','豫园'],['上海博物馆(人民广场馆)','南京路步行街'],['东方明珠广播电视塔','上海中心大厦']],
      source_events:[{name:hotel,day:1,event:'OVERNIGHT',scope:'DAY'}],nights:[{days:[1],city:'北京',preserved:[hotel]},{days:[2],city:null,preserved:[],uncertain:true},{days:[3,4],city:'上海',preserved:[]}],
      note:'夜1保留北京原酒店，夜2末站北京/次日首站上海按当前产品边界保守待确认；夜3和夜4合并上海连续住宿段。'}}]
const cases=measurementMode==='ui-settled'?[originalCases[0],originalCases[4]]:originalCases
function fingerprint(){
  const files={}
  function walk(dir){for(const item of fs.readdirSync(dir,{withFileTypes:true})){
    const file=path.join(dir,item.name)
    if(item.isDirectory()&&!item.name.startsWith('.')&&item.name!=='__pycache__')walk(file)
    else if(item.isFile()&&/\.(py|jsonl?|md|tsx?|css)$/.test(file))files[path.relative(root,file)]=createHash('sha256').update(fs.readFileSync(file)).digest('hex')
  }}
  for(const dir of ['backend/app/trip_understanding','frontend/src/app/trip/result','frontend/src/lib'])walk(path.join(root,dir))
  for(const relative of ['frontend/src/app/experience.css','backend/app/api/trip_understandings_v3.py']){
    const file=path.join(root,relative)
    files[path.relative(root,file)]=createHash('sha256').update(fs.readFileSync(file)).digest('hex')
  }
  return files
}
function serviceSnapshot(){
  const state=JSON.parse(fs.readFileSync(path.join(root,'.local-artifacts/experience/state.json'),'utf8'))
  const build=path.join(root,'frontend/.next/BUILD_ID')
  return {api:state.processes?.api,web:state.processes?.web,web_mode:state.web_mode,
    frontend_build_id:fs.existsSync(build)?fs.readFileSync(build,'utf8').trim():null,
    api_loaded_source_hash:'NOT_EXPOSED_BY_SERVICE'}
}
const report={schema_version:'real-lodging-boundaries-v1',provenance:'REAL_BROWSER_LOCAL_MODEL_AMAP_API',
  acceptance_claim:false,measurement_mode:measurementMode,measurement_script_sha256:createHash('sha256').update(fs.readFileSync(__filename)).digest('hex'),started_at:new Date().toISOString(),base_url:baseURL,maximum_new_trips:cases.length,service_before:serviceSnapshot(),source_before:fingerprint(),cases:[],cost_cny:null,
  limitations:[measurementMode==='ui-settled'?'Two original texts from the previous five-case suite; a separate visual follow-up, not a replacement quality sample.':'Three authored semantic cases; the same whole-trip source is repeated three times. Not a quality-rate acceptance sample.',
    'The parent reports API startup before measurement; other agents may edit source files without reloading this running service. Source changes are recorded, not claimed frozen.',
    'Internal overnight projection uses this run\'s actual persisted result and real resolver receipts; it is not a mocked success.',
    'Only created anonymous test resources are read and deleted; no retained user trip or browser history is accessed.']}
if(visualSettled)report.limitations.push('Final map rendering is additionally measured. SDK observers pass every call through to the real provider implementation without adding facts or overlays.')
function save(){fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n')}
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms))
const equal=(a,b)=>JSON.stringify(a)===JSON.stringify(b)
const whole=c=>c.category==='住宿'&&c.lodging_event==='OVERNIGHT'&&c.lodging_scope==='WHOLE_TRIP'
async function installMapObservation(page){
  // Observe the actual SDK operations, never substitute an SDK or an API result.
  await page.addInitScript(()=>{
    const records=[],objects=new WeakMap(),constructors=new WeakMap(),wrapped=new WeakMap()
    const coordinates=value=>{
      if(!value)return null
      const lng=Array.isArray(value)?value[0]:typeof value.getLng==='function'?value.getLng():value.lng
      const lat=Array.isArray(value)?value[1]:typeof value.getLat==='function'?value.getLat():value.lat
      return Number.isFinite(lng)&&Number.isFinite(lat)?[lng,lat]:null
    }
    const observedOverlay=overlay=>{
      const options=objects.get(overlay)
      if(options?.kind==='Polyline')return {kind:'Polyline',path:typeof overlay.getPath==='function'?overlay.getPath().map(coordinates):options.path}
      if(options?.kind==='Marker')return {kind:'Marker',position:coordinates(typeof overlay.getPosition==='function'?overlay.getPosition():options.position),
        name:options.title,aria_label:options.content?.getAttribute?.('aria-label')||null,
        lodging:!!options.content?.classList?.contains('e-map-lodging-marker')}
      return {kind:'UNOBSERVED'}
    }
    const viewport=instance=>{
      try{
        const bounds=instance.getBounds?.()
        return {center:coordinates(instance.getCenter?.()),zoom:instance.getZoom?.()??null,
          southwest:coordinates(bounds?.getSouthWest?.()),northeast:coordinates(bounds?.getNorthEast?.())}
      }catch{return null}
    }
    window.__lodgingMapObservation={records,snapshot(){return records.filter(r=>!r.destroyed&&r.container.closest('[data-testid="route-map"]')?.getBoundingClientRect().width>0).map(r=>({
      map_instance_id:r.id,observed_at:performance.now(),viewport:viewport(r.instance),
      complete_events:r.completeEvents,last_complete_at:r.completeAt,last_fit_at:r.fitAt,fit_count:r.fitCount,
      active_paths:[...r.overlays].filter(o=>objects.get(o)?.kind==='Polyline').map(o=>observedOverlay(o).path),
      active_markers:[...r.overlays].filter(o=>objects.get(o)?.kind==='Marker').map(observedOverlay),
      last_fit_overlays:r.fitOverlays.map(observedOverlay),last_fit_options:r.fitOptions,
      canvas_visible:[...r.container.querySelectorAll('canvas')].some(c=>c.width>0&&c.height>0&&c.getBoundingClientRect().width>0),
    }))}}
    function sdkProxy(sdk){
      if(!sdk||(typeof sdk!=='object'&&typeof sdk!=='function'))return sdk
      if(wrapped.has(sdk))return wrapped.get(sdk)
      const proxy=new Proxy(sdk,{get(target,key,receiver){
        const Original=Reflect.get(target,key,receiver)
        if(!['Map','Marker','Polyline'].includes(key)||typeof Original!=='function')return Original
        if(constructors.has(Original))return constructors.get(Original)
        const Observed=new Proxy(Original,{construct(C,args){
          const instance=Reflect.construct(C,args,C)
          if(key==='Map'&&args[0] instanceof HTMLElement){
            const record={id:records.length+1,instance,container:args[0],overlays:new Set(),completeEvents:0,completeAt:0,fitAt:0,fitCount:0,fitOverlays:[],fitOptions:[],destroyed:false}
            records.push(record)
            instance.on('complete',()=>{record.completeEvents++;record.completeAt=performance.now()})
            for(const method of ['add','remove','setFitView','destroy']){
              const original=instance[method]
              instance[method]=function(...values){
                if(method==='add')for(const o of (Array.isArray(values[0])?values[0]:[values[0]]))record.overlays.add(o)
                if(method==='remove')for(const o of (Array.isArray(values[0])?values[0]:[values[0]]))record.overlays.delete(o)
                if(method==='setFitView'){record.fitCount++;record.fitAt=performance.now();record.fitOverlays=[...(values[0]||[])];record.fitOptions=values.slice(1)}
                if(method==='destroy')record.destroyed=true
                return original.apply(this,values)
              }
            }
          }else if(key==='Polyline'||key==='Marker')objects.set(instance,{kind:key,...args[0]})
          return instance
        }})
        constructors.set(Original,Observed)
        return Observed
      }})
      wrapped.set(sdk,proxy)
      return proxy
    }
    let value=window.AMap
    Object.defineProperty(window,'AMap',{configurable:true,enumerable:true,get:()=>sdkProxy(value),set:next=>{value=next}})
  })
}
function watchMapResources(page){
  const pending=new Set(),stats={started:0,finished:0,failed:0,last_activity:Date.now()}
  const relevant=request=>{const host=new URL(request.url()).hostname;return /(^|\.)(amap\.com|autonavi\.com|aoscdn\.com)$/.test(host)}
  page.on('request',request=>{if(relevant(request)){pending.add(request);stats.started++;stats.last_activity=Date.now()}})
  for(const event of ['requestfinished','requestfailed'])page.on(event,request=>{if(pending.delete(request)){
    stats[event==='requestfailed'?'failed':'finished']++;stats.last_activity=Date.now()
  }})
  return ()=>({...stats,pending:pending.size,idle_ms:Date.now()-stats.last_activity})
}
async function waitForRenderedMap(page,visibleMap,body,browserMap,resources){
  const expectedPaths=(body.days||[]).flatMap(day=>day.routes.flatMap(route=>{
    const segment=route.selected_mode&&route[route.selected_mode]
    return segment?.status==='AVAILABLE'&&segment.geometry.length>=2?[segment.geometry.map(p=>[p.longitude,p.latitude])]:[]
  }))
  const normalize=paths=>paths.map(p=>JSON.stringify(p)).sort()
  const expected=normalize(expectedPaths)
  const started=Date.now()
  const remaining=()=>Math.max(1,100000-(Date.now()-started))
  if(!expectedPaths.length)throw new Error('No confirmed route geometry is available to validate final visual rendering')
  await expect.poll(()=>browserMap(),{timeout:remaining(),message:'Page polling must receive the final route shape, independently from the measurement API read'}).toEqual(
    {status:body.status,route_count:(body.days||[]).reduce((n,d)=>n+d.routes.length,0),geometry_count:expectedPaths.length})
  // Verify the product's automatic fit; clicking the fit control here could
  // hide the exact initial-centering or late-route defect being measured.
  await expect.poll(async()=>{
    const snapshots=await page.evaluate(()=>window.__lodgingMapObservation?.snapshot()||[])
    return snapshots.some(s=>s.complete_events>0&&s.fit_count>0&&s.canvas_visible&&equal(normalize(s.active_paths),expected))
  },{timeout:remaining(),message:'Actual visible map must have completed initialization and contain every real selected route Polyline'}).toBe(true)
  await expect.poll(()=>resources().pending===0&&resources().idle_ms>=2000,{timeout:remaining(),
    message:'Wait for map resources after viewport fitting, without treating marker DOM as tile readiness'}).toBe(true)
  const allPointsFit=()=>visibleMap.locator('.e-map-marker').evaluateAll(nodes=>nodes.length>0&&nodes.every(node=>{
    const a=node.getBoundingClientRect(),map=node.closest('[data-testid="route-map"]').getBoundingClientRect()
    return a.width>0&&a.height>0&&a.left>=map.left&&a.right<=map.right&&a.top>=map.top&&a.bottom<=map.bottom
  }))
  await expect.poll(allPointsFit,{timeout:remaining(),message:'Every current confirmed and lodging marker must fit inside the visible map'}).toBe(true)
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))))
  const snapshots=await page.evaluate(()=>window.__lodgingMapObservation.snapshot())
  return {wait_ms:Date.now()-started,browser_final_map:browserMap(),expected_polyline_count:expectedPaths.length,
    observed_maps:snapshots.map(({active_paths,...s})=>({...s,active_polyline_count:active_paths.length,
      route_path_sha256:active_paths.map(p=>createHash('sha256').update(JSON.stringify(p)).digest('hex'))})),
    map_resources:resources(),all_markers_fit:true,visual_pixels_require_screenshot_review:true}
}
const sortedJSON=values=>values.map(value=>JSON.stringify(value)).sort()
function expectedScope(body,days){
  const labels=new Set(days.map(day=>day.label)),tokens=new Set(days.flatMap(day=>day.activities.map(card=>card.activity_token)))
  const valid=p=>p?.coordinate_system==='GCJ02'&&Number.isFinite(p.longitude)&&Number.isFinite(p.latitude)&&Math.abs(p.longitude)<=180&&Math.abs(p.latitude)<=90
  const visits=(body.points||[]).filter(point=>tokens.has(point.activity_token)&&valid(point.position))
  const seen=new Set()
  const lodgings=(body.lodging_points||[]).filter(point=>{
    if(!labels.has(point.day_label)||!valid(point.position))return false
    const key=JSON.stringify([point.name,point.position.longitude,point.position.latitude])
    if(seen.has(key))return false
    seen.add(key);return true
  })
  const markers=[...visits.map(p=>({kind:'Marker',position:[p.position.longitude,p.position.latitude],name:p.name,aria_label:`查看${p.name}`,lodging:false})),
    ...lodgings.map(p=>({kind:'Marker',position:[p.position.longitude,p.position.latitude],name:p.name,aria_label:`查看住宿位置 ${p.name}`,lodging:true}))]
  const paths=(body.days||[]).filter(day=>labels.has(day.label)).flatMap(day=>day.routes.flatMap(route=>{
    const segment=route.selected_mode&&route[route.selected_mode]
    return ['AVAILABLE','LIMITED'].includes(body.status)&&segment?.status==='AVAILABLE'&&segment.geometry?.length>=2&&
      segment.geometry.every(p=>Number.isFinite(p.longitude)&&Number.isFinite(p.latitude))?[segment.geometry.map(p=>[p.longitude,p.latitude])]:[]
  }))
  return {day_labels:[...labels],markers,paths,source:'CURRENT_REAL_MAP_RESPONSE_FILTERED_BY_RESULT_DAY_TOKENS_AND_LABELS'}
}
function validViewport(view){return !!view&&[view.center,view.southwest,view.northeast].every(p=>Array.isArray(p)&&p.length===2&&p.every(Number.isFinite))&&
  Number.isFinite(view.zoom)&&view.southwest[0]<view.northeast[0]&&view.southwest[1]<view.northeast[1]}
function sameViewport(a,b){return validViewport(a)&&validViewport(b)&&Math.abs(a.zoom-b.zoom)<=0.00001&&
  ['center','southwest','northeast'].every(key=>a[key].every((n,i)=>Math.abs(n-b[key][i])<=0.000001))}
function scopeFitsViewport(scope,view){return validViewport(view)&&[...scope.markers.map(m=>m.position),...scope.paths.flat()].every(p=>
  p[0]>=view.southwest[0]-0.000001&&p[0]<=view.northeast[0]+0.000001&&p[1]>=view.southwest[1]-0.000001&&p[1]<=view.northeast[1]+0.000001)}
function observedScopeMatches(snapshot,scope){return snapshot?.complete_events>0&&snapshot.fit_count>0&&snapshot.canvas_visible&&
  equal(sortedJSON(snapshot.active_markers),sortedJSON(scope.markers))&&equal(sortedJSON(snapshot.active_paths),sortedJSON(scope.paths))&&
  equal(sortedJSON(snapshot.last_fit_overlays),sortedJSON([...scope.markers,...scope.paths.map(path=>({kind:'Polyline',path}))]))}
async function measureCrossCityDayFocus(page,visibleMap,body,result,row,resources,inputId){
  const measurement={status:'STARTED',basis:'Existing real map data; date controls only. No manual fit, route refresh, SDK substitution, or source correction.',
    day_index:3,checks:{},phases:{},map_resources_before:resources(),api_writes_before:{...row.api_writes},map_api_requests_before:{...row.map_api_requests}}
  row.cross_city_day_focus=measurement;save()
  const readSnapshot=()=>page.evaluate(()=>window.__lodgingMapObservation?.snapshot()||[])
  const legend=page.getByTestId('result-view-map-stay').getByLabel('日期颜色与预演选择')
  const targetDay=result.days[2],fullScope=expectedScope(body,result.days)
  const dayScope=expectedScope(body,targetDay?[targetDay]:[])
  measurement.day_label=targetDay?.label??null
  measurement.current_ready_day_cities=[...new Set((targetDay?.activities||[]).filter(c=>c.status==='READY').map(c=>c.city))]
  measurement.checks.day3_has_only_confirmed_shanghai_city=measurement.current_ready_day_cities.length===1&&measurement.current_ready_day_cities[0]?.replace(/市$/,'')==='上海'
  measurement.expected_full_scope=fullScope;measurement.expected_day_scope=dayScope
  async function phase(name,scope,action,expectNewFit=!!action){
    const state={status:'STARTED',resources_before:resources(),api_writes_before:{...row.api_writes},map_api_requests_before:{...row.map_api_requests},viewport_samples:[]}
    measurement.phases[name]=state;save()
    const started=Date.now(),remaining=()=>Math.max(1,45000-(Date.now()-started))
    try{
      state.before=await readSnapshot()
      if(action)await action(remaining)
      if(!scope.markers.length||!scope.paths.length)throw new Error('Real scope lacks markers or available route geometry; focus is not verified')
      await expect.poll(async()=>{
        const snapshots=await readSnapshot()
        if(snapshots.length!==1)return false
        const s=snapshots[0],old=state.before.find(previous=>previous.map_instance_id===s.map_instance_id)
        return observedScopeMatches(s,scope)&&(!expectNewFit||s.fit_count>(old?.fit_count||0))
      },{timeout:remaining(),message:`${name}: real SDK markers, Polylines and automatic fit must match this exact scope`}).toBe(true)
      await expect.poll(()=>resources().pending===0&&resources().idle_ms>=2000,{timeout:remaining(),message:`${name}: actual map requests must settle`}).toBe(true)
      await expect.poll(()=>visibleMap.locator('.e-map-marker').evaluateAll(nodes=>nodes.length>0&&nodes.every(node=>{
        const a=node.getBoundingClientRect(),map=node.closest('[data-testid="route-map"]').getBoundingClientRect()
        return a.width>0&&a.height>0&&a.left>=map.left&&a.right<=map.right&&a.top>=map.top&&a.bottom<=map.bottom
      })),{timeout:remaining(),message:`${name}: every marker must fit the visible map`}).toBe(true)
      await expect.poll(async()=>{
        const snapshots=await readSnapshot(),s=snapshots.length===1?snapshots[0]:null
        if(!observedScopeMatches(s,scope)||!scopeFitsViewport(scope,s.viewport)||resources().pending||resources().idle_ms<2000){state.viewport_samples=[];return false}
        state.viewport_samples.push({observed_at:s.observed_at,map_instance_id:s.map_instance_id,fit_count:s.fit_count,viewport:s.viewport})
        state.viewport_samples=state.viewport_samples.slice(-3)
        return state.viewport_samples.length===3&&state.viewport_samples[2].observed_at-state.viewport_samples[0].observed_at>=900&&
          state.viewport_samples.every(sample=>sample.map_instance_id===s.map_instance_id&&sample.fit_count===s.fit_count&&sameViewport(sample.viewport,s.viewport))
      },{timeout:remaining(),intervals:[500],message:`${name}: fitted extent, center and zoom must stay stable across three real SDK samples`}).toBe(true)
      state.observed_maps=await readSnapshot()
      state.all_markers_and_route_geometry_fit=true;state.status='PASSED'
    }catch(error){state.status='FAILED';state.failure={type:error.name,message:String(error.message).split('\n').slice(0,3).join(' ').slice(0,350)}
      state.observed_maps=await readSnapshot().catch(()=>[])
    }finally{
      state.wait_ms=Date.now()-started;state.resources_after=resources();state.api_writes_after={...row.api_writes};state.map_api_requests_after={...row.map_api_requests}
      state.sdk_request_delta=Object.fromEntries(['started','finished','failed'].map(key=>[key,state.resources_after[key]-state.resources_before[key]]))
      state.no_api_write=equal(state.api_writes_before,state.api_writes_after)
      state.screenshot=path.join(folder,`${inputId}-focus-${name}.png`)
      await page.screenshot({path:state.screenshot,fullPage:true,timeout:5000}).catch(()=>{state.screenshot=null;state.screenshot_failed=true;state.status='FAILED'})
      save()
    }
    return state
  }
  const before=await phase('all-before',fullScope,async remaining=>{
    // Read the default selection; do not click it and repair an incorrect initial scope.
    await expect(legend.getByRole('button',{name:'全部行程',exact:true})).toHaveAttribute('aria-pressed','true',{timeout:remaining()})
  },false)
  // No new fit is expected when only checking the initial default selection.
  // The two actual selection actions below are independently observed, including restoration after a failed Day 3 check.
  const day=await phase('day3',dayScope,async remaining=>{
    if(!targetDay)throw new Error('Day 3 is absent from the actual result')
    const button=legend.getByRole('button',{name:targetDay.label,exact:true})
    await button.click({timeout:remaining()});await expect(button).toHaveAttribute('aria-pressed','true',{timeout:remaining()})
  })
  const restored=await phase('all-restored',fullScope,async remaining=>{
    const button=legend.getByRole('button',{name:'全部行程',exact:true})
    await button.click({timeout:remaining()});await expect(button).toHaveAttribute('aria-pressed','true',{timeout:remaining()})
  })
  const a=before.observed_maps?.[0]?.viewport,d=day.observed_maps?.[0]?.viewport,b=restored.observed_maps?.[0]?.viewport
  measurement.checks.day3_extent_is_narrower_than_full=validViewport(a)&&validViewport(d)&&d.northeast[0]-d.southwest[0]<a.northeast[0]-a.southwest[0]&&
    d.northeast[1]-d.southwest[1]<a.northeast[1]-a.southwest[1]
  measurement.checks.restored_extent_matches_original=sameViewport(a,b)
  measurement.map_resources_after=resources()
  measurement.sdk_request_delta=Object.fromEntries(['started','finished','failed'].map(key=>[key,measurement.map_resources_after[key]-measurement.map_resources_before[key]]))
  measurement.api_writes_after={...row.api_writes};measurement.map_api_requests_after={...row.map_api_requests}
  measurement.checks.no_api_write=equal(measurement.api_writes_before,measurement.api_writes_after)
  measurement.checks.no_route_update_request=(measurement.map_api_requests_after.POST||0)===(measurement.map_api_requests_before.POST||0)
  measurement.checks.all_three_phases_passed=Object.values(measurement.phases).every(p=>p.status==='PASSED'&&p.no_api_write)
  measurement.status=Object.values(measurement.checks).every(Boolean)?'PASSED':'FAILED'
  row.ui_checks.cross_city_day_focus_and_restore=measurement.status==='PASSED';save()
}
function summarizeMap(body){return {status:body.status,message:body.message,points:(body.points||[]).map(p=>({day:p.day_label,sequence:p.sequence_index,name:p.name,position:p.position})),
  lodging_points:(body.lodging_points||[]).map(p=>({day:p.day_label,name:p.name,position:p.position})),
  days:(body.days||[]).map(d=>({label:d.label,routes:d.routes.map(e=>({from:e.from_name,to:e.to_name,selected_mode:e.selected_mode,
    walking:{status:e.walking.status,minutes:e.walking.duration_minutes,geometry_points:e.walking.geometry.length},
    transit:{status:e.transit.status,minutes:e.transit.duration_minutes,geometry_points:e.transit.geometry.length}}))}))}}
async function main(){
  const browser=await chromium.launch({headless:true})
  try{for(const [index,input] of cases.entries()){
    const row={id:input.id,kind:input.kind,repeat:input.repeat,input_text:input.text,expected:input.expected,status:'STARTED',started_at:new Date().toISOString(),checks:{},ui_checks:{},api_writes:{},map_api_requests:{},api_errors:[]}
    report.cases.push(row);save()
    const width=index===1?390:1440
    const context=await browser.newContext({baseURL,viewport:{width,height:950}}),page=await context.newPage()
    if(visualSettled)await installMapObservation(page)
    const mapResources=watchMapResources(page)
    let browserFinalMap=null
    let resource=null
    page.on('request',request=>{const u=new URL(request.url());
      if(u.origin===new URL(baseURL).origin&&u.pathname.startsWith('/api/v3/')&&/\/map-renders(?:\/latest)?$/.test(u.pathname))row.map_api_requests[request.method()]=(row.map_api_requests[request.method()]||0)+1
      if(u.origin===new URL(baseURL).origin&&u.pathname.startsWith('/api/')&&['POST','PUT','DELETE','PATCH'].includes(request.method())){
      const name=u.pathname.split('/').pop();row.api_writes[name]=(row.api_writes[name]||0)+1}})
    page.on('response',async response=>{const u=new URL(response.url());if(u.origin===new URL(baseURL).origin&&u.pathname.startsWith('/api/v3/')&&response.status()>=400)row.api_errors.push({action:u.pathname.split('/').pop(),status:response.status()})
      if(u.origin===new URL(baseURL).origin&&u.pathname.endsWith('/map-renders/latest')&&response.status()===200){try{
        const body=await response.json()
        browserFinalMap={status:body.status,route_count:(body.days||[]).reduce((n,d)=>n+d.routes.length,0),
          geometry_count:(body.days||[]).flatMap(d=>d.routes).filter(r=>r.selected_mode&&r[r.selected_mode]?.status==='AVAILABLE'&&r[r.selected_mode].geometry?.length>=2).length}
      }catch{}}
    })
    const read=async suffix=>{const response=await page.request.get(`/api/v3/trip-understandings/${resource}${suffix}`);return {status:response.status(),body:await response.json()}}
    const settled=async suffix=>{const until=Date.now()+120000;while(Date.now()<until){const r=await read(suffix);if(r.status===200&&!['PROCESSING','PREPARING'].includes(r.body.status))return r;if(![200,202].includes(r.status))throw new Error(`READ_${suffix.replace(/\W/g,'_')}_${r.status}`);await sleep(1000)}throw new Error(`TIMEOUT_${suffix.replace(/\W/g,'_')}`)}
    try{
      await page.goto('/')
      await page.getByTestId('trip-source-text').fill(input.text)
      const started=Date.now()
      await page.getByTestId('create-full-trip').click()
      await page.waitForURL(/\/trip\/result#trip=/,{timeout:25000})
      resource=new URL(page.url()).hash.slice('#trip='.length)
      row.accepted_ms=Date.now()-started
      const result=await settled('/result')
      row.final_result_ms=Date.now()-started
      const cards=result.body.days.flatMap((d,i)=>d.activities.map(c=>({...c,day:i+1})))
      row.coverage=result.body.coverage
      row.actual_days=result.body.days.map((d,i)=>({day:i+1,label:d.label,activities:d.activities.map(c=>({name:c.name,city:c.city,category:c.category,status:c.status,lodging_event:c.lodging_event,lodging_scope:c.lodging_scope,lodging_role_uncertain:c.lodging_role_uncertain,lodging_excluded_nights:c.lodging_excluded_nights}))}))
      const sourceLodgings=cards.filter(whole),sourceVisits=result.body.days.map(d=>d.activities.filter(c=>!whole(c)).map(c=>c.name))
      row.checks.day_count=result.body.days.length===input.expected.day_count
      row.checks.source_lodging_metadata=input.expected.source_lodging.every(e=>sourceLodgings.some(c=>c.name===e.name&&c.lodging_event===e.event&&c.lodging_scope===e.scope&&c.day===e.storage_day))&&sourceLodgings.length===input.expected.source_lodging.length
      row.checks.source_events=input.expected.source_events.every(e=>cards.some(c=>c.name===e.name&&c.day===e.day&&c.lodging_event===e.event&&(!e.scope||c.lodging_scope===e.scope)))
      row.checks.source_visit_days=equal(sourceVisits,input.expected.source_visits)
      const ready=cards.filter(c=>c.status==='READY')
      await expect(page.getByTestId('activity-card')).toHaveCount(ready.filter(c=>!whole(c)).length,{timeout:20000})
      await expect(page.getByTestId('source-lodging-card')).toHaveCount(ready.filter(whole).length,{timeout:20000})
      row.ui_checks.visible_confirmed_cards_match=true
      row.visible_main_names=await page.getByTestId('activity-card').getByRole('heading').allTextContents()
      row.visible_source_lodging_names=await page.getByTestId('source-lodging-card').getByRole('heading').allTextContents()
      await page.screenshot({path:path.join(folder,`${input.id}-cards.png`),fullPage:true})
      const [stay,map]=await Promise.all([settled('/stay-suggestions'),settled('/map-renders/latest')])
      row.enhancements_ms=Date.now()-started
      row.stay={status:stay.body.status,message:stay.body.message,segments:(stay.body.segments||[]).map(s=>({city:s.city,days:s.overnight_days,status:s.status,message:s.message,
        expected_boundary_count:s.expected_boundary_count,missing_boundary_count:s.missing_boundary_count,preserved_hotels:s.preserved_hotels,
        candidates:s.candidates.map(c=>({name:c.name,brand:c.brand,address:c.area_or_address,commute_summary:c.commute_summary,brand_note:c.brand_note}))}))}
      row.map=summarizeMap(map.body)
      await page.getByTestId(`${width<1024?'mobile':'desktop'}-nav-map_stay`).click()
      const visibleMap=page.getByTestId('result-view-map-stay').getByTestId('route-map')
      const tokens=new Set(ready.filter(c=>!whole(c)).map(c=>c.activity_token))
      const expectedPoints=(map.body.points||[]).filter(p=>p.position&&tokens.has(p.activity_token))
      row.ui_checks.current_points_match_ready_source_visits=expectedPoints.length===ready.filter(c=>!whole(c)).length
      try{
        const visitMarkers=visibleMap.locator('.e-map-marker:not(.e-map-lodging-marker)')
        await expect(visitMarkers).toHaveCount(expectedPoints.length,{timeout:20000})
        await expect(visibleMap.getByRole('button',{name:'查看所有地点'})).toBeVisible({timeout:20000})
        if(expectedPoints.length)await expect(visitMarkers.first()).toBeInViewport({timeout:15000})
        row.marker_labels=await visitMarkers.evaluateAll(items=>items.map(item=>item.getAttribute('aria-label')).sort())
        row.ui_checks.markers_match_current_points=expectedPoints.length>0&&equal(row.marker_labels,expectedPoints.map(p=>`查看${p.name}`).sort())
        const distinctLodgings=new Set((map.body.lodging_points||[]).map(p=>JSON.stringify([p.name,p.position.longitude,p.position.latitude])))
        await expect(visibleMap.locator('.e-map-lodging-marker')).toHaveCount(distinctLodgings.size,{timeout:20000})
        row.lodging_marker_labels=await visibleMap.locator('.e-map-lodging-marker').evaluateAll(items=>items.map(item=>item.getAttribute('aria-label')).sort())
        row.ui_checks.lodging_markers_match_selected_anchors=true
      }catch(error){row.ui_checks.markers_match_current_points=false;row.map_ui_failure={type:error.name,message:String(error.message).split('\n').slice(0,3).join(' ').slice(0,300)}}
      if(visualSettled){
        try{row.rendered_map=await waitForRenderedMap(page,visibleMap,map.body,()=>browserFinalMap,mapResources);row.ui_checks.final_route_overlays_and_all_points_fit=true}
        catch(error){row.ui_checks.final_route_overlays_and_all_points_fit=false;row.rendered_map_failure={type:error.name,message:String(error.message).split('\n').slice(0,3).join(' ').slice(0,350),browser_final_map:browserFinalMap,map_resources:mapResources()}}
      }
      await page.screenshot({path:path.join(folder,`${input.id}-map.png`),fullPage:true})
      if(visualSettled&&input.kind==='MULTI_CITY')await measureCrossCityDayFocus(page,visibleMap,map.body,result.body,row,mapResources,input.id)
      row.ui_checks.no_manual_route_post=(row.api_writes['map-renders']||0)===0
      row.status='COLLECTED'
    }catch(error){row.status='FAILED';row.failure={type:error.name,message:String(error.message).split('\n').slice(0,3).join(' ').slice(0,350)};await page.screenshot({path:path.join(folder,`${input.id}-failure.png`),fullPage:true}).catch(()=>{})}
    finally{
      if(resource){
        for(const [key,script] of [['worker_metrics','read_private_measurement.py'],['overnight_projection','read_private_lodging_measurement.py']]){
          try{row[key]=JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON||'D:\\CODEX\\BreezeTravel\\.venv\\Scripts\\python.exe',[path.join(root,'scripts',script),resource],{cwd:root,encoding:'utf8',timeout:20000,windowsHide:true}))}catch{row[key]={status:'READ_FAILED'}}
        }
        if(row.overnight_projection.status==='READ'){
          const p=row.overnight_projection
          row.checks.night_segments=equal(p.nights.map(n=>({days:n.overnight_days,city:n.city,preserved:n.preserved_hotels,...(n.uncertain?{uncertain:true}:{})})),input.expected.nights)
          if(input.expected.map_sequence)row.checks.projected_map_sequence=equal(Array.from({length:input.expected.day_count},(_,i)=>p.stops.filter(s=>s.day===i+1).map(s=>s.name)),input.expected.map_sequence)
          if(input.kind==='CHECK_OUT'){
            const old=p.stops.find(s=>s.name===hotel&&s.day===1)
            const id=old?.canonical_place_id?.replace(/^amap:/,'')
            row.checks.explicit_other_hotel_night=equal(old?.lodging_excluded_nights,[1])
            row.checks.old_branch_excluded_from_recommendations=!!id&&p.nights.some(n=>n.overnight_days.includes(1)&&n.excluded_place_ids.includes(id))
              &&p.stay_candidates.length>0&&p.stay_candidates.every(c=>c.canonical_place_id.replace(/^amap:/,'')!==id)
          }
        }
        const response=await page.request.delete(`/api/v3/trip-understandings/${resource}`,{headers:{'Idempotency-Key':randomUUID()}}).catch(()=>null)
        row.cleanup_status=response?.status()??'NETWORK_ERROR'
      }
      row.finished_at=new Date().toISOString()
      row.semantic_expectations_passed=Object.values(row.checks).length>0&&Object.values(row.checks).every(Boolean)
      await context.close();save()
      console.log(JSON.stringify({id:row.id,status:row.status,checks:row.checks,ui_checks:row.ui_checks,cleanup:row.cleanup_status}))
    }
  }}finally{await browser.close()}
  report.source_after=fingerprint()
  report.service_after=serviceSnapshot()
  report.service_identity_unchanged=equal(report.service_before,report.service_after)
  report.changed_source_files=Object.keys({...report.source_before,...report.source_after}).filter(file=>report.source_before[file]!==report.source_after[file])
  report.source_unchanged=report.changed_source_files.length===0
  report.finished_at=new Date().toISOString()
  report.summary={requested_cases:cases.length,collected:report.cases.filter(c=>c.status==='COLLECTED').length,semantic_passes:report.cases.filter(c=>c.semantic_expectations_passed).length,
    all_cases_cleaned:report.cases.every(c=>c.cleanup_status===204||c.cleanup_status===200),acceptance_claim:false}
  save()
}
main().catch(error=>{report.fatal={type:error.name};save();console.error(error.name);process.exitCode=1})
