const {test, expect} = require('@playwright/test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const React = require('react')
const {renderToStaticMarkup} = require('react-dom/server')

function load(relative) {
  const filename = path.resolve(__dirname, '../src', relative)
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText
  const exports = {}
  const localRequire = name => {
    if (name === '@/lib/trip-understanding-v3') return {}
    if (name.startsWith('@/')) return load(`${name.slice(2)}.ts`)
    if (name.startsWith('.')) {
      const relativeName = path.relative(path.resolve(__dirname, '../src'), path.resolve(path.dirname(filename), name))
      return load(relativeName + (fs.existsSync(path.resolve(path.dirname(filename), name + '.tsx')) ? '.tsx' : '.ts'))
    }
    return require(name)
  }
  vm.runInNewContext(code, {exports, require: localRequire})
  return exports
}
const view = load('lib/confirmed-trip-view.ts')
const presentation = load('app/trip/result/result-presentation.ts')
const R = 'synthetic-relative-old-result'
const P = `/api/v3/trip-understandings/${R}`
const oldTime = /2026-10-0[12]|09:15|10:45|适合上午|停留九十分钟|提前两天预约/

// Historical public data is deliberately fixed here. This checks display and
// readback compatibility; it is not model/POI accuracy or real account evidence.
function fixture() {
  const make = (name, id, status = 'READY') => ({activity_token: `synthetic-${id}-000000000000`, name,
    category: '景点', city: '北京', status, area_or_address: '合成地址', photo_url: null,
    start_time: '09:15', end_time: '10:45', visit_duration_minutes: 90,
    time_hint: '09:15', timing_source: 'USER', locked: true,
    available_actions: ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE']})
  const first = {...make('青溪公园', 'park'), source_details: [{name: '入口：南门', optional: false},
    {name: '清风亭', optional: false}, {name: '出口：北门', optional: false}],
    knowledge_suggestions: [
      {type: 'TYPICAL_DURATION', text: '停留九十分钟'},
      {type: 'SUITABLE_TIME', text: '适合上午'},
      {type: 'RESERVATION_ADVICE', text: '请在09:15预约'},
      {type: 'RESERVATION_ADVICE', text: '提前两天预约'},
      {type: 'RESERVATION_ADVICE', text: '需要提前预约'},
      {type: 'OTHER', text: '请穿舒适的鞋'},
    ].map(item => ({...item, source_url: '', source_name: '合成资料'}))}
  const last = make('星河博物馆', 'museum')
  const mode = {status: 'AVAILABLE', duration_minutes: 12, distance_meters: 850, geometry: []}
  const stay = {status: 'LIMITED', message: '逐晚住宿', available_actions: [], candidates: [],
    segments: [{segment_token: 'synthetic-stay', city: '北京', overnight_days: ['2026-10-01'],
      status: 'UNAVAILABLE', message: '尚无候选', candidates: [], preserved_hotels: []}]}
  const map = {status: 'AVAILABLE', message: '路线已准备', available_actions: ['VIEW_MAP'], points: [],
    days: [{label: '2026-10-01', day_index: 1, routes: [{
      from_activity_token: first.activity_token, to_activity_token: last.activity_token,
      from_name: first.name, to_name: last.name, selected_mode: 'walking', walking: mode,
      transit: {status: 'UNAVAILABLE', duration_minutes: null, geometry: []},
    }]}]}
  return {status: 'PARTIAL_RESULT', ownership: 'ANONYMOUS', is_demo: false, can_undo: false,
    assumptions: [{key: 'destination', label: '目的地', value: '北京', editable: true},
      {key: 'calendar', label: '日期', value: '2026-10-01', editable: true}],
    coverage: {recognized_count: 4, confirmed_count: 2, unresolved_count: 2,
      unprocessed_count: 1, unclassified_mention_count: 0, complete: false},
    days: [{label: '2026-10-01', activities: [first, make('未确认中间站', 'hidden', 'NEEDS_CONFIRMATION'), last],
      alternatives: [{name: '月光园', category: '景点', alternative_token: 'synthetic-alternative'}]},
      {label: '2026-10-02', activities: [make('未确认末日站', 'last', 'NEEDS_CONFIRMATION')],
        alternatives: [], unprocessed_count: 1}], map, stay,
    available_actions: ['EDIT_ASSUMPTIONS', 'EDIT_CARDS']}
}

test('relative presentation retains source, identities, hidden positions and route label binding', () => {
  const raw = fixture(), before = JSON.stringify(raw)
  const visible = view.confirmedTripView(raw)
  expect(JSON.stringify(raw)).toBe(before)
  expect(visible.days.map(day => day.label)).toEqual(['2026-10-01', '2026-10-02'])
  expect(visible.days.map(day => day.activities.length)).toEqual([2, 0])
  expect(visible.coverage).toEqual(raw.coverage)
  expect(visible.days[1].unprocessed_count).toBe(1)
  expect(visible.days[0].alternatives).toEqual(raw.days[0].alternatives)
  const card = visible.days[0].activities[0]
  expect(card.source_details).toEqual(raw.days[0].activities[0].source_details)
  expect(card.start_time).toBe('09:15')
  expect(card.visit_duration_minutes).toBe(90)
  expect(card.locked).toBe(true)
  expect(card.knowledge_suggestions.map(item => item.text)).toEqual(['需要提前预约', '请穿舒适的鞋'])
  const command = view.storedPositionCommand({command_type: 'ACTIVITY_INSERT', day_index: 1, position: 1, name: '新增站'}, raw)
  expect(command.position).toBe(2)
  expect(presentation.relativeDayLabel(0)).toBe('Day 1')
  expect(presentation.transportConnectorFor(visible.days[0], card, visible.days[0].activities[1], raw.map))
    .toEqual({status: 'AVAILABLE', mode: 'walking', durationMinutes: 12, distanceMeters: 850})
})

test('old editor and preview remain readable without clock actions or loss of source details', () => {
  const raw = fixture()
  // A raw old card bypasses confirmedDays: the editor must filter its own outlet.
  const editor = load('app/trip/result/place-editor.tsx').default
  const html = renderToStaticMarkup(React.createElement(editor, {editorMode: 'EDIT', card: raw.days[0].activities[0],
    dayIndex: 0, days: raw.days, resource: R, busy: false, notice: '', onDirtyChange() {}, onPreviewCandidate() {}}))
  expect(html).not.toMatch(oldTime)
  expect(html).toContain('需要提前预约')
  expect(html).toContain('清风亭')
  expect(html).toContain('Day 2')
  const preview = load('app/trip/result/change-preview-panel.tsx').default
  const old = renderToStaticMarkup(React.createElement(preview, {preview: {
    summary: '09:15开始', before: ['09:15'], after: ['10:45'], affected_days: ['2026-10-01'],
    changes: [{before: {start_time: '09:15'}, after: {start_time: '10:45'}}],
  }, result: raw, onCancel() {}}))
  expect(old).toContain('这条旧建议不再适用，请重新检查')
  expect(old).not.toMatch(/09:15|10:45|2026-10-01|确认采纳|重新预览/)
  const nights = load('app/trip/result/stay-candidates.tsx').relativeNightLabels
  expect(nights(['2026-10-01'], raw.days)).toBe('第 1 晚')
  expect(nights(['未知日期'], raw.days)).toBe('1 晚 · 所属日待确认')
})

async function show(page, legacyPreview = false) {
  const result = fixture(), requests = []
  await page.route('**/webapi.amap.com/**', r => r.abort())
  await page.route('**/restapi.amap.com/**', r => r.abort())
  await page.route('**/api/**', route => {
    const request = route.request(), pathname = new URL(request.url()).pathname
    requests.push({pathname, method: request.method()})
    const reply = json => route.fulfill({json, headers: {ETag: '"synthetic-relative-v1"'}})
    if (pathname === '/api/user/me') return route.fulfill({status: 401, json: {}})
    if (pathname === `${P}/result`) return reply(result)
    if (pathname.endsWith('/map-renders/latest')) return reply(result.map)
    if (pathname.endsWith('/stay-suggestions')) return reply(result.stay)
    if (pathname.endsWith('/daily-dining')) return reply({status: 'UNAVAILABLE', message: '未生成建议', days: []})
    if (pathname.endsWith('/supplementary')) return reply({status: 'AVAILABLE', days: []})
    if (pathname.endsWith('/materialize')) return reply({status: 'READY', message: '行程已准备', calendar: '2026-10-01', party_size: 2, checks_available: true})
    if (pathname.endsWith('/checks')) return reply({status: 'STILL_NEEDS_CONFIRMATION', message: '请核对地点', items: legacyPreview ? [{
      check_token: 'synthetic-legacy-check', title: '旧版本调整建议', message: '查看以前的建议', label: '可以更好',
      affected_days: ['2026-10-01'], affected_activity_tokens: [], can_preview: true, depends_on_routes: false,
    }] : [], remaining_must_adjust: 0, available_actions: []})
    if (pathname.endsWith('/changes/preview')) return reply({change_token: 'synthetic-old-preview',
      summary: '09:15开始', before: ['09:15'], after: ['10:45'], affected_days: ['2026-10-01'],
      changes: [{before: {start_time: '09:15'}, after: {start_time: '10:45'}}]})
    return route.fulfill({status: 404, json: {}})
  })
  await page.goto(`/trip/result#trip=${R}`)
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  return requests
}

for (const width of [1440, 390]) {
  test(`old result keeps relative days, unfinished content and real-route minutes in page and PNG at ${width}px`, async ({page}, info) => {
    await page.setViewportSize({width, height: 900})
    await page.addInitScript(() => {
      window.exportText = []
      const original = CanvasRenderingContext2D.prototype.fillText
      CanvasRenderingContext2D.prototype.fillText = function(text, ...args) {
        window.exportText.push(String(text))
        return original.call(this, text, ...args)
      }
    })
    const requests = await show(page)
    await expect(page.locator('[data-day-heading="1"]')).toHaveText('Day 1')
    await expect(page.locator('[data-day-heading="2"]')).toHaveText('Day 2')
    await expect(page.getByTestId('activity-card')).toHaveCount(2)
    await expect(page.getByTestId('day-unprocessed-2')).toContainText('1 处原文')
    await expect(page.locator('body')).not.toContainText(oldTime)
    await expect(page.getByTestId('itinerary-workspace')).toContainText('12 分钟')
    await page.screenshot({path: info.outputPath(`relative-main-${width}.png`), fullPage: true})
    const park = page.getByTestId('activity-card').filter({hasText: '青溪公园'})
    await park.getByRole('button', {name: /^青溪公园/}).click()
    await expect(page.getByTestId('source-internal-details')).toContainText('清风亭')
    await expect(page.locator('body')).not.toContainText(oldTime)
    await page.getByRole('button', {name: '收起地点确认', exact: true}).click()
    await page.getByRole('button', {name: '导出图片', exact: true}).click()
    const preview = page.getByAltText('行程横链导出预览', {exact: true})
    await expect(preview).toBeVisible()
    await expect.poll(() => preview.evaluate(image => image.naturalWidth)).toBe(1440)
    const download = page.waitForEvent('download')
    await page.getByTestId('download-itinerary-png').click()
    const png = await download
    expect(await png.failure()).toBeNull()
    await png.saveAs(info.outputPath(`relative-export-${width}.png`))
    const text = await page.evaluate(() => window.exportText.join('\n'))
    expect(text).not.toMatch(oldTime)
    for (const expected of ['Day 1', 'Day 2', '青溪公园', '星河博物馆', '清风亭', '12 分钟', '尚未完整整理']) expect(text).toContain(expected)
    await info.attach('png-drawn-text', {body: text, contentType: 'text/plain'})
    await page.reload()
    await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
    await page.getByTestId('journey-suggestions-toggle').click()
    const panel = page.getByRole('complementary', {name: '检查与建议', exact: true})
    await expect(panel.getByRole('combobox', {name: '建议所属日期'}).locator('option')).toHaveText(['Day 1', 'Day 2'])
    await panel.getByText('住宿', {exact: true}).click()
    await expect(panel.getByTestId('stay-segment')).toContainText('第 1 晚')
    await expect(panel).not.toContainText(oldTime)
    await panel.getByRole('button', {name: '关闭建议'}).click()
    await page.getByRole('button', {name: '地图', exact: true}).click()
    const map = page.getByTestId('result-view-map-stay')
    await expect(map).toBeVisible()
    await expect(map).toContainText('Day 1')
    await expect(map).not.toContainText(oldTime)
    await page.screenshot({path: info.outputPath(`relative-map-${width}.png`), fullPage: true})
    expect(requests.filter(item => /commands|changes\/adopt|place-candidates|map-renders$/.test(item.pathname))).toEqual([])
  })

  test(`old readonly share hides calendar and clock while preserving days and confirmed places at ${width}px`, async ({page}, info) => {
    await page.setViewportSize({width, height: 900})
    await page.route('**/api/**', route => route.fulfill({json: {title: '合成旧行程', message: '只读分享',
      destination: '北京', schedule: '2026-10-01 至 2026-10-02', party_size: '2人', accommodation: '青溪酒店',
      days: fixture().days.map(day => ({label: day.label, activities: day.activities.map(card => ({name: card.name,
        time_hint: '09:15–10:45', area_or_address: card.area_or_address, note: card.status === 'READY' ? '可直接查看' : '待确认'}))}))}}))
    await page.goto('/share/synthetic-relative-share')
    const shared = page.getByTestId('g06-shared-trip')
    await expect(shared).toBeVisible()
    await expect(shared.getByRole('heading', {level: 2})).toHaveText(['Day 1', 'Day 2'])
    await expect(shared).toContainText('青溪公园')
    await expect(shared).toContainText('星河博物馆')
    await expect(shared).toContainText('第 2 站')
    await expect(shared).toContainText('当天暂无地点')
    await expect(shared).not.toContainText(/2026-10|09:15|10:45|未确认中间站|未确认末日站/)
    await page.screenshot({path: info.outputPath(`relative-share-${width}.png`), fullPage: true})
  })
}

test('legacy timing preview returns to the itinerary without adopting its old fields', async ({page}, info) => {
  const requests = await show(page, true)
  await page.getByTestId('journey-suggestions-toggle').click()
  await page.getByRole('button', {name: '预览调整', exact: true}).click()
  const preview = page.getByTestId('change-preview')
  await expect(preview).toBeVisible()
  await expect(preview).toContainText('这条旧建议不再适用，请重新检查')
  await expect(page.locator('body')).not.toContainText(oldTime)
  await expect(page.getByTestId('adopt-change')).toHaveCount(0)
  await page.screenshot({path: info.outputPath('relative-old-preview.png'), fullPage: true})
  await preview.getByRole('button', {name: '返回行程'}).click()
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  expect(requests.filter(item => item.pathname.endsWith('/changes/preview'))).toHaveLength(1)
  expect(requests.filter(item => /commands|changes\/adopt/.test(item.pathname))).toEqual([])
})

for (const width of [1440, 390]) test(`profile edits relative preferences without erasing an old hidden clock at ${width}px`, async ({page}, info) => {
  await page.setViewportSize({width, height: 900})
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'synthetic-relative-profile-session')
    localStorage.setItem('authUser', JSON.stringify({userId: 'synthetic-relative-profile', nickname: '合成偏好测试'}))
  })
  let preference = {walking_tolerance_minutes: 45, preferred_start_time: '09:15',
    dining_preferences: [], hotel_preferences: [], intensity: null}
  const writes = []
  const consents = {memory_enabled: true, feedback_enabled: false, training_eval_enabled: false}
  await page.route('**/api/**', route => {
    const request = route.request(), pathname = new URL(request.url()).pathname
    if (pathname === '/api/user/me') return route.fulfill({json: {user_id: 'synthetic-relative-profile',
      nickname: '合成偏好测试', phone: null, avatar_url: null, birthday: null, created_at: '2026-01-01T00:00:00Z'}})
    if (pathname === '/api/v3/me/travel-preferences') {
      if (request.method() === 'PUT') {preference = request.postDataJSON(); writes.push(structuredClone(preference))}
      return route.fulfill({json: preference})
    }
    if (pathname === '/api/v3/me/data-consents') return route.fulfill({json: consents})
    if (pathname === '/api/v3/me/data-consents/feedback' && request.method() === 'PUT') {
      consents.feedback_enabled = request.postDataJSON().enabled
      return route.fulfill({json: consents})
    }
    return route.fulfill({status: 404, json: {}})
  })
  await page.goto('/profile')
  const panel = page.getByTestId('g06-memory-settings')
  const memory = panel.getByRole('button', {name: '切换记住结构化偏好', exact: true})
  await expect(memory).toHaveAttribute('aria-pressed', 'true')
  await expect(panel.locator('input[type="time"]')).toHaveCount(0)
  await expect(panel.getByTestId('preferred-start-time')).toHaveCount(0)
  await expect(panel).not.toContainText(/09:15|希望出发时间|出发时间/)
  await panel.getByTestId('walking-tolerance').fill('30')
  await panel.getByTestId('trip-intensity').selectOption('RELAXED')
  await panel.getByRole('button', {name: '当地风味', exact: true}).click()
  await panel.getByRole('button', {name: '连锁酒店', exact: true}).click()
  await panel.getByTestId('save-preferences').click()
  await expect(panel).toContainText('旅行偏好已更新')
  expect(writes).toEqual([{walking_tolerance_minutes: 30, preferred_start_time: '09:15',
    dining_preferences: ['LOCAL'], hotel_preferences: ['CHAIN'], intensity: 'RELAXED'}])
  await page.reload()
  await expect(panel.getByTestId('walking-tolerance')).toHaveValue('30')
  await expect(panel.getByTestId('trip-intensity')).toHaveValue('RELAXED')
  await expect(panel.getByRole('button', {name: '当地风味', exact: true})).toHaveAttribute('aria-pressed', 'true')
  await expect(panel.getByRole('button', {name: '连锁酒店', exact: true})).toHaveAttribute('aria-pressed', 'true')
  await expect(panel.locator('input[type="time"]')).toHaveCount(0)
  await panel.getByRole('button', {name: '切换允许保存产品反馈', exact: true}).click()
  await expect(panel.getByRole('button', {name: '切换允许保存产品反馈', exact: true})).toHaveAttribute('aria-pressed', 'true')
  await expect(panel.getByRole('button', {name: '切换允许用于训练或评测', exact: true})).toHaveAttribute('aria-pressed', 'false')
  await expect(memory).toHaveAttribute('aria-pressed', 'true')
  expect(preference.preferred_start_time).toBe('09:15')
  expect(writes).toHaveLength(1)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
  await panel.screenshot({path: info.outputPath(`relative-profile-${width}.png`)})
})
