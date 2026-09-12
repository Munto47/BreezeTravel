const { test, expect } = require('@playwright/test')
const { execFileSync } = require('node:child_process')
const path = require('node:path')

test.use({ actionTimeout: 10000 })

let replay
test.beforeAll(() => {
  replay = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-X', 'utf8', '-m', 'tests.confirm_source_details_page_replay'], {
    cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8',
    env: { ...process.env, RUNTIME_PROFILE: 'test', PYTHONPATH: '.', PYTHONIOENCODING: 'utf-8' },
  }))
})

async function show(page) {
  const state = { key: 'before', commands: [], searches: [], reads: [], external: [] }
  const tag = () => `"fixed-confirm-details-${state.commands.length}"`
  await page.route('**/*', route => {
    const url = new URL(route.request().url())
    if (!['localhost', '127.0.0.1'].includes(url.hostname)) {
      state.external.push(url.origin)
      return route.abort()
    }
    return route.continue()
  })
  await page.route('**/api/**', route => {
    const request = route.request(), url = new URL(request.url())
    const reply = json => route.fulfill({ json, headers: { ETag: tag() } })
    const result = replay.states[state.key]
    if (url.pathname.endsWith('/commands')) {
      const body = request.postDataJSON()
      const edge = replay.edges.find(item => item.start === state.key && item.command.command_type === body.command_type)
      expect(edge, JSON.stringify({ key: state.key, body })).toBeTruthy()
      expect(body).toEqual(edge.command)
      expect(request.headers()['if-match']).toBe(tag())
      expect(request.headers()['idempotency-key']).toBeTruthy()
      state.commands.push(body)
      state.key = edge.end
      return reply({ status: 'APPLIED', changed_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE' })
    }
    if (url.pathname.endsWith('/result')) { state.reads.push(state.key); return reply(result) }
    if (url.pathname.endsWith('/place-candidates')) {
      state.searches.push(url.search)
      return reply({ status: 'AVAILABLE', candidates: [replay.candidate] })
    }
    if (url.pathname.endsWith('/map-renders/latest')) return reply({ ...result.map, points: [], days: [] })
    if (url.pathname.endsWith('/stay-suggestions')) return reply(result.stay)
    if (url.pathname.endsWith('/daily-dining')) return reply({ status: 'UNAVAILABLE', message: '固定未生成建议', days: [] })
    if (url.pathname.endsWith('/supplementary')) return reply({ status: 'AVAILABLE', days: [] })
    if (url.pathname.endsWith('/materialize')) return reply({ status: 'READY', message: '固定检查入口', calendar: '相对日序', party_size: 2, checks_available: true })
    if (url.pathname.endsWith('/checks')) return reply({ status: 'STILL_NEEDS_CONFIRMATION', message: '请确认剩余地点', items: [], remaining_must_adjust: 0, available_actions: [] })
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/trip/result#trip=fixed-confirm-source-details-20260913')
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  return state
}

async function openPendingParent(page) {
  const panel = page.getByTestId('unresolved-places')
  if (!await panel.evaluate(element => element.open)) await page.getByTestId('unmatched-places-note').click()
  await panel.getByRole('button', { name: '故宫博物院 · 确认地点', exact: true }).click()
  await expect(page.getByTestId('source-internal-details').getByRole('listitem')).toHaveText(['太和殿', '乾清宫'])
}

async function otherDayRemains(page) {
  await expect(page.getByTestId('activity-card').filter({ has: page.getByRole('heading', { name: '外滩', exact: true }) })).toBeVisible()
  await expect(page.getByTestId('day-alternatives-2')).toHaveText('备选 · 1')
  await expect(page.locator('[data-day-heading="1"]')).toHaveText('Day 1')
  await expect(page.locator('[data-day-heading="2"]')).toHaveText('Day 2')
}

for (const width of [1440, 390]) test(`unknown-city parent confirmation keeps source details through refresh and undo at ${width}`, async ({ page }, info) => {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 })
  const state = await show(page)
  expect(replay.evidence.external_http_calls).toBe(0)
  expect(replay.states.before.days[0].activities[0].city).toBeNull()
  await expect(page.getByTestId('activity-card')).toHaveCount(1)
  await otherDayRemains(page)
  await openPendingParent(page)
  await page.screenshot({ path: info.outputPath(`pending-parent-${width}.png`), fullPage: true })
  const search = page.getByTestId('pending-place-dropdown')
  await search.getByRole('combobox', { name: '查询城市' }).selectOption('北京')
  await search.getByRole('button', { name: '搜索', exact: true }).click()
  await search.getByRole('button', { name: /故宫博物院\s+固定候选地址/ }).click()
  await search.getByRole('button', { name: '使用这个地点', exact: true }).click()
  await expect.poll(() => state.key).toBe('confirmed')
  await expect(page.getByTestId('activity-card')).toHaveCount(2)
  const parent = page.getByTestId('activity-card').filter({ has: page.getByRole('heading', { name: '故宫博物院', exact: true }) })
  await expect(parent).toContainText('原文安排 · 2 项')
  await parent.getByRole('button', { name: /故宫博物院.*查看详情/ }).click()
  const details = page.getByTestId('source-internal-details')
  await expect(details.getByRole('listitem')).toHaveText(['太和殿', '乾清宫'])
  await expect(details).toContainText('地点身份与开放情况未单独核验')
  await details.scrollIntoViewIfNeeded()
  await page.screenshot({ path: info.outputPath(`confirmed-parent-${width}.png`), fullPage: true })
  await page.getByRole('button', { name: '收起地点确认', exact: true }).click()
  await page.reload()
  await expect(page.getByTestId('activity-card')).toHaveCount(2)
  await otherDayRemains(page)
  await parent.getByRole('button', { name: /故宫博物院.*查看详情/ }).click()
  await expect(details.getByRole('listitem')).toHaveText(['太和殿', '乾清宫'])
  await page.getByRole('button', { name: '收起地点确认', exact: true }).click()
  await page.getByRole('button', { name: '撤销', exact: true }).click()
  await expect.poll(() => state.key).toBe('undone')
  await expect(page.getByTestId('activity-card')).toHaveCount(1)
  await otherDayRemains(page)
  await openPendingParent(page)
  await expect(page.getByRole('button', { name: '撤销', exact: true })).toBeDisabled()
  await page.screenshot({ path: info.outputPath(`undone-parent-${width}.png`), fullPage: true })
  await page.getByRole('button', { name: '收起地点确认', exact: true }).click()
  await page.getByTestId('day-alternatives-2').click()
  await expect(page.locator('#journey-suggestions')).toContainText('豫园')
  expect(state.commands.map(item => item.command_type)).toEqual(['PLACE_CONFIRM', 'UNDO'])
  expect(state.searches).toHaveLength(1)
  expect(state.reads.filter(key => key === 'confirmed').length).toBeGreaterThanOrEqual(2)
  expect(state.reads).toContain('undone')
  expect(state.external).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await info.attach('actual-python-command-fixture', { body: JSON.stringify(replay), contentType: 'application/json' })
})
