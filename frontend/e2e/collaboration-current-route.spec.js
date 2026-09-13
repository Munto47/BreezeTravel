// Fixed member-authorized HTTP and an in-process Yjs protocol peer. No model,
// map provider, account database or production room participates in these tests.
const {test, expect} = require('@playwright/test')
const Y = require('yjs')
const sync = require('y-protocols/sync')
const encoding = require('lib0/encoding')
const decoding = require('lib0/decoding')
test.use({actionTimeout:10000})

const roomId = 'COMMONFIXED'
const names = ['青溪公园', '星河博物馆', '晚晴园', '清风广场']
const places = names.map((name, i) => ({placeId:`common-${i}`, name, category:'attraction',
  address:'固定地址', city:'北京', coords:{lng:116.39 + i * .005, lat:39.91 + i * .003},
  source:'amap_poi', amapPhotos:[], tags:[], votedBy:['room-selection'], addedBy:'room',
  addedAt:'2026-09-13T00:00:00Z', note:'', isPinned:false}))
const apiPlace = p => ({place_id:p.placeId, name:p.name, category:p.category, address:p.address,
  city:p.city, coords:p.coords, source:p.source, amap_photos:[], tags:[]})
function itinerary(selected, version) {
  if(version>1)selected=[...selected].reverse()
  return {itinerary_id:`common-v${version}`, thread_id:'common-thread', city:'北京', version,
    generated_at:'2026-09-13T00:00:00Z', days:[{day_index:0,cluster_id:0,
      slots:selected.map((p, i) => ({place_id:p.place_id,place:p,tips:[],
        transport:i===selected.length-1 ? null : {status:'AVAILABLE',mode:'driving',duration_mins:12,distance_km:2.4}}))},
      {day_index:1,cluster_id:1,slots:[]} ]}
}

async function setup(browser, ownerWidth) {
  const state = {version:0, current:null, selection:null, posts:[], personalWrites:[], imports:[], gets:[],
    external:[], errors:[], chat:0, loseNext:false, holdOwner:false, heldOwner:null, heldRead:null, holdNextRead:false,
    holdPersonal:false,heldPersonal:null}
  const doc = new Y.Doc(), peers = new Set()
  const personal = new Map()
  places.forEach(p => doc.getMap('places').set(p.placeId,p))
  function wire(write) {const out=encoding.createEncoder();encoding.writeVarUint(out,0);write(out);return Buffer.from(encoding.toUint8Array(out))}
  doc.on('update', (update, origin) => {
    const payload=wire(out=>sync.writeUpdate(out,update))
    for(const peer of peers) if(peer!==origin) try {peer.send(payload)} catch {}
  })
  state.snapshot = () => ({room_id:roomId,version:state.version,itinerary_data:state.current,
    selection_snapshot:state.selection,published_at:state.current?'2026-09-13T00:00:00Z':null})
  const contexts=[]
  for (const [user,width] of [['owner',ownerWidth],['peer',ownerWidth===1440?1280:1440]]) {
    const context=await browser.newContext({viewport:{width,height:1000}})
    contexts.push(context)
    await context.addInitScript(user=>{
      localStorage.setItem('authToken',`fixed-${user}`)
      localStorage.setItem('authUser',JSON.stringify({userId:user,nickname:user==='owner'?'同行甲':'同行乙'}))
    },user)
    await context.routeWebSocket(url => url.pathname === `/${roomId}`, ws => {
      peers.add(ws)
      ws.onMessage(message=>{
        if(!Buffer.isBuffer(message))throw new Error(`Expected binary Yjs message, got ${typeof message} (${String(message).length} chars)`)
        const input=decoding.createDecoder(new Uint8Array(message))
        if(decoding.readVarUint(input)!==0)return
        const out=encoding.createEncoder();encoding.writeVarUint(out,0)
        try {sync.readSyncMessage(input,out,doc,ws)} catch(error) {
          throw new Error(`Invalid fixed Yjs sync frame (${message.length} bytes, ${message.subarray(0,8).toString('hex')}): ${error.message}`)
        }
        if(encoding.length(out)>1)ws.send(Buffer.from(encoding.toUint8Array(out)))
      })
      ws.onClose(()=>peers.delete(ws))
      ws.send(wire(out=>sync.writeSyncStep1(out,doc)))
      ws.send(wire(out=>sync.writeSyncStep2(out,doc)))
    })
    await context.route('**/*', async route=>{
      const req=route.request(),url=new URL(req.url()),path=url.pathname
      if(!['127.0.0.1','localhost'].includes(url.hostname)){state.external.push(url.hostname);return route.abort()}
      if(!path.startsWith('/api/'))return route.continue()
      const reply=json=>route.fulfill({json})
      if(path==='/api/user/me')return reply({user_id:user,nickname:user})
      if(path.endsWith('/state'))return reply({thread_id:'common-thread',trip_city:'北京',trip_days:2})
      if(path.endsWith('/ws-token'))return reply({token:'fixed-yjs-no-auth-authority',expires_in_seconds:3600})
      if(path.endsWith('/places'))return reply([])
      if(path==='/api/weather')return reply({city:'北京',days:[]})
      if(path==='/api/chat'){state.chat++;return route.fulfill({status:503,json:{}})}
      if(path.endsWith('/current-itinerary')){
        expect(req.headers().authorization).toBe(`Bearer fixed-${user}`)
        const snapshot=structuredClone(state.snapshot())
        state.gets.push({user,version:snapshot.version})
        if(user==='peer' && state.holdNextRead){
          state.holdNextRead=false
          await new Promise(resolve=>{state.heldRead=resolve})
        }
        return reply(snapshot)
      }
      if(path.endsWith('/itinerary')){
        if(req.method()==='POST'){
          state.personalWrites.push({user,body:req.postDataJSON()})
          personal.set(user,req.postDataJSON().itinerary_data)
          if(state.holdPersonal){state.holdPersonal=false;await new Promise(resolve=>{state.heldPersonal=resolve})}
          return reply({ok:true,itinerary_id:`personal-${user}`})
        }
        return personal.has(user)?reply({itinerary_data:personal.get(user)}):route.fulfill({status:404,json:{}})
      }
      if(path==='/api/optimize'){
        const body=req.postDataJSON()
        expect(body.relative_only).toBe(true)
        expect(body.base_room_route_version).toBeGreaterThanOrEqual(0)
        expect(body.room_route_request_id).toMatch(/^[0-9a-f-]{36}$/)
        expect(body.start_date).toBeUndefined()
        state.posts.push({user,body})
        if(user==='owner' && state.holdOwner){state.holdOwner=false;await new Promise(resolve=>{state.heldOwner=resolve})}
        if(body.base_room_route_version!==state.version)
          return route.fulfill({status:409,json:{detail:{code:'ROOM_ROUTE_VERSION_CONFLICT'}}})
        state.version++
        state.current=itinerary(body.places,state.version)
        state.selection={trip_days:body.trip_days,place_ids:body.places.map(p=>p.place_id)}
        if(state.loseNext){state.loseNext=false;return route.abort('failed')}
        return reply({itinerary:state.current,backup_pool:[],room_route_version:state.version,total_distance_km:7.2})
      }
      if(path==='/api/v3/trip-understandings/from-collaboration'){
        state.imports.push({user,body:req.postDataJSON()})
        return route.fulfill({status:409,json:{detail:{code:'ROOM_ROUTE_VERSION_CONFLICT'}}})
      }
      return route.fulfill({status:404,json:{}})
    })
  }
  const [page,peer]=await Promise.all(contexts.map(context=>context.newPage()))
  for(const p of [page,peer])p.on('pageerror',error=>state.errors.push(error.message))
  await Promise.all([page,peer].map(p=>p.goto(`/room/${roomId}`)))
  for(const p of [page,peer]){
    await expect(p.getByText('协同已连接',{exact:true})).toBeVisible()
    await expect(p.getByTestId('shared-route-status')).toContainText('房间尚未发布共同路线')
    await expect(p.getByRole('button',{name:/智能排线/})).toBeEnabled()
    await expect(p.getByRole('button',{name:`取消选择 ${names[0]}`,exact:true})).toBeVisible()
  }
  return {page,peer,state,doc,close:async()=>{await Promise.all(contexts.map(c=>c.close()));doc.destroy()}}
}

async function common(page, version, expectedNames) {
  await expect(page.getByTestId('shared-route-status')).toContainText('共同路线 · 已同步')
  await page.getByRole('button',{name:'已排路线',exact:true}).click()
  await expect(page.locator('[data-testid="collaboration-day-1"]:visible').getByTestId('collaboration-stop-name')).toHaveText(expectedNames)
  await expect(page.locator('[data-testid="collaboration-relative-itinerary"]:visible')).toContainText('12 分钟 · 2.4 km')
}

for(const width of [1440,1280]) test(`shared route publishes once, peer reads, selections invalidate and refresh keeps server transport at ${width}`, {tag:'@desktop'}, async({browser},info)=>{
  const run=await setup(browser,width),{page,peer,state,doc}=run
  try {
    await page.getByRole('button',{name:/智能排线/}).click()
    await common(page,1,names);await common(peer,1,names)
    expect(state.posts).toHaveLength(1);expect(state.personalWrites).toEqual([])
    await Promise.all([page,peer].map(p=>p.reload()))
    await common(page,1,names);await common(peer,1,names)
    expect(state.posts).toHaveLength(1);expect(state.personalWrites).toEqual([])
    await peer.getByRole('button',{name:/^候选地点(?:\s+\d+)?$/}).click()
    await peer.getByRole('button',{name:`取消选择 ${names[3]}`,exact:true}).click()
    for(const p of [page,peer])await expect(p.getByTestId('shared-route-status')).toContainText('原共同路线需要更新')
    expect(state.posts).toHaveLength(1)
    await expect(peer.getByRole('button',{name:'转入行程查',exact:true})).toHaveCount(0)
    await page.getByRole('button',{name:/智能排线/}).click()
    await common(page,2,names.slice(0,3).reverse());await common(peer,2,names.slice(0,3).reverse())
    await expect(peer.getByTestId('shared-route-status')).not.toContainText('需要更新')
    state.holdPersonal=true
    await peer.getByRole('button',{name:'保存到我的行程',exact:true}).click()
    await expect.poll(()=>Boolean(state.heldPersonal)).toBe(true)
    const sameVersionRead=peer.waitForResponse(response=>new URL(response.url()).pathname.endsWith('/current-itinerary'))
    await peer.getByRole('button',{name:'重新读取共同路线'}).click()
    await (await sameVersionRead).finished()
    await peer.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))))
    state.heldPersonal()
    await expect(peer.getByText('个人副本已保存',{exact:true})).toBeVisible()
    expect(state.personalWrites).toHaveLength(1)
    expect(state.personalWrites[0].user).toBe('peer')
    const original=structuredClone(state.snapshot())
    await peer.getByRole('button',{name:'转入行程查',exact:true}).click()
    await expect.poll(()=>state.imports.length).toBe(1)
    expect(state.imports[0].body).toEqual({room_id:roomId,room_route_version:2})
    expect(state.snapshot()).toEqual(original)
    expect(state.posts).toHaveLength(2)
    for(const p of [page,peer]) {
      expect(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
      for(const button of await p.locator('header').getByRole('button').all()) {
        if(!await button.isVisible())continue
        const box=await button.boundingBox()
        expect(box.x).toBeGreaterThanOrEqual(0)
        expect(box.x+box.width).toBeLessThanOrEqual(p.viewportSize().width)
      }
    }
    await page.screenshot({path:info.outputPath(`common-owner-${width}.png`),fullPage:true})
    await peer.screenshot({path:info.outputPath(`common-peer-${width===1440?1280:1440}.png`),fullPage:true})
    await page.getByRole('button',{name:'查看行程',exact:true}).click()
    await expect(page).toHaveURL(/\/itinerary\?shared=1$/)
    await expect(page.getByText('共同路线 · 已同步',{exact:true})).toBeVisible()
    await expect(page.getByTestId('collaboration-route-available')).toHaveCount(2)
    expect(Object.keys(doc.getMap('room').toJSON()).every(key=>!['itinerary','selection_snapshot'].includes(key))).toBe(true)
    expect(state.errors).toEqual([]);expect(state.chat).toBe(0)
  } finally {state.heldPersonal?.();await run.close()}
})

test('shared route recovers a lost publish response with GET and no second optimization', {tag:'@desktop'}, async({browser})=>{
  const run=await setup(browser,1440),{page,peer,state}=run
  try {
    state.loseNext=true
    await page.getByRole('button',{name:/智能排线/}).click()
    await common(page,1,names);await common(peer,1,names)
    await expect(page.getByTestId('shared-route-status')).toContainText('没有自动重新排线')
    expect(state.posts).toHaveLength(1);expect(state.personalWrites).toEqual([])
    expect(state.errors).toEqual([])
  } finally {await run.close()}
})

test('shared route rejects a competing publish and ignores an older delayed GET', {tag:'@desktop'}, async({browser},info)=>{
  const run=await setup(browser,1440),{page,peer,state}=run
  try {
    state.holdOwner=true
    await page.getByRole('button',{name:/智能排线/}).click()
    await expect.poll(()=>Boolean(state.heldOwner)).toBe(true)
    await peer.getByRole('button',{name:/智能排线/}).click()
    await common(peer,1,names)
    state.heldOwner()
    await common(page,1,names)
    expect(state.version).toBe(1);expect(state.posts).toHaveLength(2)
    await expect(page.getByTestId('shared-route-status')).toContainText('没有自动重新排线')
    state.holdNextRead=true
    await peer.getByRole('button',{name:'重新读取共同路线'}).click()
    await expect.poll(()=>Boolean(state.heldRead)).toBe(true)
    await page.getByRole('button',{name:/智能排线/}).click()
    await common(page,2,[...names].reverse());await common(peer,2,[...names].reverse())
    const older=peer.waitForResponse(async response=>new URL(response.url()).pathname.endsWith('/current-itinerary') && (await response.json()).version===1)
    state.heldRead()
    await (await older).finished()
    await peer.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))))
    await common(peer,2,[...names].reverse())
    expect(state.version).toBe(2);expect(state.posts).toHaveLength(3)
    expect(state.personalWrites).toEqual([])
    await peer.screenshot({path:info.outputPath('new-common-survives-old-get-1280.png'),fullPage:true})
    expect(state.errors).toEqual([])
  } finally {state.heldOwner?.();state.heldRead?.();await run.close()}
})

test('leaving a room while its shared read is pending cannot restore its route or save a personal copy', {tag:'@desktop'}, async({browser})=>{
  const run=await setup(browser,1280),{page,peer,state}=run
  try {
    await page.getByRole('button',{name:/智能排线/}).click()
    await common(peer,1,names)
    state.holdNextRead=true
    await peer.getByRole('button',{name:'重新读取共同路线'}).click()
    await expect.poll(()=>Boolean(state.heldRead)).toBe(true)
    await peer.getByRole('button',{name:'主界面',exact:true}).click()
    await expect(peer).toHaveURL(/\/$/)
    state.heldRead()
    await peer.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))))
    await expect(peer.getByTestId('shared-route-status')).toHaveCount(0)
    await expect(peer).toHaveURL(/\/$/)
    expect(state.posts).toHaveLength(1);expect(state.personalWrites).toEqual([])
    expect(state.errors).toEqual([])
  } finally {state.heldRead?.();await run.close()}
})
