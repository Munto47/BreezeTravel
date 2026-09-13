const {test, expect} = require('@playwright/test')
const {execFileSync} = require('node:child_process')
const fs = require('node:fs'), path = require('node:path'), ts = require('typescript')

let states
test.beforeAll(() => {
  states = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-X', 'utf8',
    path.resolve(__dirname, 'fixtures/stay-selection-replay.py')], {cwd:path.resolve(__dirname, '../../backend'), encoding:'utf8',
    env:{...process.env, PYTHONPATH:'.', RUNTIME_PROFILE:'test', PYTHONIOENCODING:'utf-8'}}))
})

function displayHelper() {
  const source = fs.readFileSync(path.resolve(__dirname, '../src/app/trip/result/lodging-export.ts'), 'utf8')
  const js = ts.transpileModule(source, {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
  const module = {exports:{}}
  new Function('module','exports',js)(module,module.exports)
  return module.exports.lodgingExportLines
}

function multiSegment() {
  const result = structuredClone(states.selected)
  result.days.push({label:'Day 3',activities:[],alternatives:[]})
  const first = result.stay.segments[0]
  const candidate = first.candidates.find(item => item.selected)
  candidate.name = '北京北城历史街区家庭旅行连锁酒店（钟楼南侧独立接待楼店）'
  candidate.area_or_address = '固定北京地址'
  const second = structuredClone(first)
  second.segment_token = 'fixed-other-city-segment'; second.city = '上海'; second.overnight_days = ['Day 2']; second.status = 'LIMITED'
  second.candidates = [{...candidate,candidate_token:'fixed-other-hotel-candidate',
    name:'上海浦江滨水家庭旅行连锁酒店（临江大道文化公园南侧入口独立接待楼店）',
    area_or_address:'固定上海地址', max_single_leg_minutes:999, commute_summary:'旧不完整通勤称 999 分钟'}]
  result.stay.segments = [first,second]
  result.stay.candidates = [candidate,...second.candidates] // Duplicate public projection must not print twice.
  result.lodging_constraints = [{...result.days[0].activities[0],activity_token:'fixed-source-hotel',
    name:candidate.name,city:'北京',category:'住宿',area_or_address:candidate.area_or_address,scope:'NIGHTS',overnight_days:[1]}]
  return result
}

test('selected lodging display keeps exact nights, legacy unknown and conflicting sources', {tag:'@desktop'}, () => {
  const lines = displayHelper(), selected = states.selected.stay.segments[0].candidates.find(item=>item.selected)
  expect(lines(states.before).map(row=>row.text).join('')).not.toContain(selected.name)
  const after = lines(states.selected).map(row=>row.text).join('')
  expect(after.split(selected.name)).toHaveLength(2)
  expect(after).toContain('第 1 晚（Day 1 → Day 2）')
  expect(lines(states.undo)).toEqual([])
  const legacy = structuredClone(states.selected); delete legacy.stay.segments
  expect(lines(legacy).map(row=>row.text).join('')).toContain('具体夜晚未指定')
  expect(lines(legacy).map(row=>row.text).join('')).not.toContain('第 1 晚')
  expect(lines(legacy).map(row=>row.text).join('')).not.toContain('全程住宿')
  const merged = multiSegment(), title = merged.stay.segments[0].candidates.find(item=>item.selected).name
  expect(lines(merged).filter(row=>row.heading&&row.text.includes(title))).toHaveLength(1)
  merged.lodging_constraints[0].area_or_address = '另一地址，不能证明同一家门店'
  expect(lines(merged).filter(row=>row.heading&&row.text.includes(title))).toHaveLength(2)
  merged.stay.segments[1].overnight_days = ['Day 3','未知日期']
  const last = lines(merged).map(row=>row.text).join('')
  expect(last).toContain('第 3 晚（Day 3 之后；次日行程未提供）')
  expect(last).toContain('另有夜晚所属日期待确认')
  expect(last).not.toContain('Day 4'); expect(last).not.toContain('999')
  merged.stay.status = 'NEEDS_UPDATE'
  expect(lines(merged).map(row=>row.text).join('')).toContain('住宿建议需要更新')
})

async function show(page, width, state) {
  const current = () => state.fixed || states[state.key]
  const etag = () => `"fixed-stay-export-${state.key}"`
  state.posts = []; state.unexpected = []
  await page.setViewportSize({width,height:1000})
  await page.emulateMedia({reducedMotion:'reduce'})
  await page.addInitScript(() => {
    window.stayPngText = []
    const original = CanvasRenderingContext2D.prototype.fillText
    CanvasRenderingContext2D.prototype.fillText = function(text,x,y,...rest) {
      const position = this.getTransform().transformPoint({x,y})
      window.stayPngText.push({text:String(text),x:position.x,y:position.y,width:this.measureText(String(text)).width,
        align:this.textAlign,canvasWidth:this.canvas.width,canvasHeight:this.canvas.height})
      return original.call(this,text,x,y,...rest)
    }
  })
  await page.route('**/*', route => {
    const request = route.request(), url = new URL(request.url())
    if (!['127.0.0.1','localhost'].includes(url.hostname)) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.fallback()
    const reply = json => route.fulfill({json,headers:{ETag:etag()}})
    if (url.pathname.endsWith('/stay-selection')) {
      expect(state.key).toBe('before'); expect(request.headers()['if-match']).toBe(etag())
      expect(request.headers()['idempotency-key']).toBeTruthy()
      expect(request.postDataJSON().candidate_token).toBe(states.before.stay.segments[0].candidates[0].candidate_token)
      state.posts.push('SELECT_STAY'); state.key='selected'
      return reply({selected_stay:states.selected.stay.segments[0].candidates[0].name})
    }
    if (url.pathname.endsWith('/commands')) {
      expect(request.postDataJSON()).toEqual({command_type:'UNDO'})
      expect(request.headers()['if-match']).toBe(etag()); expect(request.headers()['idempotency-key']).toBeTruthy()
      state.posts.push('UNDO'); state.key='undo'
      return reply({status:'APPLIED',changed_days:['Day 1','Day 2'],map_readiness:'NEEDS_UPDATE'})
    }
    const result=current()
    if(url.pathname.endsWith('/result'))return reply(result)
    if(url.pathname.endsWith('/stay-suggestions'))return reply(result.stay)
    if(url.pathname.endsWith('/map-renders/latest'))return reply({...result.map,points:[],days:[]})
    if(url.pathname.endsWith('/daily-dining'))return reply({status:'UNAVAILABLE',message:'固定回放不请求餐饮',days:[]})
    if(url.pathname.endsWith('/supplementary'))return reply({status:'AVAILABLE',days:[]})
    if(url.pathname.endsWith('/materialize'))return reply({status:'READY',calendar:'相对日序',party_size:2,checks_available:true})
    if(url.pathname.endsWith('/checks'))return reply({status:'STILL_NEEDS_CONFIRMATION',message:'固定检查',items:[],remaining_must_adjust:0,available_actions:[]})
    if(request.method()!=='GET')state.unexpected.push(url.pathname)
    return route.fulfill({status:404,json:{}})
  })
  await page.goto('/trip/result#trip=fixed-stay-export-browser')
  await expect(page.getByTestId('activity-card')).toHaveCount(4)
}

async function png(page, info, name) {
  await page.evaluate(()=>{window.stayPngText=[]})
  await page.getByTestId('export-itinerary-png').click()
  await expect(page.getByAltText('行程横链导出预览',{exact:true})).toBeVisible()
  const drawn=await page.evaluate(()=>window.stayPngText)
  for(const row of drawn) {
    const left=row.x-(row.align==='center'?row.width/2:row.align==='right'?row.width:0)
    expect(left,row.text).toBeGreaterThanOrEqual(0);expect(left+row.width,row.text).toBeLessThanOrEqual(row.canvasWidth)
    expect(row.y,row.text).toBeGreaterThan(0);expect(row.y,row.text).toBeLessThan(row.canvasHeight)
  }
  const download=page.waitForEvent('download')
  await page.getByTestId('download-itinerary-png').click()
  await (await download).saveAs(info.outputPath(name+'.png'))
  await page.getByRole('button',{name:'关闭图片预览',exact:true}).click()
  return drawn.map(row=>row.text).join('')
}

test('memory hotel selection survives refresh and PNG then undo removes it at 1440', {tag:'@desktop'}, async ({page},info) => {
  const state={key:'before'}, hotel=states.selected.stay.segments[0].candidates.find(item=>item.selected).name
  await show(page,1440,state)
  expect(await png(page,info,'before-selection')).not.toContain(hotel)
  await page.getByTestId('desktop-nav-map_stay').click()
  const suggestions=page.locator('#journey-suggestions')
  await suggestions.getByRole('button',{name:'住宿',exact:true}).click()
  await suggestions.getByTestId('choose-stay').first().click()
  await expect.poll(()=>state.key).toBe('selected')
  await page.getByTestId('desktop-nav-itinerary').click()
  await page.reload()
  await expect(page.getByTestId('activity-card')).toHaveCount(4)
  const selected=await png(page,info,'selected-first-night')
  expect(selected.split(hotel)).toHaveLength(2)
  expect(selected).toContain('第 1 晚（Day 1 → Day 2）')
  for(const day of states.selected.days)for(const card of day.activities)expect(selected).toContain(card.name)
  await page.getByTestId('undo-trip-command').click()
  await expect.poll(()=>state.key).toBe('undo')
  await page.reload()
  expect(await png(page,info,'after-undo')).not.toContain(hotel)
  expect(state.posts).toEqual(['SELECT_STAY','UNDO']);expect(state.unexpected).toEqual([])
})

test('long hotel names and separate cities retain precise nights and limited commute in PNG at 1280', {tag:'@desktop'}, async ({page},info) => {
  const result=multiSegment(), state={key:'controlled-cross-city',fixed:result}
  await show(page,1280,state)
  const text=await png(page,info,'cross-city-selected-hotels')
  for(const segment of result.stay.segments) {
    const selected=segment.candidates.find(item=>item.selected)
    expect(text.split(selected.name)).toHaveLength(2)
    expect(text).toContain(segment.city)
  }
  expect(text).toContain('第 1 晚（Day 1 → Day 2）')
  expect(text).toContain('第 2 晚（Day 2 → Day 3）')
  expect(text).toContain('原文住宿 / 建议已选择')
  expect(text).toContain('住宿通勤仅有部分资料，尚未完整核实')
  expect(text).not.toContain('999');expect(state.posts).toEqual([]);expect(state.unexpected).toEqual([])
})
