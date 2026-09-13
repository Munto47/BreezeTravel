// Fixed public API and map SDK only. No model, supplier, account or business DB.
// Deliberately retain missing connection fields in legacy fixtures.
const {test, expect} = require('@playwright/test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const exportsForPresentation = {}
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.resolve(__dirname,
  '../src/app/trip/result/result-presentation.ts'), 'utf8'), {compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
}}).outputText, {exports: exportsForPresentation})
const p = exportsForPresentation
const R = 'fixed-connection-scope-desktop', E = '"fixed-connection-v1"'
const warning = '起终点衔接未核实'
const card = (name, i) => ({activity_token: `fixed-route-card-${String(i).padStart(20, '0')}`, name,
  city: '北京', category: '景点', status: 'READY', area_or_address: '固定展示地址', source_details: [],
  available_actions: ['VIEW_DETAILS','REPLACE','DELETE','MOVE']})
const mode = (minutes, meters, connection) => ({status: 'AVAILABLE', duration_minutes: minutes,
  distance_meters: meters, transfer_count: 0, geometry: [{longitude:116.397,latitude:39.923},
    {longitude:116.398,latitude:39.924}], ...(connection ? {connection_status: connection,geometry_break_indices:[]} : {})})
function fixture() {
  const days = [{label: 'Day 1', activities: ['故宫博物院','景山公园',...Array.from({length:5},(_,i)=>`固定展示地点${i+3}`)].map((n,i)=>card(n,i))},
    {label:'Day 2', activities:[card('天坛公园',10),card('前门大街',11)]},
    {label:'Day 3', activities:[card('固定已衔接甲园',20),card('固定已衔接乙园',21)]}]
  days[0].activities[6].name='固定展示的超长中文场馆全名与院区分馆名称保留验证'
  const stay = {status:'UNAVAILABLE',message:'固定未准备住宿',candidates:[],available_actions:[]}
  const map = {status:'AVAILABLE', message:'固定已返回路段', available_actions:[],
    points:days.flatMap((day,i)=>day.activities.map((c,j)=>({activity_token:c.activity_token,name:c.name,
      day_label:day.label,sequence_index:j,position:{longitude:116.39+i*.01+j*.001,
        latitude:39.92+i*.01+j*.001,coordinate_system:'GCJ02'}}))),
    days:days.map((day,i)=>({label:day.label,day_index:i+1,routes:day.activities.slice(0,-1).map((c,j)=>({
      from_activity_token:c.activity_token,to_activity_token:day.activities[j+1].activity_token,
      from_name:c.name,to_name:day.activities[j+1].name,selected_mode:'walking',message:'固定返回折线',
      walking:i===2?mode(9,800,'VERIFIED'):i===0&&j===0?mode(2,137,'UNVERIFIED'):mode(10,650),
      transit:{status:'UNAVAILABLE',duration_minutes:null,distance_meters:null,geometry:[]},
    }))}))}
  // Formatting boundary only: a large finite distance makes a multi-line turn
  // label. It is synthetic, not a claim that this Beijing route is that long.
  map.days[0].routes[5].walking.distance_meters=123456789012
  const scopes = [undefined,'RETURNED_SEGMENTS','REQUESTED_POINTS']
  const dining = {status:'AVAILABLE',message:'固定展示候选',days:days.map((day,i)=>({
    day_index:i+1,label:day.label,status:'AVAILABLE',message:'固定候选，选择后才加入',meal_role:'LUNCH',
    after_activity_token:day.activities[0].activity_token,next_name:day.activities[1].name,
    candidates:[{candidate_token:`fixed-candidate-${i}`,name:`固定餐厅${i+1}`,area_or_address:'固定门店地址',
      extra_minutes:i===0?null:6,route_coverage_scope:scopes[i],recommended:true,
      reason:i===1?'已返回路段比较，经此店约多6分钟；未含未核实衔接，营业情况请到店前确认。':
        `经此店前往下一站约多${i===0?29:6}分钟；这家店提供家常菜，营业情况请到店前确认。`}] } ))}
  const result = {status:'READY',ownership:'ANONYMOUS',is_demo:false,can_undo:false,
    assumptions:[{key:'destination',label:'目的地',value:'北京',editable:true}],days,map,stay,
    available_actions:['EDIT_ASSUMPTIONS','EDIT_CARDS']}
  return {result,map,dining,stay}
}

test('connection helpers preserve measurements and geometry but reject legacy complete-day totals', () => {
  const {result,map} = fixture(), before = JSON.stringify(map)
  expect(p.routeModeSummary('walking',map.days[0].routes[0].walking)).toBe(`步行 · 2 分钟 · 137 米 · ${warning}`)
  expect(p.routeModeSummary('walking',map.days[1].routes[0].walking)).toContain(warning)
  expect(p.routeModeSummary('walking',map.days[2].routes[0].walking)).toBe('步行 · 9 分钟 · 800 米')
  expect(p.dayRouteSummary(result.days[0],map)).toBe(`已核实衔接 0/6 段 · ${warning} · 暂无完整日合计`)
  expect(p.dayRouteSummary(result.days[1],map)).toContain('暂无完整日合计')
  expect(p.dayRouteSummary(result.days[2],map)).toBe('步行 800 米 · 9 分钟')
  const geometry = p.routeGeometrySegments(map,'walking')
  expect(geometry).toHaveLength(2)
  expect(geometry[0].points).toEqual(map.days[0].routes[0].walking.geometry)
  expect(JSON.stringify(map)).toBe(before)
})

test('connection helpers split actual returned parts and never infer boundaries for old flattened lines', () => {
  const data=mode(2,137,'UNVERIFIED')
  data.geometry.push({longitude:116.400001,latitude:39.925},{longitude:116.4001,latitude:39.926})
  data.geometry_break_indices=[2]
  expect(p.routeGeometryParts(data)).toEqual([data.geometry.slice(0,2),data.geometry.slice(2)])
  expect(p.routeGeometryNotice(data)).toBe('路线分段展示，片段之间尚未衔接')
  for(const breaks of [undefined,null,[0],[4],[2,2],[2,1],[1.5]]){
    expect(p.routeGeometryParts({...data,geometry_break_indices:breaks})).toBeNull()
  }
  const legacy=mode(10,650)
  expect(p.routeModeSummary('walking',legacy)).toContain('原路线分段尚未核实，请更新路线')
  expect(p.routeModeSummary('walking',{...legacy,connection_status:'VERIFIED'})).toContain(warning)
  expect(legacy.geometry).toHaveLength(2)
})

test('connection helpers distinguish local comparison and strip only obsolete generated dining arithmetic', () => {
  const {dining} = fixture()
  const old = p.diningCandidateReason(dining.days[0].candidates[0])
  expect(old).toBe('这家店提供家常菜，营业情况请到店前确认。绕路时间尚未确认。')
  expect(old).not.toMatch(/29|约多/)
  const local = p.diningCandidateReason(dining.days[1].candidates[0])
  expect(local).toBe('已返回路段比较约多 6 分钟，未含未核实衔接。营业情况请到店前确认。')
  expect(p.diningCandidateReason(dining.days[2].candidates[0])).toContain('约多6分钟')
  expect(p.diningCandidateReason({reason:'“多29分钟咖啡”提供饮品。',extra_minutes:null})).toContain('“多29分钟咖啡”提供饮品。')
  expect(p.diningCandidateReason({reason:'附近门店；绕路时间及营业情况尚未确认。'})).toBe('附近门店；绕路时间及营业情况尚未确认。')
  expect(p.routeComparisonPresentation(undefined,29)).toEqual({comparable:false,
    heading:'路段比较范围尚未核实',note:'暂不能判断节省，请重新比较；行程没有改动。'})
  expect(p.routeComparisonPresentation('RETURNED_SEGMENTS',6).heading).toBe('已返回路段比较少 6 分钟')
  expect(p.routeComparisonPresentation('REQUESTED_POINTS',6).heading).toBe('变化路段可节省 6 分钟')
})

async function show(page, width, data = fixture()) {
  const state = {...data,writes:[],compares:0}
  await page.setViewportSize({width,height:1000})
  await page.emulateMedia({reducedMotion:'reduce'})
  await page.addInitScript(() => {
    window.fixedMapLines=[];window.fixedSimulationPositions=[];window.routeExportText=[];window.routeExportCards=[]
    const original=CanvasRenderingContext2D.prototype.fillText
    CanvasRenderingContext2D.prototype.fillText=function(text,x,y,...rest) {
      const point=this.getTransform().transformPoint({x,y}),width=this.measureText(String(text)).width
      window.routeExportText.push({text:String(text),x:point.x,y:point.y,width,align:this.textAlign,font:this.font,
        canvasWidth:this.canvas.width,canvasHeight:this.canvas.height})
      return original.call(this,text,x,y,...rest)
    }
    const originalRoundRect=CanvasRenderingContext2D.prototype.roundRect
    CanvasRenderingContext2D.prototype.roundRect=function(x,y,width,height,...rest){
      if(this.canvas.width===1440&&width<220&&height>100){
        const point=this.getTransform().transformPoint({x,y})
        window.routeExportCards.push({x:point.x,y:point.y,width,height})
      }
      return originalRoundRect.call(this,x,y,width,height,...rest)
    }
    window.AMap={Map:class{constructor(container){this.container=container}
      on(name,fn){if(name==='complete')setTimeout(fn,0)}
      add(items){if(items.some(x=>x.path)||items.filter(x=>x.content).length>1)window.fixedMapLines=items.filter(x=>x.path).map(x=>x.path);items.filter(x=>x.content).forEach((x,i)=>{
        if(x.content.className==='e-map-simulation-marker')window.fixedSimulationPositions.push(x.position)
        Object.assign(x.content.style,{position:'absolute',left:`${20+i*4}%`,top:'40%'});this.container.append(x.content)})}
      remove(items){items.forEach(x=>x.content?.remove())}setFitView(){}setCenter(){}resize(){}destroy(){}zoomIn(){}zoomOut(){}
    },Marker:class{constructor(x){Object.assign(this,x)}},Polyline:class{constructor(x){Object.assign(this,x)}}}
  })
  await page.route('**/*',route=>{
    const request=route.request(),url=new URL(request.url())
    if(!['localhost','127.0.0.1'].includes(url.hostname))return route.abort()
    if(!url.pathname.startsWith('/api/'))return route.fallback()
    if(request.method()!=='GET')state.writes.push(url.pathname)
    const reply=json=>route.fulfill({json,headers:{ETag:E}})
    if(url.pathname==='/api/user/me')return route.fulfill({status:401,json:{}})
    if(url.pathname.endsWith('/result'))return reply(data.result)
    if(url.pathname.endsWith('/map-renders/latest'))return reply(data.map)
    if(url.pathname.endsWith('/daily-dining'))return reply(data.dining)
    if(url.pathname.endsWith('/stay-suggestions'))return reply(data.stay)
    if(url.pathname.endsWith('/supplementary'))return reply({status:'AVAILABLE',days:[]})
    if(url.pathname.endsWith('/materialize'))return reply({status:'READY',message:'固定检查',calendar:'相对日序',party_size:2,checks_available:true})
    if(url.pathname.endsWith('/checks'))return reply({status:'STILL_NEEDS_CONFIRMATION',message:'固定检查',items:[],remaining_must_adjust:0,available_actions:[]})
    if(url.pathname.endsWith('/changes/preview')){
      state.compares++
      return reply({kind:'RELATIVE_ORDER',status:'AVAILABLE',message:'固定比较范围兼容验证',day_index:1,
        options:[undefined,'RETURNED_SEGMENTS','REQUESTED_POINTS'].map((scope,i)=>({kind:'RELATIVE_ORDER',change_token:`fixed-preview-${i}`,day_index:1,
          title:'调整相邻地点顺序',summary:'固定变化路段比较',before:['故宫博物院','景山公园'],after:['景山公园','故宫博物院'],
          duration_minutes_before:29,duration_minutes_after:23,minutes_saved:6,distance_meters_before:1200,distance_meters_after:900,
          comparison_scope:'CHANGED_EDGES_ONLY',route_coverage_scope:scope,
          routes_before:[{from_name:'故宫博物院',to_name:'景山公园',mode:'walking',duration_minutes:29,distance_meters:1200}],
          routes_after:[{from_name:'景山公园',to_name:'故宫博物院',mode:'walking',duration_minutes:23,distance_meters:900}],
        }))})
    }
    return route.fulfill({status:404,json:{}})
  })
  await page.goto(`/trip/result#trip=${R}`)
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  return state
}

test('desktop fixed routes retain geometry and PNG facts without claiming complete-day coverage',async({page},info)=>{
  const state=await show(page,1440)
  const lane=page.getByTestId('day-lane-1')
  await expect(lane.locator('.four-day-route-summary')).toContainText('暂无完整日合计')
  await expect(page.getByTestId('day-lane-2').locator('.four-day-route-summary')).toContainText(warning)
  await expect(page.getByTestId('day-lane-3').locator('.four-day-route-summary')).toHaveText('步行 800 米 · 9 分钟')
  await expect(lane.getByTestId('transport-connector').first()).toContainText('2 分钟 · 137 米')
  await expect(lane.getByTestId('transport-connector').first()).toContainText(warning)
  await expect(lane.locator('[data-turn="true"] .route-connection-warning').first()).toBeVisible()
  const turnBox=await lane.locator('[data-turn="true"] .serpentine-route-label').first().boundingBox()
  for(const c of await lane.getByTestId('activity-card').all()){
    const b=await c.boundingBox()
    expect(turnBox.x<b.x+b.width&&turnBox.x+turnBox.width>b.x&&turnBox.y<b.y+b.height&&turnBox.y+turnBox.height>b.y).toBe(false)
  }
  const meal=page.getByTestId('daily-meal-card').first()
  await meal.getByRole('button',{name:'查看 1 家候选'}).click()
  await expect(meal).toContainText('这家店提供家常菜')
  await expect(meal).toContainText('绕路时间尚未确认')
  await expect(meal).not.toContainText(/约多29|建议优先比较/)
  await page.screenshot({path:info.outputPath('unknown-route-cards-and-dining-1440.png'),fullPage:true})
  await page.getByTestId('desktop-nav-map_stay').click()
  await expect.poll(()=>page.evaluate(()=>window.fixedMapLines.length)).toBe(2)
  expect(await page.evaluate(()=>window.fixedMapLines[0])).toEqual([[116.397,39.923],[116.398,39.924]])
  await page.getByTestId('map-theater').locator('summary').filter({hasText:/^路线$/}).click()
  const routes=page.getByTestId('map-route-summary')
  await expect(routes.first()).toContainText('2 分钟 · 137 米')
  await expect(routes.first()).toContainText(warning)
  await expect(routes.last()).not.toContainText(warning)
  await page.screenshot({path:info.outputPath('returned-geometry-with-unknown-access-1440.png')})
  await page.getByTestId('desktop-nav-itinerary').click()
  await page.getByTestId('export-itinerary-png').click()
  const image=page.getByAltText('行程横链导出预览',{exact:true})
  await expect(image).toBeVisible()
  await expect.poll(()=>image.evaluate(img=>img.naturalWidth)).toBe(1440)
  const downloading=page.waitForEvent('download')
  await page.getByTestId('download-itinerary-png').click()
  const download=await downloading
  expect(await download.failure()).toBeNull()
  await download.saveAs(info.outputPath('route-connection-full.png'))
  const drawn=await page.evaluate(()=>window.routeExportText), text=drawn.map(x=>x.text).join('')
  for(const day of state.result.days){expect(text).toContain(day.label);for(const c of day.activities)expect(text).toContain(c.name)}
  expect(text).toContain('137 米')
  expect(text).toContain(warning)
  expect(text).not.toContain('完整日合计')
  for(const line of drawn){const x=line.x-(line.align==='center'?line.width/2:line.align==='right'?line.width:0)
    expect(x,line.text).toBeGreaterThanOrEqual(0);expect(x+line.width,line.text).toBeLessThanOrEqual(line.canvasWidth)
    expect(line.y,line.text).toBeGreaterThan(0);expect(line.y,line.text).toBeLessThan(line.canvasHeight)}
  const boxes=await page.evaluate(()=>window.routeExportCards)
  expect(boxes).toHaveLength(11)
  for(const line of drawn.filter(line=>line.font.startsWith('400 11px'))){
    const x=line.x-line.width/2
    for(const b of boxes)expect(x<b.x+b.width&&x+line.width>b.x&&line.y-11<b.y+b.height&&line.y+3>b.y,line.text).toBe(false)
  }
  await info.attach('fixed-public-and-canvas',{body:JSON.stringify({scope:'FIXED_PUBLIC_AND_SDK_NO_REAL_HTTP',map:state.map,drawn}),contentType:'application/json'})
  expect(state.writes.filter(url=>!url.endsWith('/materialize'))).toEqual([])
})

test('desktop scopes retain local differences while legacy comparison cannot claim savings or adopt',async({page},info)=>{
  await show(page,1280)
  await page.getByTestId('journey-suggestions-toggle').click()
  await page.getByRole('button',{name:'顺路优化',exact:true}).click()
  await page.getByRole('button',{name:'比较当天顺路方案',exact:true}).click()
  const options=page.getByTestId('relative-route-option')
  await expect(options).toHaveCount(3)
  await expect(options.nth(0)).toContainText('路段比较范围尚未核实')
  await expect(options.nth(0)).not.toContainText(/节省 6|29 → 23/)
  await expect(options.nth(0).getByRole('button',{name:'选择这个方案'})).toBeDisabled()
  await expect(options.nth(1)).toContainText('已返回路段比较少 6 分钟')
  await expect(options.nth(1)).toContainText('未含未核实衔接')
  await expect(options.nth(1)).not.toContainText('可节省')
  await expect(options.nth(1).getByRole('button',{name:'选择这个方案'})).toBeEnabled()
  await expect(options.nth(2)).toContainText('变化路段可节省 6 分钟')
  await expect(options.nth(2)).toContainText('不是全天交通总时长')
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
  await options.nth(1).scrollIntoViewIfNeeded()
  await page.screenshot({path:info.outputPath('relative-returned-segments-scope-1280.png')})
})

test('desktop draws returned fragments separately and playback stops before an unreturned bridge',async({page},info)=>{
  const data=fixture(),route=data.map.days[0].routes[0]
  route.walking.geometry=[{longitude:116.397,latitude:39.923},{longitude:116.398,latitude:39.924},
    {longitude:116.400001,latitude:39.925},{longitude:116.4001,latitude:39.926}]
  route.walking.geometry_break_indices=[2]
  const state=await show(page,1440,data)
  await page.getByTestId('desktop-nav-map_stay').click()
  const line=route.walking.geometry.map(p=>[p.longitude,p.latitude])
  await expect.poll(()=>page.evaluate(()=>window.fixedMapLines)).toEqual([line.slice(0,2),line.slice(2),[[116.397,39.923],[116.398,39.924]]])
  await page.getByTestId('map-theater').locator('summary').filter({hasText:/^路线$/}).click()
  const playback=page.getByTestId('route-playback')
  await expect(playback).toContainText('路线分段展示，片段之间尚未衔接')
  await playback.getByRole('button',{name:'播放',exact:true}).click()
  await expect(playback.getByRole('button',{name:'播放下一片段',exact:true})).toBeVisible()
  await expect(playback).toContainText('本片段已结束')
  const atBreak=await page.evaluate(()=>window.fixedSimulationPositions)
  expect(atBreak).toContainEqual(line[0]);expect(atBreak).toContainEqual(line[1]);expect(atBreak).not.toContainEqual(line[2])
  await page.waitForTimeout(800)
  expect(await page.evaluate(()=>window.fixedSimulationPositions)).toEqual(atBreak)
  await page.screenshot({path:info.outputPath('fragment-boundary-paused-1440.png')})
  await playback.getByRole('button',{name:'播放下一片段',exact:true}).click()
  await expect.poll(()=>page.evaluate(()=>window.fixedSimulationPositions.some(p=>p[0]===116.400001))).toBe(true)
  await page.getByLabel('日期颜色与预演选择').getByRole('button',{name:'Day 2',exact:true}).click()
  await expect.poll(()=>page.evaluate(()=>window.fixedMapLines)).toEqual([])
  await expect(page.locator('.e-map-marker')).toHaveCount(2)
  await expect(playback).toContainText('原路线分段尚未核实，请更新路线')
  await expect(playback.getByRole('button',{name:'播放',exact:true})).toBeDisabled()
  await expect(page.getByTestId('map-route-summary')).toContainText('10 分钟 · 650 米')
  await expect(page.getByTestId('map-route-summary').getByTestId('map-route-line')).toHaveCount(0)
  await page.screenshot({path:info.outputPath('legacy-route-needs-segmentation-1440.png')})
  expect(state.writes.filter(url=>!url.endsWith('/materialize'))).toEqual([])
})

// Optional captured-response replay; the ordinary tests above remain portable.
// This is the saved real supplier response rendered through fixed API reads,
// not a new account visit, supplier call, or current-opening-status check.
for (const width of [1440,1280]) test(`saved supplier response keeps four cards and partial route facts in desktop PNG at ${width}`,async({page},info)=>{
  test.skip(!process.env.ROUTE_CONNECTION_REPLAY,'Set the explicit saved public replay file to run this bounded captured-response check.')
  const captured=JSON.parse(fs.readFileSync(process.env.ROUTE_CONNECTION_REPLAY,'utf8'))
  const data={result:captured.public_result,map:captured.map,dining:captured.daily_dining,stay:captured.stay}
  const before=JSON.stringify(data),state=await show(page,width,data)
  await expect(page.getByTestId('activity-card').locator('h3')).toHaveText(['故宫博物院','景山公园','天坛公园','前门大街'])
  for(let i=1;i<=2;i++)await expect(page.getByTestId(`day-lane-${i}`).locator('.four-day-route-summary')).toContainText('暂无完整日合计')
  const meals=page.getByTestId('daily-meal-card')
  for(let i=0;i<2;i++){
    await meals.nth(i).getByRole('button',{name:'查看 3 家候选'}).click()
    await expect(meals.nth(i).getByTestId('daily-dining-candidate')).toHaveCount(3)
    for(const c of data.dining.days[i].candidates)await expect(meals.nth(i)).toContainText(c.name)
    await expect(meals.nth(i)).not.toContainText(/约多\d+|建议优先比较|局部路段优先比较/)
    await expect(meals.nth(i)).toContainText('绕路时间')
  }
  await page.screenshot({path:info.outputPath(`saved-four-cards-six-restaurants-${width}.png`),fullPage:true})
  await page.getByTestId('desktop-nav-map_stay').click()
  const expectedLines=p.routeGeometrySegments(data.map,'walking').map(part=>part.points.map(p=>[p.longitude,p.latitude]))
  expect(expectedLines.length).toBeGreaterThan(0)
  await expect.poll(()=>page.evaluate(()=>window.fixedMapLines)).toEqual(expectedLines)
  await page.getByTestId('map-theater').locator('summary').filter({hasText:/^路线$/}).click()
  await expect(page.getByTestId('map-route-summary').first()).toContainText('2 分钟 · 137 米')
  await expect(page.getByTestId('map-route-summary').first()).toContainText(warning)
  await page.screenshot({path:info.outputPath(`saved-actual-polyline-unknown-access-${width}.png`)})
  await page.getByTestId('desktop-nav-itinerary').click()
  await page.getByTestId('export-itinerary-png').click()
  const image=page.getByAltText('行程横链导出预览',{exact:true})
  await expect(image).toBeVisible()
  await expect.poll(()=>image.evaluate(img=>img.naturalWidth)).toBe(1440)
  const downloading=page.waitForEvent('download')
  await page.getByTestId('download-itinerary-png').click()
  const download=await downloading
  expect(await download.failure()).toBeNull()
  await download.saveAs(info.outputPath(`saved-actual-route-partial-${width}.png`))
  const drawn=await page.evaluate(()=>window.routeExportText),text=drawn.map(x=>x.text).join('')
  for(const name of ['Day 1','Day 2','故宫博物院','景山公园','天坛公园','前门大街','137 米',warning])expect(text).toContain(name)
  expect(text).not.toMatch(/节省|约多\d+|全程.*分钟/)
  await info.attach('saved-public-and-drawn-text',{body:JSON.stringify({scope:'SAVED_REAL_SUPPLIER_FIXED_API_AND_SDK_NO_NEW_HTTP',
    input_file:process.env.ROUTE_CONNECTION_REPLAY,map:data.map,drawn}),contentType:'application/json'})
  expect(state.writes.filter(url=>!url.endsWith('/materialize'))).toEqual([])
  expect(JSON.stringify(data)).toBe(before)
})
