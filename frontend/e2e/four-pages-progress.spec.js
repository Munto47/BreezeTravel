const { test, expect } = require('@playwright/test')

// Fixed public API responses exercise the real fetch/SSE parser, hook and page.
// They do not represent new model, map, worker or account integration evidence.
const resource = 'four-pages-progress-fixed-20260913'
const etag = 'tu3_four_pages_stopped_partial'
const card = (token, name, status, city) => ({ activity_token: token, name, status, city,
  category: '景点', area_or_address: city, time_hint: null,
  available_actions: ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE'] })
const partial = {
  status: 'PARTIAL_RESULT', ownership: 'ANONYMOUS', is_demo: false,
  coverage: { recognized_place_count: 2, confirmed_place_count: 1, unresolved_place_count: 1,
    unclassified_mention_count: 0, unprocessed_count: 2, complete: false },
  assumptions: [{ key: 'destination', label: '目的地', value: '北京、上海', editable: true },
    { key: 'calendar', label: '日序', value: 'Day 1–2', editable: true },
    { key: 'party_size', label: '人数', value: '2 人', editable: true }],
  days: [
    { label: 'Day 1', activities: [card('fixed-ready', '故宫博物院', 'READY', '北京'),
      card('fixed-pending', '天坛公园', 'NEEDS_CONFIRMATION', '北京')], alternatives: [], unprocessed_count: 0 },
    { label: 'Day 2', activities: [], alternatives: [{ activity_token: 'fixed-alternative', name: '上海博物馆东馆',
      city: '上海', category: '景点', status: 'NEEDS_CONFIRMATION', available_actions: ['ADD_TO_DAY'] }], unprocessed_count: 2 },
  ],
  map: { status: 'UNAVAILABLE', message: '路线尚未准备。', days: [], available_actions: ['RENDER_MAP'] },
  stay: { status: 'UNAVAILABLE', message: '住宿待选择', area_summary: null, searched_scopes: [], candidates: [], available_actions: [] },
  available_actions: ['EDIT_ASSUMPTIONS', 'EDIT_CARDS'],
}

function progress(phase = 'RECEIVED', cursor = 0, checked = 0) {
  const snapshot = phase === 'RECEIVED' ? null : partial
  return { status: 'PROCESSING', message: '固定事件：整理仍在进行。', phase, event_cursor: cursor, retry_after_ms: 500,
    progress: { day_count: snapshot ? 2 : 0, card_count: snapshot ? 2 : 0, places_checked: checked, places_total: snapshot ? 2 : 0 }, snapshot }
}

async function install(page, { cancelOutcome = 'STOPPED_EMPTY', cancelFailures = 0 } = {}) {
  const calls = { results: 0, events: [], cancels: [], other: [], external: [] }
  let current = progress()
  let cancelled = false
  let waiting = null
  let finished = false
  await page.addInitScript((ref) => {
    sessionStorage.setItem('bt_active_trip_ref', ref)
    sessionStorage.setItem('bt_active_trip_mode', 'FULL')
  }, resource)
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) {
      calls.external.push(url.origin)
      return route.abort()
    }
    return route.continue()
  })
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (!pathname.startsWith(`/api/v3/trip-understandings/${resource}/`)) {
      calls.other.push(pathname)
      return route.fulfill({ status: 404, json: { detail: { code: 'FIXED_TEST_ONLY' } } })
    }
    if (pathname.endsWith('/result')) {
      calls.results++
      return cancelled && cancelOutcome === 'STOPPED_WITH_DRAFT'
        ? route.fulfill({ status: 200, headers: { ETag: etag }, json: partial })
        : route.fulfill({ status: 202, json: current })
    }
    if (pathname.endsWith('/events')) {
      calls.events.push(request.headers())
      const body = finished ? '' : await new Promise((resolve) => { waiting = resolve })
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body }).catch(() => undefined)
    }
    if (pathname.endsWith('/cancel')) {
      calls.cancels.push(request.headers())
      if (calls.cancels.length <= cancelFailures)
        return route.fulfill({ status: 503, json: { detail: { code: 'SERVICE_UNAVAILABLE' } } })
      cancelled = true
      return route.fulfill({ status: 200, headers: { ETag: etag }, json: { status: cancelOutcome,
        message: '固定停止结果。', has_editable_result: cancelOutcome === 'STOPPED_WITH_DRAFT' } })
    }
    if (pathname.endsWith('/map-renders/latest')) return route.fulfill({ json: partial.map })
    if (pathname.endsWith('/stay-suggestions')) return route.fulfill({ json: partial.stay })
    if (pathname.endsWith('/supplementary')) return route.fulfill({ json: { status: 'UNAVAILABLE', days: [] } })
    if (pathname.endsWith('/materialize')) return route.fulfill({ headers: { ETag: etag }, json: {
      status: 'READY', message: '固定检查入口', calendar: 'Day 1–2', party_size: 2, checks_available: true } })
    if (pathname.endsWith('/checks')) return route.fulfill({ json: { status: 'STILL_NEEDS_CONFIRMATION',
      message: '仍有内容需要确认。', items: [], remaining_must_adjust: 0, available_actions: [] } })
    calls.other.push(pathname)
    return route.fulfill({ status: 404, json: { detail: { code: 'FIXED_TEST_ONLY' } } })
  })
  return { calls,
    async emit(phase, cursor, checked) {
      await expect.poll(() => Boolean(waiting)).toBe(true)
      current = progress(phase, cursor, checked)
      const resolve = waiting
      waiting = null
      resolve(`id: ${cursor}\nevent: progress\ndata: ${JSON.stringify(current)}\n\n`)
      await expect.poll(() => calls.events.length).toBeGreaterThan(cursor)
    },
    close() { finished = true; waiting?.(''); waiting = null },
  }
}

for (const width of [1440, 390]) {
  test(`202 and SSE keep truthful phases and two-day confirmed/pending preview at ${width}`, { tag: width === 390 ? '@small-screen' : '@desktop' }, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 })
    const api = await install(page)
    try {
      await page.goto('/trip/result')
      const workspace = page.getByTestId('generation-workspace')
      await expect(workspace).toBeVisible()
      await expect(workspace.getByRole('heading', { name: '正在阅读你的旅行安排' })).toBeVisible()
      await expect(workspace.getByRole('progressbar')).toHaveCount(0)
      await expect(workspace).not.toContainText(/\d+\s*%|预计|剩余\s*\d+|\d+\s*(秒|分钟)/)
      await page.waitForTimeout(2800)
      await expect(workspace.locator('[data-generation-token]')).toHaveCount(0)
      await api.emit('CARDS_AVAILABLE', 1, 0)
      await expect(workspace.locator('[data-generation-token]')).toHaveCount(2)
      await expect(workspace.locator('[data-generation-token="fixed-ready"]')).toContainText('已确认')
      await expect(workspace.locator('[data-generation-token="fixed-pending"]')).toContainText('需要确认')
      await expect(workspace.locator('[data-generation-token]').first()).toContainText('故宫博物院')
      await expect(workspace.locator('[data-generation-token]').nth(1)).toContainText('天坛公园')
      await api.emit('CHECKING_PLACES', 2, 1)
      await expect(workspace).toContainText('2 已识别主线地点')
      const day2 = workspace.locator('.live-day').filter({ has: page.getByRole('heading', { name: 'Day 2', exact: true }) })
      await expect(day2).toContainText('上海博物馆东馆')
      await expect(day2.locator('header')).toContainText('上海')
      await expect(day2).toContainText('还有 2 项原文尚未整理')
      await expect(day2).toContainText('这一天的主线地点仍在整理')
      await expect(day2.locator('[data-generation-token]')).toHaveCount(0)
      await expect(workspace.getByRole('button', { name: /编辑|^加入|拖动|选择地点|更新路线/ })).toHaveCount(0)
      await expect(page.getByTestId('itinerary-workspace')).toHaveCount(0)
      await expect(workspace).toContainText('交通路线尚未完成检查')
      await expect(workspace.getByRole('link', { name: '后台继续' })).toHaveAttribute('href', '/my-trips')
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: testInfo.outputPath(`checking-${width}.png`), fullPage: true })
      // Keep the second event stream open: a UI animation is not a completion event.
      await page.waitForTimeout(2800)
      await expect(workspace).toBeVisible()
      await expect(workspace.locator('[data-generation-token]')).toHaveCount(2)
      expect(api.calls.results).toBeGreaterThanOrEqual(3)
      expect(api.calls.events[1]['last-event-id']).toBe('1')
      expect(api.calls.events[2]['last-event-id']).toBe('2')
      expect(api.calls.cancels).toHaveLength(0)
      expect(api.calls.external).toEqual([])
    } finally { api.close() }
  })
}

for (const width of [1440, 390]) for (const outcome of ['STOPPED_EMPTY', 'STOPPED_WITH_DRAFT']) {
  test(`cancel failure is retryable and ${outcome} stops the progress workspace at ${width}`, { tag: width === 390 ? '@small-screen' : '@desktop' }, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 })
    const api = await install(page, { cancelOutcome: outcome, cancelFailures: 1 })
    try {
      await page.goto('/trip/result')
      const workspace = page.getByTestId('generation-workspace')
      await expect(workspace).toBeVisible()
      if (outcome === 'STOPPED_WITH_DRAFT') await api.emit('CHECKING_PLACES', 1, 1)
      await workspace.getByRole('button', { name: '停止整理', exact: true }).click()
      await expect(workspace.getByRole('alert')).toHaveText('尚未确认是否已经停止，系统不会重复创建行程，请稍后重试。')
      await expect(workspace.getByRole('button', { name: '停止整理', exact: true })).toBeEnabled()
      await page.screenshot({ path: testInfo.outputPath(`cancel-retry-${outcome}-${width}.png`), fullPage: true })
      await workspace.getByRole('button', { name: '停止整理', exact: true }).click()
      await expect(workspace).toHaveCount(0)
      await expect(page.getByTestId('generation-stages')).toHaveCount(0)
      if (outcome === 'STOPPED_EMPTY') {
        await expect(page.getByRole('heading', { name: '整理已经停止' })).toBeVisible()
        await expect(page.getByTestId('itinerary-workspace')).toHaveCount(0)
      } else {
        await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
        await expect(page.getByText('已停止继续核对地点，当前卡片可以编辑。')).toBeVisible()
        await expect(page.getByRole('button', { name: '拖动 故宫博物院', exact: true })).toBeEnabled()
        await expect(page.getByRole('button', { name: '拖动 天坛公园', exact: true })).toHaveCount(0)
      }
      expect(api.calls.cancels).toHaveLength(2)
      expect(api.calls.cancels[0]['idempotency-key']).toBeTruthy()
      expect(api.calls.cancels[1]['idempotency-key']).toBe(api.calls.cancels[0]['idempotency-key'])
      expect(api.calls.cancels[1]['if-match']).toBeUndefined()
      expect(api.calls.external).toEqual([])
      await page.screenshot({ path: testInfo.outputPath(`cancelled-${outcome}-${width}.png`), fullPage: true })
    } finally { api.close() }
  })
}
