const {test,expect} = require('@playwright/test')
const {execFileSync} = require('node:child_process')
const path = require('node:path')
const resource = 'mobile-layout-synthetic-trip'
const card = (id,name,status='READY') => ({activity_token:`mobile-layout-place-${id}`,name,city:'北京',category:'景点',status,area_or_address:'界面用例地址',time_hint:null,available_actions:['VIEW_DETAILS','MOVE','DELETE','REPLACE']})

async function openTrip(page, options={}) {
  const days=[{label:'Day 1',activities:[card(1,'故宫博物院'),card('pending','待确认入口','NEEDS_CONFIRMATION'),card(2,'景山公园'),card(3,'北海公园')]},
    {label:'Day 2',activities:[card(4,'颐和园'),card(5,'圆明园')]}]
  if(options.long) days[0].activities[0].name='深圳市当代艺术与城市规划馆公共艺术与城市历史专题展览交流中心'
  if(options.capacity) for(let i=2;i<14;i++)days.push({label:`Day ${i+1}`,activities:Array.from({length:10},(_,j)=>card(`${i}-${j}`,`第${i+1}天的行程地点${j+1}`))})
  const map={status:'AVAILABLE',message:'受控界面路线',available_actions:['RENDER_MAP'],
    points:days.flatMap((day,i)=>day.activities.filter(c=>c.status==='READY').map((c,j)=>({activity_token:c.activity_token,name:c.name,day_label:day.label,sequence_index:j,position:{longitude:116.4+i*.1+j*.01,latitude:39.9,coordinate_system:'GCJ02'}}))),
    days:days.map(day=>({label:day.label,routes:day.activities.filter(c=>c.status==='READY').slice(0,-1).map((c,j)=>({from_activity_token:c.activity_token,to_activity_token:day.activities.filter(c=>c.status==='READY')[j+1].activity_token,from_name:c.name,to_name:day.activities.filter(c=>c.status==='READY')[j+1].name,selected_mode:'walking',message:'受控路段',walking:{status:'AVAILABLE',duration_minutes:10,distance_meters:650,transfer_count:0,geometry:[],connection_status:'VERIFIED'},transit:{status:'UNAVAILABLE',duration_minutes:null,distance_meters:null,transfer_count:null,geometry:[]}}))}))}
  const state={writes:[],result:{status:'PARTIAL_RESULT',ownership:'ANONYMOUS',can_undo:false,can_redo:false,
    assumptions:[{key:'destination',label:'目的地',value:'北京',editable:true}],days,
    coverage:{recognized_place_count:6,confirmed_place_count:5,unresolved_place_count:1,unprocessed_count:0,unclassified_mention_count:0,complete:false},
    map:{status:'AVAILABLE',message:'已准备',available_actions:['RENDER_MAP']},stay:{status:'UNAVAILABLE',message:'暂未生成住宿建议',area_summary:null,searched_scopes:[],candidates:[],available_actions:[]},available_actions:['EDIT_CARDS','EDIT_ASSUMPTIONS']}}
  await page.emulateMedia({reducedMotion:'reduce'})
  await page.addInitScript(()=>{
    window.__mobileMap={centers:[],fits:[],pans:[]}
    window.AMap={Map:class {constructor(container){this.container=container} on(name,cb){if(name==='complete')setTimeout(cb,0)}
      add(items){items.filter(x=>x.content).forEach((x,i)=>{Object.assign(x.content.style,{position:'absolute',left:`${20+i%3*20}%`,top:`${25+Math.floor(i/3)*15}%`});this.container.append(x.content)})}
      remove(items){items.forEach(x=>x.content?.remove())} destroy(){} resize(){} zoomIn(){} zoomOut(){}
      setFitView(items,immediate,padding){window.__mobileMap.fits.push(padding)}
      setCenter(center,immediate,duration){if(duration!==undefined && typeof duration!=='number')throw new TypeError('Map duration must be a number');window.__mobileMap.centers.push({center})}
      panBy(x,y,duration){window.__mobileMap.pans.push({x,y,duration})}},Marker:class{constructor(o){Object.assign(this,o)}},Polyline:class{constructor(o){Object.assign(this,o)}}}
  })
  if(options.mapFailure)await page.addInitScript(()=>{window.AMap.Map=class{constructor(){throw new Error('controlled-map-unavailable')}}})
  await page.route('**/*',route=>{const url=new URL(route.request().url());return ['localhost','127.0.0.1'].includes(url.hostname)?route.continue():route.abort()})
  await page.route('**/api/**',route=>{
    const req=route.request(),url=new URL(req.url()),action=url.pathname.split(resource)[1]
    // Materialization is the existing initial result-read preparation; measure
    // actual edits and route requests separately from that initial request.
    if(req.method()!=='GET' && action !== '/materialize')state.writes.push({action,body:req.postDataJSON()})
    const reply=json=>route.fulfill({json,headers:{ETag:`"mobile-v${state.writes.length}"`}})
    if(action==='/result')return reply(state.result)
    if(action==='/source')return reply({status:'AVAILABLE',text:'Day 1 故宫博物院、景山公园、北海公园\nDay 2 颐和园、圆明园',activities:[]})
    if(action==='/map-renders/latest')return reply(state.writes.length ? {...map,status:'NEEDS_UPDATE',points:[],days:[]} : map)
    if(action==='/stay-suggestions')return reply(state.result.stay)
    if(action==='/daily-dining')return reply({status:'UNAVAILABLE',message:'暂无用餐建议',days:[]})
    if(action==='/supplementary')return reply({status:'AVAILABLE',days:[]})
    if(action==='/materialize')return reply({status:'READY',message:'已准备',calendar:'相对日序',party_size:2,checks_available:true})
    if(action==='/checks')return reply({status:'STILL_NEEDS_CONFIRMATION',message:'待确认',items:[],remaining_must_adjust:0,available_actions:[]})
    if(action==='/commands') {
      const command=req.postDataJSON()
      state.result=JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python',['-X','utf8','-c',
        "import json,sys; from pydantic import TypeAdapter; from app.trip_understanding.models import UserFacingTripResult,TripUnderstandingCommand; from app.trip_understanding.commands import apply_public_command; v=json.load(sys.stdin); print(apply_public_command(UserFacingTripResult.model_validate(v['result']),TypeAdapter(TripUnderstandingCommand).validate_python(v['command'])).result.model_dump_json())"],
        {cwd:path.resolve(__dirname,'../../backend'),input:JSON.stringify({result:state.result,command}),encoding:'utf8',env:{...process.env,RUNTIME_PROFILE:'test',PYTHONPATH:'.',PYTHONIOENCODING:'utf-8'}}))
      return reply({status:'APPLIED',changed_days:state.result.days.map(d=>d.label),map_readiness:'NEEDS_UPDATE'})
    }
    return route.fulfill({status:404,json:{}})
  })
  await page.goto(`/trip/result#trip=${resource}`)
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  return state
}

for(const width of [360,390,430]) test(`mobile content and map remain usable at ${width}`,async({page},info)=>{
  await page.setViewportSize({width,height:844})
  const state=await openTrip(page)
  const cards=page.getByTestId('day-lane-1').getByTestId('activity-card')
  await expect(cards).toHaveCount(3)
  const second=await cards.nth(1).boundingBox()
  expect(second.y+second.height).toBeLessThan(844)
  expect(second.height).toBeLessThanOrEqual(120)
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
  await page.screenshot({path:info.outputPath('cards.png')})
  await page.getByRole('button',{name:'地图',exact:true}).click()
  const map=page.getByTestId('map-theater'),directory=page.getByTestId('map-place-directory')
  await expect(directory).toHaveAttribute('data-sheet','collapsed')
  const box=await map.boundingBox(),drawer=await directory.boundingBox()
  expect(box.height-drawer.height-60).toBeGreaterThanOrEqual(844*.65)
  await page.screenshot({path:info.outputPath('map.png')})
  await page.getByRole('button',{name:'住宿',exact:true}).click()
  const stay=page.getByRole('dialog',{name:'住宿',exact:true})
  await expect(stay).toBeVisible()
  await expect(stay).toContainText('暂未生成住宿建议')
  await stay.getByRole('button',{name:'关闭',exact:true}).click()
  await expect(stay).toBeHidden()
  await page.getByRole('button',{name:'查看地点列表',exact:true}).click()
  await expect(directory).toHaveAttribute('data-sheet','half')
  await directory.getByTestId('map-directory-place').filter({hasText:'景山公园'}).click()
  await expect(page.locator('.e-map-marker[aria-label="查看景山公园"]')).toHaveAttribute('aria-pressed','true')
  await expect(page.locator('.e-map-marker[aria-label="查看景山公园"]')).toHaveText('2')
  await expect.poll(()=>page.evaluate(()=>window.__mobileMap.pans.at(-1)?.y)).toBeLessThan(0)
  await page.getByRole('button',{name:'展开完整地点列表'}).click()
  await expect(directory).toHaveAttribute('data-sheet','full')
  await page.getByRole('button',{name:'半屏查看地点列表'}).click()
  await expect(directory).toHaveAttribute('data-sheet','half')
  await page.getByRole('button',{name:'收起地点列表'}).click()
  await page.getByLabel('日期颜色与预演选择').getByRole('button',{name:'Day 2',exact:true}).click()
  await expect(page.locator('.e-map-marker')).toHaveCount(2)
  await page.getByRole('button',{name:'检查与建议',exact:true}).click()
  const inspector=page.getByRole('dialog',{name:'检查与建议'})
  await expect(inspector).toBeVisible()
  expect((await inspector.boundingBox()).width).toBe(width)
  await page.goBack()
  await expect(inspector).toBeHidden()
  await expect(map).toBeVisible()
  expect(state.writes).toEqual([])
})

test('mobile move translates past pending places; cancel and touch cancellation do not write',async({page})=>{
  const state=await openTrip(page)
  const handle = await page.getByTestId('drag-handle-1-0').boundingBox()
  await page.mouse.move(handle.x+20,handle.y+20); await page.mouse.down()
  await page.getByTestId('drag-handle-1-0').dispatchEvent('pointercancel',{pointerId:1})
  await page.mouse.up()
  expect(state.writes).toEqual([])
  await page.getByRole('button',{name:'故宫博物院更多操作'}).click()
  await page.getByRole('button',{name:'移动地点',exact:true}).click()
  const dialog=page.getByRole('dialog',{name:'移动 故宫博物院'})
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button',{name:'取消',exact:true}).click()
  await expect(dialog).toBeHidden()
  expect(state.writes).toEqual([])
  await page.getByRole('button',{name:'故宫博物院更多操作'}).click()
  await page.getByRole('button',{name:'移动地点',exact:true}).click()
  await page.getByLabel('放置位置').selectOption('1')
  await page.getByRole('button',{name:'确认移动',exact:true}).click()
  await expect.poll(()=>state.writes.length).toBe(1)
  expect(state.writes[0].body).toMatchObject({command_type:'ACTIVITY_MOVE',target_day_index:1,target_position:2})
  await expect(page.getByTestId('day-lane-1').getByTestId('activity-card').first()).toContainText('景山公园')
  await expect(page.locator('.mobile-route-status')).toContainText('更新路线')
  await page.reload()
  await expect(page.getByTestId('day-lane-1').getByTestId('activity-card').first()).toContainText('景山公园')
  expect(state.writes).toHaveLength(1)
})

test('mobile details, nested export, and browser back retain the itinerary',async({page})=>{
  const state=await openTrip(page)
  await page.getByTestId('day-lane-1').locator('.four-card-copy').first().click()
  const place=page.getByRole('dialog',{name:'修改地点 故宫博物院'})
  await expect(place).toBeVisible()
  await expect(place.getByLabel('查询城市')).toHaveCSS('font-size','16px')
  await page.goBack()
  await expect(place).toBeHidden()
  await page.getByRole('button',{name:'更多行程操作',exact:true}).click()
  const actions=page.getByRole('dialog',{name:'行程操作'})
  await expect(actions).toBeVisible()
  await actions.getByRole('button',{name:'查看原文',exact:true}).click()
  await expect(page.getByRole('dialog',{name:'行程原文'})).toContainText('Day 2 颐和园、圆明园')
  await page.goBack()
  await expect(page.getByRole('dialog',{name:'行程原文'})).toBeHidden()
  await expect(actions).toBeVisible()
  await actions.getByRole('button',{name:'导出图片',exact:true}).click()
  await expect(page.getByRole('dialog',{name:'图片预览'})).toBeVisible({timeout:20000})
  await expect(page.locator('#png-preview-description')).toContainText('全部 2 天')
  await page.goBack()
  await expect(page.getByRole('dialog',{name:'图片预览'})).toBeHidden()
  await expect(actions).toBeVisible()
  await page.goBack()
  await expect(actions).toBeHidden()
  await expect(page.locator('body')).not.toHaveCSS('overflow','hidden')
  expect(state.writes).toEqual([])
})

test('short viewport keeps place inputs and close controls reachable',async({page})=>{
  await openTrip(page)
  await page.getByTestId('day-lane-1').locator('.four-card-copy').first().click()
  const dialog=page.getByRole('dialog',{name:'修改地点 故宫博物院'})
  await expect(dialog).toBeVisible()
  await page.setViewportSize({width:390,height:420})
  const city=dialog.getByLabel('查询城市')
  await city.scrollIntoViewIfNeeded()
  await expect(city).toBeInViewport()
  expect((await dialog.boundingBox()).height).toBeLessThanOrEqual(420)
  await dialog.getByRole('button',{name:'收起地点确认'}).click()
  await expect(dialog).toBeHidden()
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
})

test('map failure keeps the directory and route text accessible',async({page})=>{
  const state=await openTrip(page,{mapFailure:true})
  await page.getByRole('button',{name:'地图',exact:true}).click()
  await expect(page.getByRole('button',{name:'重试加载地图'})).toBeVisible()
  await page.getByRole('button',{name:'展开完整地点列表'}).click()
  await expect(page.getByTestId('map-place-directory').getByText('景山公园',{exact:true})).toBeVisible()
  await page.getByRole('button',{name:'收起地点列表'}).click()
  await page.getByRole('button',{name:'路线',exact:true}).click()
  const routes=page.getByRole('dialog',{name:'路线',exact:true})
  await expect(routes).toBeVisible()
  await routes.locator('summary').filter({hasText:'路线摘要'}).click()
  await expect(routes.getByTestId('map-route-summary').first()).toContainText('故宫博物院')
  await page.goBack()
  await expect(routes).toBeHidden()
  expect(state.writes).toEqual([])
})

test('save to account opens login without losing the return destination',async({page})=>{
  await openTrip(page)
  await page.getByRole('button',{name:'更多行程操作',exact:true}).click()
  await page.getByRole('dialog',{name:'行程操作'}).getByRole('button',{name:'保存到账号',exact:true}).click()
  await expect(page).toHaveURL(/\/login/)
  expect(await page.evaluate(()=>sessionStorage.getItem('bt_login_return'))).toContain(resource)
})

test('long names, fourteen days and landscape do not overflow',async({page})=>{
  await openTrip(page,{long:true,capacity:true})
  await expect(page.getByTestId('trip-days').locator(':scope > section')).toHaveCount(14)
  const name=page.getByTestId('day-lane-1').locator('.four-card-name').first()
  expect(await name.evaluate(el=>el.scrollWidth<=el.clientWidth+1)).toBe(true)
  await page.getByLabel('跳转行程日期').getByRole('button',{name:'Day 14',exact:true}).click()
  await expect(page.getByTestId('day-lane-14')).toBeInViewport()
  await page.getByRole('button',{name:'地图',exact:true}).click()
  await page.setViewportSize({width:844,height:390})
  await page.getByRole('button',{name:'查看地点列表',exact:true}).click()
  await expect(page.getByTestId('map-place-directory')).toHaveAttribute('data-sheet','full')
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(844)
})

for(const width of [1280,1440])test(`desktop layout and keyboard remain available at ${width}`,async({page},info)=>{
  await page.setViewportSize({width,height:1000})
  const state=await openTrip(page)
  await expect(page.getByTestId('result-desktop-nav')).toBeVisible()
  await expect(page.locator('.mobile-trip-header')).toHaveCount(0)
  await expect(page.getByTestId('serpentine-canvas-1')).not.toHaveAttribute('data-columns','1')
  await page.getByTestId('drag-handle-1-0').focus()
  await page.keyboard.press('Enter');await page.keyboard.press('ArrowRight');await page.keyboard.press('Escape')
  expect(state.writes).toEqual([])
  await page.getByRole('button',{name:'地图',exact:true}).click()
  await expect(page.getByTestId('map-place-directory')).toBeVisible()
  await page.screenshot({path:info.outputPath('desktop-map.png')})
})
