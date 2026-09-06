const { test, expect } = require('@playwright/test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')

// Synthetic browser state only. Backend candidate signing and city validation
// remain covered by backend tests; this fixture exercises the visible editor.
const RESOURCE = 'synthetic-place-city-trip'
const ACTIVITY = 'synthetic-place-city-activity'
const ETAG = '"synthetic-city-initial"'

function deferred() {
  let release
  const promise = new Promise(resolve => { release = resolve })
  return { promise, release }
}

function candidate(city) {
  return { candidate_token: `synthetic-candidate-${city}`, name: `${city}星河博物馆`,
    category: '景点', area_or_address: `${city}合成地址`,
    position: { longitude: 121.48, latitude: 31.23, coordinate_system: 'GCJ02' } }
}

async function fixture(page, options = {}) {
  const card = { activity_token: ACTIVITY, name: '星河博物馆', city: options.cardCity ?? '北京',
    category: '景点', area_or_address: '地点待确认', status: 'NEEDS_CONFIRMATION', time_hint: null,
    available_actions: ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE'], knowledge_suggestions: [] }
  const state = { searches: [], commands: [], routePosts: 0, unsupported: [], etag: ETAG,
    result: { status: 'PARTIAL_RESULT', ownership: 'ANONYMOUS', is_demo: false, can_undo: false,
      expires_at: '2099-09-08T12:00:00Z', updated_at: '2026-09-06T12:00:00Z',
      assumptions: [{ key: 'destination', label: '目的地', value: '北京', editable: true },
        { key: 'calendar', label: '日期', value: 'Day 1', editable: true },
        { key: 'party_size', label: '人数', value: '2 人', editable: true }],
      days: [{ label: 'Day 1', activities: [card], alternatives: [] }],
      map: { status: 'UNAVAILABLE', message: '路线暂未准备。', available_actions: ['RENDER_MAP'] },
      stay: { status: 'UNAVAILABLE', message: '住宿待选择。', area_summary: '', searched_scopes: [],
        candidates: [], available_actions: [] }, available_actions: ['EDIT_ASSUMPTIONS', 'EDIT_CARDS'] } }
  const firstSearch = deferred()
  const saving = deferred()
  await page.addInitScript(({ resource, ignoreAbort }) => {
    localStorage.removeItem('authToken')
    localStorage.removeItem('authUser')
    sessionStorage.setItem('bt_active_trip_ref', resource)
    sessionStorage.setItem('bt_active_trip_mode', 'FULL')
    // Simulate a transport that finishes even after the editor cancels it.
    // The old response must actually arrive for the isolation assertion.
    if (ignoreAbort) {
      const fetch = window.fetch.bind(window)
      window.fetch = (url, init) => String(url).includes('/place-candidates')
        ? fetch(url, { ...init, signal: undefined }) : fetch(url, init)
    }
  }, { resource: RESOURCE, ignoreAbort: options.ignoreAbort === true })
  await page.route('**/restapi.amap.com/**', route => route.abort('blockedbyclient'))
  await page.route('**/api/user/me', route => route.fulfill({ status: 401, json: {} }))
  await page.route('**/api/v3/trip-understandings/**', async route => {
    const request = route.request()
    const action = new URL(request.url()).pathname.split(RESOURCE)[1]
    const reply = (body, status = 200) => route.fulfill({ status, json: body,
      headers: { ETag: state.etag, 'Cache-Control': 'no-store' } })
    if (action === '/result') return reply(state.result)
    if (action === '/map-renders/latest') return reply({ ...state.result.map, points: [], days: [] })
    if (action === '/stay-suggestions') return reply(state.result.stay)
    if (action === '/supplementary') return reply({ status: 'AVAILABLE', days: [] })
    if (action === '/materialize') return reply({ status: 'READY', message: '行程已准备好。',
      calendar: '按 Day 编号安排', party_size: 2, checks_available: true })
    if (action === '/checks') return reply({ status: 'STILL_NEEDS_CONFIRMATION', message: '先确认地点。',
      items: [], remaining_must_adjust: 0, available_actions: [] })
    if (action === '/place-candidates') {
      const body = request.postDataJSON()
      state.searches.push(body)
      const isFirst = state.searches.length === 1
      if (options.holdFirstSearch && isFirst) await firstSearch.promise
      if ((options.failCity && body.city === options.failCity) || (isFirst && options.failFirstSearch)) return reply({}, 503)
      return reply({ status: 'AVAILABLE', candidates: [candidate(body.city || '北京')] })
    }
    if (action === '/commands') {
      const body = request.postDataJSON()
      state.commands.push(body)
      if (options.holdCommand) await saving.promise
      const city = ['北京', '上海', '杭州'].find(value => candidate(value).candidate_token === body.candidate_token)
      if (body.command_type !== 'PLACE_CONFIRM' || body.activity_token !== ACTIVITY || !city)
        return reply({ detail: { message: '候选已变化。' } }, 409)
      Object.assign(card, { name: candidate(city).name, city, status: 'READY',
        area_or_address: candidate(city).area_or_address, activity_token: ACTIVITY + '-updated' })
      state.result.status = 'READY'
      state.result.can_undo = true
      state.result.map = { status: 'NEEDS_UPDATE', message: '请手动更新路线。', available_actions: ['RENDER_MAP'] }
      state.etag = '"synthetic-city-updated"'
      return reply({ status: 'APPLIED', changed_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE' })
    }
    if (action === '/map-renders') state.routePosts++
    state.unsupported.push(action)
    return reply({}, 404)
  })
  await page.goto(`/trip/result#trip=${RESOURCE}`)
  await page.getByTestId('activity-card').first().getByRole('button').filter({ hasText: '星河博物馆' }).click()
  const dropdown = page.getByTestId('pending-place-dropdown')
  await expect(dropdown).toBeVisible()
  return { state, dropdown, releaseSearch: firstSearch.release, releaseCommand: saving.release }
}

async function paint(page) {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
}

for (const width of [1440, 1280, 390, 360]) {
  test(`city search stays inline and explicit at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 })
    const { state, dropdown } = await fixture(page, { cardCity: '杭州' })
    await expect(dropdown.getByLabel('查询城市')).toHaveValue('杭州')
    await expect(dropdown.getByRole('textbox')).toHaveAttribute('maxlength', '40')
    await expect(dropdown.getByRole('textbox')).toBeFocused()
    await dropdown.getByLabel('查询城市').selectOption('上海')
    await paint(page)
    expect(state.searches).toEqual([])
    expect(state.commands).toEqual([])
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await dropdown.getByRole('button', { name: '搜索', exact: true }).click()
    await expect(dropdown.locator('.pending-place-option')).toContainText('上海星河博物馆')
    expect(state.searches).toEqual([{ activity_token: ACTIVITY, query: '星河博物馆', city: '上海' }])
    const box = await dropdown.boundingBox()
    expect(box.x).toBeGreaterThanOrEqual(0)
    expect(box.x + box.width).toBeLessThanOrEqual(width)
    await page.screenshot({ path: info.outputPath(`city-${width}.png`), fullPage: true })
    expect(state.routePosts).toBe(0)
    expect(state.unsupported).toEqual([])
  })
}

test('unsupported card city follows the itinerary without adding a city field', async ({ page }) => {
  const { state, dropdown } = await fixture(page, { cardCity: '广州' })
  await expect(dropdown.getByLabel('查询城市')).toHaveValue('')
  await dropdown.getByRole('button', { name: '搜索', exact: true }).click()
  await expect(dropdown.locator('.pending-place-option')).toBeVisible()
  expect(state.searches).toEqual([{ activity_token: ACTIVITY, query: '星河博物馆' }])
})

test('changing city clears candidates, selection, errors and busy state without searching', async ({ page }) => {
  const { state, dropdown } = await fixture(page, { failCity: '上海' })
  await dropdown.getByRole('button', { name: '搜索', exact: true }).click()
  await dropdown.locator('.pending-place-option').click()
  await expect(dropdown.getByRole('button', { name: '使用这个地点' })).toBeVisible()
  await dropdown.getByLabel('查询城市').selectOption('上海')
  await expect(dropdown.locator('.pending-place-option')).toHaveCount(0)
  await expect(dropdown.getByRole('button', { name: '使用这个地点' })).toHaveCount(0)
  expect(state.searches).toHaveLength(1)
  await dropdown.getByRole('button', { name: '搜索', exact: true }).click()
  await expect(dropdown.getByRole('status')).toContainText('查询暂不可用')
  await dropdown.getByLabel('查询城市').selectOption('杭州')
  await expect(dropdown.getByRole('status')).toHaveCount(0)
  await expect(dropdown.getByRole('button', { name: '搜索', exact: true })).toBeEnabled()
  await paint(page)
  expect(state.searches).toHaveLength(2)
  expect(state.commands).toEqual([])
})

for (const failFirstSearch of [false, true]) {
  test(`late previous-city ${failFirstSearch ? 'error' : 'result'} cannot replace the current city`, async ({ page }) => {
    const { state, dropdown, releaseSearch } = await fixture(page, {
      holdFirstSearch: true, ignoreAbort: true, failFirstSearch,
    })
    try {
      const oldResponse = page.waitForResponse(response => response.url().endsWith('/place-candidates')
        && response.request().postDataJSON().city === '北京')
      await dropdown.getByRole('button', { name: '搜索', exact: true }).click()
      await expect.poll(() => state.searches.length).toBe(1)
      await expect(dropdown.getByRole('button', { name: '查询中…' })).toBeDisabled()
      await dropdown.getByLabel('查询城市').selectOption('上海')
      await expect(dropdown.getByRole('button', { name: '搜索', exact: true })).toBeEnabled()
      expect(state.searches).toHaveLength(1)
      await dropdown.getByRole('button', { name: '搜索', exact: true }).click()
      await expect(dropdown.locator('.pending-place-option')).toContainText('上海星河博物馆')
      releaseSearch()
      const delivered = await oldResponse
      // The client rejects a non-2xx response from its headers without reading
      // its body. Waiting on Chromium's unconsumed error body can never finish;
      // the delivered status followed by a paint exercises the actual catch.
      if (failFirstSearch) expect(delivered.status()).toBe(503)
      else await delivered.finished()
      await paint(page)
      await expect(dropdown.locator('.pending-place-option')).toHaveCount(1)
      await expect(dropdown.locator('.pending-place-option')).toContainText('上海星河博物馆')
      await expect(dropdown.getByRole('status')).toHaveCount(0)
      expect(state.searches.map(body => body.city)).toEqual(['北京', '上海'])
      expect(state.commands).toEqual([])
    } finally { releaseSearch() }
  })
}

test('only the selected current-city candidate is confirmed once', async ({ page }) => {
  const { state, dropdown, releaseCommand } = await fixture(page, { holdCommand: true })
  try {
    await dropdown.getByRole('button', { name: '搜索', exact: true }).click()
    await dropdown.locator('.pending-place-option').click()
    await expect(dropdown.getByRole('button', { name: '使用这个地点' })).toBeVisible()
    await dropdown.getByLabel('查询城市').selectOption('上海')
    await expect(dropdown.getByRole('button', { name: '使用这个地点' })).toHaveCount(0)
    await dropdown.getByRole('button', { name: '搜索', exact: true }).click()
    await dropdown.locator('.pending-place-option').click()
    const confirm = dropdown.getByRole('button', { name: '使用这个地点' })
    await confirm.evaluate(button => { button.click(); button.click() })
    await expect.poll(() => state.commands.length).toBe(1)
    await expect(dropdown.getByLabel('查询城市')).toBeDisabled()
    await expect(confirm).toBeDisabled()
    expect(state.commands).toEqual([{ command_type: 'PLACE_CONFIRM', activity_token: ACTIVITY,
      candidate_token: candidate('上海').candidate_token }])
    releaseCommand()
    await expect(dropdown).toHaveCount(0)
    await expect(page.getByTestId('activity-card').first()).toContainText('上海星河博物馆')
    expect(state.commands).toHaveLength(1)
    expect(state.routePosts).toBe(0)
    expect(state.unsupported).toEqual([])
  } finally { releaseCommand() }
})

// Exercise the actual helpers without a build artifact or a live HTTP service.
function loadTs(filename, globals = {}) {
  const module = { exports: {} }
  const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText
  const context = { module, exports: module.exports, ...globals,
    require: name => loadTs(path.resolve(path.dirname(filename), name + '.ts'), globals) }
  vm.runInNewContext(output, context, { filename })
  return module.exports
}

test('browser query helper keeps the existing AbortSignal slot and optionally sends city', async () => {
  const requests = []
  const helper = loadTs(path.resolve(__dirname, '../src/lib/trip-understanding-v3.ts'), {
    fetch: async (url, options) => {
      requests.push({ url, ...options })
      return { ok: true, json: async () => ({ status: 'EMPTY', candidates: [] }) }
    },
  })
  const controller = new AbortController()
  await helper.queryTripPlaceCandidates(RESOURCE, ACTIVITY, '星河博物馆', controller.signal)
  await helper.queryTripPlaceCandidates(RESOURCE, ACTIVITY, '星河博物馆', controller.signal, '杭州')
  expect(JSON.parse(requests[0].body)).toEqual({ activity_token: ACTIVITY, query: '星河博物馆' })
  expect(JSON.parse(requests[1].body)).toEqual({ activity_token: ACTIVITY, query: '星河博物馆', city: '杭州' })
  expect(requests.every(request => request.signal === controller.signal && request.credentials === 'include')).toBe(true)
})

test('shared client keeps the three-argument query and appends optional city', async () => {
  const requests = []
  const { TripCheckClient } = loadTs(path.resolve(__dirname, '../../packages/trip-check-client/src/client.ts'))
  const client = new TripCheckClient({ request: async request => {
    requests.push(request)
    return { status: 200, data: { status: 'EMPTY', candidates: [] }, headers: {} }
  } })
  await client.queryTripPlaceCandidates(RESOURCE, ACTIVITY, '星河博物馆')
  await client.queryTripPlaceCandidates(RESOURCE, ACTIVITY, '星河博物馆', '上海')
  expect(requests.map(request => request.body)).toEqual([
    { activity_token: ACTIVITY, query: '星河博物馆' },
    { activity_token: ACTIVITY, query: '星河博物馆', city: '上海' },
  ])
})
