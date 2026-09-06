const { test, expect } = require('@playwright/test')
const fs = require('node:fs')
const { randomUUID } = require('node:crypto')

// Two original synthetic inputs; the accommodation journey is independently
// selectable and reuses Beijing. No mocks, demo mode, extra provider
// retries, account login or collaboration calls. Run only after the integrator
// confirms the final local API/build is ready and uses the existing live binding.
// Resource IDs, ETags and candidate credentials are held in memory only. Reports
// contain counts, public status labels and synthetic city names, never responses.
const API = '/api/v3/trip-understandings'
const BEIJING = '北京两日游。Day 1：故宫博物院 → 景山公园。备选：北海公园（有空再考虑）。Day 2：天坛公园 → 前门大街。'
const CROSS_CITY = '上海和杭州三日游。Day 1 在上海，依次去外滩、豫园。Day 2 留在上海，当天没有确定的活动。备选：武康路（有空再考虑）。Day 3 到杭州，依次去西湖、河坊街。'
const CITY_BOUNDS = {
  北京: [115.4, 117.6, 39.4, 41.1],
  上海: [120.8, 122.1, 30.6, 31.9],
  杭州: [118.3, 120.8, 29.1, 30.7],
}
const states = new WeakMap()

function sameAction(request, action, method = 'POST') {
  return request.method() === method && new URL(request.url()).pathname.endsWith(`/${action}`)
}

test.beforeEach(async ({ page }, testInfo) => {
  const state = {
    resource: null,
    commands: [],
    stayRequests: [],
    summary: {
      scenario: testInfo.title.startsWith('Beijing accommodation') ? 'beijing_accommodation'
        : testInfo.title.startsWith('Beijing') ? 'beijing' : 'shanghai_hangzhou',
      created: 0, diningRequests: 0, mapPosts: 0, placeSearches: 0,
      staySelections: 0,
      commandTypes: [], quotedCommandValidators: [], apiFailureStatuses: [], cleanup: 'NOT_NEEDED',
    },
  }
  states.set(page, state)
  page.on('request', request => {
    if (sameAction(request, 'dining-candidates')) state.summary.diningRequests++
    if (sameAction(request, 'map-renders')) state.summary.mapPosts++
    if (sameAction(request, 'place-candidates')) state.summary.placeSearches++
    if (sameAction(request, 'stay-selection')) {
      state.stayRequests.push(request.postDataJSON())
      state.summary.staySelections++
      state.summary.stayQuotedValidator = /^"[^"\r\n]+"$/.test(request.headers()['if-match'] || '')
      state.summary.stayIdempotencyPresent = Boolean(request.headers()['idempotency-key'])
    }
    if (sameAction(request, 'commands')) {
      const command = request.postDataJSON()
      state.commands.push(command)
      state.summary.commandTypes.push(['DINING_INSERT', 'UNDO', 'ACTIVITY_INSERT'].includes(command?.command_type)
        ? command.command_type : 'OTHER')
      state.summary.quotedCommandValidators.push(/^"[^"\r\n]+"$/.test(request.headers()['if-match'] || ''))
    }
  })
  page.on('response', response => {
    if (new URL(response.url()).pathname.startsWith(API) && response.status() >= 400)
      state.summary.apiFailureStatuses.push(response.status())
  })
})

async function readOwned(page, state, suffix) {
  expect(Boolean(state.resource), '只读取本次成功创建的行程').toBe(true)
  let response
  try {
    response = await page.request.get(`${API}/${encodeURIComponent(state.resource)}/${suffix}`, {
      timeout: 12_000, headers: { 'Cache-Control': 'no-cache' },
    })
  } catch {
    throw new Error('本次合成行程的私有结果读取失败，未记录响应或请求地址')
  }
  expect(response.status(), '私有结果应可由本次浏览器会话读取').toBe(200)
  let body
  try { body = await response.json() } catch { throw new Error('私有结果没有返回有效结构') }
  return { body, etag: response.headers().etag }
}

async function waitOwned(page, state, suffix, predicate, message, timeout = 60_000) {
  let latest
  await expect.poll(async () => {
    latest = await readOwned(page, state, suffix)
    return predicate(latest.body)
  }, { message, timeout, intervals: [800, 1500, 2500] }).toBe(true)
  return latest
}

async function createTrip(page, source) {
  const state = states.get(page)
  await page.goto('/')
  await page.getByTestId('trip-source-text').fill(source)
  const accepted = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === API, { timeout: 30_000 })
  await page.getByTestId('create-full-trip').click()
  const response = await accepted
  let body
  try { body = await response.json() } catch { throw new Error('生成请求没有返回有效结构') }
  // Capture ownership before asserting readiness so failed generation is cleaned up.
  if (response.status() === 202 && typeof body.public_resource_id === 'string') {
    state.resource = body.public_resource_id
    state.summary.created++
  }
  expect(response.status(), '真实生成请求应被接受').toBe(202)
  expect(Boolean(state.resource), '生成请求应返回本次私有行程').toBe(true)
  const workspace = page.getByTestId('itinerary-workspace')
  const failed = page.getByRole('heading', { name: '这次没有整理完成', exact: true })
  await expect(workspace.or(failed)).toBeVisible({ timeout: 120_000 })
  state.summary.generation = await failed.isVisible() ? 'FAILED' : 'READY'
  expect(state.summary.generation, '真实文字生成必须得到可操作卡片').toBe('READY')
  await expect(workspace).toBeVisible()
  await expect(page.getByTestId('drag-handle-1-0')).toBeEnabled()
  const result = await readOwned(page, state, 'result')
  expect(result.body.is_demo, '合成文字应使用真实生成入口').toBe(false)
  expect(/^"[^"\r\n]+"$/.test(result.etag || ''), '使用真实带引号的不透明 ETag').toBe(true)
  expect(result.body.ownership).toBe('ANONYMOUS')
  return result
}

function recordCards(state, result) {
  state.summary.days = result.days.length
  state.summary.cardsPerDay = result.days.map(day => day.activities.length)
  state.summary.alternativesPerDay = result.days.map(day => day.alternatives?.length || 0)
  state.summary.confirmedPerDay = result.days.map(day => day.activities.filter(card => card.status === 'READY').length)
  state.summary.citiesPerDay = result.days.map(day => [...new Set(day.activities.map(card => card.city))])
  state.summary.alternativeCitiesPerDay = result.days.map(day => [...new Set((day.alternatives || []).map(card => card.city))])
}

function assertCards(result, expected) {
  expect(result.days.length, '明确日数不可丢失').toBe(expected.length)
  expected.forEach((day, index) => {
    const actual = result.days[index]
    expect(actual.label).toBe(`Day ${index + 1}`)
    expect(actual.activities.map(card => card.name)).toEqual(day.names.map(name => expect.stringMatching(name)))
    expect(actual.activities.map(card => card.city)).toEqual(day.names.map(() => day.city))
    expect((actual.alternatives || []).map(card => card.name)).toEqual(day.alternatives || [])
    // Unverified alternatives explicitly allow a null city in the public
    // contract. Never require invented place facts or POI lookup just for a
    // nonempty display value; a supplied city must still be the correct one.
    for (const card of actual.alternatives || [])
      expect(card.city === null || card.city === day.city, '备选城市可空，但不可显示错误城市').toBe(true)
  })
}

function assertMapCities(map, result) {
  let confirmedPoints = 0
  const plannedTokens = new Set(result.days.flatMap(day => day.activities.map(card => card.activity_token)))
  expect(map.points.every(point => plannedTokens.has(point.activity_token)), '地图不能自动加入备选地点').toBe(true)
  for (const day of result.days) for (const card of day.activities) {
    const point = map.points.find(item => item.activity_token === card.activity_token)
    if (card.status !== 'READY') {
      expect(Boolean(point?.position), '待确认卡片不可冒充已确认地图地点').toBe(false)
      continue
    }
    expect(Boolean(point?.position), '每个已确认卡片应有真实地图坐标').toBe(true)
    const [west, east, south, north] = CITY_BOUNDS[card.city]
    const { longitude, latitude } = point.position
    expect(longitude >= west && longitude <= east && latitude >= south && latitude <= north,
      '已确认地点必须位于卡片所属城市').toBe(true)
    expect(point.day_label).toBe(day.label)
    confirmedPoints++
  }
  return confirmedPoints
}

async function suggestions(page) {
  const toggle = page.getByTestId('journey-suggestions-toggle')
  if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click()
  const panel = page.getByRole('complementary', { name: '检查与建议', exact: true })
  await expect(panel).toBeVisible()
  return panel
}

async function expandSection(page, panel, name) {
  const section = panel.locator('details').filter({ has: page.locator('summary').filter({ hasText: name }) })
  if (await section.getAttribute('open') === null) await section.locator('summary').click()
  return section
}

async function screenshot(page, testInfo, filename) {
  await page.screenshot({ path: testInfo.outputPath(filename), fullPage: true, animations: 'disabled' })
}

test.afterEach(async ({ page, baseURL }, testInfo) => {
  const state = states.get(page)
  if (!state) return
  let cleanupError = false
  if (state.resource) {
    try {
      // Context request shares only the cookie established by this test's real UI.
      // Never enumerate, restore, delete or authorize any pre-existing trip.
      const response = await page.request.delete(`${API}/${encodeURIComponent(state.resource)}`, {
        timeout: 12_000,
        headers: { 'Idempotency-Key': randomUUID(), Origin: new URL(baseURL).origin },
      })
      const readback = await page.request.get(`${API}/${encodeURIComponent(state.resource)}/result`, { timeout: 12_000 })
      state.summary.cleanup = response.status() === 204 && readback.status() === 410 ? 'DELETED_AND_GONE' : 'FAILED'
      state.summary.cleanupStatuses = [response.status(), readback.status()]
      cleanupError = state.summary.cleanup === 'FAILED'
    } catch {
      state.summary.cleanup = 'FAILED'
      cleanupError = true
    }
  }
  state.summary.outcome = cleanupError ? 'failed' : testInfo.status
  const safe = JSON.stringify(state.summary, null, 2)
  fs.writeFileSync(testInfo.outputPath('summary.json'), safe)
  await testInfo.attach('sanitized-live-observations', { body: safe, contentType: 'application/json' })
  expect(cleanupError, '本次合成行程必须实际删除并确认不可回读').toBe(false)
})

test('Beijing: real dining adoption, undo, checks, accommodation and one manual map update', async ({ page }, testInfo) => {
  const state = states.get(page)
  const initial = await createTrip(page, BEIJING)
  recordCards(state, initial.body)
  assertCards(initial.body, [
    { city: '北京', names: [/^故宫博物院(?:\(.*\))?$/, /^景山公园$/], alternatives: ['北海公园'] },
    { city: '北京', names: [/^天坛(?:公园)?$/, /^前门大街$/] },
  ])
  expect(initial.body.days.flatMap(day => day.activities).every(card => card.status === 'READY')).toBe(true)
  await expect(page.getByTestId('day-lane-1').getByTestId('activity-card')).toHaveCount(2)
  await expect(page.getByTestId('day-lane-2').getByTestId('activity-card')).toHaveCount(2)
  expect(state.summary.diningRequests).toBe(0)
  await screenshot(page, testInfo, 'beijing-cards.png')

  const map = await waitOwned(page, state, 'map-renders/latest', body => body.status !== 'PREPARING', '首次真实地图应结束准备')
  state.summary.initialMapStatus = map.body.status
  state.summary.confirmedMapPoints = assertMapCities(map.body, initial.body)

  let panel = await suggestions(page)
  await expect(panel).toContainText('营业、预约与天气尚未核验')
  expect(state.summary.diningRequests, '展开检查不能自动查询餐饮').toBe(0)
  const stays = await waitOwned(page, state, 'stay-suggestions', body => body.status !== 'PREPARING',
    '住宿必须给出真实候选或明确受限终态', 90_000)
  state.summary.stayStatus = stays.body.status
  state.summary.stayCandidates = stays.body.candidates.length
  state.summary.stayUnknownMinutes = stays.body.candidates.filter(candidate => candidate.max_single_leg_minutes === null).length
  state.summary.stayReported120Count = stays.body.candidates.filter(candidate => candidate.max_single_leg_minutes === 120).length
  const staySection = await expandSection(page, panel, /^住宿$/)
  // Availability and limitations are separate outcomes, never a fabricated count.
  if (stays.body.status === 'AVAILABLE') {
    expect(stays.body.candidates.length, '完整住宿建议应展示三家真实候选').toBe(3)
    await expect(staySection.getByRole('heading', { level: 3 })).toHaveCount(3)
  } else {
    expect(['LIMITED', 'UNAVAILABLE'].includes(stays.body.status)).toBe(true)
    await expect(staySection).toContainText(/暂|缺|待|未|部分|稍后/)
  }
  for (const candidate of stays.body.candidates) {
    expect(candidate.category).toBe('住宿')
    if (candidate.max_single_leg_minutes === null)
      expect(candidate.commute_summary, '无通勤事实时不能出现评分惩罚分钟').not.toMatch(/\d+\s*分钟/)
    if (/尚未得到有效核对/.test(candidate.commute_summary)) expect(candidate.max_single_leg_minutes).toBeNull()
  }
  await screenshot(page, testInfo, 'beijing-checks-stays.png')

  const dining = await expandSection(page, panel, /^附近餐饮$/)
  await dining.getByLabel('餐饮附近地点').selectOption('1')
  const diningResponse = page.waitForResponse(response => sameAction(response.request(), 'dining-candidates'))
  await dining.getByRole('button', { name: '找附近餐饮', exact: true }).click()
  const response = await diningResponse
  expect(response.status(), '真实餐饮查询应成功响应').toBe(200)
  let candidates
  try { candidates = await response.json() } catch { throw new Error('餐饮查询没有返回有效结构') }
  state.summary.diningStatus = candidates.status
  state.summary.diningCandidates = candidates.candidates?.length || 0
  expect(candidates.status, '诚实受限不能替代餐饮真实成功证据').toBe('AVAILABLE')
  expect(candidates.candidates.length).toBeGreaterThan(0)
  expect(candidates.candidates.length).toBeLessThanOrEqual(3)
  expect(response.headers().etag === initial.etag, '候选应绑定当前带引号 ETag').toBe(true)
  expect(response.request().postDataJSON().activity_token === initial.body.days[0].activities[1].activity_token,
    '真实查询应使用明确选择的当天锚点').toBe(true)
  expect(state.summary.diningRequests).toBe(1)
  const chosen = candidates.candidates[0]
  expect(chosen.category).toBe('餐饮')
  const [west, east, south, north] = CITY_BOUNDS.北京
  expect(chosen.position.longitude >= west && chosen.position.longitude <= east
    && chosen.position.latitude >= south && chosen.position.latitude <= north).toBe(true)
  await expect(dining.getByRole('button', { name: '加入行程', exact: true })).toHaveCount(candidates.candidates.length)
  await screenshot(page, testInfo, 'beijing-real-dining.png')

  const appliedResponse = page.waitForResponse(response => sameAction(response.request(), 'commands'))
  await dining.getByRole('button', { name: '加入行程', exact: true }).first().click()
  expect((await appliedResponse).status(), '真实餐饮应原子采纳一次').toBe(200)
  await expect(page.getByTestId('day-lane-1').getByTestId('activity-card')).toHaveCount(3)
  const inserted = await readOwned(page, state, 'result')
  const added = inserted.body.days[0].activities[2]
  expect(added.name).toBe(chosen.name)
  expect(added.category).toBe('餐饮')
  expect(added.city).toBe('北京')
  expect(added.status).toBe('READY')
  expect(inserted.etag !== initial.etag).toBe(true)
  expect(state.summary.commandTypes).toEqual(['DINING_INSERT'])
  expect(state.commands[0].after_activity_token === initial.body.days[0].activities[1].activity_token).toBe(true)
  expect(inserted.body.map.status).toBe('NEEDS_UPDATE')
  expect(state.summary.mapPosts, '采纳餐饮不能自动重算路线').toBe(0)
  state.summary.diningAppliedAndReadBack = true
  await screenshot(page, testInfo, 'beijing-dining-added.png')

  if (await page.getByTestId('journey-suggestions-toggle').getAttribute('aria-expanded') === 'true') await page.keyboard.press('Escape')
  const undoResponse = page.waitForResponse(response => sameAction(response.request(), 'commands'))
  await page.getByTestId('result-action-bar').getByRole('button', { name: '撤销', exact: true }).click()
  expect((await undoResponse).status()).toBe(200)
  await expect(page.getByTestId('day-lane-1').getByTestId('activity-card')).toHaveCount(2)
  const undone = await readOwned(page, state, 'result')
  expect(undone.body.days.map(day => day.activities.map(card => card.name)))
    .toEqual(initial.body.days.map(day => day.activities.map(card => card.name)))
  expect(undone.etag !== inserted.etag && undone.etag !== initial.etag).toBe(true)
  expect(state.summary.commandTypes).toEqual(['DINING_INSERT', 'UNDO'])
  expect(state.summary.quotedCommandValidators).toEqual([true, true])
  expect(state.summary.mapPosts, '撤销也不能自动重算路线').toBe(0)
  state.summary.undoReadBack = true

  await page.getByTestId('desktop-nav-map_stay').click()
  await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status', 'NEEDS_UPDATE')
  const rendered = page.waitForResponse(response => sameAction(response.request(), 'map-renders'))
  await page.getByTestId('render-map').click()
  expect([200, 202].includes((await rendered).status())).toBe(true)
  const refreshed = await waitOwned(page, state, 'map-renders/latest', body => body.status !== 'PREPARING', '手动地图更新应有真实终态')
  state.summary.finalMapStatus = refreshed.body.status
  state.summary.availableRouteEdges = refreshed.body.days.flatMap(day => day.routes)
    .filter(edge => edge.walking.status === 'AVAILABLE' || edge.transit.status === 'AVAILABLE').length
  expect(['AVAILABLE', 'LIMITED'].includes(refreshed.body.status), '真实地图不可用不能算更新成功').toBe(true)
  expect(state.summary.availableRouteEdges).toBe(2)
  assertMapCities(refreshed.body, undone.body)
  await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status', /^(AVAILABLE|LIMITED)$/)
  expect(state.summary.mapPosts, '只由最后明确点击发送一次路线请求').toBe(1)
  await screenshot(page, testInfo, 'beijing-updated-map.png')

  await page.getByTestId('desktop-nav-itinerary').click()
  await page.getByTestId('export-itinerary-png').click()
  await expect(page.getByTestId('png-preview')).toBeVisible()
  const downloadPromise = page.waitForEvent('download')
  await page.getByTestId('download-itinerary-png').click()
  const download = await downloadPromise
  const exported = testInfo.outputPath('beijing-complete-itinerary.png')
  await download.saveAs(exported)
  const bytes = fs.readFileSync(exported)
  expect(bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))).toBe(true)
  expect(bytes.length).toBeGreaterThan(1000)
  state.summary.pngExported = true
  state.summary.pngBytes = bytes.length
})

test('Shanghai and Hangzhou: distinct cities, preserved empty day and optional place on mobile', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const state = states.get(page)
  const initial = await createTrip(page, CROSS_CITY)
  recordCards(state, initial.body)
  assertCards(initial.body, [
    { city: '上海', names: [/^(?:上海)?外滩(?:风景区)?$/, /^(?:上海)?豫园$/] },
    { city: '上海', names: [], alternatives: ['武康路'] },
    { city: '杭州', names: [/^(?:杭州)?西湖(?:风景名胜区|风景区)?$/, /^河坊街(?:景区)?$/] },
  ])
  expect(state.summary.confirmedPerDay[0], '上海应有真实确认地点').toBeGreaterThan(0)
  expect(state.summary.confirmedPerDay[2], '杭州应有真实确认地点').toBeGreaterThan(0)
  for (const [index, count] of [2, 0, 2].entries())
    await expect(page.getByTestId(`day-lane-${index + 1}`).getByTestId('activity-card')).toHaveCount(count)
  const map = await waitOwned(page, state, 'map-renders/latest', body => body.status !== 'PREPARING', '跨城地图应结束准备')
  state.summary.mapStatus = map.body.status
  state.summary.confirmedMapPoints = assertMapCities(map.body, initial.body)
  await screenshot(page, testInfo, 'cross-city-mobile-cards.png')

  const panel = await suggestions(page)
  await expect(panel).toContainText('营业、预约与天气尚未核验')
  await panel.getByLabel('建议所属日期').selectOption('1')
  const alternatives = await expandSection(page, panel, /^备选地点/)
  await expect(alternatives).toContainText('武康路')
  if (initial.body.days[1].alternatives[0].city !== null)
    await expect(alternatives).toContainText('武康路 · 上海')
  await expect(alternatives.getByRole('button', { name: '加入待确认', exact: true })).toBeEnabled()
  const dining = await expandSection(page, panel, /^附近餐饮$/)
  await expect(dining).toContainText('先确认当天的一个地点')
  expect(state.summary.diningRequests).toBe(0)
  expect(state.summary.placeSearches).toBe(0)
  expect(state.summary.commandTypes).toEqual([])
  const bounds = await panel.boundingBox()
  expect(bounds.x).toBeGreaterThanOrEqual(0)
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(390)
  expect(bounds.y).toBeGreaterThanOrEqual(0)
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(844)
  await screenshot(page, testInfo, 'cross-city-mobile-optional.png')
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('journey-suggestions-toggle')).toBeFocused()
  await page.getByTestId('mobile-nav-map_stay').click()
  await expect(page.getByTestId('route-map')).toBeVisible()
  await page.getByTestId('map-directory-toggle').click()
  await expect(page.getByTestId('map-place-directory')).toContainText('外滩')
  await expect(page.getByTestId('map-place-directory')).toContainText('西湖')
  await expect(page.getByTestId('map-place-directory')).not.toContainText('武康路')
  expect(state.summary.mapPosts, '跨城查看地图不能额外触发重算').toBe(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  state.summary.mobileChecksAndMapOperable = true
  await screenshot(page, testInfo, 'cross-city-mobile-map.png')
})

test('Beijing accommodation: three real candidates, one selection, persistent overnight anchor and no automatic map request', async ({ page }, testInfo) => {
  const state = states.get(page)
  const initial = await createTrip(page, BEIJING)
  recordCards(state, initial.body)
  assertCards(initial.body, [
    { city: '北京', names: [/^故宫博物院(?:\(.*\))?$/, /^景山公园$/], alternatives: ['北海公园'] },
    { city: '北京', names: [/^天坛(?:公园)?$/, /^前门大街$/] },
  ])
  expect(initial.body.days.flatMap(day => day.activities).every(card => card.status === 'READY')).toBe(true)
  const initialMap = await waitOwned(page, state, 'map-renders/latest', body => body.status !== 'PREPARING',
    '选择住宿前，首次地图应结束准备')
  assertMapCities(initialMap.body, initial.body)
  const stays = await waitOwned(page, state, 'stay-suggestions', body => body.status !== 'PREPARING',
    '真实住宿查询应得到终态', 90_000)
  state.summary.stayStatus = stays.body.status
  state.summary.stayCandidates = stays.body.candidates.length
  state.summary.stayUnknownMinutes = stays.body.candidates.filter(candidate => candidate.max_single_leg_minutes === null).length
  state.summary.stayReported120Count = stays.body.candidates.filter(candidate => candidate.max_single_leg_minutes === 120).length
  expect(['AVAILABLE', 'LIMITED'].includes(stays.body.status), '住宿不可用不能替代真实候选成功证据').toBe(true)
  expect(stays.body.candidates.length, '应给出三家可直接选择的真实住宿').toBe(3)
  for (const candidate of stays.body.candidates) {
    expect(candidate.category).toBe('住宿')
    expect(Boolean(candidate.name && candidate.brand && candidate.area_or_address)).toBe(true)
    expect(candidate.selected).toBe(false)
    expect(candidate.available_actions).toContain('CHOOSE_STAY')
    if (candidate.max_single_leg_minutes === null)
      expect(candidate.commute_summary).not.toMatch(/\d+\s*分钟/)
    if (/尚未得到有效核对/.test(candidate.commute_summary)) expect(candidate.max_single_leg_minutes).toBeNull()
  }

  let panel = await suggestions(page)
  const checkSection = panel.locator('details').filter({ has: page.locator('summary').filter({ hasText: /^行程检查$/ }) })
  if (await checkSection.getAttribute('open') !== null) await checkSection.locator('summary').click()
  let staySection = await expandSection(page, panel, /^住宿$/)
  await expect(staySection.getByRole('heading', { level: 3 })).toHaveCount(3)
  await expect(staySection.getByRole('button', { name: '选择这家住宿', exact: true })).toHaveCount(3)
  await staySection.getByRole('heading', { level: 3 }).first().scrollIntoViewIfNeeded()
  await screenshot(page, testInfo, 'beijing-three-real-stays.png')

  const chosen = stays.body.candidates[0]
  const appliedResponse = page.waitForResponse(response => sameAction(response.request(), 'stay-selection'))
  await staySection.getByRole('button', { name: '选择这家住宿', exact: true }).first().click()
  const response = await appliedResponse
  expect(response.status(), '住宿选择应实际写入成功').toBe(200)
  let applied
  try { applied = await response.json() } catch { throw new Error('住宿选择没有返回有效结构') }
  expect(applied.status).toBe('APPLIED')
  expect(applied.selected_stay).toBe(chosen.name)
  expect(applied.overnight_days).toEqual(['Day 1'])
  expect(applied.map_readiness).toBe('NEEDS_UPDATE')
  expect(state.stayRequests[0].candidate_token === chosen.candidate_token, '只提交用户所选的候选').toBe(true)
  expect(state.summary.staySelections).toBe(1)
  expect(state.summary.stayQuotedValidator).toBe(true)
  expect(state.summary.stayIdempotencyPresent).toBe(true)
  state.summary.overnightDays = applied.overnight_days

  const selected = await waitOwned(page, state, 'result', body => body.stay.candidates.some(candidate => candidate.selected),
    '私有结果必须回读已选择的住宿')
  expect(selected.etag !== initial.etag).toBe(true)
  expect(/^"[^"\r\n]+"$/.test(selected.etag || '')).toBe(true)
  expect(selected.body.days.map(day => day.activities.map(card => card.name)))
    .toEqual(initial.body.days.map(day => day.activities.map(card => card.name)))
  expect(selected.body.stay.candidates.length).toBe(1)
  expect(selected.body.stay.candidates[0].name).toBe(chosen.name)
  expect(selected.body.stay.candidates[0].selected).toBe(true)
  expect(selected.body.stay.candidates[0].reason).toContain('已作为所有过夜日的住宿')
  expect(selected.body.map.status).toBe('NEEDS_UPDATE')
  const readback = await readOwned(page, state, 'stay-suggestions')
  expect(readback.body.candidates.length).toBe(1)
  expect(readback.body.candidates[0].name).toBe(chosen.name)
  expect(readback.body.candidates[0].selected).toBe(true)
  state.summary.stayAppliedAndReadBack = true

  // Public points deliberately exclude hotel identities. Their current plan
  // positions do expose the overnight start anchor: Day 1's cards shift from
  // 0/1 to 1/2, while the non-overnight day remains 0/1. This and the authorized
  // overnight-day receipt verify the public contract without diagnostic APIs.
  const selectedMap = await readOwned(page, state, 'map-renders/latest')
  expect(selectedMap.body.status).toBe('NEEDS_UPDATE')
  assertMapCities(selectedMap.body, selected.body)
  const positions = (map, label) => map.points.filter(point => point.day_label === label)
    .sort((a, b) => a.sequence_index - b.sequence_index).map(point => point.sequence_index)
  expect(positions(initialMap.body, 'Day 1')).toEqual([0, 1])
  expect(positions(selectedMap.body, 'Day 1')).toEqual([1, 2])
  expect(positions(selectedMap.body, 'Day 2')).toEqual([0, 1])
  state.summary.overnightStartAnchorObserved = true
  state.summary.mapPostsAfterSelection = state.summary.mapPosts
  expect(state.summary.mapPosts, '选择住宿不能自动重算路线').toBe(0)

  await page.reload()
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  panel = await suggestions(page)
  staySection = await expandSection(page, panel, /^住宿$/)
  await expect(staySection.getByRole('heading', { level: 3 })).toHaveCount(1)
  await expect(staySection.getByRole('heading', { level: 3 })).toHaveText(chosen.name)
  await expect(staySection.getByRole('button', { name: '已选择', exact: true })).toBeDisabled()
  await staySection.getByRole('heading', { level: 3 }).scrollIntoViewIfNeeded()
  await screenshot(page, testInfo, 'beijing-selected-stay-readback.png')
  await page.keyboard.press('Escape')
  await page.getByTestId('desktop-nav-map_stay').click()
  await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status', 'NEEDS_UPDATE')
  expect(state.summary.staySelections, '刷新只能读取已选结果，不能重复写入').toBe(1)
  expect(state.summary.mapPosts, '重新打开住宿和地图也不能自动重算').toBe(0)
  expect(state.summary.diningRequests).toBe(0)
  expect(state.summary.commandTypes).toEqual([])
  state.summary.selectedStaySurvivedReload = true
  await screenshot(page, testInfo, 'beijing-stay-map-needs-update.png')
})
