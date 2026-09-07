const { test, expect } = require('@playwright/test')
const { randomUUID } = require('node:crypto')
const fs = require('node:fs')

// Original public-place text only, live local service, no response/credential logs.
// Every test deletes only the resource it created using its own cookie.
const API = '/api/v3/trip-understandings'
const states = new WeakMap()
const normalize = name => name.replace(/[()（）·—-]/g, '')
test.beforeEach(async ({ page }) => {
  const state = { resource: null, summary: { created: 0, searches: 0, commands: 0, mapPosts: 0 } }
  states.set(page, state)
  page.on('request', request => {
    if (request.method() !== 'POST') return
    const pathname = new URL(request.url()).pathname
    if (pathname.endsWith('/place-candidates')) state.summary.searches++
    if (pathname.endsWith('/commands')) state.summary.commands++
    if (pathname.endsWith('/map-renders')) state.summary.mapPosts++
  })
})

async function ownedResult(page) {
  const state = states.get(page)
  let response
  try { response = await page.request.get(`${API}/${encodeURIComponent(state.resource)}/result`) }
  catch { throw new Error('本次合成行程读取失败，未保留响应或请求地址') }
  expect(response.status()).toBe(200)
  return { body: await response.json(), etag: response.headers().etag }
}

async function create(page, source) {
  await page.goto('/')
  await page.getByTestId('trip-source-text').fill(source)
  const accepted = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === API)
  await page.getByTestId('create-full-trip').click()
  const response = await accepted
  const body = await response.json()
  if (response.status() === 202 && typeof body.public_resource_id === 'string') {
    states.get(page).resource = body.public_resource_id
    states.get(page).summary.created++
  }
  expect(response.status()).toBe(202)
  const workspace = page.getByTestId('itinerary-workspace')
  const failed = page.getByRole('heading', { name: '这次没有整理完成', exact: true })
  await expect(workspace.or(failed)).toBeVisible({ timeout: 120_000 })
  expect(await failed.isVisible(), '真实文字应得到可操作卡片').toBe(false)
  await expect(page.getByTestId('drag-handle-1-0')).toBeEnabled()
  const result = await ownedResult(page)
  expect(result.body.is_demo).toBe(false)
  return result
}

test.afterEach(async ({ page, baseURL }, info) => {
  const state = states.get(page)
  if (!state) return
  state.summary.cleanup = 'NOT_NEEDED'
  if (state.resource) {
    try {
      const deleted = await page.request.delete(`${API}/${encodeURIComponent(state.resource)}`, {
        headers: { 'Idempotency-Key': randomUUID(), Origin: new URL(baseURL).origin },
      })
      const gone = await page.request.get(`${API}/${encodeURIComponent(state.resource)}/result`)
      state.summary.cleanup = deleted.status() === 204 && gone.status() === 410 ? 'DELETED_AND_GONE' : 'FAILED'
    } catch { state.summary.cleanup = 'FAILED' }
  }
  state.summary.outcome = info.status
  fs.writeFileSync(info.outputPath('summary.json'), JSON.stringify(state.summary, null, 2))
  expect(state.summary.cleanup, '本次合成行程必须实际删除并确认不可回读').toBe('DELETED_AND_GONE')
})

test('explicit three-city campuses, gate and restaurant branch survive live text generation', async ({ page }, info) => {
  const source = '北京、上海、杭州三日游。\nDay 1 北京：故宫北门 → 中国美术馆 → 全聚德前门店吃晚餐。\n'
    + 'Day 2 上海：上海博物馆东馆 → 上海博物馆人民广场馆 → 上海科技馆。\n'
    + 'Day 3 杭州：浙江省博物馆之江馆区 → 浙江省博物馆孤山馆区。\n'
    + '备选：杭州植物园（有空再考虑）。不去：雷峰塔。攻略链接 https://example.com/trip 仅供参考。'
  const { body } = await create(page, source)
  const expected = [
    ['北京', ['故宫博物院神武门', '中国美术馆', '全聚德前门店']],
    ['上海', ['上海博物馆东馆', '上海博物馆人民广场馆', '上海科技馆']],
    ['杭州', ['浙江省博物馆之江馆区', '浙江省博物馆孤山馆区']],
  ]
  expect(body.days.length).toBe(3)
  expected.forEach(([city, names], index) => {
    const cards = body.days[index].activities
    expect(cards.map(card => normalize(card.name))).toEqual(names)
    expect(cards.map(card => card.city)).toEqual(names.map(() => city))
    expect(cards.every(card => card.status === 'READY'), '明确馆区和分店均须真实确认').toBe(true)
  })
  expect(body.days[2].alternatives.map(card => card.name)).toEqual(['杭州植物园'])
  const summary = states.get(page).summary
  summary.days = 3
  summary.confirmedCards = 8
  summary.optionalCards = 1
  expect(summary.searches).toBe(0)
  await page.screenshot({ path: info.outputPath('explicit-place-cards.png'), fullPage: true, animations: 'disabled' })
})

test('conflicting original district stays pending and explicit city correction works on mobile', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const { body, etag } = await create(page, '上海一日游。Day 1：参观位于浦东新区的上海博物馆人民广场馆。')
  const card = body.days[0].activities[0]
  expect(normalize(card.name)).toBe('上海博物馆人民广场馆')
  expect(card.status, '原文行政区与地点实际归属矛盾，必须待确认').toBe('NEEDS_CONFIRMATION')
  await page.getByTestId('activity-card').first().getByRole('button').filter({ hasText: card.name }).click()
  const dropdown = page.getByTestId('pending-place-dropdown')
  await expect(dropdown).toBeVisible()
  expect(states.get(page).summary.searches).toBe(0)
  await dropdown.getByLabel('查询城市').selectOption('北京')
  await dropdown.getByLabel('搜索地点名称').fill('中国美术馆')
  expect(states.get(page).summary.searches).toBe(0)
  await dropdown.getByRole('button', { name: '搜索', exact: true }).click()
  const option = dropdown.getByRole('button').filter({ has: page.locator('strong').filter({ hasText: /^中国美术馆$/ }) })
  await expect(option).toHaveCount(1)
  await option.click()
  await dropdown.getByRole('button', { name: '使用这个地点', exact: true }).click()
  await expect(dropdown).toBeHidden()
  const confirmed = await ownedResult(page)
  expect(confirmed.etag).not.toBe(etag)
  expect(confirmed.body.days[0].activities[0].name).toBe('中国美术馆')
  expect(confirmed.body.days[0].activities[0].city).toBe('北京')
  expect(confirmed.body.days[0].activities[0].status).toBe('READY')
  expect(confirmed.body.map.status).toBe('NEEDS_UPDATE')
  const summary = states.get(page).summary
  expect(summary.searches).toBe(1)
  expect(summary.commands).toBe(1)
  expect(summary.mapPosts).toBe(0)
  summary.originalConflictPending = true
  summary.explicitCityCorrected = true
  await page.screenshot({ path: info.outputPath('city-corrected-mobile.png'), fullPage: true, animations: 'disabled' })
})
