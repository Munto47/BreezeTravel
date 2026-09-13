// Real desktop room components; fixed HTTP/Yjs and controllable SSE body only.
// No model, POI, account database or real room is used.
const {test, expect}=require('@playwright/test')
const Y=require('yjs'), sync=require('y-protocols/sync')
const encoding=require('lib0/encoding'), decoding=require('lib0/decoding')
const roomId='CHATDESKTOP'
const names=['故宫博物院','景山公园']
const answer='我会优先选择故宫博物院。\n这里适合了解北京的历史建筑；景山公园可留作另一站。\n这只是选择建议，共同路线和你们已选的地点没有改变。'
const places=names.map((name,i)=>({placeId:`place_chat_${i}`,name,category:'attraction',city:'北京',address:'固定测试地址',coords:{lng:116.397,lat:39.918+i*.007},source:'amap_poi',amapPhotos:[],tags:[],votedBy:['owner'],addedBy:'owner',addedAt:'2026-09-13T00:00:00Z',note:'',isPinned:false}))
const apiPlace=p=>({place_id:p.placeId,name:p.name,category:p.category,city:p.city,address:p.address,coords:p.coords})
async function setup(browser,width){
 const context=await browser.newContext({viewport:{width,height:1000}})
 const doc=new Y.Doc(); places.forEach(p=>doc.getMap('places').set(p.placeId,p))
 const before=doc.getMap('places').toJSON(), writes=[], errors=[]
 await context.addInitScript(()=>{
  localStorage.setItem('authToken','fixed-chat-owner');localStorage.setItem('authUser',JSON.stringify({userId:'owner',nickname:'问答验证'}))
  window.__chatRequests=[];window.__chatStreams=[];window.__chatMode='answer';window.__abortedChats=0
  const original=window.fetch.bind(window)
  window.fetch=async(input,init)=>{
   const url=typeof input==='string'?input:input.url
   if(new URL(url,location.href).pathname!=='/api/chat')return original(input,init)
   window.__chatRequests.push(JSON.parse(init.body))
   init.signal?.addEventListener('abort',()=>window.__abortedChats++)
   if(window.__chatMode==='selection409')return new Response('{}',{status:409,headers:{'Content-Type':'application/json'}})
   if(window.__chatMode==='late401')return new Promise(resolve=>{window.__release401=()=>resolve(new Response('{}',{status:401,headers:{'Content-Type':'application/json'}}))})
   const mode=window.__chatMode, encoder=new TextEncoder()
   const stream=new ReadableStream({start(controller){
    const send=(event,data)=>controller.enqueue(encoder.encode(`data: ${JSON.stringify({event,data})}\n\n`))
    window.__chatStreams.push({send,close:()=>controller.close()})
    if(mode==='answer'){
     send('text',{delta:'我会优先选择故宫博物院。\n'})
     send('text',{delta:'这里适合了解北京的历史建筑；景山公园可留作另一站。\n'})
     send('text',{delta:'这只是选择建议，共同路线和你们已选的地点没有改变。'})
     send('done',{status:'READY',total_places:0});controller.close()
    }else if(mode==='search'){send('place',{place:{place_id:'place_search_new',name:'首都博物馆',category:'attraction',city:'北京',address:'固定地址',coords:{lng:116.34,lat:39.9}}});send('text',{delta:'找到一处博物馆，供你手动选择。'});send('done',{status:'READY',total_places:1});controller.close()}else if(mode==='empty'){send('done',{status:'READY',total_places:0});controller.close()}
    else if(mode==='truncated'){send('text',{delta:'已收到的部分回答'});controller.close()}
    else send('text',{delta:'已经收到的建议。'})
   }})
   return new Response(stream,{status:200,headers:{'Content-Type':'text/event-stream'}})
  }
 })
 const wire=fn=>{const e=encoding.createEncoder();encoding.writeVarUint(e,0);fn(e);return Buffer.from(encoding.toUint8Array(e))}
 await context.routeWebSocket(url=>url.pathname===`/${roomId}`,ws=>{
  ws.onMessage(message=>{const d=decoding.createDecoder(new Uint8Array(message));if(decoding.readVarUint(d)!==0)return;const e=encoding.createEncoder();encoding.writeVarUint(e,0);sync.readSyncMessage(d,e,doc,ws);if(encoding.length(e)>1)ws.send(Buffer.from(encoding.toUint8Array(e)))})
  ws.send(wire(e=>sync.writeSyncStep1(e,doc)));ws.send(wire(e=>sync.writeSyncStep2(e,doc)))
 })
 await context.route('**/*',route=>{
  const req=route.request(),url=new URL(req.url()),p=url.pathname
  if(!['127.0.0.1','localhost'].includes(url.hostname))return route.abort()
  if(!p.startsWith('/api/'))return route.continue()
  const reply=json=>route.fulfill({json})
  if(req.method()!=='GET'&&!p.endsWith('/ws-token'))writes.push({path:p,method:req.method()})
  if(p==='/api/user/me')return reply({user_id:'owner',nickname:'问答验证'})
  if(p.endsWith('/state'))return reply({thread_id:'chat-thread',trip_city:'北京',trip_days:1})
  if(p.endsWith('/ws-token'))return reply({token:'fixed-ws',expires_in_seconds:3600})
  if(p.endsWith('/places'))return reply(places.map(p=>({...apiPlace(p),source:p.source,amap_photos:[],tags:[],room_selected:true})))
  if(p==='/api/weather')return reply({city:'北京',days:[]})
  if(p.endsWith('/current-itinerary'))return reply({room_id:roomId,version:1,published_at:'2026-09-13T00:00:00Z',selection_snapshot:{trip_days:1,place_ids:places.map(p=>p.placeId)},itinerary_data:{itinerary_id:'fixed-current',thread_id:'chat-thread',city:'北京',version:1,generated_at:'2026-09-13T00:00:00Z',days:[{day_index:0,cluster_id:0,slots:places.map((p,i)=>({place_id:p.placeId,place:apiPlace(p),tips:[],transport:i?null:{status:'AVAILABLE',mode:'driving',duration_mins:12,distance_km:2.4}}))}]}})
  return route.fulfill({status:404,json:{}})
 })
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message))
 await page.goto(`/room/${roomId}`)
 await expect(page.getByText('协同已连接',{exact:true})).toBeVisible()
 await expect(page.getByTestId('shared-route-status')).toContainText('共同路线 · 已同步')
 await expect(page.getByTestId('chat-input').filter({visible:true})).toBeVisible()
 const send=async(text='这些地点中，第一次来北京可以优先选哪一处？请简短回答。')=>{await page.getByTestId('chat-input').filter({visible:true}).fill(text);await page.getByTestId('chat-send').filter({visible:true}).click()}
 return {page,doc,before,writes,errors,send,close:async()=>{await context.close();doc.destroy()}}
}
for(const width of [1440,1280])test(`selected-place question displays the complete answer without changing route or choices at ${width}`,{tag:'@desktop'},async({browser},info)=>{
 const r=await setup(browser,width),{page}=r
 try{
  await r.send()
  const text=page.getByText(answer,{exact:true})
  await expect(text).toBeVisible();await expect(page.getByTestId('chat-assistant-message').filter({visible:true}).last()).toHaveAttribute('data-status','done');await expect(text).toHaveCSS('white-space','pre-wrap')
  expect(await page.evaluate(()=>window.__chatRequests)).toEqual([{thread_id:'chat-thread',user_id:'owner',room_id:roomId,message:'这些地点中，第一次来北京可以优先选哪一处？请简短回答。',selected_place_ids:places.map(p=>p.placeId),trip_city:'北京',use_long_term_memory:false}])
  expect(r.doc.getMap('places').toJSON()).toEqual(r.before);expect(r.writes).toEqual([])
  await expect(page.getByText('你好！我是你的旅行顾问',{exact:true})).toHaveCount(0)
  await expect(page.getByTestId('chat-assistant-message').filter({visible:true}).last().locator('..')).toHaveCSS('opacity','1')
  await page.screenshot({path:info.outputPath(`complete-answer-${width}.png`),fullPage:true})
  await page.reload()
  await expect(page.getByTestId('shared-route-status')).toContainText('共同路线 · 已同步')
  await expect(page.getByText(answer,{exact:true})).toHaveCount(0)
  expect(await page.evaluate(()=>window.__chatRequests)).toEqual([])
  expect(r.doc.getMap('places').toJSON()).toEqual(r.before);expect(r.writes).toEqual([]);expect(r.errors).toEqual([])
 }finally{await r.close()}
})
test('stop aborts the answer, retains partial text and rejects late delta and done',{tag:'@desktop'},async({browser})=>{
 const r=await setup(browser,1440),{page}=r
 try{
  await page.evaluate(()=>window.__chatMode='wait');await r.send()
  await expect(page.getByText('已经收到的建议。',{exact:true})).toBeVisible()
  await page.getByRole('button',{name:'停止回答',exact:true}).click()
  await expect(page.getByText('回答已停止，以上内容尚未完成。',{exact:true})).toBeVisible()
  expect(await page.evaluate(()=>window.__abortedChats)).toBe(1)
  await page.evaluate(()=>{const s=window.__chatStreams[0];s.send('text',{delta:'迟到内容不能复活'});s.send('done',{status:'READY'});s.close()})
  await expect(page.getByText(/迟到内容不能复活/)).toHaveCount(0)
  await expect(page.getByText('已经收到的建议。',{exact:true})).toBeVisible()
  await page.evaluate(()=>window.__chatMode='answer');await r.send('再回答一次')
  await expect(page.getByText(answer,{exact:true})).toBeVisible()
  expect(r.doc.getMap('places').toJSON()).toEqual(r.before);expect(r.writes).toEqual([]);expect(r.errors).toEqual([])
 }finally{await r.close()}
})
test('empty terminal and interrupted answer do not become a successful answer',{tag:'@desktop'},async({browser})=>{
 const r=await setup(browser,1280),{page}=r
 try{
  await page.evaluate(()=>window.__chatMode='empty');await r.send()
  await expect(page.getByText('没有收到有效回答，请重试。',{exact:true})).toBeVisible()
  await page.evaluate(()=>window.__chatMode='truncated');await r.send('保留已收到部分')
  await expect(page.getByText('已收到的部分回答',{exact:true})).toBeVisible()
  await expect(page.getByText('连接中断，以上回答尚未完成；请重试。',{exact:true})).toBeVisible()
  expect(r.writes).toEqual([]);await expect(page.getByTestId('chat-assistant-message').filter({visible:true}).last()).toHaveAttribute('data-status','error')
 }finally{await r.close()}
})
test('changed account credentials reject a late answer without expiring the new login',{tag:'@desktop'},async({browser})=>{
 const r=await setup(browser,1440),{page}=r
 try{
  await page.evaluate(()=>window.__chatMode='wait');await r.send()
  await expect(page.getByText('已经收到的建议。',{exact:true})).toBeVisible()
  await page.evaluate(()=>{localStorage.setItem('authToken','fixed-new-account');localStorage.setItem('authUser',JSON.stringify({userId:'new-account',nickname:'新同行'}));const s=window.__chatStreams[0];s.send('text',{delta:'旧账号迟到回答'});s.send('done',{status:'READY'});s.close()})
  await expect(page.getByText(/旧账号迟到回答/)).toHaveCount(0)
  expect(await page.evaluate(()=>localStorage.getItem('authToken'))).toBe('fixed-new-account')
  expect(r.writes).toEqual([]);expect(r.doc.getMap('places').toJSON()).toEqual(r.before)
 }finally{await r.close()}
})

test('late unauthorized response does not log out a newer account', {tag:'@desktop'}, async({browser})=>{
 const r=await setup(browser,1440),{page}=r
 try{
  await page.evaluate(()=>window.__chatMode='late401');await r.send()
  await expect.poll(()=>page.evaluate(()=>typeof window.__release401)).toBe('function')
  await page.evaluate(()=>{localStorage.setItem('authToken','fixed-next-account');window.__release401()})
  await expect(page.getByTestId('chat-send').filter({visible:true})).toBeVisible()
  expect(await page.evaluate(()=>localStorage.getItem('authToken'))).toBe('fixed-next-account')
  await expect(page).toHaveURL(new RegExp(`/room/${roomId}$`));expect(r.writes).toEqual([])
 }finally{await r.close()}
})
test('explicit place search still completes candidate output without choosing or planning', {tag:'@desktop'}, async({browser})=>{
 const r=await setup(browser,1280),{page}=r
 try{
  await page.evaluate(()=>window.__chatMode='search');await r.send('找一处博物馆供选择')
  await expect(page.getByText('找到一处博物馆，供你手动选择。',{exact:true})).toBeVisible()
  await expect(page.getByTestId('chat-assistant-message').filter({visible:true}).last()).toHaveAttribute('data-status','done')
  await expect.poll(()=>r.doc.getMap('places').get('place_search_new')?.name).toBe('首都博物馆')
  expect(r.doc.getMap('places').get('place_search_new').votedBy).toEqual([])
  for(const p of places)expect(r.doc.getMap('places').get(p.placeId)).toEqual(r.before[p.placeId])
  expect(r.writes.filter(row=>!row.path.endsWith('/places'))).toEqual([])
 }finally{await r.close()}
})

test('changed room selection explains the conflict without an automatic resend', {tag:'@desktop'}, async({browser})=>{
 const r=await setup(browser,1280),{page}=r
 try{
  await page.evaluate(()=>window.__chatMode='selection409');await r.send()
  await expect(page.getByText('地点选择正在同步或已变化，请稍后重新发送。',{exact:true})).toBeVisible()
  await expect(page.getByTestId('chat-assistant-message').filter({visible:true}).last()).toHaveAttribute('data-status','error')
  expect(await page.evaluate(()=>window.__chatRequests.length)).toBe(1)
  expect(r.doc.getMap('places').toJSON()).toEqual(r.before);expect(r.writes).toEqual([])
  await page.evaluate(()=>window.__chatMode='answer');await r.send('查看同步后的地点')
  await expect(page.getByText(answer,{exact:true})).toBeVisible()
  expect(await page.evaluate(()=>window.__chatRequests.length)).toBe(2)
 }finally{await r.close()}
})
