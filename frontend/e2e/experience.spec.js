const { test, expect } = require('@playwright/test')
const fs = require('node:fs')
const path = require('node:path')
const screenshotDirectory = path.resolve(
  __dirname,
  '../../.local-artifacts/frontend-refresh/screenshots',
)
fs.mkdirSync(screenshotDirectory, { recursive: true })
const requestSummaries = new WeakMap()
let recentWrites = []

test.beforeEach(async ({ page }) => {
  // All contexts share the real 60 writes/IP/minute budget. Leave room for the
  // longest account journey; do not raise the product limit for a fast test suite.
  recentWrites = recentWrites.filter((stamp) => Date.now() - stamp < 60000)
  if (recentWrites.length >= 40) {
    await new Promise((resolve) =>
      setTimeout(resolve, Math.max(0, 60100 - (Date.now() - recentWrites[0]))),
    )
    recentWrites = recentWrites.filter((stamp) => Date.now() - stamp < 60000)
  }
  const summary = { writes: {}, rateLimitedResponses: 0 }
  requestSummaries.set(page, summary)
  page.on('request', (request) => {
    const pathname = new URL(request.url()).pathname
    if (
      !pathname.startsWith('/api/') ||
      !['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method())
    )
      return
    recentWrites.push(Date.now())
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
  await page.getByTestId('create-full-trip').click()
  await expect(page).toHaveURL(/\/trip\/result(?:#.*)?$/)
  await expect(
    page.getByRole('button', { name: '编辑故宫博物院', exact: true }),
  ).toBeVisible({ timeout: 60000 })
  await expect(page.getByText('正在检查时间与路线…')).toHaveCount(0, {
    timeout: 30000,
  })
}

async function currentResult(page) {
  return page.evaluate(async () => {
    const resource = sessionStorage.getItem('bt_active_trip_ref')
    const token = localStorage.getItem('authToken')
    const response = await fetch(
      `/api/v3/trip-understandings/${encodeURIComponent(resource)}/result`,
      {
        credentials: 'include',
        cache: 'no-store',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      },
    )
    if (!response.ok) throw new Error('Result readback failed')
    return {
      etag: response.headers.get('etag'),
      days: (await response.json()).days,
    }
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
    await expect(page.getByRole('button', { name: '整理行程' })).toBeVisible()
    await expect(page.getByRole('button', { name: '整理行程' })).toBeEnabled({
      timeout: 15000,
    })
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
    if (width < 1024) {
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
  await page.locator('.e-edit-stop').first().click()
  await page.getByLabel('开始时间', { exact: true }).fill('09:30')
  await page.getByLabel('结束时间', { exact: true }).fill('11:30')
  await page.getByLabel('预计停留分钟').fill('120')
  await page.getByRole('button', { name: '应用修改' }).click()
  await expect(page.locator('.e-context-panel')).toHaveCount(0, {
    timeout: 30000,
  })
  await expect(
    page.getByTestId('trip-days').getByText(/^09:30(?:–|$)/),
  ).toBeVisible()
  expect(routeWrites).toBe(0)
  await page.reload()
  await expect(
    page.getByTestId('trip-days').getByText(/^09:30(?:–|$)/),
  ).toBeVisible({ timeout: 30000 })
  await page.getByRole('button', { name: '撤销', exact: true }).click()
  await expect(
    page.getByTestId('trip-days').getByText(/^09:30(?:–|$)/),
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
  await page.getByRole('button', { name: '整理行程' }).click()
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
  await page.locator('.e-edit-stop').first().click()
  await page.getByLabel('开始时间', { exact: true }).fill('10:15')
  await page.getByRole('button', { name: '应用修改' }).click()
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
    page.getByTestId('trip-days').getByText(/^10:15(?:–|$)/),
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
  await page.locator('.e-edit-stop').first().click()
  await page.getByLabel('搜索地点名称').fill('天坛公园')
  await page.getByRole('button', { name: '搜索地点' }).click()
  await expect(page.locator('.e-candidate').first()).toBeVisible({
    timeout: 30000,
  })
  const chosenName = await page
    .locator('.e-candidate strong')
    .first()
    .textContent()
  const before = await currentResult(page)
  await page.locator('.e-candidate').first().click()
  await expect(page.getByRole('button', { name: '使用这个地点' })).toBeVisible()
  expect(await currentResult(page)).toEqual(before)
  await page.getByRole('button', { name: '使用这个地点' }).click()
  await expect(page.getByRole('button', { name: '使用这个地点' })).toHaveCount(
    0,
    { timeout: 30000 },
  )
  await page.locator('.e-context-head button').click()
  await expect(
    page.getByTestId('trip-days').getByText(chosenName, { exact: true }),
  ).toBeVisible()
  expect(routeWrites).toBe(0)
  await page.reload()
  await expect(
    page.getByTestId('trip-days').getByText(chosenName, { exact: true }),
  ).toBeVisible({ timeout: 30000 })
})

test('a time conflict has a read-only preview and can be adopted', async ({
  page,
}) => {
  await openDemo(page)
  async function edit(index, start, end) {
    await page.locator('.e-edit-stop').nth(index).click()
    await page.getByLabel('开始时间', { exact: true }).fill(start)
    await page.getByLabel('结束时间', { exact: true }).fill(end)
    const minutes = (value) =>
      Number(value.slice(0, 2)) * 60 + Number(value.slice(3))
    await page
      .getByLabel('预计停留分钟')
      .fill(String(minutes(end) - minutes(start)))
    await page.getByRole('button', { name: '应用修改' }).click()
    await expect(page.locator('.e-context-panel')).toHaveCount(0, {
      timeout: 30000,
    })
    await expect(page.getByText('正在检查时间与路线…')).toHaveCount(0, {
      timeout: 30000,
    })
  }
  await edit(0, '09:00', '11:00')
  await edit(1, '10:00', '11:00')
  await page.getByRole('button', { name: '更新路线', exact: true }).click()
  const fixConflict = page
    .locator('.e-inline-issue.is-hard')
    .getByRole('button', { name: '预览调整' })
    .first()
  await expect(fixConflict).toBeVisible({ timeout: 60000 })
  const before = await currentResult(page)
  await fixConflict.click()
  await expect(page.locator('.e-context-panel')).toBeVisible()
  expect(await currentResult(page)).toEqual(before)
  await expect(page.getByText('预览中 · 当前行程尚未改变')).toBeVisible()
  await page.getByRole('button', { name: '确认采纳' }).click()
  await expect(page.locator('.e-context-panel')).toHaveCount(0, {
    timeout: 30000,
  })
  await expect(
    page.getByTestId('trip-days').getByText(/^10:00(?:–|$)/),
  ).toHaveCount(0, { timeout: 30000 })
})

test('deleting imported text retains the itinerary and route view after reload', async ({
  page,
}) => {
  await openDemo(page)
  await expect(page.locator('.e-transport summary')).toContainText('步行', {
    timeout: 30000,
  })
  const before = await page.getByTestId('trip-days').innerText()
  await page.getByLabel('更多行程操作').click()
  await page.getByRole('button', { name: '删除导入文字', exact: true }).click()
  await expect(page.locator('.e-context-panel')).toContainText(
    '现有行程、已确认地点和路线仍保留',
  )
  await page.getByRole('button', { name: '确认永久删除', exact: true }).click()
  await expect(page.locator('.e-context-panel')).toHaveCount(0, {
    timeout: 30000,
  })
  await page.getByLabel('更多行程操作').click()
  await expect(
    page.getByRole('button', { name: '导入文字已删除' }),
  ).toBeDisabled()
  await page.reload()
  await expect(
    page.getByTestId('trip-days').getByText('故宫博物院', { exact: true }),
  ).toBeVisible({ timeout: 30000 })
  await expect(page.getByText('正在检查时间与路线…')).toHaveCount(0, {
    timeout: 30000,
  })
  expect(await page.getByTestId('trip-days').innerText()).toBe(before)
  await expect(
    page.getByRole('heading', { name: '这一天，怎么走' }),
  ).toBeVisible()
  await page.getByLabel('更多行程操作').click()
  await expect(
    page.getByRole('button', { name: '导入文字已删除' }),
  ).toBeDisabled()
  await expect(page.getByText('这份行程已删除或过期')).toHaveCount(0)
})

test('saving and reopening an edited account trip in another browser', async ({
  page,
  browser,
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
  await page.locator('.e-edit-stop').first().click()
  await page.getByLabel('开始时间', { exact: true }).fill('09:45')
  await page.getByRole('button', { name: '应用修改' }).click()
  await expect(page.locator('.e-context-panel')).toHaveCount(0, {
    timeout: 30000,
  })
  await page.getByRole('button', { name: '保存到账号', exact: true }).click()
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
  await expect(page).toHaveURL(/\/trip\/result(?:#.*)?$/)
  await expect(
    page.getByRole('button', { name: '已保存到账号', exact: true }),
  ).toBeVisible({ timeout: 30000 })
  await expect(
    page.getByTestId('trip-days').getByText(/^09:45(?:–|$)/),
  ).toBeVisible()
  await expect(
    page.getByText('示例行程 · 安排与路线为固定回放', { exact: false }),
  ).toBeVisible()
  await page.reload()
  await expect(
    page.getByRole('button', { name: '已保存到账号', exact: true }),
  ).toBeVisible({ timeout: 30000 })
  await expect(
    page.getByTestId('trip-days').getByText(/^09:45(?:–|$)/),
  ).toBeVisible()
  expect(
    (
      await page.request.get(
        `/api/v3/trip-understandings/${secondReference}/result`,
      )
    ).status(),
  ).toBe(200)
  await page.getByRole('link', { name: '我的行程', exact: true }).click()
  await expect(page.locator('.e-trip-list-row')).toHaveCount(1)
  await page.getByRole('button', { name: '退出', exact: true }).click()
  await expect(page).toHaveURL(/\/login$/)
  await page.getByLabel('邮箱', { exact: true }).fill(email)
  await page.getByLabel('密码', { exact: true }).fill(password)
  await page.getByRole('button', { name: '登录并继续', exact: true }).click()
  await expect(page).toHaveURL(/\/my-trips$/)
  await page.getByRole('button', { name: /^继续编辑/ }).click()
  await expect(
    page.getByRole('button', { name: '已保存到账号', exact: true }),
  ).toBeVisible({ timeout: 30000 })
  await expect(
    page.getByTestId('trip-days').getByText(/^09:45(?:–|$)/),
  ).toBeVisible()
  const otherBrowser = await browser.newContext()
  const otherPage = await otherBrowser.newPage()
  try {
    await otherPage.goto(new URL('/my-trips', page.url()).toString())
    await otherPage.getByRole('button', { name: '登录并查看' }).click()
    await otherPage.getByLabel('邮箱', { exact: true }).fill(email)
    await otherPage.getByLabel('密码', { exact: true }).fill(password)
    await otherPage.getByRole('button', { name: '登录并继续' }).click()
    await expect(otherPage).toHaveURL(/\/my-trips$/)
    await expect(otherPage.locator('.e-trip-list-row')).toHaveCount(1, {
      timeout: 30000,
    })
    await expect(otherPage.locator('.e-trip-list-row')).toContainText(
      '固定示例',
    )
    await expect(otherPage.locator('.e-trip-list-row')).toContainText('保留至')
    await otherPage.getByRole('button', { name: /^继续编辑/ }).click()
    await expect(
      otherPage.getByTestId('trip-days').getByText(/^09:45(?:–|$)/),
    ).toBeVisible({ timeout: 30000 })
    await otherPage.locator('.e-edit-stop').first().click()
    await otherPage.getByLabel('开始时间', { exact: true }).fill('10:05')
    await otherPage
      .getByRole('button', { name: '应用修改', exact: true })
      .click()
    await expect(otherPage.locator('.e-context-panel')).toHaveCount(0, {
      timeout: 30000,
    })
    await otherPage.reload()
    await expect(
      otherPage.getByTestId('trip-days').getByText(/^10:05(?:–|$)/),
    ).toBeVisible({ timeout: 30000 })
    await otherPage.getByRole('link', { name: '我的行程', exact: true }).click()
    await expect(otherPage.locator('.e-trip-list-row')).toHaveCount(1)
    for (const width of [1440, 360]) {
      await otherPage.setViewportSize({ width, height: 900 })
      expect(
        await otherPage.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBeTruthy()
      await otherPage.screenshot({
        path: path.join(screenshotDirectory, `my-trips-${width}.png`),
        fullPage: true,
      })
    }
    let preferenceReads = 0
    otherPage.on('request', (request) => {
      if (
        request.method() === 'GET' &&
        new URL(request.url()).pathname === '/api/v3/me/travel-preferences'
      )
        preferenceReads++
    })
    await otherPage.getByRole('link', { name: '账号', exact: true }).click()
    await expect(otherPage.getByLabel('称呼', { exact: true })).toHaveValue(
      '旅行体验测试',
    )
    const memory = otherPage.getByRole('button', {
      name: '切换记住结构化偏好',
      exact: true,
    })
    await expect(memory).toBeEnabled()
    await expect(memory).toHaveAttribute('aria-pressed', 'false')
    expect(preferenceReads).toBe(0)
    await memory.click()
    await expect(memory).toHaveAttribute('aria-pressed', 'true')
    await otherPage.getByTestId('walking-tolerance').fill('30')
    await otherPage.getByTestId('preferred-start-time').fill('08:30')
    await otherPage.getByTestId('save-preferences').click()
    await expect(
      otherPage.getByText('旅行偏好已更新。', { exact: true }),
    ).toBeVisible()
    await otherPage.reload()
    await expect(otherPage.getByTestId('walking-tolerance')).toHaveValue('30')
    await expect(otherPage.getByTestId('preferred-start-time')).toHaveValue(
      '08:30',
    )
    await otherPage.getByTestId('clear-preferences').click()
    await expect(otherPage.getByTestId('walking-tolerance')).toHaveValue('')
    await memory.click()
    await expect(memory).toHaveAttribute('aria-pressed', 'false')
    await expect(otherPage.getByText('偏好设置暂时无法读取。')).toHaveCount(0)
    for (const width of [1440, 360]) {
      await otherPage.setViewportSize({ width, height: 900 })
      expect(
        await otherPage.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBeTruthy()
      await otherPage.screenshot({
        path: path.join(screenshotDirectory, `profile-${width}.png`),
        fullPage: true,
      })
    }
    await otherPage.getByRole('link', { name: '我的行程', exact: true }).click()
    // A draft retained while processing must disappear when its trip is deleted,
    // even if that draft never reached a successful result read in this browser.
    await otherPage.evaluate(() => {
      sessionStorage.setItem(
        'bt_input_draft',
        JSON.stringify({
          text: '这段待整理原文属于即将删除的行程',
          demo: false,
          key: 'delete-recovery-regression',
          expires: Date.now() + 60000,
          resource: sessionStorage.getItem('bt_active_trip_ref'),
        }),
      )
    })
    await otherPage.getByRole('button', { name: /^删除北京/ }).click()
    await expect(otherPage.getByRole('dialog')).toBeVisible()
    await otherPage.getByRole('button', { name: '确认永久删除' }).click()
    await expect(otherPage.getByRole('dialog')).toHaveCount(0, {
      timeout: 30000,
    })
    await expect(otherPage.getByText('这里还没有保存的行程')).toBeVisible()
    expect(
      await otherPage.evaluate(() => sessionStorage.getItem('bt_input_draft')),
    ).toBeNull()
    await otherPage.reload()
    await expect(otherPage.getByText('这里还没有保存的行程')).toBeVisible({
      timeout: 30000,
    })
  } finally {
    await otherBrowser.close()
  }
})
