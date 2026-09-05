const { test, expect } = require('@playwright/test')

// These are UI regression checks. Only FIXED_DEMO reaches the real local API.
// FULL submission errors and account-list errors are explicitly intercepted;
// they do not count as live inference or authenticated account evidence.
test.beforeEach(async ({ page }) => {
  await page.route('**/api/v3/trip-understandings', async (route) => {
    if (
      route.request().method() === 'POST' &&
      route.request().postDataJSON()?.mode === 'FULL'
    ) {
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Controlled UI-only failure' }),
      })
    } else await route.continue()
  })
})

async function openDemo(page) {
  await page.goto('/')
  await expect(page.getByTestId('start-demo')).toBeEnabled({ timeout: 15000 })
  await page.getByTestId('start-demo').click()
  await expect(page.getByTestId('trip-source-text')).toHaveValue(/北京三日慢游/)
  await page.getByTestId('create-full-trip').click()
  await expect(page).toHaveURL(/\/trip\/result#trip=/)
  await expect(
    page.getByRole('button', { name: '编辑故宫博物院', exact: true }),
  ).toBeEnabled({ timeout: 60000 })
  await expect(
    page.getByText('正在检查时间与路线…', { exact: true }),
  ).toHaveCount(0, { timeout: 30000 })
}

test('refinement: an authoritative expiry clears only its own input recovery [mocked API]', async ({
  browser,
}) => {
  for (const resource of ['expired-trip', 'another-trip', undefined]) {
    const context = await browser.newContext()
    try {
      const page = await context.newPage()
      await page.addInitScript(
        ({ resource }) => {
          sessionStorage.setItem('bt_active_trip_ref', 'expired-trip')
          sessionStorage.setItem(
            'bt_input_draft',
            JSON.stringify({
              text: '需要保护或删除的原文草稿',
              demo: false,
              key: 'expiry-recovery-regression',
              expires: Date.now() + 60000,
              resource,
            }),
          )
        },
        { resource },
      )
      await page.route(
        '**/api/v3/trip-understandings/expired-trip/result',
        (route) =>
          route.fulfill({
            status: 410,
            contentType: 'application/json',
            body: '{}',
          }),
      )
      await page.goto('/')
      await expect
        .poll(() =>
          page.evaluate(() => sessionStorage.getItem('bt_active_trip_ref')),
        )
        .toBeNull()
      const input = page.getByRole('textbox', { name: '你的攻略或行程' })
      if (resource === 'expired-trip') {
        await expect(input).toHaveValue('')
        expect(
          await page.evaluate(() => sessionStorage.getItem('bt_input_draft')),
        ).toBeNull()
      } else {
        await expect(input).toHaveValue('需要保护或删除的原文草稿')
        expect(
          await page.evaluate(
            () => JSON.parse(sessionStorage.getItem('bt_input_draft')).text,
          ),
        ).toBe('需要保护或删除的原文草稿')
      }
    } finally {
      await context.close()
    }
  }
})

function observeUserWrites(page) {
  const writes = []
  page.on('request', (request) => {
    const pathname = new URL(request.url()).pathname
    if (
      request.method() === 'POST' &&
      /\/(commands|adopt|map-renders)$/.test(pathname)
    )
      writes.push(pathname.split('/').pop())
  })
  return writes
}

test('refinement: failed preference reads do not pretend consent is off and can retry [mocked API]', async ({
  page,
}) => {
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'controlled-ui-only-session')
    localStorage.setItem(
      'authUser',
      JSON.stringify({ userId: 'controlled-user', nickname: '设置测试' }),
    )
  })
  await page.route('**/api/user/me', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        user_id: 'controlled-user',
        nickname: '设置测试',
        phone: null,
        avatar_url: null,
        birthday: null,
        created_at: '2026-09-05T00:00:00Z',
      }),
    }),
  )
  let calls = 0
  await page.route('**/api/v3/me/data-consents', (route) => {
    calls++
    return route.fulfill({
      status: calls === 1 ? 503 : 200,
      contentType: 'application/json',
      body: JSON.stringify(
        calls === 1
          ? {}
          : {
              memory_enabled: true,
              feedback_enabled: false,
              training_eval_enabled: false,
            },
      ),
    })
  })
  await page.route('**/api/v3/me/travel-preferences', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        walking_tolerance_minutes: 45,
        preferred_start_time: '09:00',
        dining_preferences: [],
        hotel_preferences: [],
        intensity: null,
      }),
    }),
  )
  await page.goto('/profile')
  const panel = page.getByTestId('g06-memory-settings')
  await expect(
    panel.getByRole('button', { name: '重新读取偏好设置', exact: true }),
  ).toBeVisible()
  await expect(
    panel.getByRole('button', { name: '切换记住结构化偏好', exact: true }),
  ).toHaveCount(0)
  await panel
    .getByRole('button', { name: '重新读取偏好设置', exact: true })
    .click()
  await expect(
    panel.getByRole('button', { name: '切换记住结构化偏好', exact: true }),
  ).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByTestId('walking-tolerance')).toHaveValue('45')
})

test('refinement: a concurrent edit rejects the old preview and can prepare a new one', async ({
  page,
}) => {
  await openDemo(page)
  await page
    .locator('.e-inline-issue')
    .getByRole('button', { name: '预览调整' })
    .first()
    .click()
  await expect(page.getByText('预览中 · 当前行程尚未改变')).toBeVisible()
  const reference = await page.evaluate(() =>
    sessionStorage.getItem('bt_active_trip_ref'),
  )
  const base = `/api/v3/trip-understandings/${reference}`
  const beforeResponse = await page.request.get(base + '/result')
  const before = await beforeResponse.json()
  // A second authorized client changes a stop while the first preview is open.
  const edited = await page.request.post(base + '/commands', {
    headers: {
      'Idempotency-Key': require('node:crypto').randomUUID(),
      'If-Match': beforeResponse.headers().etag,
    },
    data: {
      command_type: 'ACTIVITY_TIME_SET',
      activity_token: before.days[0].activities[1].activity_token,
      start_time: '14:10',
    },
  })
  expect(edited.status()).toBe(200)
  const after = (await (await page.request.get(base + '/result')).json()).days
  await page.getByRole('button', { name: '确认采纳' }).click()
  await expect(
    page.getByText('预览已失效 · 行程或路线依据已有变化'),
  ).toBeVisible()
  expect(
    (await (await page.request.get(base + '/result')).json()).days,
  ).toEqual(after)
  await page.getByRole('button', { name: '重新预览', exact: true }).click()
  await expect(page.getByText('预览中 · 当前行程尚未改变')).toBeVisible({
    timeout: 15000,
  })
  expect(
    (await (await page.request.get(base + '/result')).json()).days,
  ).toEqual(after)
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await expect(
    page.getByTestId('trip-days').getByText('14:10', { exact: true }),
  ).toBeVisible()
})

async function expectNoHorizontalOverflow(page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true)
}

test('refinement: sample prefill and replacement confirmation never submit a trip', async ({
  page,
}) => {
  let submissions = 0
  await page.route('**/api/v3/trip-understandings', async (route) => {
    if (route.request().method() === 'POST') submissions++
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: '{}',
    })
  })
  await page.goto('/')
  const text = page.getByTestId('trip-source-text')
  await expect(text).toBeEnabled()
  await text.fill('这是我还没有整理的攻略，请保留。')
  await page.getByTestId('start-demo').click()
  const confirm = page.getByRole('alertdialog', { name: '替换输入确认' })
  await expect(confirm).toBeVisible()
  await confirm.getByRole('button', { name: '保留我的文字' }).click()
  await expect(text).toHaveValue('这是我还没有整理的攻略，请保留。')
  await page.getByTestId('start-demo').click()
  await confirm.getByRole('button', { name: '填入示例', exact: true }).click()
  await expect(text).toHaveValue(/北京三日慢游/)
  await expect(text).toBeFocused()
  await expect(
    page.getByText(
      '固定示例已填入。点击整理将打开回放；修改文字后会按真实攻略整理。',
    ),
  ).toBeVisible()
  await expect(page).toHaveURL(/\/$/)
  expect(submissions).toBe(0)
})

test('refinement: editing the sample submits FULL and preserves the same failed attempt after reload [mocked failure]', async ({
  page,
}) => {
  const requests = []
  await page.route('**/api/v3/trip-understandings', async (route) => {
    requests.push({
      body: route.request().postDataJSON(),
      key: route.request().headers()['idempotency-key'],
    })
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Controlled UI-only failure' }),
    })
  })
  await page.goto('/')
  await page.getByTestId('start-demo').click()
  const text = page.getByTestId('trip-source-text')
  const edited = (await text.inputValue()) + '\n我改了示例，最后一天提前结束。'
  await text.fill(edited)
  await page.getByTestId('create-full-trip').click()
  await expect(page.locator('main').getByRole('alert')).toContainText(
    '文字仍在这里',
  )
  await expect(text).toHaveValue(edited)
  await page.reload()
  await expect(text).toHaveValue(edited)
  await page.getByTestId('create-full-trip').click()
  await expect(page.locator('main').getByRole('alert')).toContainText(
    '重试会确认同一次请求',
  )
  expect(requests).toHaveLength(2)
  expect(requests[0].body).toEqual({
    mode: 'FULL',
    source: { type: 'TEXT', text: edited },
  })
  expect(requests[1]).toEqual(requests[0])
  expect(requests[0].key).toBeTruthy()
  await expect(page).not.toHaveURL(/login|trip\/result/)
})

test('refinement: cancelling dirty context restores day, selected activity, scroll and keyboard focus without writes', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const writes = observeUserWrites(page)
  await openDemo(page)
  const days = page.getByRole('navigation', { name: '选择行程日期' })
  await days.getByRole('button', { name: 'Day 2', exact: true }).click()
  const editor = page.getByRole('button', { name: '编辑前门大街', exact: true })
  await editor.scrollIntoViewIfNeeded()
  await editor.focus()
  const scroll = await page.evaluate(() => window.scrollY)
  await editor.press('Enter')
  const panel = page.getByRole('region', { name: '前门大街', exact: true })
  await expect(panel).toBeVisible()
  await expect(
    days.getByRole('button', { name: 'Day 1', exact: true }),
  ).toBeDisabled()
  await panel.getByLabel('开始时间', { exact: true }).fill('09:25')
  await panel.getByRole('button', { name: '返回Day 2', exact: true }).click()
  await expect(panel.getByRole('alert')).toContainText('有尚未应用的修改')
  await panel.getByRole('button', { name: '继续编辑', exact: true }).click()
  await expect(panel.getByLabel('开始时间', { exact: true })).toHaveValue(
    '09:25',
  )
  await panel.getByRole('button', { name: '返回Day 2', exact: true }).click()
  await panel
    .getByRole('button', { name: '放弃修改并返回', exact: true })
    .click()
  await expect(panel).toHaveCount(0)
  await expect(
    days.getByRole('button', { name: 'Day 2', exact: true }),
  ).toHaveAttribute('aria-pressed', 'true')
  await expect(
    page
      .getByTestId('trip-days')
      .locator('.e-stop-select', { hasText: '前门大街' }),
  ).toHaveAttribute('aria-pressed', 'true')
  await expect(editor).toBeFocused()
  await expect
    .poll(() =>
      page.evaluate((expected) => Math.abs(window.scrollY - expected), scroll),
    )
    .toBeLessThanOrEqual(4)
  expect(writes).toEqual([])
})

test('refinement: 320px and 640 CSS-pixel reflow keep context controls visible and keyboard focus inside', async ({
  page,
}) => {
  // 640 CSS px represents the layout width of a 1280px viewport at 200% zoom.
  // This checks reflow at that width; it does not claim a native browser-zoom test.
  await page.setViewportSize({ width: 320, height: 720 })
  await openDemo(page)
  for (const width of [320, 640]) {
    await page.setViewportSize({ width, height: 720 })
    await expectNoHorizontalOverflow(page)
    await page
      .getByRole('navigation', { name: '行程和地图' })
      .getByRole('button', { name: '地图', exact: true })
      .click()
    await expect(page.getByTestId('route-map')).toBeVisible()
    await expectNoHorizontalOverflow(page)
    await page.getByRole('button', { name: '返回行程', exact: true }).click()
    const editor = page.getByRole('button', {
      name: '编辑故宫博物院',
      exact: true,
    })
    await editor.click()
    const panel = page.getByRole('dialog', { name: '故宫博物院', exact: true })
    await expect(panel).toBeVisible()
    await expectNoHorizontalOverflow(page)
    await panel.getByRole('button', { name: '返回Day 1', exact: true }).focus()
    await page.keyboard.press('Shift+Tab')
    expect(
      await panel.evaluate((node) => node.contains(document.activeElement)),
    ).toBe(true)
    for (let step = 0; step < 8; step++) {
      await page.keyboard.press('Tab')
      expect(
        await panel.evaluate((node) => node.contains(document.activeElement)),
      ).toBe(true)
    }
    expect(
      await page.evaluate(
        () => getComputedStyle(document.activeElement).outlineStyle,
      ),
    ).not.toBe('none')
    const visibleControls = await panel
      .locator('input:not([type=hidden]),select,button')
      .evaluateAll((nodes) =>
        nodes
          .filter((node) => node.getClientRects().length > 0)
          .every((node) => {
            const rect = node.getBoundingClientRect()
            return rect.left >= -1 && rect.right <= innerWidth + 1
          }),
      )
    expect(visibleControls).toBe(true)
    await page.keyboard.press('Escape')
    await expect(panel).toHaveCount(0)
    await expect(editor).toBeFocused()
  }
})

test('refinement: an unavailable map SDK leaves the itinerary editable [blocked SDK or unconfigured fixture]', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.route('https://webapi.amap.com/**', (route) =>
    route.abort('failed'),
  )
  const writes = observeUserWrites(page)
  await openDemo(page)
  await expect(page.getByTestId('route-map').getByRole('status')).toContainText(
    /地图底图暂时无法加载|当前环境尚未启用地图底图/,
    { timeout: 20000 },
  )
  await page
    .getByRole('button', { name: '编辑故宫博物院', exact: true })
    .click()
  const panel = page.getByRole('region', { name: '故宫博物院', exact: true })
  await panel.getByLabel('开始时间', { exact: true }).fill('09:25')
  await panel.getByRole('button', { name: '应用修改', exact: true }).click()
  await expect(panel).toHaveCount(0, { timeout: 30000 })
  await expect(
    page.getByTestId('trip-days').getByText(/^09:25(?:–|$)/),
  ).toBeVisible()
  expect(writes.filter((action) => action === 'commands')).toHaveLength(1)
  expect(writes.filter((action) => action === 'map-renders')).toHaveLength(0)
})

test('refinement: original text loads on demand and optional or cancelled places stay outside the route', async ({
  page,
}) => {
  let sourceReads = 0
  page.on('request', (request) => {
    if (
      request.method() === 'GET' &&
      /\/source$/.test(new URL(request.url()).pathname)
    )
      sourceReads++
  })
  await page.setViewportSize({ width: 1440, height: 900 })
  await openDemo(page)
  expect(sourceReads).toBe(0)
  const optional = page
    .locator('details')
    .filter({ has: page.locator('summary', { hasText: '备选地点' }) })
  const excluded = page
    .locator('details')
    .filter({ has: page.locator('summary', { hasText: '已取消的安排' }) })
  await optional.locator('summary').click()
  await excluded.locator('summary').click()
  await expect(optional).toContainText('南锣鼓巷')
  await expect(excluded).toContainText('北京环球影城')
  const days = page.getByRole('navigation', { name: '选择行程日期' })
  for (const label of ['Day 1', 'Day 2', 'Day 3']) {
    await days.getByRole('button', { name: label, exact: true }).click()
    await expect(page.getByTestId('trip-days')).not.toContainText('南锣鼓巷')
    await expect(page.getByTestId('trip-days')).not.toContainText(
      '北京环球影城',
    )
  }
  const reference = await page.evaluate(() =>
    sessionStorage.getItem('bt_active_trip_ref'),
  )
  const map = await page.request.get(
    `/api/v3/trip-understandings/${reference}/map-renders/latest`,
  )
  expect(map.status()).toBe(200)
  expect((await map.json()).points.map((point) => point.name)).not.toEqual(
    expect.arrayContaining(['南锣鼓巷']),
  )
  expect((await map.json()).points.map((point) => point.name)).not.toEqual(
    expect.arrayContaining(['北京环球影城']),
  )
  await page.getByLabel('更多行程操作').click()
  await page.getByRole('button', { name: '查看导入文字', exact: true }).click()
  const panel = page.getByRole('region', {
    name: '导入的攻略文字',
    exact: true,
  })
  await expect(panel.locator('pre')).toContainText('北京三日慢游', {
    timeout: 10000,
  })
  expect(sourceReads).toBe(1)
  await panel.getByRole('button', { name: '返回Day 3', exact: true }).click()
  await expect(page.locator('pre.e-original-text')).toHaveCount(0)
})

test('refinement: unsigned account library explains login without pretending the list is empty', async ({
  page,
}) => {
  let listReads = 0
  await page.route('**/api/v3/me/trips**', async (route) => {
    listReads++
    await route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: '{}',
    })
  })
  await page.goto('/my-trips')
  await expect(
    page.getByRole('heading', { name: '登录，找回保存过的行程' }),
  ).toBeVisible()
  await expect(
    page.getByRole('heading', { name: '这里还没有保存的行程' }),
  ).toHaveCount(0)
  expect(listReads).toBe(0)
  await page.getByRole('button', { name: '登录并查看', exact: true }).click()
  await expect(page).toHaveURL(/\/login$/)
  expect(
    await page.evaluate(() => sessionStorage.getItem('bt_login_return')),
  ).toBe('/my-trips')
})

test('refinement: account-library failure remains retryable without a false empty state [mocked session and API]', async ({
  page,
}) => {
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'ui-boundary-only-invalid-token')
    localStorage.setItem(
      'authUser',
      JSON.stringify({ userId: 'ui-boundary-only', nickname: '界面边界测试' }),
    )
  })
  let listReads = 0
  await page.route('**/api/v3/me/trips**', async (route) => {
    listReads++
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'private-diagnostic-marker' }),
    })
  })
  await page.goto('/my-trips')
  const error = page.locator('main').getByRole('alert')
  await expect(error).toContainText('暂时无法载入行程')
  await expect(
    page.getByRole('heading', { name: '这里还没有保存的行程' }),
  ).toHaveCount(0)
  await expect(page.getByRole('list', { name: '已保存行程' })).toHaveCount(0)
  await expect(page.locator('main')).not.toContainText(
    'private-diagnostic-marker',
  )
  const beforeRetry = listReads
  await error.getByRole('button', { name: '重新载入', exact: true }).click()
  await expect.poll(() => listReads).toBeGreaterThan(beforeRetry)
  await expect(error).toContainText('已保存的内容不会因此丢失')
  await expect(
    page.getByRole('link', { name: '整理新行程', exact: true }),
  ).toBeVisible()
})

test('refinement: an accepted but failed understanding preserves the input and starts a new attempt [mocked 202 and failure]', async ({
  page,
}) => {
  const reference = 'failed-trip-ui-boundary-000001'
  expect(reference).toMatch(/^[A-Za-z0-9_-]{20,80}$/)
  const base = `/api/v3/trip-understandings/${reference}`
  const text = '北京第一天去故宫和景山，这份文字在整理失败后仍需要保留。'
  const submissions = []
  await page.route('**/api/v3/trip-understandings', async (route) => {
    submissions.push({
      body: route.request().postDataJSON(),
      key: route.request().headers()['idempotency-key'],
    })
    await route.fulfill({
      status: submissions.length === 1 ? 202 : 503,
      contentType: 'application/json',
      body: JSON.stringify(
        submissions.length === 1
          ? {
              public_resource_id: reference,
              status: 'PROCESSING',
              message: '正在整理每天行程',
              result_url: `${base}/result`,
              events_url: `${base}/events`,
            }
          : { detail: 'Controlled retry failure; no model called' },
      ),
    })
  })
  await page.route(`**${base}/**`, async (route) => {
    await route.fulfill({
      status: 409,
      contentType: 'application/json',
      body: JSON.stringify({
        detail: {
          code: 'UNDERSTANDING_FAILED',
          message: '这次没有整理完成，可以重新尝试',
        },
      }),
    })
  })
  await page.goto('/')
  const input = page.getByTestId('trip-source-text')
  await expect(input).toBeEnabled()
  await input.fill(text)
  await page.getByTestId('create-full-trip').click()
  await expect(page).toHaveURL(new RegExp(`/trip/result#trip=${reference}$`))
  await expect(
    page.getByText('这次没有整理完成，可以回到首页调整文字后重试。', {
      exact: true,
    }),
  ).toBeVisible()
  expect(
    await page.evaluate(
      () => JSON.parse(sessionStorage.getItem('bt_input_draft')).text,
    ),
  ).toBe(text)
  await page.getByRole('link', { name: '重新整理', exact: true }).click()
  await expect(input).toHaveValue(text)
  // The acknowledged failed job is terminal; this is a new attempt, unlike a lost response retry.
  await expect(page.locator('main').getByRole('alert')).toContainText(
    '上次没有完整整理成功',
  )
  await page.getByTestId('create-full-trip').click()
  await expect(page.locator('main').getByRole('alert')).toContainText(
    '文字仍在这里',
  )
  expect(submissions).toHaveLength(2)
  expect(submissions[0].body).toEqual({
    mode: 'FULL',
    source: { type: 'TEXT', text },
  })
  expect(submissions[1].body).toEqual(submissions[0].body)
  expect(submissions[1].key).toBeTruthy()
  expect(submissions[1].key).not.toBe(submissions[0].key)
  await expect(input).toHaveValue(text)
})

test('refinement: an expired result clears its pending operation and allows a new home submission [mocked 410]', async ({
  page,
}) => {
  const reference = 'expired-trip-ui-boundary-00001'
  expect(reference).toMatch(/^[A-Za-z0-9_-]{20,80}$/)
  const text = '杭州两天，第一天游览西湖。过期行程不应阻止整理这份新文字。'
  const submissions = []
  const resourceWrites = []
  await page.route(
    `**/api/v3/trip-understandings/${reference}/**`,
    async (route) => {
      if (route.request().method() !== 'GET')
        resourceWrites.push(route.request().method())
      await route.fulfill({
        status: 410,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: { code: 'RESOURCE_GONE', message: '这份行程已过期' },
        }),
      })
    },
  )
  await page.route('**/api/v3/trip-understandings', async (route) => {
    submissions.push(route.request().postDataJSON())
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Controlled UI-only submission failure' }),
    })
  })
  await page.goto('/')
  await expect(page.getByTestId('trip-source-text')).toBeEnabled()
  // Seed once, not in addInitScript: a later navigation must not recreate the deleted pending operation.
  await page.evaluate(
    ({ reference, text }) => {
      sessionStorage.setItem('bt_active_trip_ref', reference)
      sessionStorage.setItem('bt_active_trip_mode', 'FULL')
      sessionStorage.setItem(
        'bt_pending_operation',
        JSON.stringify({
          type: 'command',
          resource: reference,
          etag: 'ui-pending-etag',
          key: 'ui-pending-command',
          command: { command_type: 'UNDO' },
        }),
      )
      sessionStorage.setItem(
        'bt_input_draft',
        JSON.stringify({
          text,
          demo: false,
          key: 'ui-new-home-attempt',
          expires: Date.now() + 86400000,
        }),
      )
    },
    { reference, text },
  )
  await page.goto(`/trip/result#trip=${reference}`)
  await expect(
    page.getByRole('heading', { name: '这份行程已无法打开', exact: true }),
  ).toBeVisible()
  expect(
    await page.evaluate(() => sessionStorage.getItem('bt_pending_operation')),
  ).toBeNull()
  expect(
    await page.evaluate(() => sessionStorage.getItem('bt_active_trip_ref')),
  ).toBeNull()
  expect(resourceWrites).toEqual([])
  await page.getByRole('link', { name: '重新整理', exact: true }).click()
  await expect(page.getByTestId('trip-source-text')).toHaveValue(text)
  await page.getByTestId('create-full-trip').click()
  await expect(page.locator('main').getByRole('alert')).toContainText(
    '文字仍在这里',
  )
  expect(submissions).toEqual([
    { mode: 'FULL', source: { type: 'TEXT', text } },
  ])
  expect(
    await page.evaluate(() => sessionStorage.getItem('bt_pending_operation')),
  ).toBeNull()
})
