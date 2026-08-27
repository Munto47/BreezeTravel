const { expect, test } = require('@playwright/test')


const EXPECTED_DAYS = [
  ['故宫博物院', '景山公园'],
  ['天坛公园', '前门大街'],
  ['颐和园', '圆明园'],
]

const FORBIDDEN_PUBLIC_KEYS = new Set([
  'raw_text',
  'source',
  'source_id',
  'span',
  'span_start',
  'span_end',
  'offset',
  'confidence',
  'model',
  'provider',
  'uuid',
  'hash',
  'revision',
  'receipt',
  'run',
  'stage',
])


function collectKeys(value, keys = []) {
  if (Array.isArray(value)) {
    value.forEach((item) => collectKeys(item, keys))
  } else if (value && typeof value === 'object') {
    Object.entries(value).forEach(([key, child]) => {
      keys.push(key.toLowerCase())
      collectKeys(child, keys)
    })
  }
  return keys
}


test('anonymous Beijing demo uses the durable v3 create, events and result chain', async ({ browser, page }) => {
  const apiPaths = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.pathname.startsWith('/api/')) apiPaths.push(url.pathname)
  })

  await page.goto('/')
  await expect(page.getByRole('heading', { name: /把攻略变成/ })).toBeVisible()
  await expect(page.locator('main input')).toHaveCount(0)
  await expect(page.locator('a[href*="/intake"], a[href*="/import"], a[href*="/room"], a[href*="/workspace"]')).toHaveCount(0)

  const createdPromise = page.waitForResponse((response) => {
    const request = response.request()
    return request.method() === 'POST' && new URL(response.url()).pathname === '/api/v3/trip-understandings'
  })
  const readyPromise = page.waitForResponse((response) => {
    const path = new URL(response.url()).pathname
    return response.status() === 200 && /\/api\/v3\/trip-understandings\/[^/]+\/result$/.test(path)
  })

  await page.getByTestId('start-demo').click()
  const createdResponse = await createdPromise
  expect(createdResponse.status()).toBe(202)
  const accepted = await createdResponse.json()
  const resultResponse = await readyPromise
  const result = await resultResponse.json()

  expect(Object.keys(result).sort()).toEqual(
    ['status', 'assumptions', 'days', 'map', 'stay', 'available_actions'].sort(),
  )
  expect(collectKeys(result).filter((key) => FORBIDDEN_PUBLIC_KEYS.has(key))).toEqual([])
  expect(result.days.map((day) => day.activities.map((activity) => activity.name))).toEqual(EXPECTED_DAYS)
  expect(['PREPARING', 'AVAILABLE']).toContain(result.map.status)
  expect(result.stay.status).toBe('UNAVAILABLE')

  await expect(page).toHaveURL(/\/trip\/result$/)
  await expect(page.getByTestId('trip-days').locator('section')).toHaveCount(3)
  for (const names of EXPECTED_DAYS) {
    for (const name of names) await expect(page.getByRole('heading', { name })).toBeVisible()
  }
  await expect(page.getByText('步行和公交路线已准备，出发前请再核对实时情况')).toBeVisible()
  const finalResultResponse = await page.request.get(new URL(accepted.result_url, page.url()).toString())
  expect(finalResultResponse.status()).toBe(200)
  const finalResult = await finalResultResponse.json()
  expect(finalResult.map.status).toBe('AVAILABLE')
  expect(collectKeys(finalResult).filter((key) => FORBIDDEN_PUBLIC_KEYS.has(key))).toEqual([])
  const mapResponse = await page.request.get(
    new URL(`/api/v3/trip-understandings/${accepted.public_resource_id}/map-renders/latest`, page.url()).toString(),
  )
  expect(mapResponse.status()).toBe(200)
  const mapResult = await mapResponse.json()
  expect(mapResult.status).toBe('AVAILABLE')
  expect(mapResult.days.flatMap((day) => day.routes.map((route) => route.selected_mode))).toEqual([
    'walking',
    'transit',
    'transit',
  ])
  expect(collectKeys(mapResult).filter((key) => FORBIDDEN_PUBLIC_KEYS.has(key))).toEqual([])
  const visibleText = await page.locator('body').innerText()
  expect(visibleText).not.toContain(accepted.public_resource_id)
  expect(visibleText).not.toMatch(/原文映射|source span|confidence|Provider|revision|receipt|RunSpec|Audit|Repair|postcheck|UNKNOWN|自动验证/i)
  expect(apiPaths.length).toBeGreaterThan(0)
  expect(apiPaths.every((path) => path.startsWith('/api/v3/trip-understandings'))).toBe(true)

  await page.reload()
  await expect(page.getByTestId('trip-days').locator('section')).toHaveCount(3)
  await expect(page.getByRole('heading', { name: '故宫博物院' })).toBeVisible()
  expect(await page.locator('body').innerText()).not.toContain(accepted.public_resource_id)

  const isolatedContext = await browser.newContext()
  const denied = await isolatedContext.request.get(new URL(accepted.result_url, page.url()).toString())
  expect(denied.status()).toBe(404)
  await isolatedContext.close()

  const login = await page.request.post('/api/auth/test-login')
  expect(login.ok()).toBe(true)
  const auth = await login.json()
  await page.evaluate(({ token, userId, nickname }) => {
    localStorage.setItem('authToken', token)
    localStorage.setItem('authUser', JSON.stringify({ userId, nickname }))
  }, { token: auth.token, userId: auth.user_id, nickname: auth.nickname })
  await page.reload()
  await expect(page.getByTestId('claim-demo-trip')).toBeVisible()
  const claimResponsePromise = page.waitForResponse((response) => {
    const requestData = response.request()
    return requestData.method() === 'POST'
      && /\/api\/v3\/trip-understandings\/[^/]+\/claim$/.test(new URL(response.url()).pathname)
  })
  await page.getByTestId('claim-demo-trip').click()
  const claimResponse = await claimResponsePromise
  expect(claimResponse.status()).toBe(200)
  const claimed = await claimResponse.json()
  expect(claimed.public_resource_id).not.toBe(accepted.public_resource_id)
  await expect(page.getByText('已保存到你的账号，匿名访问凭证已经失效。')).toBeVisible()
  expect(await page.locator('body').innerText()).not.toContain(claimed.public_resource_id)

  const oldReadback = await page.request.get(new URL(accepted.result_url, page.url()).toString(), {
    headers: { Authorization: `Bearer ${auth.token}` },
  })
  expect(oldReadback.status()).toBe(410)

  page.once('dialog', (dialog) => dialog.accept())
  const deleteResponsePromise = page.waitForResponse((response) => {
    const requestData = response.request()
    return requestData.method() === 'DELETE'
      && /\/api\/v3\/trip-understandings\/[^/]+$/.test(new URL(response.url()).pathname)
  })
  await page.getByTestId('delete-entire-trip').click()
  expect((await deleteResponsePromise).status()).toBe(204)
  await expect(page.getByTestId('trip-deleted')).toBeVisible()
  expect(await page.locator('body').innerText()).not.toContain(claimed.public_resource_id)
})


test('logged-in text creates user-owned cards through the durable FULL chain', async ({ page, request }) => {
  const login = await request.post('/api/auth/test-login')
  expect(login.ok()).toBe(true)
  const auth = await login.json()
  await page.addInitScript(({ token, userId, nickname }) => {
    localStorage.setItem('authToken', token)
    localStorage.setItem('authUser', JSON.stringify({ userId, nickname }))
  }, { token: auth.token, userId: auth.user_id, nickname: auth.nickname })

  const sourceText = [
    '北京三日行程',
    'Day 1：故宫博物院、景山公园。',
    'Day 2：天坛公园、前门大街。',
    'Day 3：颐和园、圆明园。',
    '有空可以考虑南锣鼓巷，不去上海迪士尼乐园。',
    '预约说明：https://example.com/booking',
  ].join('\n')

  await page.goto('/')
  await expect(page.getByTestId('trip-source-text')).toBeVisible()
  await page.getByTestId('trip-source-text').fill(sourceText)
  const createdPromise = page.waitForResponse((response) => {
    const requestData = response.request()
    return requestData.method() === 'POST'
      && new URL(response.url()).pathname === '/api/v3/trip-understandings'
      && requestData.postDataJSON()?.mode === 'FULL'
  })
  const readyPromise = page.waitForResponse((response) => {
    const path = new URL(response.url()).pathname
    return response.status() === 200 && /\/api\/v3\/trip-understandings\/[^/]+\/result$/.test(path)
  })

  await page.getByTestId('create-full-trip').click()
  const created = await createdPromise
  expect(created.status()).toBe(202)
  const accepted = await created.json()
  const result = await (await readyPromise).json()
  expect(result.status).toBe('READY')
  expect(result.days.map((day) => day.activities.map((activity) => activity.name))).toEqual(EXPECTED_DAYS)
  expect(collectKeys(result).filter((key) => FORBIDDEN_PUBLIC_KEYS.has(key))).toEqual([])

  await expect(page).toHaveURL(/\/trip\/result$/)
  await expect(page.getByTestId('trip-days').locator('section')).toHaveCount(3)
  await expect(page.getByText('步行和公交路线已准备，出发前请再核对实时情况')).toBeVisible()
  const visibleText = await page.locator('body').innerText()
  expect(visibleText).not.toContain(accepted.public_resource_id)
  expect(visibleText).not.toContain(sourceText)
  expect(visibleText).not.toContain('南锣鼓巷')
  expect(visibleText).not.toContain('上海迪士尼乐园')

  const applyVisibleCommand = async (action) => {
    const commandResponse = page.waitForResponse((response) => {
      const requestData = response.request()
      return requestData.method() === 'POST'
        && /\/api\/v3\/trip-understandings\/[^/]+\/commands$/.test(new URL(response.url()).pathname)
    })
    const refreshedResult = page.waitForResponse((response) => {
      const path = new URL(response.url()).pathname
      return response.status() === 200 && /\/api\/v3\/trip-understandings\/[^/]+\/result$/.test(path)
    })
    await action()
    expect((await commandResponse).status()).toBe(200)
    await refreshedResult
  }

  await applyVisibleCommand(() => page.getByRole('button', { name: '下移 故宫博物院' }).click())
  await expect(page.getByTestId('trip-days').locator('section').nth(0).locator('h3')).toHaveText([
    '景山公园',
    '故宫博物院',
  ])

  await page.getByRole('button', { name: '新增地点到 Day 1' }).click()
  await page.getByTestId('card-editor-name').fill('北海公园')
  await applyVisibleCommand(() => page.getByTestId('save-card-editor').click())
  await expect(page.getByRole('heading', { name: '北海公园' })).toBeVisible()

  await page.getByRole('heading', { name: '景山公园' }).click()
  await page.getByRole('button', { name: '编辑文字' }).click()
  await page.getByTestId('card-editor-name').fill('景山公园东门')
  await applyVisibleCommand(() => page.getByTestId('save-card-editor').click())
  await expect(page.getByRole('heading', { name: '景山公园东门' })).toBeVisible()

  await page.getByRole('heading', { name: '北海公园' }).click()
  page.once('dialog', (dialog) => dialog.accept())
  await applyVisibleCommand(() => page.getByRole('button', { name: '删除这张卡片' }).click())
  await expect(page.getByRole('heading', { name: '北海公园' })).toHaveCount(0)
  await expect(page.getByText('行程已修改，路线尚未更新')).toBeVisible()

  await page.reload()
  await expect(page.getByRole('heading', { name: '景山公园东门' })).toBeVisible()
  await expect(page.getByTestId('trip-days').locator('section').nth(0).locator('h3')).toHaveText([
    '景山公园东门',
    '故宫博物院',
  ])
  expect(await page.locator('body').innerText()).not.toContain(accepted.public_resource_id)

  page.once('dialog', (dialog) => dialog.accept())
  const sourceDeletePromise = page.waitForResponse((response) => {
    const requestData = response.request()
    return requestData.method() === 'DELETE'
      && /\/api\/v3\/trip-understandings\/[^/]+\/source$/.test(new URL(response.url()).pathname)
  })
  await page.getByTestId('delete-trip-source').click()
  expect((await sourceDeletePromise).status()).toBe(204)
  await expect(page.getByText('原文已永久删除，逐日卡片仍可继续查看和调整。')).toBeVisible()
  await expect(page.getByRole('heading', { name: '景山公园东门' })).toBeVisible()

  page.once('dialog', (dialog) => dialog.accept())
  await page.getByTestId('delete-entire-trip').click()
  await expect(page.getByTestId('trip-deleted')).toBeVisible()
})


test('freshly authenticated account can clear all v3 travel data with confirmed readback', async ({ page, request }) => {
  const suffix = `${Date.now()}-${Math.random().toString(16).slice(2)}`
  const register = await request.post('/api/auth/email-register', {
    data: {
      email: `g01-privacy-${suffix}@example.com`,
      password: 'BreezeTravel-test-2026!',
      nickname: '隐私链测试',
    },
  })
  expect(register.ok()).toBe(true)
  const auth = await register.json()
  await page.addInitScript(({ token, userId, nickname }) => {
    localStorage.setItem('authToken', token)
    localStorage.setItem('authUser', JSON.stringify({ userId, nickname }))
  }, { token: auth.token, userId: auth.user_id, nickname: auth.nickname })

  await page.goto('/')
  await page.getByTestId('trip-source-text').fill('Day 1 去故宫博物院。Day 2 去天坛公园。')
  const createdPromise = page.waitForResponse((response) => {
    const requestData = response.request()
    return requestData.method() === 'POST'
      && new URL(response.url()).pathname === '/api/v3/trip-understandings'
      && requestData.postDataJSON()?.mode === 'FULL'
  })
  await page.getByTestId('create-full-trip').click()
  const created = await createdPromise
  expect(created.status()).toBe(202)
  const accepted = await created.json()
  await expect(page.getByTestId('trip-days')).toBeVisible()

  await page.goto('/profile')
  await expect(page.getByTestId('open-account-travel-delete')).toBeVisible()
  await page.getByTestId('open-account-travel-delete').click()
  await page.getByTestId('account-travel-delete-confirmation').fill('清空全部旅行数据')
  const deletePromise = page.waitForResponse((response) => {
    const requestData = response.request()
    return requestData.method() === 'DELETE'
      && new URL(response.url()).pathname === '/api/v3/me/travel-data'
  })
  const statusPromise = page.waitForResponse((response) => (
    response.request().method() === 'GET'
    && new URL(response.url()).pathname === '/api/v3/me/travel-data-deletion'
  ))
  await page.getByTestId('confirm-account-travel-delete').click()
  expect((await deletePromise).status()).toBe(202)
  expect((await statusPromise).status()).toBe(200)
  await expect(page.getByTestId('account-travel-delete-status')).toHaveText('旅行数据已清空')

  const deletedReadback = await request.get(new URL(accepted.result_url, page.url()).toString(), {
    headers: { Authorization: `Bearer ${auth.token}` },
  })
  expect(deletedReadback.status()).toBe(410)
  expect(await page.evaluate(() => sessionStorage.getItem('bt_active_trip_ref'))).toBeNull()
  const visibleText = await page.locator('body').innerText()
  expect(visibleText).not.toContain(accepted.public_resource_id)
  expect(visibleText).not.toMatch(/receipt|revision|Provider|deletion_job|understanding_id/i)
})
