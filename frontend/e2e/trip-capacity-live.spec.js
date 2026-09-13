const {test,expect}=require('@playwright/test')
const fs=require('node:fs')
const path=require('node:path')
const {randomUUID}=require('node:crypto')

// Explicit live run only: one generated stress input, real worker/model/places,
// persisted commands and UI. Repeated visits test capacity, not route feasibility.
test.skip(process.env.RUN_CAPACITY_LIVE!=='1','Use the dedicated local live config')
const NAMES=['故宫博物院','景山公园','天坛公园','颐和园','圆明园遗址公园','北海公园',
 '北京动物园','中国国家博物馆','中国科学技术馆','北京天文馆','中国美术馆','首都博物馆']
const days=Array.from({length:14},(_,i)=>NAMES.slice(0,i<6?12:11))
const source='北京14天行程。每天按各自清单顺序依次到访；同一地点出现在不同天时是再次到访，保留每一次安排。\n'+
 days.map((names,i)=>`Day${i+1}：${names.join('、')}。`).join('\n')
const API='/api/v3/trip-understandings'

test('live 160 visits: confirm, persist, edit, undo and export every day on desktop and mobile',async({page},info)=>{
 const out=path.resolve(__dirname,'../../.local-artifacts/evaluation',process.env.CAPACITY_LIVE_LABEL)
 fs.mkdirSync(out,{recursive:true})
 const report={source_type:'AGENT_AUTHORED_CAPACITY_STRESS_INPUT',source,expectedDays:days,steps:[],pageErrors:[]}
 const save=()=>fs.writeFileSync(path.join(out,'result.json'),JSON.stringify(report,null,2))
 let resource,routePosts=0
 page.on('pageerror',error=>report.pageErrors.push(error.name+': '+error.message))
 page.on('request',request=>{if(request.method()==='POST'&&new URL(request.url()).pathname.endsWith('/map-renders'))routePosts++})
 await page.addInitScript(()=>{
  window.capacityExportText=[]
  const draw=CanvasRenderingContext2D.prototype.fillText
  CanvasRenderingContext2D.prototype.fillText=function(text,x,y,...rest){
   const point=this.getTransform().transformPoint({x,y})
   window.capacityExportText.push({text:String(text),x:point.x,y:point.y,width:this.measureText(String(text)).width,
    canvasWidth:this.canvas.width,canvasHeight:this.canvas.height})
   return draw.call(this,text,x,y,...rest)
  }
 })
 async function read(){
  const response=await page.request.get(`${API}/${resource}/result`)
  expect(response.status()).toBe(200)
  return response.json()
 }
 function assertVisits(result){
  expect(result.days).toHaveLength(14)
  expect(result.days.map(day=>day.activities.map(card=>card.name))).toEqual(days)
  expect(result.days.reduce((n,day)=>n+(day.alternatives||[]).length,0)).toBe(0)
 }
 async function verifyPage(){
  await expect(page.getByTestId('activity-card')).toHaveCount(160)
  for(let i=0;i<14;i++)expect(await page.getByTestId(`day-lane-${i+1}`).getByTestId('activity-card').getByRole('heading').allTextContents()).toEqual(days[i])
 }
 async function settlePendingSave(){
  const recovery=page.getByRole('button',{name:'确认保存结果',exact:true})
  if(await recovery.isVisible()){
   await recovery.click()
   await expect(recovery).toHaveCount(0,{timeout:30000})
   report.steps.push('pending save recovered through the page')
  }
  await expect(page.getByRole('button',{name:'导出图片',exact:true})).toBeEnabled({timeout:30000})
 }
 try{
  if(process.env.CAPACITY_LIVE_RESUME_LABEL){
   const previous=JSON.parse(fs.readFileSync(path.resolve(out,'..',process.env.CAPACITY_LIVE_RESUME_LABEL,'result.json'),'utf8'))
   expect(previous.source).toBe(source)
   resource=previous.resource
   report.resumes=process.env.CAPACITY_LIVE_RESUME_LABEL
   await page.goto(`/trip/result#trip=${resource}`)
  }else{
   await page.goto('/')
   await page.getByTestId('trip-source-text').fill(source)
   const accepted=page.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname===API)
   await page.getByTestId('create-full-trip').click()
   const response=await accepted
   expect(response.status()).toBe(202)
   resource=(await response.json()).public_resource_id
  }
  report.resource=resource;save()
  await page.context().storageState({path:path.join(out,'browser-state.json')})
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible({timeout:180000})
  await settlePendingSave()
  report.initial=await read();save();assertVisits(report.initial)
  const pending=report.initial.days.flatMap(day=>day.activities).filter(card=>card.status!=='READY')
  report.initialConfirmed=160-pending.length;report.steps.push('160 dated visits persisted');save()
  // Only the known unresolved identity is expected. Never accept an arbitrary
  // first search result merely to make all cards ready.
  for(let i=0;i<pending.length;i++){
   // Commands create immutable versions with new opaque activity tokens.
   // Reread the remaining visits instead of using a token from an old version.
   const current=await read();assertVisits(current)
   const card=current.days.flatMap(day=>day.activities).find(item=>item.status!=='READY')
   expect(card).toBeDefined()
   expect(card.name).toBe('北京天文馆')
   const panel=page.getByTestId('unresolved-places')
   if(await panel.getAttribute('open')===null)await panel.locator('summary').click()
   await page.locator(`[id="recover-${card.activity_token}"]`).click()
   const dropdown=page.getByTestId('pending-place-dropdown')
   await dropdown.getByLabel('查询城市',{exact:true}).selectOption('北京')
   await dropdown.getByRole('button',{name:'搜索',exact:true}).click()
   const option=dropdown.locator('.pending-place-option').filter({has:page.locator('strong',{hasText:/^北京天文馆$/})})
   await expect(option).toHaveCount(1,{timeout:30000})
   await expect(option).toContainText(/西直门外大街.*138/)
   await option.click()
   await dropdown.getByRole('button',{name:'使用这个地点',exact:true}).click()
   await expect(dropdown).toHaveCount(0,{timeout:30000})
  }
  await page.reload();await verifyPage()
  report.confirmed=await read();assertVisits(report.confirmed)
  expect(report.confirmed.days.flatMap(day=>day.activities).every(card=>card.status==='READY')).toBe(true)
  expect.soft(report.confirmed.coverage.complete,'Any retained semantic warning still fails full acceptance').toBe(true)
  report.steps.push('all identities confirmed and reload retained 160');save()
  // The last day remains editable after all visits have been persisted.
  await page.getByTestId('drag-handle-14-0').press('Enter')
  await page.keyboard.press('ArrowUp');await page.keyboard.press('Enter')
  await expect.poll(async()=>((await read()).days[13].activities.length)).toBe(10)
  await page.getByRole('button',{name:/撤销/}).first().click()
  await expect.poll(async()=>((await read()).days[13].activities.length)).toBe(11)
  // A committed server result can arrive before the browser consumes the
  // response. Wait for its write lock before reloading the page.
  await expect(page.getByRole('button',{name:'导出图片',exact:true})).toBeEnabled()
  await page.reload();await verifyPage();await settlePendingSave();assertVisits(await read())
  report.steps.push('last-day move and undo preserved all visits');save()
  for(const width of [1440,390]){
   await page.setViewportSize({width,height:900});await verifyPage()
   await page.getByTestId('activity-card').last().scrollIntoViewIfNeeded()
   await expect(page.getByTestId('activity-card').last()).toBeVisible()
   await page.screenshot({path:info.outputPath(`last-day-${width}.png`)})
   await page.evaluate(()=>{window.capacityExportText=[]})
   await page.getByRole('button',{name:'导出图片',exact:true}).click()
   const pendingDownload=page.waitForEvent('download')
   await page.getByTestId('download-itinerary-png').click()
   const png=await pendingDownload;expect(await png.failure()).toBeNull()
   await png.saveAs(info.outputPath(`complete-14-days-${width}.png`))
   const drawn=await page.evaluate(()=>window.capacityExportText)
   for(const name of NAMES){
    const occurrences=drawn.filter(item=>item.text===name)
    expect(occurrences).toHaveLength(days.flat().filter(item=>item===name).length)
    for(const item of occurrences){expect(item.y).toBeGreaterThan(0);expect(item.y).toBeLessThan(item.canvasHeight);expect(item.x+item.width).toBeLessThan(item.canvasWidth)}
   }
   for(let day=1;day<=14;day++)expect(drawn.some(item=>item.text===`Day ${day}`)).toBe(true)
   await page.getByLabel('关闭图片预览').click()
   report.steps.push(`${width}px all 160 cards and 14-day PNG`);save()
  }
  await page.getByRole('button',{name:'保存到账号',exact:true}).click()
  await page.getByRole('button',{name:'注册账号',exact:true}).click()
  await page.getByLabel('邮箱',{exact:true}).fill(`capacity-${randomUUID()}@example.test`)
  await page.getByLabel('密码',{exact:true}).fill(`Test${randomUUID()}9`)
  await page.getByRole('button',{name:'注册并继续',exact:true}).click()
  await expect(page.getByRole('button',{name:'已保存到账号',exact:true})).toBeVisible({timeout:30000})
  const anonymousResource=resource
  resource=new URLSearchParams(new URL(page.url()).hash.slice(1)).get('trip')
  expect(resource).toBeTruthy();expect(resource).not.toBe(anonymousResource)
  report.savedResource=resource;save()
  const savedRead=page.waitForResponse(response=>new URL(response.url()).pathname===`${API}/${resource}/result`&&response.status()===200)
  await page.reload();await verifyPage()
  // Observe the page's authenticated request; APIRequestContext only has the
  // old anonymous cookie and does not attach the app's account authorization.
  report.final=await (await savedRead).json();assertVisits(report.final)
  report.steps.push('saved to account and reopened');report.status=info.errors.length?'FAILED':'PASSED'
  expect(routePosts).toBe(0);expect(report.pageErrors).toEqual([])
 }catch(error){
  report.status='FAILED';report.failure=error.message;throw error
 }finally{
  report.routePosts=routePosts;report.finalPageUrl=page.url();save()
  await page.context().storageState({path:path.join(out,'browser-state.json')})
 }
})
