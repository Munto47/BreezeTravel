const { test, expect } = require('@playwright/test')
const fs = require('node:fs')
const path = require('node:path')
const screenshotDirectory = path.resolve(
  __dirname,
  '../../.local-artifacts/experience/screenshots',
)
fs.mkdirSync(screenshotDirectory, { recursive: true })
const requestSummaries = new WeakMap()

test.beforeEach(async ({ page }) => {
  const summary = { writes: {}, rateLimitedResponses: 0 }
  requestSummaries.set(page, summary)
  page.on('request', (request) => {
    const pathname = new URL(request.url()).pathname
    if (
      !pathname.startsWith('/api/') ||
      !['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method())
    )
      return
    const action = pathname.split('/').pop()
    // Record action counts only; resource identifiers and request bodies stay private.
    const label = [
      'commands',
      'materialize',
      'map-renders',
      'place-candidates',
      'trip-understandings',
      'source',
      'adopt',
      'preview',
    ].includes(action)
      ? action
      : 'other'
    summary.writes[label] = (summary.writes[label] || 0) + 1
  })
  page.on('response', (response) => {
    if (
      new URL(response.url()).pathname.startsWith('/api/') &&
      response.status() === 429
    )
      summary.rateLimitedResponses++
  })
})

test.afterEach(async ({ page }, testInfo) => {
  await testInfo.attach('request-counts', {
    body: JSON.stringify(requestSummaries.get(page)),
    contentType: 'application/json',
  })
})

async function openDemo(page) {
  await page.goto('/')
  await expect(page.getByTestId('start-demo')).toBeEnabled({ timeout: 15000 })
  await page.getByTestId('start-demo').click()
  await expect(page).toHaveURL(/\/trip\/result$/)
  await expect(
    page.getByRole('heading', { name: '故宫博物院', exact: true }).first(),
  ).toBeVisible({ timeout: 60000 })
  await expect(page.getByText('正在检查时间与路线…')).toHaveCount(0, {
    timeout: 30000,
  })
}

for (const width of [1440, 1280, 390, 360]) {
  test(`new entry and daily workspace fit ${width}px`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/')
    await expect(
      page.getByRole('textbox', { name: '你的攻略或行程' }),
    ).toBeVisible()
    await expect(
      page.getByRole('button', { name: '整理这份行程' }),
    ).toBeVisible()
    await expect(
      page.getByRole('button', { name: '整理这份行程' }),
    ).toBeEnabled({ timeout: 15000 })
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBeTruthy()
    await page.screenshot({
      path: testInfo.outputPath(`home-${width}.png`),
      fullPage: true,
    })
    if ([1440, 360].includes(width))
      await page.screenshot({
        path: path.join(screenshotDirectory, `home-${width}.png`),
        fullPage: true,
      })
    await openDemo(page)
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBeTruthy()
    await page.screenshot({
      path: testInfo.outputPath(`itinerary-${width}.png`),
      fullPage: true,
    })
    if (width === 360)
      await page.screenshot({
        path: path.join(screenshotDirectory, 'itinerary-360.png'),
        fullPage: true,
      })
    if (width < 760) {
      await page
        .getByRole('navigation', { name: '行程和地图' })
        .getByRole('button', { name: '地图', exact: true })
        .click()
      await expect(
        page.getByRole('heading', { name: '这一天，怎么走' }),
      ).toBeVisible()
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBeTruthy()
      await page.screenshot({
        path: testInfo.outputPath(`map-${width}.png`),
        fullPage: true,
      })
    }
  })
}

test('time edit persists through reload and undo without automatic route calculation', async ({
  page,
}) => {
  let routeWrites = 0
  page.on('request', (request) => {
    if (
      request.method() === 'POST' &&
      /\/map-renders$/.test(new URL(request.url()).pathname)
    )
      routeWrites++
  })
  await openDemo(page)
  await page
    .getByRole('button', { name: '详情与编辑', exact: true })
    .first()
    .click()
  await page.getByLabel('开始时间', { exact: true }).fill('09:30')
  await page.getByLabel('结束时间', { exact: true }).fill('11:30')
  await page.getByLabel('预计停留分钟').fill('120')
  await page.getByRole('button', { name: '保存时间安排' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0, { timeout: 30000 })
  await expect(
    page.getByTestId('trip-days').getByText('09:30', { exact: true }),
  ).toBeVisible()
  expect(routeWrites).toBe(0)
  await page.reload()
  await expect(
    page.getByTestId('trip-days').getByText('09:30', { exact: true }),
  ).toBeVisible({ timeout: 30000 })
  await page.getByRole('button', { name: '撤销', exact: true }).click()
  await expect(
    page.getByTestId('trip-days').getByText('09:30', { exact: true }),
  ).toHaveCount(0, { timeout: 30000 })
  expect(routeWrites).toBe(0)
  await expect(
    page.getByRole('button', { name: '更新路线', exact: true }),
  ).toBeEnabled()
  await page.getByRole('button', { name: '更新路线', exact: true }).click()
  await expect.poll(() => routeWrites).toBe(1)
})

test('custom text uses anonymous FULL instead of the demo or login', async ({
  page,
}) => {
  let captured
  await page.route('**/api/v3/trip-understandings', async (route) => {
    captured = route.request().postDataJSON()
    await route.fulfill({
      status: 429,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'quota' }),
    })
  })
  await page.goto('/')
  await page
    .getByRole('textbox', { name: '你的攻略或行程' })
    .fill('第一天去上海博物馆和外滩，豫园是备选。')
  await page.getByRole('button', { name: '整理这份行程' }).click()
  await expect(page.locator('main').getByRole('alert')).toContainText(
    '当前体验次数已用完',
  )
  expect(captured).toEqual({
    mode: 'FULL',
    source: { type: 'TEXT', text: '第一天去上海博物馆和外滩，豫园是备选。' },
  })
  await expect(page).not.toHaveURL(/login/)
  await expect(
    page.getByRole('textbox', { name: '你的攻略或行程' }),
  ).toHaveValue(captured.source.text)
})

test('a lost save response retries the same operation after reload', async ({
  page,
}) => {
  await openDemo(page)
  const keys = []
  let lost = false
  await page.route(
    '**/api/v3/trip-understandings/*/commands',
    async (route) => {
      keys.push(route.request().headers()['idempotency-key'])
      if (!lost) {
        lost = true
        await route.fetch()
        await route.abort('failed')
      } else await route.continue()
    },
  )
  await page
    .getByRole('button', { name: '详情与编辑', exact: true })
    .first()
    .click()
  await page.getByLabel('开始时间', { exact: true }).fill('10:15')
  await page.getByRole('button', { name: '保存时间安排' }).click()
  await expect(
    page.getByRole('button', { name: '确认保存结果', exact: true }),
  ).toBeAttached({ timeout: 30000 })
  await page.reload()
  await expect(
    page.getByRole('button', { name: '确认保存结果', exact: true }),
  ).toBeVisible({ timeout: 30000 })
  await page.getByRole('button', { name: '确认保存结果', exact: true }).click()
  await expect(
    page.getByRole('button', { name: '确认保存结果', exact: true }),
  ).toHaveCount(0, { timeout: 30000 })
  expect(keys).toHaveLength(2)
  expect(keys[1]).toBe(keys[0])
  await expect(
    page.getByTestId('trip-days').getByText('10:15', { exact: true }),
  ).toBeVisible()
})

test('@live actual map displays server-provided locations', async ({
  page,
}, testInfo) => {
  const knownMapErrors = new Set()
  page.on('console', (message) => {
    const match = message
      .text()
      .match(
        /INVALID_USER_SCODE|INVALID_USER_KEY|USERKEY_PLAT_NOMATCH|INVALID_USER_DOMAIN|SERVICE_NOT_AVAILABLE/,
      )
    if (match) knownMapErrors.add(match[0])
  })
  await page.setViewportSize({ width: 1440, height: 1000 })
  await openDemo(page)
  await expect(page.locator('.e-map-marker')).toHaveCount(2, { timeout: 30000 })
  await expect(page.locator('.e-map-marker').first()).toBeInViewport({
    timeout: 30000,
  })
  await expect(page.locator('.e-map-empty')).toHaveCount(0, { timeout: 30000 })
  expect([...knownMapErrors]).toEqual([])
  await page.screenshot({
    path: testInfo.outputPath('actual-map.png'),
    fullPage: true,
  })
  await page.screenshot({
    path: path.join(screenshotDirectory, 'itinerary-1440.png'),
    fullPage: true,
  })
})

test('@live a real candidate can replace a place without recalculating routes', async ({
  page,
}) => {
  let routeWrites = 0
  page.on('request', (request) => {
    if (
      request.method() === 'POST' &&
      /\/map-renders$/.test(new URL(request.url()).pathname)
    )
      routeWrites++
  })
  await openDemo(page)
  await page
    .getByRole('button', { name: '详情与编辑', exact: true })
    .first()
    .click()
  await page.getByLabel('搜索地点名称').fill('天坛公园')
  await page.getByRole('button', { name: '搜索真实地点' }).click()
  await expect(page.locator('.e-candidate').first()).toBeVisible({
    timeout: 30000,
  })
  const chosenName = await page
    .locator('.e-candidate strong')
    .first()
    .textContent()
  await page.locator('.e-candidate').first().click()
  await expect(page.getByRole('dialog')).toHaveCount(0, { timeout: 30000 })
  await expect(
    page
      .getByTestId('trip-days')
      .getByRole('heading', { name: chosenName, exact: true }),
  ).toBeVisible()
  expect(routeWrites).toBe(0)
  await page.reload()
  await expect(
    page
      .getByTestId('trip-days')
      .getByRole('heading', { name: chosenName, exact: true }),
  ).toBeVisible({ timeout: 30000 })
})

test('a time conflict has a read-only preview and can be adopted', async ({
  page,
}) => {
  await openDemo(page)
  async function edit(index, start, end) {
    await page
      .getByRole('button', { name: '详情与编辑', exact: true })
      .nth(index)
      .click()
    await page.getByLabel('开始时间', { exact: true }).fill(start)
    await page.getByLabel('结束时间', { exact: true }).fill(end)
    const minutes = (value) =>
      Number(value.slice(0, 2)) * 60 + Number(value.slice(3))
    await page
      .getByLabel('预计停留分钟')
      .fill(String(minutes(end) - minutes(start)))
    await page.getByRole('button', { name: '保存时间安排' }).click()
    await expect(page.getByRole('dialog')).toHaveCount(0, { timeout: 30000 })
    await expect(page.getByText('正在检查时间与路线…')).toHaveCount(0, {
      timeout: 30000,
    })
  }
  await edit(0, '09:00', '11:00')
  await edit(1, '10:00', '11:00')
  await page.getByRole('button', { name: '更新路线', exact: true }).click()
  const fixConflict = page
    .locator('.e-finding.hard')
    .getByRole('button', { name: '查看怎么调整 →' })
    .first()
  await expect(fixConflict).toBeVisible({ timeout: 60000 })
  const before = await page.getByTestId('trip-days').innerText()
  await fixConflict.click()
  await expect(page.getByRole('dialog')).toBeVisible()
  expect(await page.getByTestId('trip-days').innerText()).toBe(before)
  await expect(
    page.getByRole('dialog').getByText('现在的安排', { exact: true }),
  ).toBeVisible()
  await page.getByRole('button', { name: '采纳这次调整' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0, { timeout: 30000 })
  await expect(
    page.getByTestId('trip-days').getByText('10:00', { exact: true }),
  ).toHaveCount(0, { timeout: 30000 })
})

test('deleting imported text retains the itinerary and route view after reload', async ({
  page,
}) => {
  await openDemo(page)
  await expect(page.locator('.e-route-note')).toContainText('步行约', {
    timeout: 30000,
  })
  const before = await page.getByTestId('trip-days').innerText()
  await page.getByRole('button', { name: '删除导入文字', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('整理后的行程仍保留')
  await page.getByRole('button', { name: '确认永久删除', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0, { timeout: 30000 })
  await expect(
    page.getByRole('button', { name: '导入文字已删除' }),
  ).toBeDisabled()
  await page.reload()
  await expect(
    page
      .getByTestId('trip-days')
      .getByRole('heading', { name: '故宫博物院', exact: true }),
  ).toBeVisible({ timeout: 30000 })
  await expect(page.getByText('正在检查时间与路线…')).toHaveCount(0, {
    timeout: 30000,
  })
  expect(await page.getByTestId('trip-days').innerText()).toBe(before)
  await expect(
    page.getByRole('heading', { name: '这一天，怎么走' }),
  ).toBeVisible()
  await expect(
    page.getByRole('button', { name: '导入文字已删除' }),
  ).toBeDisabled()
  await expect(page.getByText('这份行程已删除或过期')).toHaveCount(0)
})

test('saving an edited anonymous trip registers by email and restores the account trip', async ({
  page,
}) => {
  await openDemo(page)
  const secondDraft = await page.request.post('/api/v3/trip-understandings', {
    data: { mode: 'DEMO' },
    headers: { 'Idempotency-Key': require('node:crypto').randomUUID() },
  })
  expect(secondDraft.status()).toBe(202)
  const secondReference = (await secondDraft.json()).public_resource_id
  await expect
    .poll(
      async () =>
        (
          await page.request.get(
            `/api/v3/trip-understandings/${secondReference}/result`,
          )
        ).status(),
      { timeout: 30000 },
    )
    .toBe(200)
  await page
    .getByRole('button', { name: '详情与编辑', exact: true })
    .first()
    .click()
  await page.getByLabel('开始时间', { exact: true }).fill('09:45')
  await page.getByRole('button', { name: '保存时间安排' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0, { timeout: 30000 })
  await page.getByRole('button', { name: '保存行程', exact: true }).click()
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByLabel('邮箱', { exact: true })).toBeVisible()
  await expect(page.getByText('短信', { exact: false })).toHaveCount(0)
  for (const width of [360, 1440]) {
    await page.setViewportSize({ width, height: 900 })
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBeTruthy()
    await page.screenshot({
      path: path.join(screenshotDirectory, `login-${width}.png`),
      fullPage: true,
    })
  }
  await page.getByRole('button', { name: '注册账号', exact: true }).click()
  const suffix = require('node:crypto').randomUUID().slice(0, 8)
  const email = `experience-${Date.now()}-${suffix}@example.test`
  const password = `Trip${require('node:crypto').randomUUID()}7`
  await page.getByLabel('邮箱', { exact: true }).fill(email)
  await page.getByLabel('密码', { exact: true }).fill(password)
  await page.getByLabel('称呼（选填）', { exact: true }).fill('旅行体验测试')
  const accountResult = page.waitForResponse(
    async (response) => {
      if (
        !new URL(response.url()).pathname.startsWith('/api/') ||
        !new URL(response.url()).pathname.endsWith('/result') ||
        response.status() !== 200
      )
        return false
      return (await response.json()).ownership === 'ACCOUNT'
    },
    { timeout: 30000 },
  )
  await page.getByRole('button', { name: '注册并继续', exact: true }).click()
  await accountResult
  await expect(page).toHaveURL(/\/trip\/result$/)
  await expect(
    page.getByRole('button', { name: '已保存到账号', exact: true }),
  ).toBeVisible({ timeout: 30000 })
  await expect(
    page.getByTestId('trip-days').getByText('09:45', { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText('北京三日示例 · 固定回放', { exact: false }),
  ).toBeVisible()
  await page.reload()
  await expect(
    page.getByRole('button', { name: '已保存到账号', exact: true }),
  ).toBeVisible({ timeout: 30000 })
  await expect(
    page.getByTestId('trip-days').getByText('09:45', { exact: true }),
  ).toBeVisible()
  expect(
    (
      await page.request.get(
        `/api/v3/trip-understandings/${secondReference}/result`,
      )
    ).status(),
  ).toBe(200)
  await page.getByRole('button', { name: '退出', exact: true }).click()
  await expect(page).toHaveURL(/\/login$/)
  await page.getByLabel('邮箱', { exact: true }).fill(email)
  await page.getByLabel('密码', { exact: true }).fill(password)
  await page.getByRole('button', { name: '登录并继续', exact: true }).click()
  await expect(page).toHaveURL(/\/trip\/result$/)
  await expect(
    page.getByRole('button', { name: '已保存到账号', exact: true }),
  ).toBeVisible({ timeout: 30000 })
  await expect(
    page.getByTestId('trip-days').getByText('09:45', { exact: true }),
  ).toBeVisible()
})
