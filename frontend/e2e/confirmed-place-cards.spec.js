const { test, expect } = require('@playwright/test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
function load(name) {
  const filename = path.join(__dirname, '../src/lib', name + '.ts')
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
  const exports = {}; vm.runInNewContext(code, { exports }); return exports
}
const view = load('confirmed-trip-view')
const photos = load('place-type-photo')
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
  const state = { result: structuredClone(original), commands: [], searches: [], mapPosts: 0, version: 0 }
  if (options.brokenPhoto) state.result.days[0].activities[1].photo_url = 'https://store.is.autonavi.com/synthetic-broken.jpg'
  if (options.allMissing) state.result.days.forEach(d => d.activities.forEach(c => { c.status = 'NEEDS_CONFIRMATION' }))
  await page.route('**/restapi.amap.com/**', route => route.abort())
  await page.route('https://store.is.autonavi.com/**', route => route.fulfill({ status: 404, body: '' }))
  if (options.failFallback) await page.route('**/place-types/restaurant.jpg', route => route.fulfill({ status: 404, body: '' }))
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
    if (action === '/map-renders/latest') return reply({ ...state.result.map, points: [], days: [] })
    if (action === '/stay-suggestions') return reply(state.result.stay)
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

test('filter preserves original records, truthful status, empty days and positional commands', () => {
  const raw = fixtureResult(), before = JSON.stringify(raw), shown = view.confirmedTripView(raw)
  expect(shown.days.map(d => d.activities.length)).toEqual([9, 0])
  expect(shown.days[0].alternatives).toEqual([])
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
    await expect(photo).toHaveAttribute('src', `/place-types/${type}.jpg`)
    await expect.poll(() => photo.evaluate(img => img.complete && img.naturalWidth > 0)).toBe(true)
  }
  await expect(page.getByText(/未匹配的地点[一二三]/)).toHaveCount(0)
  await expect(page.getByTestId('day-alternatives-1')).toHaveCount(0)
  await expect(page.getByTestId('unmatched-places-note')).toContainText('3 项')
  await page.reload()
  await expect(page.getByRole('heading', { name: types[0][0], exact: true })).toBeVisible()
  await expect(page.getByText(/未匹配的地点[一二三]/)).toHaveCount(0)
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
