// Fixed desktop HTTP and Yjs; no real room or provider requests.
const {test,expect}=require('@playwright/test')
const Y=require('yjs'),sync=require('y-protocols/sync'),encoding=require('lib0/encoding'),decoding=require('lib0/decoding')
const room='SYNCCHECK'
const place=(id,name)=>({placeId:id,name,category:'attraction',address:'固定地址',city:'北京',coords:{lng:116.39,lat:39.91},source:'amap_poi',amapPhotos:[],tags:[],constraintEvidence:[],votedBy:['room-selection'],addedBy:'room',addedAt:'2026-09-13T00:00:00Z',note:'',isPinned:false})
const initial=[place('place_sync_a','青溪公园'),place('place_sync_b','星河博物馆')]
const raw=p=>({place_id:p.placeId,name:p.name,category:p.category,address:p.address,city:p.city,coords:p.coords,source:p.source,amap_photos:[],tags:[],constraint_evidence:[],description:null,district:null,amap_rating:null,amap_price:null,opening_hours:null,phone:null,geo_evidence:[],confirmation_actions:[],estimated_duration:null,room_selected:p.votedBy.length>0,voted_by:[]})
async function setup(browser,width,hold=false){
 const doc=new Y.Doc(),peers=new Set(),state={saved:initial.map(raw),writes:[],errors:[],holdRead:hold,heldRead:null,failNext:false,holdWrite:false,heldWrite:null,otherWrites:[]}
 initial.forEach(p=>doc.getMap('places').set(p.placeId,p))
 const context=await browser.newContext({viewport:{width,height:1000}})
 await context.addInitScript(()=>{localStorage.setItem('authToken','fixed-sync');localStorage.setItem('authUser',JSON.stringify({userId:'sync-user',nickname:'同步测试'}))})
 const wire=fn=>{const e=encoding.createEncoder();encoding.writeVarUint(e,0);fn(e);return Buffer.from(encoding.toUint8Array(e))}
 doc.on('update',(update,origin)=>{const packet=wire(e=>sync.writeUpdate(e,update));for(const peer of peers)if(peer!==origin)try{peer.send(packet)}catch{}})
 await context.routeWebSocket(url=>url.pathname===`/${room}`,ws=>{peers.add(ws);ws.onClose(()=>peers.delete(ws));ws.onMessage(message=>{const d=decoding.createDecoder(new Uint8Array(message));if(decoding.readVarUint(d)!==0)return;const e=encoding.createEncoder();encoding.writeVarUint(e,0);sync.readSyncMessage(d,e,doc,ws);if(encoding.length(e)>1)ws.send(Buffer.from(encoding.toUint8Array(e)))});ws.send(wire(e=>sync.writeSyncStep1(e,doc)));ws.send(wire(e=>sync.writeSyncStep2(e,doc)))})
 await context.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url()),p=url.pathname,reply=json=>route.fulfill({json})
  if(!['127.0.0.1','localhost'].includes(url.hostname))return route.abort()
  if(!p.startsWith('/api/'))return route.continue()
  if(p==='/api/user/me')return reply({user_id:'sync-user',nickname:'同步测试'})
  if(p.endsWith('/state'))return reply({thread_id:'sync-thread',trip_city:'北京',trip_days:1})
  if(p.endsWith('/ws-token'))return reply({token:'fixed-ws',expires_in_seconds:3600})
  if(p==='/api/weather')return reply({days:[]})
  if(p.endsWith('/current-itinerary'))return reply({room_id:room,version:0,itinerary_data:null,selection_snapshot:null,published_at:null})
  if(p.endsWith('/places')){const snapshot=structuredClone(state.saved);if(state.holdRead){state.holdRead=false;await new Promise(resolve=>state.heldRead=resolve)}return reply(snapshot)}
  if(p.endsWith('/places/sync')){
   state.writes.push(req.postDataJSON().places)
   if(state.failNext){state.failNext=false;return route.fulfill({status:503,json:{}})}
   if(state.holdWrite){state.holdWrite=false;await new Promise(resolve=>state.heldWrite=resolve)}
   state.saved=structuredClone(req.postDataJSON().places);return reply({ok:true,synced:state.saved.length})
  }
  if(req.method()!=='GET')state.otherWrites.push(p)
  return route.fulfill({status:404,json:{}})
 })
 const page=await context.newPage();page.on('pageerror',e=>state.errors.push(e.message))
 await page.goto(`/room/${room}`)
 await expect(page.getByText('协同已连接',{exact:true})).toBeVisible()
 await expect(page.getByRole('button',{name:'取消选择 青溪公园',exact:true})).toBeVisible()
 return {page,state,doc,close:async()=>{state.heldRead?.();state.heldWrite?.();await context.close();doc.destroy()}}
}
for(const width of [1440,1280])test(`restored values do not write while local and peer place changes still persist at ${width}`,{tag:'@desktop'},async({browser})=>{
 const r=await setup(browser,width),{page,state,doc}=r
 try{
  await page.waitForTimeout(3200);expect(state.writes).toEqual([])
  await page.reload();await expect(page.getByRole('button',{name:'取消选择 青溪公园',exact:true})).toBeVisible();await page.waitForTimeout(3200);expect(state.writes).toEqual([])
  await page.getByRole('button',{name:'取消选择 青溪公园',exact:true}).click()
  await expect.poll(()=>state.writes.length).toBe(1);expect(state.saved.find(p=>p.place_id==='place_sync_a').room_selected).toBe(false)
  await page.getByRole('button',{name:'选择 青溪公园',exact:true}).click()
  await expect.poll(()=>state.writes.length).toBe(2);expect(state.saved.find(p=>p.place_id==='place_sync_a').room_selected).toBe(true)
  doc.getMap('places').set('place_sync_b',{...doc.getMap('places').get('place_sync_b'),votedBy:[]})
  await expect.poll(()=>state.writes.length).toBe(3);expect(state.saved.find(p=>p.place_id==='place_sync_b').room_selected).toBe(false)
  doc.getMap('places').set('place_sync_c',place('place_sync_c','明月广场'))
  await expect.poll(()=>state.writes.length).toBe(4);expect(state.saved.map(p=>p.place_id)).toContain('place_sync_c')
  doc.getMap('places').delete('place_sync_c')
  await expect.poll(()=>state.writes.length).toBe(5);expect(state.saved.map(p=>p.place_id)).not.toContain('place_sync_c')
  expect(state.otherWrites).toEqual([]);expect(state.errors).toEqual([])
 }finally{await r.close()}
})
test('changes during the initial read survive and persist instead of being skipped or restored over',{tag:'@desktop'},async({browser})=>{
 const r=await setup(browser,1280,true),{page,state,doc}=r
 try{
  await expect.poll(()=>Boolean(state.heldRead)).toBe(true)
  await page.getByRole('button',{name:'取消选择 青溪公园',exact:true}).click()
  doc.getMap('places').delete('place_sync_b');doc.getMap('places').set('place_sync_c',place('place_sync_c','明月广场'))
  await expect(page.getByRole('heading',{name:'明月广场',exact:true}).filter({visible:true})).toBeVisible()
  state.heldRead()
  await expect.poll(()=>state.writes.length).toBe(1)
  expect(state.saved.map(p=>p.place_id).sort()).toEqual(['place_sync_a','place_sync_c'])
  expect(state.saved.find(p=>p.place_id==='place_sync_a').room_selected).toBe(false)
  expect(state.otherWrites).toEqual([])
 }finally{await r.close()}
})
test('uncertain write pauses further overwrites and refreshing can persist the retained shared selection',{tag:'@desktop'},async({browser})=>{
 const r=await setup(browser,1440),{page,state}=r
 try{
  await page.waitForTimeout(3200);expect(state.writes).toEqual([])
  state.failNext=true;await page.getByRole('button',{name:'取消选择 青溪公园',exact:true}).click()
  await expect.poll(()=>state.writes.length).toBe(1)
  await expect(page.getByText(/候选地点保存结果暂时无法确认/)).toBeVisible()
  expect(state.saved.find(p=>p.place_id==='place_sync_a').room_selected).toBe(true)
  await page.waitForTimeout(2200);expect(state.writes).toHaveLength(1)
  await page.reload();await expect(page.getByRole('button',{name:'选择 青溪公园',exact:true})).toBeVisible()
  await expect.poll(()=>state.writes.length).toBe(2)
  expect(state.saved.find(p=>p.place_id==='place_sync_a').room_selected).toBe(false)
 }finally{await r.close()}
})
test('reverting while an earlier save is in flight is not swallowed by the original baseline',{tag:'@desktop'},async({browser})=>{
 const r=await setup(browser,1440),{page,state}=r
 try{
  await page.waitForTimeout(3200);expect(state.writes).toEqual([])
  state.holdWrite=true;await page.getByRole('button',{name:'取消选择 青溪公园',exact:true}).click()
  await expect.poll(()=>Boolean(state.heldWrite)).toBe(true)
  await page.getByRole('button',{name:'选择 青溪公园',exact:true}).click()
  state.heldWrite()
  await expect.poll(()=>state.writes.length).toBe(2)
  expect(state.saved.find(p=>p.place_id==='place_sync_a').room_selected).toBe(true)
 }finally{await r.close()}
})
