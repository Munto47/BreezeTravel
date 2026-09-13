// Explicit local integration only. Two real accounts/PG/Yjs; chat is fixed.
// Live optimize/import need separate deliberate enablement and are each issued once.
const {test,expect}=require('@playwright/test')
const {randomUUID}=require('node:crypto')
const fs=require('node:fs')
const pathModule=require('node:path')

const names=['故宫博物院','景山公园','天坛公园','前门大街']
const positions=[[116.397,39.918],[116.397,39.925],[116.413,39.882],[116.397,39.899]]
const places=names.map((name,index)=>({place_id:`place_relative_${index}`,name,category:'attraction',
 city:'北京',address:'北京',coords:{lng:positions[index][0],lat:positions[index][1]},
 source:'synthesized',amap_photos:[],tags:[],description:'固定测试候选；身份在转入后另行核验',suggested_visit_minutes:300}))
const expectedDays=[[names[0],names[1]],[names[2],names[3]]]
const live=process.env.COLLAB_RELATIVE_LIVE==='1'
const importLive=live && process.env.COLLAB_RELATIVE_IMPORT_LIVE!=='0'

async function register(page,nickname){
 await page.goto('/collaborate')
 await expect(page).toHaveURL(/\/login$/)
 await page.getByRole('button',{name:'注册账号',exact:true}).click()
 await page.getByLabel('邮箱',{exact:true}).fill(`relative-${randomUUID()}@example.test`)
 await page.getByLabel('密码',{exact:true}).fill(`Test${randomUUID()}9`)
 await page.getByLabel('称呼（选填）').fill(nickname)
 await page.getByRole('button',{name:'注册并继续',exact:true}).click()
 await expect(page).toHaveURL(/\/collaborate$/,{timeout:30000})
}

async function get(page,path){
 return page.evaluate(async(path)=>{
  const response=await fetch(path,{headers:{Authorization:`Bearer ${localStorage.getItem('authToken')}`}})
  return {status:response.status,body:await response.json()}
 },path)
}

test('local collaboration relative days: two identities sync, save, refresh and one-way import',async({browser},info)=>{
 test.skip(process.env.COLLAB_RELATIVE_LOCAL!=='1','Requires explicit isolated local PG/Yjs integration.')
 test.setTimeout(180000)
 const owner=await browser.newContext({viewport:{width:1440,height:1000}})
 const peerContext=await browser.newContext({viewport:{width:1280,height:1000}})
 const page=await owner.newPage(),peer=await peerContext.newPage()
 const record={scope:{realLogin:true,realRoomDatabase:true,realYjs:true,chat:'FIXED',
  optimize:live?'LIVE_DRIVING':'FIXED',import:importLive?'REAL_WORKER':'NOT_RUN',liveQuestionAnswer:'NOT_RUN'},
  pageErrors:[],chatRequests:0,optimizeRequests:0,importRequests:0,blockedExternalHosts:[]}
 let scheduled
 let published
 for(const context of [owner,peerContext]){
  await context.route('**/*',async(route)=>{
   const url=new URL(route.request().url()),path=url.pathname
   if(!['127.0.0.1','localhost'].includes(url.hostname)){
    record.blockedExternalHosts.push(url.hostname);return route.abort()
   }
   if(path==='/api/chat'){
    record.chatRequests++
    const events=[...places.map(place=>({event:'place',data:{place}})),
     {event:'text',data:{delta:'这是固定候选数据。选点与保存将使用真实本地房间。'}},
     {event:'done',data:{status:'READY',total_places:4}}]
    return route.fulfill({status:200,contentType:'text/event-stream',body:events.map(e=>`data: ${JSON.stringify(e)}\n\n`).join('')})
   }
   if(path==='/api/weather')return route.fulfill({status:200,json:{days:[]}})
   if(path.endsWith('/current-itinerary') && !live)return route.fulfill({json:published || {
    room_id:path.split('/')[3],version:0,itinerary_data:null,selection_snapshot:null,published_at:null}})
   if(path==='/api/optimize'){
    record.optimizeRequests++
    const request=route.request().postDataJSON()
    expect(request.relative_only).toBe(true)
    expect(request.base_room_route_version).toBe(0)
    expect(request.room_route_request_id).toMatch(/^[0-9a-f-]{36}$/)
    expect(request.start_date).toBeUndefined()
    expect(request.places.map(p=>p.name).sort()).toEqual([...names].sort())
    if(!live){
     scheduled={itinerary:{itinerary_id:'controlled-relative',thread_id:request.thread_id,city:'北京',
      days:[[0,1],[2,3]].map((indices,day_index)=>({day_index,cluster_id:day_index,
       slots:indices.map(i=>({place_id:places[i].place_id,place:places[i],start_time:null,end_time:null,transport:null,tips:[]}))})),
      generated_at:'2026-09-13T00:00:00Z',version:1},backup_pool:[],total_distance_km:null,room_route_version:1}
     published={room_id:request.room_id,version:1,itinerary_data:scheduled.itinerary,
      selection_snapshot:{trip_days:request.trip_days,place_ids:request.places.map(p=>p.place_id)},published_at:'2026-09-13T00:00:00Z'}
     return route.fulfill({status:200,json:scheduled})
    }
   }
   if(path==='/api/v3/trip-understandings/from-collaboration'){
    record.importRequests++
    expect(importLive).toBe(true)
    expect(record.importRequests).toBe(1)
    expect(route.request().postDataJSON().room_route_version).toBe(scheduled.room_route_version)
   }
   return route.continue()
  })
 }
 for(const p of [page,peer])p.on('pageerror',error=>record.pageErrors.push(error.name+': '+error.message))
 try{
  await register(page,'相对协同甲')
  await page.getByLabel('目的地（选填）').fill('北京')
  await page.getByLabel('行程天数').selectOption('2')
  await page.getByRole('button',{name:'创建协同房间',exact:true}).click()
  await expect(page).toHaveURL(/\/room\/[^/?#]+$/,{timeout:30000})
  const roomUrl=page.url(),roomId=new URL(roomUrl).pathname.split('/').pop()
  record.roomId=roomId
  fs.mkdirSync(pathModule.resolve(__dirname,'../../.local-artifacts/verification'),{recursive:true})
  fs.writeFileSync(pathModule.resolve(__dirname,'../../.local-artifacts/verification/collaboration-relative-local-active.json'),
   JSON.stringify({room_id:roomId,names,live},null,2))
  fs.writeFileSync(info.outputPath('operation-target.json'),JSON.stringify({roomId,scope:record.scope,names},null,2))
  await expect(page.getByText('协同已连接',{exact:true})).toBeVisible({timeout:30000})
  await expect(page.getByRole('button',{name:`选择 ${names[0]}`,exact:true})).toBeVisible({timeout:30000})
  await register(peer,'相对协同乙')
  await peer.getByLabel('房间号').fill(roomId)
  await peer.getByRole('button',{name:'加入协同房间',exact:true}).click()
  await expect(peer).toHaveURL(roomUrl,{timeout:30000})
  await expect(peer.getByText('协同已连接',{exact:true})).toBeVisible({timeout:30000})
  for(let i=0;i<names.length;i++){
   const actor=i%2?page:peer,other=i%2?peer:page
   await actor.getByRole('button',{name:`选择 ${names[i]}`,exact:true}).click()
   await expect(other.getByRole('button',{name:`取消选择 ${names[i]}`,exact:true})).toBeVisible({timeout:15000})
  }
  expect(record.optimizeRequests).toBe(0)
  const readPlaces=()=>get(page,`/api/room/${roomId}/places`)
  await expect.poll(async()=>((await readPlaces()).body||[]).filter(p=>p.room_selected).length,{timeout:15000}).toBe(4)
  const optimized=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/optimize')
  await page.getByRole('button',{name:/智能排线/}).click()
  const optimizationResponse=await optimized
  expect(optimizationResponse.status()).toBe(200)
  scheduled=await optimizationResponse.json()
  record.optimization=scheduled
  expect(scheduled.itinerary.days.map(day=>day.slots.map(slot=>slot.place.name))).toEqual(expectedDays)
  expect(scheduled.backup_pool).toEqual([])
  expect(scheduled.room_route_version).toBe(1)
  await expect(page.getByRole('button',{name:'转入行程查',exact:true})).toBeEnabled({timeout:30000})
  for(let day=1;day<=2;day++)expect(await page.locator(`[data-testid="collaboration-day-${day}"]:visible`).getByTestId('collaboration-stop-name').allTextContents()).toEqual(expectedDays[day-1])
  await expect(page.locator('[data-testid="collaboration-relative-itinerary"]:visible')).not.toContainText(/\d{2}:\d{2}|300min|5h/)
  await expect(peer.getByTestId('shared-route-status')).toContainText('共同路线 · 已同步')
  await peer.getByRole('button',{name:'已排路线',exact:true}).click()
  for(let day=1;day<=2;day++)await expect(peer.locator(`[data-testid="collaboration-day-${day}"]:visible`).getByTestId('collaboration-stop-name')).toHaveText(expectedDays[day-1])
  expect((await get(peer,`/api/room/${roomId}/itinerary`)).status).toBe(404)
  await page.getByRole('button',{name:'保存到我的行程',exact:true}).click()
  await expect(page.getByText('个人副本已保存',{exact:true})).toBeVisible()
  record.before={places:(await readPlaces()).body,saved:(await get(page,`/api/room/${roomId}/itinerary`)).body,
   common:(await get(page,`/api/room/${roomId}/current-itinerary`)).body}
  expect((await get(peer,`/api/room/${roomId}/current-itinerary`)).body).toEqual(record.before.common)
  await page.screenshot({path:info.outputPath('relative-collaboration-saved-1440.png'),fullPage:true})
  await Promise.all([page.reload(),peer.reload()])
  await expect(page.getByRole('button',{name:'转入行程查',exact:true})).toBeEnabled({timeout:30000})
  await page.getByRole('button',{name:'已排路线',exact:true}).click()
  for(let day=1;day<=2;day++)expect(await page.locator(`[data-testid="collaboration-day-${day}"]:visible`).getByTestId('collaboration-stop-name').allTextContents()).toEqual(expectedDays[day-1])
  expect((await get(page,`/api/room/${roomId}/itinerary`)).body).toEqual(record.before.saved)
  await expect(peer.getByTestId('shared-route-status')).toContainText('共同路线 · 已同步')
  await peer.getByRole('button',{name:'已排路线',exact:true}).click()
  for(let day=1;day<=2;day++)await expect(peer.locator(`[data-testid="collaboration-day-${day}"]:visible`).getByTestId('collaboration-stop-name')).toHaveText(expectedDays[day-1])
  expect((await get(peer,`/api/room/${roomId}/current-itinerary`)).body).toEqual(record.before.common)
  await page.getByRole('button',{name:'查看行程',exact:true}).click()
  await expect(page.getByRole('heading',{name:'行程详情',exact:true})).toBeVisible()
  await expect(page).toHaveURL(/\?shared=1$/)
  const availableLegs=scheduled.itinerary.days.flatMap(day=>day.slots).filter(slot=>slot.transport?.status==='AVAILABLE')
  await expect(page.getByTestId('collaboration-route-available')).toHaveCount(availableLegs.length)
  for(const slot of availableLegs)await expect(page.locator('body')).toContainText(`${slot.transport.duration_mins} 分钟 · ${slot.transport.distance_km} km`)
  await expect(page.locator('body')).not.toContainText(/09:00|11:30|2026年9月/)
  await page.getByRole('button',{name:'返回工作台',exact:true}).click()
  await expect(page.getByRole('button',{name:'转入行程查',exact:true})).toBeEnabled({timeout:30000})
  if(importLive){
   const accepted=peer.waitForResponse(response=>new URL(response.url()).pathname==='/api/v3/trip-understandings/from-collaboration')
   await peer.getByRole('button',{name:'转入行程查',exact:true}).click()
   const acceptedResponse=await accepted
   expect(acceptedResponse.status()).toBe(202)
   record.accepted=await acceptedResponse.json()
   fs.writeFileSync(info.outputPath('import-target.json'),JSON.stringify(record.accepted,null,2))
   await expect(peer.getByTestId('itinerary-workspace')).toBeVisible({timeout:120000})
   record.imported=(await get(peer,record.accepted.result_url)).body
   expect(record.imported.days.map(day=>day.activities.map(card=>card.name))).toEqual(expectedDays)
   expect(record.imported.days.flatMap(day=>day.activities).every(card=>card.status==='READY')).toBe(true)
   await expect(peer.getByTestId('activity-card')).toHaveCount(4)
   await peer.screenshot({path:info.outputPath('imported-checked-1280.png'),fullPage:true})
   await peer.reload()
   await expect(peer.getByTestId('activity-card')).toHaveCount(4)
  }
  // Import and its new versions must not change the shared choices or saved route.
  record.after={places:(await get(peer,`/api/room/${roomId}/places`)).body,
   saved:(await get(page,`/api/room/${roomId}/itinerary`)).body,
   common:(await get(page,`/api/room/${roomId}/current-itinerary`)).body}
  expect(record.after).toEqual(record.before)
  const chatsBefore=record.chatRequests
  await peer.goto(roomUrl)
  await expect(peer.getByText('协同已连接',{exact:true})).toBeVisible({timeout:30000})
  await peer.getByRole('button',{name:/^候选地点(?:\s+\d+)?$/}).click()
  for(const name of names)await expect(peer.getByRole('button',{name:`取消选择 ${name}`,exact:true})).toBeVisible({timeout:15000})
  expect(record.chatRequests).toBe(chatsBefore)
  expect(record.chatRequests).toBe(1)
  expect(record.optimizeRequests).toBe(1)
  expect(record.importRequests).toBe(importLive?1:0)
  expect(record.pageErrors).toEqual([])
  await peer.screenshot({path:info.outputPath('peer-selection-retained-1280.png'),fullPage:true})
 } finally {
  fs.writeFileSync(info.outputPath('local-collaboration-result.json'),JSON.stringify(record,null,2))
  await owner.storageState({path:info.outputPath('owner.private.json')})
  await peerContext.storageState({path:info.outputPath('peer.private.json')})
  await owner.close();await peerContext.close()
 }
})
