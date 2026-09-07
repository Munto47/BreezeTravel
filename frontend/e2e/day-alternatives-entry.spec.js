const { test, expect } = require('@playwright/test')

// Synthetic, cookie-authorized browser fixtures. No model, POI or route calls.
const RESOURCE = 'day-options-synthetic-trip-00001'
const COOKIE = 'day-options-synthetic-owner'
const BASE = `/api/v3/trip-understandings/${RESOURCE}`

function card(name, token, status = 'READY') {
  return { name, activity_token: token, city: '北京', category: '景点',
    status, area_or_address: '北京市合成地址', time_hint: null,
    available_actions: ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE'], knowledge_suggestions: [] }
}

function fixtureResult() {
  return { status: 'PARTIAL_RESULT', ownership: 'ANONYMOUS', can_undo: false, is_demo: false,
    expires_at: '2099-09-09T12:00:00Z', updated_at: '2026-09-06T12:00:00Z',
    assumptions: [{ key: 'destination', label: '目的地', value: '北京', editable: true },
      { key: 'calendar', label: '日期', value: 'Day 1、Day 2、Day 3', editable: true },
      { key: 'party_size', label: '人数', value: '2 人', editable: true }],
    days: [
      { label: 'Day 1', activities: [card('青石公园', 'day-options-first-card-token')], alternatives: ['雨松书院', '青石公园'].map(name => ({ name, category: '景点', city: '北京' })) },
      { label: 'Day 2', activities: [card('山月美术馆', 'day-options-second-card-token')], alternatives: [] },
      { label: 'Day 3', activities: [], alternatives: ['星河公园', '青溪古镇', '云汀美术馆'].map(name => ({ name, category: '景点', city: '北京' })) },
    ],
    map: { status: 'UNAVAILABLE', message: '路线尚待确认。', available_actions: ['RENDER_MAP'] },
    stay: { status: 'UNAVAILABLE', message: '住宿待选择。', area_summary: null, searched_scopes: [], candidates: [], available_actions: [] },
    available_actions: ['EDIT_ASSUMPTIONS', 'EDIT_CARDS'] }
}

async function setup(page, baseURL) {
  const state = { result: fixtureResult(), etag: '"day-options-initial-validator"',
    posts: [], commands: [], checksReads: 0, unsafeRequests: [], denied: 0 }
  await page.context().addCookies([{ name: 'bt_demo_capability', value: COOKIE,
    domain: new URL(baseURL).hostname, path: '/api/v3/trip-understandings', httpOnly: true, sameSite: 'Lax' }])
  await page.addInitScript(resource => {
    sessionStorage.setItem('bt_active_trip_ref', resource)
    sessionStorage.setItem('bt_active_trip_mode', 'FULL')
  }, RESOURCE)
  await page.route('**/restapi.amap.com/**', route => { state.unsafeRequests.push('EXTERNAL_POI'); return route.abort('blockedbyclient') })
  await page.route('**/api/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const headers = await request.allHeaders()
    if (path === '/api/user/me') return route.fulfill({ status: 401, json: {} })
    const reply = (body, status = 200) => route.fulfill({ status, headers: { ETag: state.etag, 'Cache-Control': 'no-store' }, json: body })
    if (!path.startsWith(`${BASE}/`) || !(headers.cookie || '').split(';').some(value => value.trim() === `bt_demo_capability=${COOKIE}`)) {
      state.denied++
      return reply({}, 404)
    }
    const action = path.slice(BASE.length)
    if (request.method() === 'POST') state.posts.push(action)
    if (action === '/result') return reply(state.result)
    if (action === '/map-renders/latest') return reply({ ...state.result.map, points: [], days: [] })
    if (action === '/stay-suggestions') return reply(state.result.stay)
    if (action === '/supplementary') return reply({ status: 'AVAILABLE', days: [] })
    if (action === '/materialize') {
      if (!headers['idempotency-key'] || headers['if-match'] !== state.etag) return reply({}, 409)
      return reply({ status: 'READY', message: '行程已准备好。', calendar: '按Day编号', party_size: 2, checks_available: true })
    }
    if (action === '/checks') {
      state.checksReads++
      return reply({ status: 'STILL_NEEDS_CONFIRMATION', message: '未选择的方案需要确认。',
        items: [1, 2, 3].map(index => ({ check_token: `day-options-check-token-${index}`, label: '需要确认',
          title: `核对第${index}天安排`, message: '未确定的地点与时间暂不作为已核验事实。',
          affected_days: [`Day ${index}`], can_preview: false })), remaining_must_adjust: 0, available_actions: [] })
    }
    if (action === '/commands') {
      const body = request.postDataJSON()
      state.commands.push({ body, key: headers['idempotency-key'], etag: headers['if-match'] })
      if (!headers['idempotency-key'] || headers['if-match'] !== state.etag || body.command_type !== 'ACTIVITY_INSERT') return reply({}, 409)
      const day = state.result.days[body.day_index - 1]
      if (!day || !day.alternatives.some(item => item.name === body.name && item.category === body.category) || body.position !== day.activities.length) return reply({}, 409)
      day.activities.push(card(body.name, `day-options-inserted-card-${state.commands.length}`, 'NEEDS_CONFIRMATION'))
      state.result.map = { status: 'NEEDS_UPDATE', message: '行程已调整，请手动更新路线。', available_actions: ['RENDER_MAP'] }
      state.result.stay = { ...state.result.stay, status: 'NEEDS_UPDATE' }
      state.result.can_undo = true
      state.etag = '"day-options-updated-validator"'
      return reply({ status: 'APPLIED', changed_days: [day.label], map_readiness: 'NEEDS_UPDATE' })
    }
    state.unsafeRequests.push(action)
    return reply({}, 404)
  })
  await page.goto(`/trip/result#trip=${RESOURCE}`)
  await expect(page.getByTestId('day-lane-3')).toBeVisible()
  await expect.poll(() => state.checksReads).toBeGreaterThan(0)
  return state
}

async function openDay(page, day) {
  const trigger = page.getByTestId(`day-alternatives-${day}`)
  await trigger.click()
  const panel = page.getByRole('complementary', { name: '检查与建议', exact: true })
  await expect(panel).toBeVisible()
  await expect(panel.getByLabel('建议所属日期')).toHaveValue(String(day - 1))
  const choices = panel.locator('details').filter({ has: page.locator('summary', { hasText: /^备选地点/ }) })
  await expect(choices).toHaveAttribute('open', '')
  await expect(choices.locator('summary')).toBeInViewport()
  return { trigger, panel, choices }
}

for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
  test(`${viewport.width}px: empty main day has a visible alternatives entry, day switching is read-only and focus returns`, async ({ page, baseURL }, testInfo) => {
    await page.setViewportSize(viewport)
    const state = await setup(page, baseURL)
    await expect(page.getByTestId('day-lane-3')).toContainText('0 个地点')
    await expect(page.getByTestId('day-alternatives-3')).toHaveText('备选 · 3')
    await expect(page.getByTestId('day-alternatives-1')).toHaveText('备选 · 2')
    await expect(page.getByTestId('day-alternatives-2')).toHaveCount(0)
    const before = state.posts.length // Initial materialization is existing result loading, not an entry action.
    const { trigger, panel, choices } = await openDay(page, 3)
    await expect(choices.getByRole('button', { name: '加入待确认', exact: true })).toHaveCount(3)
    await page.screenshot({ path: testInfo.outputPath('empty-day-alternatives.png') })
    await panel.getByLabel('建议所属日期').selectOption('0')
    await expect(choices).toContainText('雨松书院')
    await page.keyboard.press('Escape')
    await expect(panel).toHaveCount(0)
    await expect(trigger).toBeFocused()
    const first = await openDay(page, 1)
    await expect(first.choices).toContainText('雨松书院')
    // Same name/category can describe distinct planned and optional mentions.
    // The API's alternatives remain visible; looking at them does not adopt one.
    await expect(first.choices).toContainText('青石公园')
    await expect(first.choices.getByRole('button', { name: '加入待确认', exact: true })).toHaveCount(2)
    await expect(page.getByTestId('day-lane-1')).toContainText('青石公园')
    expect(state.commands).toEqual([])
    await first.panel.getByRole('button', { name: '关闭建议', exact: true }).click()
    await expect(first.trigger).toBeFocused()
    await expect(first.panel).toHaveCount(0)
    const toolbarTrigger = page.getByTestId('journey-suggestions-toggle')
    await toolbarTrigger.click()
    await expect(first.panel).toBeVisible()
    await expect(first.choices).not.toHaveAttribute('open', '')
    expect(await first.panel.evaluate(element => element.scrollTop)).toBe(0)
    await page.keyboard.press('Escape')
    await expect(first.panel).toHaveCount(0)
    await expect(toolbarTrigger).toBeFocused()
    expect(state.posts.length).toBe(before)
    expect(state.commands).toEqual([])
    expect(state.unsafeRequests).toEqual([])
    expect(state.denied).toBe(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
  })

  test(`${viewport.width}px: explicitly adding one day-three alternative writes once, stays pending and does not search`, async ({ page, baseURL }) => {
    await page.setViewportSize(viewport)
    const state = await setup(page, baseURL)
    const { choices } = await openDay(page, 3)
    await choices.getByRole('button', { name: '加入待确认', exact: true }).first().click()
    await expect(page.getByTestId('day-lane-3')).toContainText('星河公园')
    await expect(page.getByTestId('day-lane-3')).toContainText('待确认')
    // ACTIVITY_INSERT readback still contains all three source alternatives.
    await expect(page.getByTestId('day-alternatives-3')).toHaveText('备选 · 3')
    expect(state.commands).toHaveLength(1)
    expect(state.commands[0].body).toEqual({ command_type: 'ACTIVITY_INSERT', day_index: 3, position: 0,
      name: '星河公园', category: '景点', city: '北京' })
    expect(state.commands[0].key).toBeTruthy()
    expect(state.commands[0].etag).toBe('"day-options-initial-validator"')
    expect(state.posts.filter(action => action === '/commands')).toHaveLength(1)
    expect(state.result.days[0].activities).toHaveLength(1)
    expect(state.result.days[2].activities[0].status).toBe('NEEDS_CONFIRMATION')
    expect(state.unsafeRequests).toEqual([])
    expect(state.denied).toBe(0)
  })
}
