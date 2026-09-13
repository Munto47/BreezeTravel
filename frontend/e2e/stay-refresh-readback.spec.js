const { test, expect } = require('@playwright/test')

// Controlled API fixtures verify browser state transitions, not hotel quality.
const RESOURCE = 'synthetic-stay-refresh-readback-only'
const PREFIX = `/api/v3/trip-understandings/${RESOURCE}`
const OLD_MESSAGE = '行程已修改，住宿通勤尚未更新。'
const READY_MESSAGE = '住宿已重新核验，部分通勤仍待确认。'
const HOTEL = '合成核验连锁酒店'
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }

function stay(status) {
  return { status, message: status === 'NEEDS_UPDATE' ? OLD_MESSAGE : status === 'PREPARING' ? '正在核验本次住宿。' : READY_MESSAGE,
    searched_scopes: [], area_summary: null, candidates: [], available_actions: [],
    segments: status === 'LIMITED' ? [{ segment_token: 'synthetic-segment', city: '北京', overnight_days: ['Day 1'], status: 'LIMITED',
      message: '只有部分通勤已确认，可以选择已核验门店。', preserved_hotels: [], candidates: [{
        candidate_token: 'synthetic-current-stay-candidate', name: HOTEL, brand: '合成品牌', brand_note: '受控测试门店',
        category: '住宿', area_or_address: '合成门店地址', commute_summary: '部分通勤尚未确认', reason: '合成建议', selected: false,
        available_actions: ['CHOOSE_STAY'],
      }] }] : [] }
}

function result(status) {
  const card = (name, i) => ({ activity_token: `synthetic-place-${i}-000000000`, name, category: '景点', city: '北京',
    status: 'READY', area_or_address: '合成地址', time_hint: null, photo_url: null, available_actions: ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE'] })
  return { status: 'READY', ownership: 'ANONYMOUS', is_demo: false, can_undo: false,
    assumptions: [{ key: 'destination', label: '目的地', value: '北京', editable: true }],
    days: [1, 2].map(i => ({ label: `Day ${i}`, activities: [card(`合成第${i}天公园`, i)], alternatives: [] })),
    map: { status: 'NEEDS_UPDATE', message: '路线需要手动更新。', available_actions: ['RENDER_MAP'] },
    stay: stay(status), available_actions: ['EDIT_ASSUMPTIONS', 'EDIT_CARDS'] }
}

async function fixture(page, options = {}) {
  const oldRead = deferred(), lateRead = deferred(), materialize = deferred()
  const state = { resultReads: 0, afterPostReads: 0, stayPosts: [], selections: [], mapPosts: 0, commands: 0,
    complete: !options.preparing, oldWaiting: false, oldDelivered: false, lateWaiting: false, materializeWaiting: false, version: 2 }
  await page.route('**/restapi.amap.com/**', route => route.abort())
  await page.route('**/api/user/me', route => route.fulfill({ status: 401, json: {} }))
  await page.route('**/api/v3/trip-understandings/**', async route => {
    const request = route.request(), action = new URL(request.url()).pathname.slice(PREFIX.length)
    const reply = (json, status = 200, version = state.version) => route.fulfill({ status, json, headers: { ETag: `"fixture-${version}"` } })
    if (action === '/result') {
      state.resultReads++
      if (state.resultReads === 1) return reply(result('NEEDS_UPDATE'))
      if (options.oldRead && !state.stayPosts.length) {
        state.oldWaiting = true
        await oldRead.promise
        await reply(result('NEEDS_UPDATE'), 200, 2)
        state.oldDelivered = true
        return
      }
      state.afterPostReads++
      if (options.lateResult) {
        state.lateWaiting = true
        await lateRead.promise
        return reply(result('LIMITED'), 200, options.responseVersion ?? 2)
      }
      if (options.readFailure) return reply({}, 503)
      return reply(result(state.complete ? 'LIMITED' : 'PREPARING'))
    }
    if (action === '/stay-suggestions') {
      if (request.method() === 'POST') { state.stayPosts.push(request.headers()); return reply(stay('PREPARING')) }
      return reply(stay(!state.stayPosts.length ? 'NEEDS_UPDATE' : state.complete ? 'LIMITED' : 'PREPARING'))
    }
    if (action === '/stay-selection') {
      state.selections.push(request.postDataJSON())
      state.version++
      return reply({ status: 'APPLIED', selected_stay: HOTEL, overnight_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE' })
    }
    if (action === '/materialize') {
      if (options.oldRead && !state.oldWaiting) return reply({}, 409)
      if (options.changeVersion && !state.materializeWaiting) {
        state.materializeWaiting = true
        await materialize.promise
        state.version = 3
      }
      return reply({ status: 'READY', message: '行程已准备。', calendar: '按日期', party_size: 2, checks_available: true })
    }
    if (action === '/checks') return reply({ status: 'STILL_NEEDS_CONFIRMATION', message: '路线资料不足。', items: [], remaining_must_adjust: 0, available_actions: [] })
    if (action === '/map-renders/latest') return reply({ ...result('NEEDS_UPDATE').map, days: [], points: [] })
    if (action === '/supplementary') return reply({ status: 'AVAILABLE', days: [] })
    if (action === '/daily-dining') return reply({ status: 'UNAVAILABLE', message: '合成用餐未配置。', days: [] })
    if (action === '/map-renders') state.mapPosts++
    if (action === '/commands') state.commands++
    return reply({}, 404)
  })
  await page.goto(`/trip/result#trip=${RESOURCE}`)
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  await page.getByTestId('desktop-nav-map_stay').click()
  const panel = page.getByTestId('stay-panel')
  await panel.locator('summary').click()
  await expect(panel).toContainText(OLD_MESSAGE)
  return { state, panel, releaseOld: oldRead.resolve, releaseLate: lateRead.resolve, releaseMaterialize: materialize.resolve,
    cleanup: () => { oldRead.resolve(); lateRead.resolve(); materialize.resolve() } }
}

test('manual refresh replaces the stale summary and enables a verified hotel selection', async ({ page }) => {
  const f = await fixture(page)
  await f.panel.getByTestId('retry-stay').click()
  await expect(f.panel.getByRole('heading', { name: HOTEL })).toBeVisible()
  await expect(f.panel).not.toContainText(OLD_MESSAGE)
  await expect(f.panel).toContainText(READY_MESSAGE)
  await expect(f.panel.getByTestId('choose-stay')).toBeEnabled()
  await f.panel.getByTestId('choose-stay').click()
  await expect.poll(() => f.state.selections.length).toBe(1)
  expect(f.state.selections[0].candidate_token).toBe('synthetic-current-stay-candidate')
  expect(f.state.stayPosts).toHaveLength(1)
  expect(f.state.stayPosts[0]['if-match']).toBe('"fixture-2"')
  expect(f.state.mapPosts).toBe(0)
  expect(f.state.commands).toBe(0)
})

test('authoritative PREPARING summary permits the later LIMITED polling result', async ({ page }) => {
  const f = await fixture(page, { preparing: true })
  await f.panel.getByTestId('retry-stay').click()
  await expect.poll(() => f.state.afterPostReads).toBe(1)
  await expect(f.panel.getByTestId('retry-stay')).toHaveText('正在准备住宿…')
  await expect(f.panel.getByTestId('retry-stay')).toBeDisabled()
  f.state.complete = true
  await expect(f.panel.getByTestId('choose-stay')).toBeEnabled({ timeout: 10000 })
  await expect(f.panel).toContainText(READY_MESSAGE)
  await expect(f.panel).not.toContainText(OLD_MESSAGE)
  expect(f.state.mapPosts).toBe(0)
})

test('a pre-refresh result read arriving last cannot restore the old same-version summary', async ({ page }) => {
  const f = await fixture(page, { oldRead: true })
  try {
    await expect.poll(() => f.state.oldWaiting).toBe(true)
    await f.panel.getByTestId('retry-stay').click()
    await expect(f.panel.getByTestId('choose-stay')).toBeEnabled()
    const oldResponse = page.waitForResponse(response => new URL(response.url()).pathname === `${PREFIX}/result`)
    f.releaseOld()
    await (await oldResponse).finished()
    await expect.poll(() => f.state.oldDelivered).toBe(true)
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    await expect.poll(() => f.state.afterPostReads).toBe(1)
    await expect(f.panel).toContainText(READY_MESSAGE)
    await expect(f.panel).not.toContainText(OLD_MESSAGE)
    await expect(f.panel.getByTestId('choose-stay')).toBeEnabled()
    expect(f.state.mapPosts).toBe(0)
  } finally { f.cleanup() }
})

test('a delayed old-ETag result cannot unlock current-version candidates', async ({ page }) => {
  const f = await fixture(page, { lateResult: true, responseVersion: 1 })
  try {
    await f.panel.getByTestId('retry-stay').click()
    await expect.poll(() => f.state.lateWaiting).toBe(true)
    f.releaseLate()
    await expect(page.getByText('住宿建议更新尚未确认，可稍后再试。', { exact: true })).toBeVisible()
    await expect(f.panel).toContainText(OLD_MESSAGE)
    await expect(f.panel.getByTestId('choose-stay')).toBeDisabled()
    expect(await page.evaluate(() => sessionStorage.getItem('bt_active_trip_etag'))).toBe('"fixture-2"')
    expect(f.state.selections).toHaveLength(0)
  } finally { f.cleanup() }
})

test('an authoritative summary read failure preserves stale-result protection', async ({ page }) => {
  const f = await fixture(page, { readFailure: true })
  await f.panel.getByTestId('retry-stay').click()
  await expect(page.getByText('住宿建议更新尚未确认，可稍后再试。', { exact: true })).toBeVisible()
  await expect(f.panel).toContainText(OLD_MESSAGE)
  await expect(f.panel.getByTestId('choose-stay')).toBeDisabled()
  await expect(f.panel.getByTestId('retry-stay')).toBeEnabled()
  expect(f.state.selections).toHaveLength(0)
})

test('a version change during summary read clears details and rejects the late old result', async ({ page }) => {
  const f = await fixture(page, { changeVersion: true, lateResult: true })
  try {
    await expect.poll(() => f.state.materializeWaiting).toBe(true)
    await f.panel.getByTestId('retry-stay').click()
    await expect.poll(() => f.state.lateWaiting).toBe(true)
    f.releaseMaterialize()
    await expect.poll(() => page.evaluate(() => sessionStorage.getItem('bt_active_trip_etag'))).toBe('"fixture-3"')
    f.releaseLate()
    await expect(f.panel.getByTestId('retry-stay')).toBeEnabled()
    await expect(f.panel.getByTestId('choose-stay')).toHaveCount(0)
    await expect(f.panel).not.toContainText(HOTEL)
    await expect(f.panel).toContainText(OLD_MESSAGE)
    expect(await page.evaluate(() => sessionStorage.getItem('bt_active_trip_etag'))).toBe('"fixture-3"')
    expect(f.state.mapPosts).toBe(0)
  } finally { f.cleanup() }
})
