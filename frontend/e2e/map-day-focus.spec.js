// Synthetic API/SDK regression: selecting a date only projects existing results.
const {test,expect}=require('@playwright/test')
const R='synthetic-map-day-focus',P=`/api/v3/trip-understandings/${R}`
const position=(longitude,latitude)=>({longitude,latitude,coordinate_system:'GCJ02'})
const card=(id,name,city)=>({activity_token:id,name,city,category:'景点',status:'READY',area_or_address:'合成地址',time_hint:null,
  available_actions:['VIEW_DETAILS','REPLACE','DELETE','MOVE']})
const days=[{label:'Day 1',activities:[card('synthetic-bj-1-0000000000','北京合成公园','北京'),card('synthetic-bj-2-0000000000','北京合成古街','北京')]},
  {label:'Day 2',activities:[card('synthetic-sh-1-0000000000','上海合成公园','上海'),card('synthetic-sh-2-0000000000','上海合成古街','上海')]}]
const coordinates=[[116.4,39.9],[116.42,39.91],[121.48,31.23],[121.50,31.24]]
function mapView(){return {status:'AVAILABLE',message:'受控路线已准备',available_actions:[],
  points:days.flatMap((day,i)=>day.activities.map((c,j)=>({activity_token:c.activity_token,name:c.name,day_label:day.label,sequence_index:j,
    position:position(...coordinates[i*2+j])}))),
  lodging_points:[{point_token:'synthetic-bj-hotel',name:'北京合成酒店',day_label:'Day 1',position:position(116.41,39.9)},
    {point_token:'synthetic-sh-hotel',name:'上海合成酒店',day_label:'Day 2',position:position(121.49,31.23)}],
  days:days.map((day,i)=>({label:day.label,day_index:i+1,routes:[{from_activity_token:day.activities[0].activity_token,to_activity_token:day.activities[1].activity_token,
    from_name:day.activities[0].name,to_name:day.activities[1].name,selected_mode:'walking',message:'合成步行',
    walking:{status:'AVAILABLE',duration_minutes:10,distance_meters:900,transfer_count:0,geometry:coordinates.slice(i*2,i*2+2).map(([longitude,latitude])=>({longitude,latitude}))},
    transit:{status:'UNAVAILABLE',duration_minutes:null,distance_meters:null,transfer_count:null,geometry:[]}}]}))}}
async function fixture(page, {pending=false}={}){
  const writes=[]
  await page.addInitScript(()=>{
    window.__mapFocus={fits:[],lines:[]}
    window.AMap={Map:class{
      constructor(container){this.container=container}
      on(name,callback){if(name==='complete')setTimeout(callback,0)}
      add(items){items.filter(x=>x.content).forEach((x,index)=>{Object.assign(x.content.style,{position:'absolute',left:(20+(index%3)*25)+'%',top:(28+Math.floor(index/3)*28)+'%'});this.container.append(x.content)});window.__mapFocus.lines=items.filter(x=>x.path).map(x=>({path:x.path,color:x.strokeColor}))}
      remove(items){items.forEach(x=>x.content?.remove())}
      setFitView(items){window.__mapFocus.fits.push(items.filter(x=>x.position).map(x=>x.position))}
      setCenter(){} resize(){} destroy(){} zoomIn(){} zoomOut(){}
    },Marker:class{constructor(options){Object.assign(this,options)}},Polyline:class{constructor(options){Object.assign(this,options)}}}
  })
  await page.route('**/webapi.amap.com/**',r=>r.abort())
  await page.route('**/restapi.amap.com/**',r=>r.abort())
  await page.route('**/api/user/me',r=>r.fulfill({status:401,json:{}}))
  await page.route('**/api/v3/trip-understandings/**',route=>{
    const req=route.request(),action=new URL(req.url()).pathname.slice(P.length)
    if(req.method()!=='GET')writes.push({action,method:req.method()})
    const reply=(json,status=200)=>route.fulfill({status,json,headers:{ETag:'"synthetic-map-v1"'}})
    if(action==='/result')return reply({status:'READY',ownership:'ANONYMOUS',is_demo:false,assumptions:[{key:'destination',label:'目的地',value:'北京、上海',editable:true}],days: days.map((day, index) => ({...day, activities:[...day.activities, ...(pending && index === 1 ? [{...card('synthetic-pending-0000000000','尚未确认的上海分馆','上海'),status:'NEEDS_CONFIRMATION',category:'场馆'}] : [])]})),
      map:{status:'AVAILABLE',message:'受控路线',available_actions:[]},stay:{status:'UNAVAILABLE',message:'未配置合成建议',candidates:[],searched_scopes:[],area_summary:null,available_actions:[]},available_actions:['EDIT_ASSUMPTIONS','EDIT_CARDS']})
    if(action==='/map-renders/latest')return reply(mapView())
    if(action==='/stay-suggestions')return reply({status:'UNAVAILABLE',message:'未配置合成建议',candidates:[],searched_scopes:[],area_summary:null,available_actions:[]})
    if(action==='/daily-dining')return reply({status:'UNAVAILABLE',message:'未配置合成餐饮',days:[]})
    if(action==='/supplementary')return reply({status:'AVAILABLE',days:[]})
    if(action==='/materialize')return reply({status:'READY',message:'已准备',calendar:'按日期',party_size:2,checks_available:true})
    if(action==='/checks')return reply({status:'STILL_NEEDS_CONFIRMATION',message:'受控检查',items:[],remaining_must_adjust:0,available_actions:[]})
    return reply({},404)
  })
  await page.goto(`/trip/result#trip=${R}`)
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  await page.getByRole('button',{name:'地图',exact:true}).click()
  await expect(page.locator('.e-map-marker')).toHaveCount(6)
  await page.waitForLoadState('networkidle')
  return writes
}
for(const width of [1440,390])test(`cross-city map focuses one day and restores all existing geometry at ${width}px`,async({page})=>{
  await page.setViewportSize({width,height:950})
  const writes=await fixture(page)
  const legend=page.getByLabel('日期颜色与预演选择'),scope=page.getByTestId('map-theater')
  await expect(legend.getByRole('button',{name:'全部行程',exact:true})).toHaveAttribute('aria-pressed','true')
  const before=writes.length
  const sh=legend.getByRole('button',{name:'Day 2',exact:true})
  await sh.focus();await page.keyboard.press('Enter')
  await expect(page.locator('.e-map-marker')).toHaveCount(3)
  await expect(page.locator('.e-map-marker[aria-label="查看北京合成公园"]')).toHaveCount(0)
  await expect(page.locator('.e-map-lodging-marker')).toHaveAttribute('aria-label','查看住宿位置 上海合成酒店')
  await expect.poll(()=>page.evaluate(()=>window.__mapFocus.lines)).toEqual([{path:[[121.48,31.23],[121.5,31.24]],color:'#2563eb'}])
  await expect.poll(()=>page.evaluate(()=>window.__mapFocus.fits.at(-1)?.every(p=>p[0]>120))).toBe(true)
  await scope.locator('summary').filter({hasText:/^路线$/}).click()
  await expect(page.getByTestId('map-route-summary')).toHaveCount(1)
  await expect(page.getByTestId('map-route-summary')).toContainText('上海合成公园')
  const popover=await page.getByTestId('map-route-tools').boundingBox()
  expect(popover.x).toBeGreaterThanOrEqual(8)
  expect(popover.x+popover.width).toBeLessThanOrEqual(width-8)
  await page.getByLabel('路线文字摘要').locator('summary').click()
  await page.screenshot({path:test.info().outputPath('day-2-map.png'),fullPage:true})
  const stayPanel=scope.getByTestId('stay-panel')
  await stayPanel.locator('summary').first().click()
  await expect(stayPanel.getByLabel('住宿建议',{exact:true})).toBeVisible()
  await expect(page.getByTestId('map-route-tools')).toBeHidden()
  await scope.locator('summary').filter({hasText:/^路线$/}).click()
  await expect(page.getByTestId('map-route-tools')).toBeVisible()
  await expect(stayPanel.getByLabel('住宿建议',{exact:true})).toBeHidden()
  await legend.getByRole('button',{name:'Day 1',exact:true}).click()
  await expect.poll(()=>page.evaluate(()=>window.__mapFocus.fits.at(-1)?.every(p=>p[0]<120))).toBe(true)
  await expect(page.locator('.e-map-marker')).toHaveCount(3)
  await legend.getByRole('button',{name:'全部行程',exact:true}).click()
  await expect(page.locator('.e-map-marker')).toHaveCount(6)
  await expect.poll(()=>page.evaluate(()=>window.__mapFocus.lines.length)).toBe(2)
  await expect(page.getByTestId('map-route-summary')).toHaveCount(2)
  expect(writes.slice(before)).toEqual([])
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true)
})

// Fixed SDK/API only: filtering changes the directory, never invents map coordinates.
for (const width of [1440,390]) test(`map directory and selected suggestions stay in sync at ${width}px`,async({page})=>{
  await page.setViewportSize({width,height:950})
  const writes=await fixture(page,{pending:true})
  if(width<1024)await page.getByTestId('map-directory-toggle').click()
  const directory=page.getByTestId('map-place-directory')
  await expect(directory.getByTestId('map-directory-place')).toHaveCount(5)
  await directory.getByLabel('搜索本行程地点').fill('上海')
  await expect(directory.getByTestId('map-directory-place')).toHaveCount(3)
  await directory.getByLabel('地点确认状态').selectOption('NEEDS_CONFIRMATION')
  await expect(directory.getByTestId('map-directory-place')).toHaveCount(1)
  await expect(directory.getByTestId('map-directory-place')).toContainText('尚未确认的上海分馆')
  await expect(page.locator('.e-map-marker')).toHaveCount(6)
  await expect(page.locator('.e-map-marker[aria-label*=尚未确认]')).toHaveCount(0)
  await directory.getByLabel('地点确认状态').selectOption('all')
  await directory.getByLabel('地点类别').selectOption('场馆')
  await expect(directory.getByTestId('map-directory-place')).toHaveCount(1)
  await directory.getByLabel('地点类别').selectOption('all')
  await directory.getByLabel('搜索本行程地点').fill('无此地名')
  await expect(directory).toContainText('没有符合筛选条件的地点')
  await directory.getByLabel('搜索本行程地点').fill('')
  const selected=directory.getByTestId('map-directory-place').filter({hasText:'上海合成古街'})
  await selected.click()
  await expect(selected).toHaveAttribute('aria-pressed','true')
  if(width<1024){await directory.getByRole('button',{name:'收起',exact:true}).click();await page.getByTestId('journey-suggestions-toggle').click()}
  const suggestions=page.getByRole('complementary',{name:'检查与建议',exact:true})
  await expect(suggestions.getByLabel('当前选中地点')).toContainText('上海合成古街')
  await expect(suggestions.getByLabel('当前选中地点')).toContainText('Day 2')
  await suggestions.getByRole('button',{name:'顺路优化',exact:true}).click()
  await expect(suggestions).toContainText('暂无已核验的顺路调整方案')
  await suggestions.getByRole('button',{name:'住宿',exact:true}).click()
  await expect(suggestions.getByRole('button',{name:'更新住宿建议',exact:true})).toBeVisible()
  await page.screenshot({path:test.info().outputPath(`three-pane-${width}.png`),fullPage:true})
  await suggestions.getByRole('button',{name:'关闭建议',exact:true}).click()
  await page.locator('.e-map-marker[aria-label="查看北京合成公园"]').click()
  if(width<1024)await page.getByTestId('map-directory-toggle').click()
  await expect(directory.getByTestId('map-directory-place').filter({hasText:'北京合成公园'})).toHaveAttribute('aria-pressed','true')
  const cardTab=width<1024 ? 'mobile-nav-itinerary':'desktop-nav-itinerary'
  await page.getByTestId(cardTab).click()
  await page.getByTestId(width<1024 ? 'mobile-nav-map_stay':'desktop-nav-map_stay').click()
  if(width<1024)await page.getByTestId('journey-suggestions-toggle').click()
  await expect(suggestions.getByLabel('当前选中地点')).toContainText('北京合成公园')
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true)
  expect(writes.filter(write=>write.action!=='/materialize')).toEqual([])
})
