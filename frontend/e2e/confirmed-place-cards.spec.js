const { test, expect } = require('@playwright/test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
function load(name) {
  const filename = path.join(__dirname, '../src/lib', name + '.ts')
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText
  const exports = {}; vm.runInNewContext(code, { exports }); return exports
}
const view = load('confirmed-trip-view')
const photos = load('place-type-photo')
const presentation = load('../app/trip/result/result-presentation')
const R = 'synthetic-confirmed-trip-only'
const P = `/api/v3/trip-understandings/${R}`
const types = [
  ['星河山景餐厅', '餐饮', 'restaurant'], ['星河大厦', '地点', 'modern'],
  ['青溪寺', '景点', 'historic'], ['水岸酒店', '住宿', 'hotel'], ['青溪山', '景点', 'mountain'],
  ['星河湖', '景点', 'water'], ['青溪公园', '景点', 'park'], ['星河博物馆', '景点', 'museum'],
  ['星河路', '地点', 'street'],
]
const card = (name, category = '景点', status = 'READY', i = name) => ({
  activity_token: `synthetic-card-${i}-0000000000`, name, category, status, city: '北京',
  area_or_address: status === 'READY' ? '合成测试地址' : '地点待确认', time_hint: null, photo_url: null,
  available_actions: ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE'],
})
function fixtureResult() {
  const stay = { status: 'UNAVAILABLE', message: '尚未选择住宿。', candidates: [], searched_scopes: [], area_summary: null, available_actions: [] }
  return { status: 'PARTIAL_RESULT', ownership: 'ANONYMOUS', is_demo: false, can_undo: false,
    assumptions: [{ key: 'destination', label: '目的地', value: '北京', editable: true }],
    days: [{ label: 'Day 1', activities: [card('未匹配的地点一', '地点', 'NEEDS_CONFIRMATION'), card(...types[0].slice(0, 2)),
      card('未匹配的地点二', '地点', 'NEEDS_CONFIRMATION'), ...types.slice(1).map(t => card(t[0], t[1]))],
      alternatives: [{ name: '尚未查询的备选', category: '景点' }] },
      { label: 'Day 2', activities: [card('未匹配的地点三', '地点', 'NEEDS_CONFIRMATION')], alternatives: [] }],
    map: { status: 'UNAVAILABLE', message: '路线暂不可用。', available_actions: ['RENDER_MAP'] }, stay,
    available_actions: ['EDIT_ASSUMPTIONS', 'EDIT_CARDS'] }
}
async function fixture(page, options = {}) {
  const original = fixtureResult()
  const state = { result: structuredClone(original), commands: [], searches: [], mapPosts: 0, diningPosts:0, diningRefreshes:[], stayRefreshes:[], stays:[], version: 0 }
  if (options.meals) {
    state.result.days[0].activities = [card('青溪公园','景点','READY','park'),card('青溪博物馆','景点','READY','museum')]
    state.result.days[1].activities = [card('原文午餐餐厅','餐饮','READY','lunch')]
  }
  if (options.pendingLunch) state.result.days[1].activities = [{...card('原文午餐餐厅','餐饮','NEEDS_CONFIRMATION','lunch'), meal_role:'LUNCH'}]
  if (options.segments) state.result.stay = { ...state.result.stay, status:'LIMITED', message:'按过夜行程分别选择住宿。',
    segments:['广州','深圳'].map((city,index) => ({segment_token:`segment-${index}`,city,overnight_days:[`Day ${index+1}`],status:'AVAILABLE',message:'比较当晚最后一站和次日第一站。',preserved_hotels:[],candidates:[{
      candidate_token:`stay-synthetic-${index}-00000000`,name:`合成${city}连锁酒店`,brand:'合成品牌',brand_note:'合成门店核验说明',category:'住宿',area_or_address:'合成测试地址',commute_summary:'部分通勤尚未确认',reason:'合成建议',available_actions:['CHOOSE_STAY'],selected:false,
    }]})) }
  if (options.brokenPhoto) state.result.days[0].activities[1].photo_url = 'https://store.is.autonavi.com/synthetic-broken.jpg'
  if (options.allMissing) state.result.days.forEach(d => d.activities.forEach(c => { c.status = 'NEEDS_CONFIRMATION' }))
  if (options.semanticGap) {
    state.result.days.forEach(d => { d.activities = d.activities.filter(c => c.status === 'READY') })
    state.result.coverage = { recognized_count: 9, confirmed_count: 9, unresolved_count: 0, unclassified_mention_count: 0, unprocessed_count: 1, complete: false }
  }
  if (options.historicPlaces) {
    state.result.days[0].activities = ['天坛公园', '颐和园', '圆明园', ...Array.from({ length: 7 }, (_, i) => `合成${i}寺`)].map(name => card(name))
    state.result.days[1].activities = []
  }
  if (options.routes) {
    const visible = state.result.days[0].activities.filter(card => card.status === 'READY')
    const mode = minutes => ({ status: 'AVAILABLE', duration_minutes: minutes, distance_meters: 1500, geometry: [] })
    state.result.map = { status: 'AVAILABLE', message: '路线已准备', available_actions: ['VIEW_MAP'], days: [{ label: 'Day 1', day_index: 1,
      routes: [0, 1].map(i => ({ from_activity_token: visible[i].activity_token, to_activity_token: visible[i + 1].activity_token,
        from_name: visible[i].name, to_name: visible[i + 1].name, selected_mode: 'walking', walking: mode(i ? 30 : 31), transit: mode(40) })) }] }
  }
  const enhancementsReadyAt=Date.now()+(options.slowEnhancements?12000:0)
  await page.route('**/restapi.amap.com/**', route => route.abort())
  await page.route('https://store.is.autonavi.com/**', route => route.fulfill({ status: 404, body: '' }))
  if (options.failFallback) await page.route('**/place-types/restaurant*.jpg', route => route.fulfill({ status: 404, body: '' }))
  await page.route('**/api/user/me', route => route.fulfill({ status: 401, json: {} }))
  await page.route('**/api/v3/trip-understandings/**', async route => {
    const request = route.request(), action = new URL(request.url()).pathname.slice(P.length)
    const reply = (json, status = 200) => route.fulfill({ status, json, headers: { ETag: `"fixture-${state.version}"` } })
    if (action === '/result') return options.progress ? reply({
      status: 'PROCESSING', message: '正在整理地点。', retry_after_ms: 10000,
      phase: 'CHECKING_PLACES', event_cursor: 1,
      progress: { day_count: 2, card_count: 12, places_checked: 9, places_total: 12 },
      snapshot: state.result,
    }, 202) : reply(state.result)
    if (action === '/map-renders/latest') {
      if(Date.now()<enhancementsReadyAt)return reply({status:'PREPARING',message:'正在准备路线',available_actions:[],days:[],points:[]})
      return reply({ ...state.result.map, points: [], days: state.result.map.days || [] })
    }
    if (action === '/stay-suggestions') {
      if (request.method() === 'POST') {
        state.stayRefreshes.push(request.headers())
        await new Promise(resolve => setTimeout(resolve, 150))
      }
      return reply(state.result.stay)
    }
    if (action === '/stay-selection') {
      state.stays.push(request.postDataJSON().candidate_token)
      state.result.stay.segments.flatMap(s=>s.candidates).forEach(c=>{c.selected=state.stays.includes(c.candidate_token)})
      state.version++
      return reply({status:'APPLIED',selected_stay:'合成酒店',overnight_days:['Day 1'],map_readiness:'NEEDS_UPDATE'})
    }
    if (action === '/daily-dining') {
      if (request.method()==='POST') {
        state.diningPosts++
        state.diningRefreshes.push(request.headers())
        if (options.refreshFailure && state.diningPosts === 1) return reply({},503)
      }
      if (options.pendingLunch) {
        const lunch=state.result.days[1].activities[0]
        return reply({status:'AVAILABLE',message:'用餐安排',days:[{day_index:2,label:'Day 2',
          status:lunch.status==='READY'?'EXISTING':'NEEDS_CONFIRMATION',meal_role:'LUNCH',
          message:lunch.status==='READY'?'已安排原文午餐餐厅。':'原文已安排午餐，请先确认地点，不重复推荐。',
          existing_activity_token:lunch.activity_token,candidates:[]}]})
      }
      if (!options.meals) return reply({status:'UNAVAILABLE',message:'合成用餐查询未配置。',days:[]})
      if (state.version && !state.diningPosts) return reply({status:'NEEDS_UPDATE',message:'行程已调整，请更新用餐建议。',days:[]})
      return reply({status:'AVAILABLE',message:'中途用餐建议',days:[{day_index:1,label:'Day 1',status:'AVAILABLE',message:'选择后才加入行程。',area:'合成商圈',...(options.nearbyArea?{area_relation:'NEARBY',area_distance_m:480}:{}),next_name:'青溪博物馆',after_activity_token:state.result.days[0].activities[0].activity_token,candidates:[{
        candidate_token:'synthetic-daily-meal-token',name:'合成中途餐厅',area_or_address:'合成商圈店址',reason:'经此店前往下一站约多6分钟。',extra_minutes:6,recommended:true,
      }]},{day_index:2,label:'Day 2',status:'EXISTING',message:'已安排原文午餐餐厅，可在地点卡片更换。',candidates:[]}]})
    }
    if (action === '/supplementary') return reply({ status: 'AVAILABLE', days: [] })
    if (action === '/materialize') return reply({ status: 'READY', message: '行程已准备。', calendar: '按日期', party_size: 2, checks_available: true })
    if (action === '/checks') return reply({ status: 'STILL_NEEDS_CONFIRMATION', message: '路线资料不足。', items: [], remaining_must_adjust: 0, available_actions: [] })
    if (action === '/place-candidates') {
      state.searches.push(request.postDataJSON())
      return reply({ status: 'AVAILABLE', candidates: [{ candidate_token: 'synthetic-replace-candidate-000', name: '雨湖餐厅', category: '餐饮', area_or_address: '合成新地址', position: { longitude: 116.4, latitude: 39.9, coordinate_system: 'GCJ02' } }] })
    }
    if (action === '/commands') {
      const command = request.postDataJSON(); state.commands.push(command)
      const days = state.result.days
      if (command.command_type === 'UNDO') state.result = structuredClone(original)
      else if (command.command_type === 'PLACE_CONFIRM') {
        const target = days.flatMap(d => d.activities).find(c => c.activity_token === command.activity_token)
        Object.assign(target, { name: '雨湖餐厅', category: '餐饮', photo_url: null, status: 'READY' })
      } else if (command.command_type === 'ACTIVITY_MOVE') {
        let moved
        for (const d of days) { const i = d.activities.findIndex(c => c.activity_token === command.activity_token); if (i >= 0) moved = d.activities.splice(i, 1)[0] }
        days[command.target_day_index - 1].activities.splice(command.target_position, 0, moved)
      } else if (command.command_type === 'ACTIVITY_INSERT') {
        days[command.day_index - 1].activities.splice(command.position, 0, card(command.name, '地点', 'NEEDS_CONFIRMATION', 'new'))
      } else if (command.command_type === 'DINING_INSERT') {
        const day = days.find(d=>d.activities.some(c=>c.activity_token===command.after_activity_token))
        const index = day.activities.findIndex(c=>c.activity_token===command.after_activity_token)
        day.activities.splice(index+1,0,card('合成中途餐厅','餐饮','READY','daily-meal'))
      } else return reply({}, 400)
      state.version++; state.result.can_undo = command.command_type !== 'UNDO'
      state.result.map = { status: 'NEEDS_UPDATE', message: '路线需要手动更新。', available_actions: ['RENDER_MAP'] }
      return reply({ status: 'APPLIED', changed_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE' })
    }
    if (action === '/map-renders') state.mapPosts++
    return reply({}, 404)
  })
  await page.goto(`/trip/result#trip=${R}`)
  await expect(options.progress ? page.locator('.e-progress-workspace') : page.getByTestId('itinerary-workspace')).toBeVisible()
  return state
}

for (const width of [1440,390]) test(`pending source lunch is confirmed in place without duplicate insertion at ${width}px`, async ({page}) => {
  await page.setViewportSize({width,height:900})
  const state=await fixture(page,{pendingLunch:true})
  const meal=page.getByTestId('day-lane-2').getByTestId('daily-meal-card')
  await expect(meal).toContainText('原文已安排午餐')
  await expect(meal.getByRole('button',{name:'加入行程',exact:true})).toHaveCount(0)
  await expect(meal.getByRole('button',{name:'更新用餐建议',exact:true})).toHaveCount(0)
  await meal.getByRole('button',{name:'确认原文午餐',exact:true}).click()
  expect(state.searches).toEqual([])
  const dropdown=meal.getByTestId('pending-place-dropdown')
  await expect(dropdown.getByRole('textbox').first()).toHaveValue('原文午餐餐厅')
  await dropdown.getByRole('button',{name:'搜索',exact:true}).click()
  await dropdown.getByRole('button',{name:/雨湖餐厅/}).click()
  await dropdown.getByRole('button',{name:'使用这个地点',exact:true}).click()
  await expect(page.getByTestId('day-lane-2').getByRole('heading',{name:'雨湖餐厅',exact:true})).toBeVisible()
  await expect(meal).toContainText('已安排原文午餐餐厅')
  expect(state.commands).toEqual([{command_type:'PLACE_CONFIRM',activity_token:'synthetic-card-lunch-0000000000',candidate_token:'synthetic-replace-candidate-000'}])
  expect(state.result.days[1].activities).toHaveLength(1)
  expect(state.diningPosts).toBe(0)
  expect(state.mapPosts).toBe(0)
})

test('filter preserves original records, truthful status, empty days and positional commands', () => {
  const raw = fixtureResult(), before = JSON.stringify(raw), shown = view.confirmedTripView(raw)
  expect(shown.days.map(d => d.activities.length)).toEqual([9, 0])
  expect(shown.days[0].alternatives).toEqual(raw.days[0].alternatives)
  expect(shown.status).toBe('PARTIAL_RESULT')
  expect(JSON.stringify(raw)).toBe(before)
  const token = raw.days[0].activities[1].activity_token
  expect(view.storedPositionCommand({ command_type: 'ACTIVITY_MOVE', activity_token: token, target_day_index: 1, target_position: 1 }, raw).target_position).toBe(3)
  expect(view.storedPositionCommand({ command_type: 'ACTIVITY_MOVE', activity_token: token, target_day_index: 2, target_position: 0 }, raw).target_position).toBe(1)
  expect(view.storedPositionCommand({ command_type: 'ACTIVITY_INSERT', day_index: 1, position: 0, name: '新地点' }, raw).position).toBe(1)
})

test('photo types follow business categories and distinguish landscape, streets and architecture', () => {
  for (const [name, category, type] of [...types,
    ['山水酒店', '餐饮', 'restaurant'], ['星河餐厅', '住宿', 'hotel'], ['天坛公园', '景点', 'historic'],
    ['中国尊', '地点', 'modern'], ['西湖路', '地点', 'street'], ['故宫博物院', '景点', 'historic'],
    ['古城咖啡', '餐饮', 'restaurant'], ['星河公园', '景点', 'park'], ['青溪桥', '景点', null],
    ['未知名称', '景点', null]]) expect(photos.placePhotoType({ name, category })).toBe(type)
})

for (const width of [1440, 1280, 390, 360]) test(`confirmed-only cards and nine local photo types at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 })
  await fixture(page)
  for (const [name, , type] of types) {
    const title = page.getByRole('heading', { name, exact: true })
    await title.scrollIntoViewIfNeeded(); await expect(title).toBeVisible()
    const photo = page.locator(`img[data-image-type="${type}"]`).first()
    await expect(photo).toHaveAttribute('src', new RegExp(`^/place-types/${type}(?:-\\d{2})?\\.jpg$`))
    await expect.poll(() => photo.evaluate(img => img.complete && img.naturalWidth > 0)).toBe(true)
  }
  await expect(page.getByText(/未匹配的地点[一二三]/)).toHaveCount(0)
  await expect(page.getByTestId('day-alternatives-1')).toHaveCount(1)
  await expect(page.getByRole('heading',{name:'尚未查询的备选',exact:true})).toHaveCount(0)
  await expect(page.getByTestId('unmatched-places-note')).toContainText('3 项')
  await expect(page.getByText('类型配图', { exact: true })).toHaveCount(0)
  await page.reload()
  await expect(page.getByRole('heading', { name: types[0][0], exact: true })).toBeVisible()
  await expect(page.getByText(/未匹配的地点[一二三]/)).toHaveCount(0)
})

test('every photo subtype has at least ten distinct, traceable local assets', () => {
  const crypto = require('node:crypto')
  const directory = path.join(__dirname, '../public/place-types')
  const sources = JSON.parse(fs.readFileSync(path.join(directory, 'sources.json'), 'utf8'))
  for (const type of Object.keys(photos.PLACE_PHOTO_LABELS)) {
    const entries = sources.filter(item => item.type === type)
    expect(entries.length).toBeGreaterThanOrEqual(10)
    expect(new Set(entries.map(item => item.sha256)).size).toBe(entries.length)
    for (const item of entries) {
      const content = fs.readFileSync(path.join(directory, item.file))
      expect(crypto.createHash('sha256').update(content).digest('hex')).toBe(item.sha256)
      expect(item.license).toBe('https://www.pexels.com/license/')
    }
  }
})

test('ten similar places use ten pictures, stable after refresh and reordering', async ({ page }) => {
  const state = await fixture(page, { historicPlaces: true })
  const images = page.locator('img[data-image-type="historic"]')
  await expect(images).toHaveCount(10)
  const read = () => images.evaluateAll(items => items.map(item => item.getAttribute('src')))
  const first = await read()
  expect(new Set(first).size).toBe(10)
  for (const image of await images.all()) {
    await image.scrollIntoViewIfNeeded()
    await expect.poll(() => image.evaluate(img => img.complete && img.naturalWidth > 0)).toBe(true)
  }
  await expect(page.getByText('类型配图', { exact: true })).toHaveCount(0)
  await page.reload()
  await expect(images).toHaveCount(10)
  expect(await read()).toEqual(first)
  state.result.days[0].activities.reverse()
  await page.reload()
  await expect(images).toHaveCount(10)
  expect(await read()).toEqual([...first].reverse())
})

test('repeated visits keep a picture and larger groups reuse the pool evenly', () => {
  const cards = Array.from({ length: 21 }, (_, i) => card(`合成${i}寺`))
  const selection = photos.allocatePlacePhotos([...cards, cards[0]])
  expect(selection.size).toBe(21)
  const counts = {}
  for (const picture of selection.values()) counts[picture.src] = (counts[picture.src] || 0) + 1
  expect(Object.keys(counts)).toHaveLength(10)
  expect(Math.max(...Object.values(counts)) - Math.min(...Object.values(counts))).toBeLessThanOrEqual(1)
  const reversed = photos.allocatePlacePhotos([...cards].reverse())
  for (const [key, picture] of selection) expect(reversed.get(key).src).toBe(picture.src)
})

test('broken POI photo falls back to matching type', async ({ page }) => {
  await fixture(page, { brokenPhoto: true })
  await expect(page.locator('img[data-image-type="restaurant"]')).toBeVisible()
})

test('generation snapshots also show only confirmed cards', async ({ page }) => {
  await fixture(page, { progress: true })
  const cards = page.locator('.e-progress-card')
  await expect(cards).toHaveCount(9)
  await expect(cards.getByText('已确认', { exact: true })).toHaveCount(9)
  await expect(page.getByText(/未匹配的地点[一二三]/)).toHaveCount(0)
  await expect(cards.getByText('待确认', { exact: true })).toHaveCount(0)
})

test('tips change every 2.5 seconds and pause while reading', async ({ page }) => {
  await page.clock.install()
  await fixture(page, { progress: true })
  await page.mouse.move(0, 0)
  const tip = page.getByTestId('generation-reading')
  await expect(tip.getByRole('heading')).toHaveText('先排顺序，再慢慢完善')
  await page.clock.fastForward(2500)
  await expect(tip.getByRole('heading')).toHaveText('地点已匹配，仍可随时更改')
  await tip.hover()
  await page.clock.fastForward(5000)
  await expect(tip.getByRole('heading')).toHaveText('地点已匹配，仍可随时更改')
})

test('cards use walking through 30 minutes and transit beyond it', async ({ page }) => {
  await fixture(page, { routes: true })
  await expect(page.getByText('公交 · 40 分钟 · 1.5 公里', { exact: true })).toBeVisible()
  await expect(page.getByText('步行 · 30 分钟 · 1.5 公里', { exact: true })).toBeVisible()
  await expect(page.getByText('步行 · 31 分钟', { exact: false })).toHaveCount(0)
})

test('route threshold handles unavailable modes and stale routes honestly', () => {
  const raw = fixtureResult(), day = raw.days[0], from = day.activities[1], to = day.activities[3]
  for (const [walk, bus, expected] of [[29,10,'walking'],[30,10,'walking'],[31,40,'transit'],[null,40,'transit'],[31,null,null],[null,null,null]]) {
    const mode = minutes => ({ status: minutes ? 'AVAILABLE' : 'UNAVAILABLE', duration_minutes: minutes, distance_meters: 500 })
    const map = { status: 'AVAILABLE', days: [{ label: day.label, routes: [{ from_activity_token: from.activity_token,
      to_activity_token: to.activity_token, selected_mode: 'walking', walking: mode(walk), transit: mode(bus) }] }] }
    const result = presentation.transportConnectorFor(day, from, to, map)
    if (expected) expect(result.mode).toBe(expected)
    else expect(result.status).toBe('UNAVAILABLE')
    expect(presentation.transportConnectorFor(day, from, to, { ...map, status: 'NEEDS_UPDATE' }).status).toBe('NEEDS_UPDATE')
  }
})

test('older read-only shares omit unresolved cards while preserving day labels', async ({ page }) => {
  await page.route('**/api/v3/shares/synthetic-share', route => route.fulfill({ json: {
    title: '北京行程', destination: '北京', schedule: '按天安排', party_size: '2人', accommodation: null,
    days: [{ label: 'Day 1', activities: [
      { name: '未匹配的地点一', area_or_address: '地点待确认', time_hint: null, note: '地点待确认' },
      { name: '合成公园', area_or_address: '合成地址', time_hint: null, note: '可直接查看' },
    ] }, { label: 'Day 2', activities: [] }],
  } }))
  await page.goto('/share/synthetic-share')
  await expect(page.getByTestId('g06-shared-trip').getByText('合成公园', { exact: true })).toBeVisible()
  await expect(page.getByText('未匹配的地点一', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'Day 2', exact: true })).toBeVisible()
})
test('both image sources fail without broken image or unusable card', async ({ page }) => {
  await fixture(page, { brokenPhoto: true, failFallback: true })
  await expect(page.locator('img[data-image-type="restaurant"]')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: types[0][0], exact: true })).toBeVisible()
})

test('empty matching results retain days and add entry without invented cards', async ({ page }) => {
  await fixture(page, { allMissing: true })
  await expect(page.getByTestId('itinerary-workspace').getByRole('heading', { level: 3 })).toHaveCount(0)
  await expect(page.getByTestId('unmatched-places-note')).toBeVisible()
  await expect(page.getByRole('button', { name: '新增地点到 Day 1', exact: true })).toBeVisible()
})

test('reorder across hidden entries, replace and undo preserve records and never auto-render routes', async ({ page }) => {
  const state = await fixture(page)
  await page.getByTestId('drag-handle-1-0').press('Enter')
  await page.keyboard.press('ArrowRight')
  await page.keyboard.press('Enter')
  await expect.poll(() => state.commands.length).toBe(1)
  expect(state.commands[0].target_position).toBe(3)
  await page.getByRole('heading', { name: types[0][0], exact: true }).click()
  await page.getByRole('textbox', { name: '搜索地点名称' }).fill('雨湖餐厅')
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  await page.getByRole('button', { name: /雨湖餐厅.*合成新地址/ }).click()
  await page.getByRole('button', { name: '使用这个地点', exact: true }).click()
  await expect(page.getByRole('heading', { name: '雨湖餐厅', exact: true })).toBeVisible()
  await page.getByRole('button', { name: /撤销/ }).first().click()
  await expect(page.getByRole('heading', { name: types[0][0], exact: true })).toBeVisible()
  expect(state.result.days[0].activities).toHaveLength(11)
  expect(state.mapPosts).toBe(0)
})

test('adding a place searches before a new confirmed card appears', async ({ page }) => {
  const state = await fixture(page)
  await page.getByRole('button', { name: '新增地点到 Day 2', exact: true }).click()
  await page.getByRole('textbox', { name: '新增地点名称' }).fill('星河新餐厅')
  await page.getByRole('button', { name: '查找这个地点', exact: true }).click()
  await expect(page.getByRole('heading', { name: '星河新餐厅', exact: true })).toHaveCount(0)
  await expect(page.getByText('准备加入 Day 2', { exact: true })).toBeVisible()
  await expect(page.getByText('移动或移除这个地点', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: '搜索地点', exact: true }).click()
  await page.getByRole('button', { name: /候选 1.*雨湖餐厅/ }).click()
  await page.getByRole('button', { name: '使用这个地点', exact: true }).click()
  await expect(page.getByRole('heading', { name: '雨湖餐厅', exact: true })).toBeVisible()
  expect(state.commands.map(c => c.command_type)).toEqual(['ACTIVITY_INSERT', 'PLACE_CONFIRM'])
  expect(state.mapPosts).toBe(0)
})


for (const width of [1440,390]) test(`daily dining adopts once and editing requires explicit update at ${width}px`, async ({page}) => {
  await page.setViewportSize({width,height:900})
  const state = await fixture(page,{meals:true})
  const meals = page.getByTestId('daily-meal-card')
  await expect(meals).toHaveCount(2)
  await expect(meals.first()).toContainText('合成商圈')
  await expect(meals.nth(1)).toContainText('已安排原文午餐餐厅')
  await expect(meals.nth(1).getByRole('button',{name:'加入行程'})).toHaveCount(0)
  expect(state.commands).toHaveLength(0)
  await meals.first().getByRole('button',{name:'加入行程'}).dblclick()
  await expect(page.getByRole('heading',{name:'合成中途餐厅',exact:true})).toBeVisible()
  expect(state.commands.filter(c=>c.command_type==='DINING_INSERT')).toHaveLength(1)
  await expect(meals.first()).toContainText('行程已调整')
  expect(state.mapPosts).toBe(0)
  expect(state.diningPosts).toBe(0)
  await meals.first().getByRole('button',{name:'更新用餐建议'}).click()
  await expect.poll(()=>state.diningPosts).toBe(1)
  expect(state.diningRefreshes[0]['if-match']).toBe('"fixture-1"')
  expect(state.mapPosts).toBe(0)
  await page.screenshot({path:`test-results/daily-dining-${width}.png`,fullPage:true})
})

test('unresolved places can be recovered without showing unconfirmed main cards',async ({page})=>{
  const state = await fixture(page)
  await page.getByTestId('unmatched-places-note').click()
  const recover = page.getByTestId('unresolved-places')
  await recover.getByRole('button',{name:'未匹配的地点一 · 北京 · 确认地点',exact:true}).click()
  await recover.getByRole('button',{name:'搜索',exact:true}).click()
  await recover.getByRole('button',{name:/雨湖餐厅.*合成新地址/}).click()
  await recover.getByRole('button',{name:'使用这个地点',exact:true}).click()
  await expect(page.getByRole('heading',{name:'雨湖餐厅',exact:true})).toBeVisible()
  await expect(page.getByTestId('unmatched-places-note')).toContainText('2 项')
  expect(state.commands[0].command_type).toBe('PLACE_CONFIRM')
  expect(state.mapPosts).toBe(0)
})

test('all overnight cities stay visible and a choice retains the remaining segment',async ({page})=>{
  await page.setViewportSize({width:1440,height:900})
  const state=await fixture(page,{segments:true})
  await page.getByTestId('desktop-nav-map_stay').click()
  await page.getByTestId('stay-panel').locator('summary').click()
  const panel=page.getByTestId('stay-panel')
  await expect(panel.getByTestId('stay-segment')).toHaveCount(2)
  await expect(panel).toContainText('合成广州连锁酒店')
  await expect(panel).toContainText('合成深圳连锁酒店')
  await expect(panel).toContainText('部分通勤尚未确认')
  await panel.getByTestId('choose-stay').first().click()
  await expect.poll(()=>state.stays.length).toBe(1)
  await expect(panel.getByTestId('choose-stay').nth(1)).toBeEnabled()
  expect(state.mapPosts).toBe(0)
})

test('incomplete source without unresolved places explains recovery without a zero-count warning', async ({page}) => {
  await fixture(page,{semanticGap:true})
  const note=page.getByTestId('unmatched-places-note')
  await expect(note).toContainText('部分原文尚未完整整理')
  await expect(note).not.toContainText('0 项')
  await note.click()
  await expect(page.getByText('部分原文尚未完整整理，请对照原文补充；已确认地点可以继续使用。')).toBeVisible()
  await expect(page.getByTestId('activity-card')).toHaveCount(9)
})

test('background routes completing after ten seconds appear without a generation click', async ({page}) => {
  const state=await fixture(page,{routes:true,slowEnhancements:true})
  await page.getByTestId('desktop-nav-map_stay').click()
  await expect(page.getByText('准备中',{exact:true})).toBeVisible()
  await expect(page.getByTestId('map-route-summary')).toHaveCount(2,{timeout:22000})
  await expect(page.getByText('准备中',{exact:true})).toHaveCount(0)
  await expect(page.getByText('路线暂不可用',{exact:true})).toHaveCount(0)
  expect(state.mapPosts).toBe(0)
})

test('nearby dining area remains distinct from restaurant membership and walking distance',async({page})=>{
  await fixture(page,{meals:true,nearbyArea:true})
  const meal=page.getByTestId('daily-meal-card').first()
  await expect(meal).toContainText('附近用餐区：合成商圈')
  await expect(meal).toContainText('距首选饭店直线约480米，步行路线待确认。')
  await expect(meal.getByRole('button',{name:'加入行程'})).toBeEnabled()
})

test('stay refresh is explicit, version bound and double clicks make one request', async ({page}) => {
  const state=await fixture(page,{segments:true})
  await page.getByTestId('desktop-nav-map_stay').click()
  const panel=page.getByTestId('stay-panel')
  await panel.locator('summary').click()
  await expect(panel.getByTestId('stay-segment')).toHaveCount(2)
  expect(state.stayRefreshes).toHaveLength(0)
  await panel.getByRole('button',{name:'更新住宿建议',exact:true}).dblclick()
  await expect.poll(()=>state.stayRefreshes.length).toBe(1)
  expect(state.stayRefreshes[0]['if-match']).toBe('"fixture-0"')
  expect(state.stayRefreshes[0]['idempotency-key']).toBeTruthy()
  await expect(panel.getByRole('button',{name:'更新住宿建议',exact:true})).toBeEnabled()
  expect(state.mapPosts).toBe(0)
  expect(state.commands).toHaveLength(0)
})

test('uncertain daily refresh reuses its request key without changing routes',async ({page})=>{
  const state=await fixture(page,{meals:true,refreshFailure:true})
  await page.getByTestId('daily-meal-card').first().getByRole('button',{name:'加入行程'}).click()
  const update=page.getByTestId('daily-meal-card').first().getByRole('button',{name:'更新用餐建议'})
  await update.click()
  await expect(page.getByTestId('daily-meal-card').first()).toContainText('建议更新未确认')
  await update.click()
  await expect.poll(()=>state.diningRefreshes.length).toBe(2)
  expect(state.diningRefreshes[0]['idempotency-key']).toBe(state.diningRefreshes[1]['idempotency-key'])
  expect(state.mapPosts).toBe(0)
})
