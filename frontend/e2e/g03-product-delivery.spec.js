const { expect, test } = require('@playwright/test')


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
  'uid',
  'hash',
  'revision',
  'receipt',
  'run',
  'stage',
  'plan_ref',
  'job',
  'snapshot',
  'evidence',
  'audit',
  'repair',
  'postcheck',
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


test('text to cards, map, stay, top-3, preview, adopt and full recheck is public-safe', async ({ page, request }) => {
  const browserRouteProviderCalls = []
  page.on('request', (browserRequest) => {
    const url = new URL(browserRequest.url())
    if (url.hostname.endsWith('amap.com')) browserRouteProviderCalls.push(url.href)
  })

  const login = await request.post('/api/auth/test-login')
  expect(login.ok()).toBe(true)
  const auth = await login.json()
  await page.addInitScript(({ token, userId, nickname }) => {
    localStorage.setItem('authToken', token)
    localStorage.setItem('authUser', JSON.stringify({ userId, nickname }))
  }, { token: auth.token, userId: auth.user_id, nickname: auth.nickname })

  await page.goto('/')
  await page.getByTestId('trip-source-text').fill([
    '北京三日自由行，没有写真实日历日期。',
    'Day 1 上午故宫博物院，下午景山公园。',
    'Day 2 上午天坛公园，下午前门大街。',
    'Day 3 上午颐和园，下午圆明园。',
  ].join('\n'))
  const createResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/v3/trip-understandings'
  ))
  await page.getByTestId('create-full-trip').click()
  const created = await createResponse
  expect(created.status()).toBe(202)
  const accepted = await created.json()
  const resourcePath = `/api/v3/trip-understandings/${accepted.public_resource_id}`

  await expect(page).toHaveURL(/\/trip\/result$/)
  await expect(page.getByTestId('trip-days')).toBeVisible({ timeout: 30_000 })
  await page.getByTestId('desktop-nav-map_stay').click()
  await expect(page.getByTestId('map-theater').locator('svg[role="img"]')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByTestId('stay-candidate')).toHaveCount(3, { timeout: 30_000 })

  const selectedName = await page.getByTestId('stay-candidate').first().locator('h3').innerText()
  const selectResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/stay-selection')
  ))
  await page.getByTestId('choose-stay').first().click()
  expect((await selectResponse).status()).toBe(200)
  await expect(page.getByText(`整程住宿已选择：${selectedName}`)).toBeVisible()

  await page.getByTestId('desktop-nav-checks').click()
  await expect(page.getByTestId('trip-check-item')).toHaveCount(3, { timeout: 30_000 })
  await expect(page.getByTestId('trip-check-item').first().getByText('可以更好')).toBeVisible()
  const checksResponse = await page.request.get(`${resourcePath}/checks`, {
    headers: { Authorization: `Bearer ${auth.token}` },
  })
  expect(checksResponse.status()).toBe(200)
  const checks = await checksResponse.json()
  expect(checks.items).toHaveLength(3)
  expect(checks.items.every((item) => item.can_preview)).toBe(true)
  expect(collectKeys(checks).filter((key) => FORBIDDEN_PUBLIC_KEYS.has(key))).toEqual([])

  const renderResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/map-renders')
  ))
  await page.getByTestId('desktop-nav-map_stay').click()
  await page.getByTestId('render-map').click()
  expect((await renderResponse).status()).toBe(202)
  await expect(page.getByTestId('map-theater').getByText('路线已准备，可以切换步行或公交查看。')).toBeVisible({ timeout: 30_000 })

  const previewResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/changes/preview')
  ))
  await page.getByTestId('desktop-nav-checks').click()
  await page.getByTestId('preview-change').first().click()
  const previewHttp = await previewResponse
  expect(previewHttp.status()).toBe(200)
  const preview = await previewHttp.json()
  expect(collectKeys(preview).filter((key) => FORBIDDEN_PUBLIC_KEYS.has(key))).toEqual([])
  await expect(page.getByTestId('change-preview')).toBeVisible()
  await expect(page.getByText('12:30 预留午餐时间，具体餐厅仍可稍后选择')).toBeVisible()

  const adoptResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/changes/adopt')
  ))
  await page.getByTestId('adopt-change').click()
  const adoptedHttp = await adoptResponse
  expect(adoptedHttp.status()).toBe(200)
  const adopted = await adoptedHttp.json()
  expect(adopted.status).toBe('STILL_NEEDS_CONFIRMATION')
  expect(adopted.map_readiness).toBe('NEEDS_UPDATE')
  expect(collectKeys(adopted).filter((key) => FORBIDDEN_PUBLIC_KEYS.has(key))).toEqual([])

  await expect(page.getByText('改动已保存，完整复核后仍有内容需要确认')).toBeVisible()
  await expect(page.getByTestId('change-preview')).toHaveCount(0)
  await page.getByTestId('desktop-nav-itinerary').click()
  await expect(page.getByRole('heading', { name: '午餐时间' })).toBeVisible()
  await expect(page.getByTestId('itinerary-workspace').getByText('行程已修改，路线尚未更新')).toBeVisible()

  const resultResponse = await page.request.get(`${resourcePath}/result`, {
    headers: { Authorization: `Bearer ${auth.token}` },
  })
  const result = await resultResponse.json()
  expect(result.map.status).toBe('NEEDS_UPDATE')
  expect(result.days[0].activities.some((item) => item.name === '午餐时间')).toBe(true)
  expect(collectKeys(result).filter((key) => FORBIDDEN_PUBLIC_KEYS.has(key))).toEqual([])

  const publicText = await page.locator('body').innerText()
  expect(publicText).not.toContain(accepted.public_resource_id)
  expect(publicText).not.toMatch(/source span|confidence|Provider|revision|receipt|RunSpec|Evidence|Audit|Repair|Postcheck|UNKNOWN/i)
  expect(browserRouteProviderCalls).toEqual([])
})
