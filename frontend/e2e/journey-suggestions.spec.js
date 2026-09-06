const { test, expect } = require('@playwright/test')

// Controlled browser evidence only. Every text and identity below is synthetic.
// The API fixture models one cookie-authorized resource, opaque quoted ETags,
// bound candidate credentials, idempotent commands and authoritative readback.
// It does not replace real backend permission or cryptographic verification tests.
const RESOURCE = 'quality-synthetic-trip-000001'
const INITIAL_ETAG = '"tu3_quality_synthetic_initial"'
const OWNER_COOKIE = 'quality-synthetic-owner-cookie'
const A = 'quality-activity-token-000001'
const B = 'quality-activity-token-000002'
const C = 'quality-activity-token-000003'
const DINING_NAME = '合成槐香小馆'

function deferred() {
  let release
  const promise = new Promise(resolve => { release = resolve })
  return { promise, release }
}

function activity(token, name, category = '景点', status = 'READY') {
  return { activity_token: token, name, city: '北京', category,
    area_or_address: '北京市东城区合成地址', time_hint: null,
    start_time: null, end_time: null, visit_duration_minutes: null,
    timing_source: 'UNSPECIFIED', locked: false, fixed_commitment: false,
    status, available_actions: ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE'],
    knowledge_suggestions: [], photo_url: null }
}

function initialStay() {
  return { status: 'LIMITED', message: '部分住宿通勤信息暂缺，可以继续查看行程。',
    area_summary: '东城区', searched_scopes: ['2公里'], available_actions: ['CHOOSE_STAY'],
    candidates: [{ candidate_token: 'quality-stay-candidate-token-000001',
      name: '合成安心酒店', brand: '合成连锁', category: '住宿', area_or_address: '北京市东城区合成地址',
      commute_summary: '通勤信息尚不完整，暂不展示完整耗时。', max_single_leg_minutes: null,
      transfer_count: 0, reason: '可先核对位置，部分通勤仍需确认。',
      available_actions: ['CHOOSE_STAY'], selected: false }] }
}

function initialResult() {
  return { status: 'READY', ownership: 'ANONYMOUS', can_undo: false, is_demo: false,
    expires_at: '2099-09-08T12:00:00Z', updated_at: '2026-09-06T12:00:00Z',
    assumptions: [{ key: 'destination', label: '目的地', value: '北京', editable: true },
      { key: 'calendar', label: '日期', value: 'Day 1、Day 2', editable: true },
      { key: 'party_size', label: '人数', value: '2 人', editable: true }],
    days: [{ label: 'Day 1', activities: [activity(A, '故宫博物院'), activity(B, '景山公园')],
      alternatives: [{ name: '北海公园', category: '景点', city: '北京' }] },
    { label: 'Day 2', activities: [activity(C, '天坛公园')], alternatives: [] }],
    map: { status: 'UNAVAILABLE', message: '路线暂未准备。', available_actions: ['RENDER_MAP'] },
    stay: initialStay(), available_actions: ['EDIT_ASSUMPTIONS', 'EDIT_CARDS'] }
}

async function installFixture(page, baseURL, options = {}) {
  const state = { result: initialResult(), etag: INITIAL_ETAG, searches: [], commands: [],
    commandApplications: 0, routePosts: 0, placeSearches: 0, providerRequests: 0, checksReads: 0,
    rejectedAccess: 0, unsupported: [], issued: new Map(), replays: new Map() }
  const searchGate = deferred()
  const commandGate = deferred()
  await page.context().addCookies([{ name: 'bt_demo_capability', value: OWNER_COOKIE,
    url: new URL(baseURL).origin, httpOnly: true, sameSite: 'Lax' }])
  await page.addInitScript(({ resource }) => {
    localStorage.removeItem('authToken')
    localStorage.removeItem('authUser')
    sessionStorage.setItem('bt_active_trip_ref', resource)
    sessionStorage.setItem('bt_active_trip_mode', 'FULL')
  }, { resource: RESOURCE })
  await page.route('**/restapi.amap.com/**', async route => {
    state.providerRequests++
    await route.abort('blockedbyclient')
  })
  await page.route('**/api/user/me', route => route.fulfill({ status: 401, json: { detail: { message: '请登录' } } }))
  await page.route('**/api/v3/trip-understandings/**', async route => {
    const request = route.request()
    const headers = await request.allHeaders()
    const pathname = new URL(request.url()).pathname
    const base = `/api/v3/trip-understandings/${RESOURCE}`
    const fulfill = (body, status = 200, etag = state.etag) => route.fulfill({ status,
      contentType: 'application/json', headers: { ETag: etag, 'Cache-Control': 'no-store' },
      body: JSON.stringify(body) })
    if (!pathname.startsWith(base + '/') || !(headers.cookie || '').includes(`bt_demo_capability=${OWNER_COOKIE}`)) {
      state.rejectedAccess++
      return fulfill({ detail: { code: 'NOT_FOUND', message: '无法访问这份行程' } }, 404)
    }
    const action = pathname.slice(base.length)
    if (action === '/result') return fulfill(state.result)
    if (action === '/map-renders/latest') return fulfill({ ...state.result.map, points: [], days: [] })
    if (action === '/stay-suggestions') return fulfill(state.result.stay)
    if (action === '/supplementary') return fulfill({ status: 'AVAILABLE', days: [] })
    if (action === '/materialize') {
      if (!headers['idempotency-key'] || headers['if-match'] !== state.etag)
        return fulfill({ detail: { code: 'REVISION_CONFLICT', message: '行程已更新' } }, 409)
      return fulfill({ status: 'READY', message: '行程已准备好，可以检查。',
        calendar: '按 Day 编号安排', party_size: 2, checks_available: true })
    }
    if (action === '/checks') {
      state.checksReads++
      return fulfill({ status: 'STILL_NEEDS_CONFIRMATION', message: '尚有明确时间和路线需要确认。',
        items: [{ check_token: 'quality-check-token-000001', label: '需要确认', title: '补充活动时间',
          message: '缺少明确时间，暂不能判断当天是否来得及。', affected_days: ['Day 1'],
          affected_activity_tokens: [state.result.days[0].activities[0].activity_token],
          can_preview: false, depends_on_routes: false, basis_status: 'CURRENT' }],
        remaining_must_adjust: 0, available_actions: [] })
    }
    if (action === '/dining-candidates') {
      const body = request.postDataJSON()
      state.searches.push(body)
      const capturedTag = options.staleSearch ? '"tu3_quality_older_result"' : state.etag
      const capturedAnchor = state.result.days.flatMap(day => day.activities)
        .find(card => card.activity_token === body.activity_token && card.status === 'READY')
      if (!capturedAnchor) return fulfill({ status: 'NEEDS_CONFIRMATION', message: '先确认当天的一个地点。', candidates: [] })
      if (options.holdFirstSearch && state.searches.length === 1) await searchGate.promise
      if (options.failFirstSearch && state.searches.length === 1)
        return fulfill({ detail: { message: '暂时无法查找附近餐饮。' } }, 503)
      const token = `quality-opaque-dining-candidate-${body.activity_token}`
      state.issued.set(token, { anchor: body.activity_token, etag: capturedTag })
      return fulfill({ status: 'AVAILABLE', message: `以下餐饮靠近${capturedAnchor.name}，营业情况尚待确认。`,
        candidates: [{ candidate_token: token, name: DINING_NAME, category: '餐饮',
          area_or_address: '北京市东城区合成街道', reason: `靠近${capturedAnchor.name}，按直线距离比较。`,
          position: { longitude: 116.398, latitude: 39.917, coordinate_system: 'GCJ02' } }] }, 200, capturedTag)
    }
    if (action === '/commands') {
      const command = request.postDataJSON()
      state.commands.push({ body: command, etag: headers['if-match'], key: headers['idempotency-key'] })
      if (options.holdCommand) await commandGate.promise
      const key = headers['idempotency-key']
      const requestBinding = JSON.stringify([command, headers['if-match']])
      const replay = state.replays.get(key)
      if (replay && replay.requestBinding === requestBinding)
        return fulfill(replay.body, 200, replay.etag)
      if (!key || replay || headers['if-match'] !== state.etag)
        return fulfill({ detail: { code: 'REVISION_CONFLICT', message: '行程已调整，请刷新。' } }, 409)
      let targetDay
      let position
      let inserted
      if (command.command_type === 'DINING_INSERT') {
        const credential = state.issued.get(command.candidate_token)
        targetDay = state.result.days.find(day => day.activities.some(card => card.activity_token === command.after_activity_token))
        if (!credential || credential.anchor !== command.after_activity_token || credential.etag !== state.etag || !targetDay)
          return fulfill({ detail: { code: 'COMMAND_TARGET_CHANGED', message: '餐饮建议已变化，请重新查询。' } }, 409)
        position = targetDay.activities.findIndex(card => card.activity_token === command.after_activity_token) + 1
        inserted = activity('quality-inserted-dining-token-000001', DINING_NAME, '餐饮')
      } else if (command.command_type === 'ACTIVITY_INSERT') {
        targetDay = state.result.days[command.day_index - 1]
        if (!targetDay || !targetDay.alternatives.some(item => item.name === command.name && item.category === command.category))
          return fulfill({ detail: { code: 'COMMAND_TARGET_CHANGED', message: '备选已变化。' } }, 409)
        position = command.position
        inserted = activity('quality-inserted-optional-token-000001', command.name, command.category, 'NEEDS_CONFIRMATION')
      } else {
        state.unsupported.push(action + ':' + command.command_type)
        return fulfill({ detail: { message: '本测试未定义此操作。' } }, 422)
      }
      targetDay.activities.splice(position, 0, inserted)
      state.commandApplications++
      for (const day of state.result.days) for (const card of day.activities)
        card.activity_token = `${card.activity_token}-next${state.commandApplications}`
      state.result.can_undo = true
      state.result.status = command.command_type === 'ACTIVITY_INSERT' ? 'PARTIAL_RESULT' : 'READY'
      state.result.map = { status: 'NEEDS_UPDATE', message: '行程已调整，请手动更新路线。', available_actions: ['RENDER_MAP'] }
      state.result.stay = { ...initialStay(), status: 'NEEDS_UPDATE', message: '住宿通勤需要重新确认。', candidates: [], available_actions: [] }
      state.etag = `"tu3_quality_synthetic_updated_${state.commandApplications}"`
      const body = { status: 'APPLIED', changed_days: [targetDay.label], map_readiness: 'NEEDS_UPDATE' }
      state.replays.set(key, { body, etag: state.etag, requestBinding })
      return fulfill(body)
    }
    if (action === '/map-renders') state.routePosts++
    if (action === '/place-candidates') state.placeSearches++
    state.unsupported.push(action)
    return fulfill({ detail: { message: '本测试未定义此操作。' } }, 404)
  })
  await page.goto(`/trip/result#trip=${RESOURCE}`)
  await expect(page.getByTestId('journey-suggestions-toggle')).toBeVisible()
  await expect.poll(() => state.checksReads).toBeGreaterThan(0)
  return { state, releaseSearch: searchGate.release, releaseCommand: commandGate.release }
}

async function openPanel(page) {
  await page.getByTestId('journey-suggestions-toggle').click()
  const panel = page.getByRole('complementary', { name: '检查与建议', exact: true })
  await expect(panel).toBeVisible()
  return panel
}

async function openDining(page) {
  const panel = await openPanel(page)
  await panel.locator('summary').filter({ hasText: /^附近餐饮$/ }).click()
  return panel
}

function noSideEffects(state) {
  expect(state.routePosts).toBe(0)
  expect(state.placeSearches).toBe(0)
  expect(state.providerRequests).toBe(0)
  expect(state.rejectedAccess).toBe(0)
  expect(state.unsupported).toEqual([])
}

test('suggestions start collapsed; expanding sections does not search or write', async ({ page, baseURL }) => {
  const { state } = await installFixture(page, baseURL)
  await expect(page.getByTestId('journey-suggestions-toggle')).toHaveAttribute('aria-expanded', 'false')
  await expect(page.locator('#journey-suggestions')).toHaveCount(0)
  expect(state.searches).toEqual([])
  const panel = await openDining(page)
  await panel.locator('summary').filter({ hasText: /^住宿$/ }).click()
  expect(state.searches).toEqual([])
  expect(state.commands).toEqual([])
  noSideEffects(state)
})

test('explicit dining search uses the selected current-day anchor exactly once', async ({ page, baseURL }) => {
  const { state } = await installFixture(page, baseURL)
  const panel = await openDining(page)
  await panel.getByLabel('餐饮附近地点').selectOption('1')
  expect(state.searches).toEqual([])
  await panel.getByRole('button', { name: '找附近餐饮', exact: true }).click()
  await expect(panel.getByRole('heading', { name: DINING_NAME })).toBeVisible()
  expect(state.searches).toEqual([{ activity_token: B }])
  await expect(panel.getByText('靠近景山公园，按直线距离比较。')).toBeVisible()
  expect(state.commands).toEqual([])
  noSideEffects(state)
})

test('rapid dining adoption sends one bound command and reads back one inserted card', async ({ page, baseURL }) => {
  const fixture = await installFixture(page, baseURL, { holdCommand: true })
  try {
    const panel = await openDining(page)
    await panel.getByRole('button', { name: '找附近餐饮', exact: true }).click()
    const add = panel.getByRole('button', { name: '加入行程', exact: true })
    await expect(add).toBeEnabled()
    // Two synchronous activations exercise the immediate lock before React rerenders.
    await add.evaluate(button => { button.click(); button.click() })
    await expect.poll(() => fixture.state.commands.length).toBe(1)
    expect(fixture.state.commands[0]).toMatchObject({ etag: INITIAL_ETAG,
      body: { command_type: 'DINING_INSERT', after_activity_token: A,
        candidate_token: `quality-opaque-dining-candidate-${A}` } })
    expect(fixture.state.commands[0].key).toBeTruthy()
    fixture.releaseCommand()
    await expect(page.getByTestId('trip-days').getByText(DINING_NAME, { exact: true })).toBeVisible()
    expect(fixture.state.result.days[0].activities.map(card => card.name)).toEqual(['故宫博物院', DINING_NAME, '景山公园'])
    expect(fixture.state.commandApplications).toBe(1)
    expect(fixture.state.commands).toHaveLength(1)
    expect(fixture.state.result.map.status).toBe('NEEDS_UPDATE')
    noSideEffects(fixture.state)
  } finally { fixture.releaseCommand() }
})

test('a dining response bound to an older quoted ETag cannot be adopted', async ({ page, baseURL }) => {
  const { state } = await installFixture(page, baseURL, { staleSearch: true })
  const panel = await openDining(page)
  await panel.getByRole('button', { name: '找附近餐饮', exact: true }).click()
  await expect(panel.getByText('行程有调整，请重新查询。')).toBeVisible()
  await expect(panel.getByRole('button', { name: '加入行程', exact: true })).toHaveCount(0)
  expect(state.commands).toHaveLength(0)
  noSideEffects(state)
})

test('a dining lookup failure keeps cards and permits one explicit retry', async ({ page, baseURL }) => {
  const { state } = await installFixture(page, baseURL, { failFirstSearch: true })
  const panel = await openDining(page)
  const find = panel.getByRole('button', { name: '找附近餐饮', exact: true })
  await find.click()
  await expect(panel.getByText('暂时没能查到附近餐饮，请重试。')).toBeVisible()
  await expect(page.getByTestId('trip-days').getByText('故宫博物院', { exact: true })).toBeVisible()
  await expect(find).toBeEnabled()
  await find.click()
  await expect(panel.getByRole('heading', { name: DINING_NAME })).toBeVisible()
  expect(state.searches).toHaveLength(2)
  expect(state.commands).toHaveLength(0)
  noSideEffects(state)
})

test('switching days discards a late dining response for the previous anchor', async ({ page, baseURL }) => {
  const fixture = await installFixture(page, baseURL, { holdFirstSearch: true })
  try {
    const panel = await openDining(page)
    await panel.getByRole('button', { name: '找附近餐饮', exact: true }).click()
    await expect.poll(() => fixture.state.searches.length).toBe(1)
    await panel.getByLabel('建议所属日期').selectOption('1')
    fixture.releaseSearch()
    await expect(panel.getByLabel('餐饮附近地点')).toHaveText('天坛公园')
    await expect(panel.getByRole('heading', { name: DINING_NAME })).toHaveCount(0)
    await panel.getByRole('button', { name: '找附近餐饮', exact: true }).click()
    await expect(panel.getByRole('heading', { name: DINING_NAME })).toBeVisible()
    expect(fixture.state.searches).toEqual([{ activity_token: A }, { activity_token: C }])
    await expect(panel.getByText('靠近天坛公园，按直线距离比较。')).toBeVisible()
    noSideEffects(fixture.state)
  } finally { fixture.releaseSearch() }
})

test('adding an alternative keeps it unconfirmed and makes no automatic POI or route request', async ({ page, baseURL }) => {
  const { state } = await installFixture(page, baseURL)
  const panel = await openPanel(page)
  await panel.locator('summary').filter({ hasText: /^备选地点/ }).click()
  await panel.getByRole('button', { name: '加入待确认', exact: true }).click()
  await expect(page.getByTestId('trip-days').getByText('北海公园', { exact: true })).toBeVisible()
  expect(state.commands).toHaveLength(1)
  expect(state.commands[0].body).toEqual({ command_type: 'ACTIVITY_INSERT', day_index: 1,
    position: 2, name: '北海公园', category: '景点', city: '北京' })
  expect(state.result.days[0].activities.at(-1).status).toBe('NEEDS_CONFIRMATION')
  await expect(page.getByTestId('trip-days').getByText('待确认', { exact: true })).toBeVisible()
  expect(state.searches).toHaveLength(0)
  noSideEffects(state)
})

test('checks explain missing times and nullable hotel minutes never become a made-up number', async ({ page, baseURL }) => {
  const { state } = await installFixture(page, baseURL)
  const panel = await openPanel(page)
  await expect(panel.getByRole('heading', { name: '补充活动时间' })).toBeVisible()
  await expect(panel.getByText('缺少明确时间，暂不能判断当天是否来得及。')).toBeVisible()
  await expect(panel.getByText('按已有地点、路线和明确时间检查；营业、预约与天气尚未核验。')).toBeVisible()
  await panel.locator('summary').filter({ hasText: /^行程检查$/ }).click()
  await expect(panel.getByRole('heading', { name: '补充活动时间' })).not.toBeVisible()
  await panel.locator('summary').filter({ hasText: /^住宿$/ }).click()
  await expect(panel.getByRole('heading', { name: '合成安心酒店' })).toBeVisible()
  await expect(panel.getByText('通勤信息尚不完整，暂不展示完整耗时。')).toBeVisible()
  await expect(panel).not.toContainText(/120\s*分钟|\bnull\b|NaN|0\s*分钟/)
  noSideEffects(state)
})

for (const width of [1440, 1280, 390, 360]) {
  test(`suggestions fit ${width}px and Escape restores the trigger focus`, async ({ page, baseURL }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    const { state } = await installFixture(page, baseURL)
    const panel = await openDining(page)
    await panel.locator('summary').filter({ hasText: /^住宿$/ }).click()
    await panel.locator('summary').filter({ hasText: /^备选地点/ }).click()
    const bounds = await panel.boundingBox()
    expect(bounds.x).toBeGreaterThanOrEqual(0)
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(width + 1)
    expect(bounds.y).toBeGreaterThanOrEqual(0)
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(901)
    expect(await panel.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath(`suggestions-${width}.png`), fullPage: true })
    await panel.getByLabel('建议所属日期').focus()
    await page.keyboard.press('Escape')
    await expect(panel).toHaveCount(0)
    await expect(page.getByTestId('journey-suggestions-toggle')).toBeFocused()
    expect(state.searches).toEqual([])
    noSideEffects(state)
  })
}
