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
  'hash',
  'revision',
  'receipt',
  'run',
  'stage',
  'plan_ref',
  'job',
  'snapshot',
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


test('map theater and one-stay journey remain manual, current and publicly redacted', async ({ page }) => {
  const mapRenderPosts = []
  const browserRouteProviderCalls = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.hostname.endsWith('amap.com')) browserRouteProviderCalls.push(request.url())
    if (
      request.method() === 'POST'
      && /\/api\/v3\/trip-understandings\/[^/]+\/map-renders$/.test(url.pathname)
    ) mapRenderPosts.push(request.url())
  })

  await page.goto('/')
  const createdPromise = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/v3/trip-understandings'
  ))
  await page.getByTestId('start-demo').click()
  const created = await createdPromise
  expect(created.status()).toBe(202)
  const accepted = await created.json()

  await expect(page).toHaveURL(/\/trip\/result$/)
  await expect(page.getByTestId('map-theater')).toBeVisible()
  await expect(page.getByTestId('stay-panel')).toBeVisible()
  await expect(page.getByTestId('stay-candidate')).toHaveCount(3, { timeout: 30_000 })
  await expect(page.getByTestId('map-theater').locator('svg[role="img"]')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByTestId('map-mode-walking')).toBeVisible()
  await page.getByTestId('map-mode-transit').click()
  await expect(page.getByTestId('map-theater').locator('svg[aria-label="公交路线图"]')).toBeVisible()
  await page.getByTestId('map-mode-walking').click()
  await expect(page.getByTestId('map-theater').locator('svg[aria-label="步行路线图"]')).toBeVisible()

  const resourcePath = `/api/v3/trip-understandings/${accepted.public_resource_id}`
  const initialResultResponse = await page.request.get(`${resourcePath}/result`)
  const initialMapResponse = await page.request.get(`${resourcePath}/map-renders/latest`)
  const initialStayResponse = await page.request.get(`${resourcePath}/stay-suggestions`)
  expect(initialResultResponse.status()).toBe(200)
  expect(initialMapResponse.status()).toBe(200)
  expect(initialStayResponse.status()).toBe(200)
  for (const response of [initialResultResponse, initialMapResponse, initialStayResponse]) {
    const payload = await response.json()
    expect(collectKeys(payload).filter((key) => FORBIDDEN_PUBLIC_KEYS.has(key))).toEqual([])
  }
  const stayPayload = await initialStayResponse.json()
  expect(stayPayload.candidates.length).toBeLessThanOrEqual(3)
  expect(stayPayload.candidates.every((candidate) => candidate.brand)).toBe(true)

  const selectedName = await page.getByTestId('stay-candidate').first().locator('h3').innerText()
  const selectionPromise = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && /\/stay-selection$/.test(new URL(response.url()).pathname)
  ))
  await page.getByTestId('choose-stay').first().click()
  expect((await selectionPromise).status()).toBe(200)
  await expect(page.getByText(`整程住宿已选择：${selectedName}`)).toBeVisible()
  await expect(page.getByText('行程已修改，路线尚未更新')).toBeVisible()
  expect(mapRenderPosts).toHaveLength(0)

  const renderPromise = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && /\/map-renders$/.test(new URL(response.url()).pathname)
  ))
  await page.getByTestId('render-map').click()
  expect((await renderPromise).status()).toBe(202)
  expect(mapRenderPosts).toHaveLength(1)
  await expect(page.getByText('步行和公交路线已准备，出发前请再核对实时情况')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText(new RegExp(`^${selectedName} →`)).first()).toBeVisible()
  await expect(page.getByText(new RegExp(`→ ${selectedName}$`)).first()).toBeVisible()

  const refreshedMapResponse = await page.request.get(`${resourcePath}/map-renders/latest`)
  const refreshedMap = await refreshedMapResponse.json()
  expect(refreshedMap.status).toBe('AVAILABLE')
  const overnightRoutes = refreshedMap.days.slice(0, 2).flatMap((day) => day.routes)
  expect(overnightRoutes.filter((route) => route.from_name === selectedName)).toHaveLength(2)
  expect(overnightRoutes.filter((route) => route.to_name === selectedName)).toHaveLength(2)
  expect(collectKeys(refreshedMap).filter((key) => FORBIDDEN_PUBLIC_KEYS.has(key))).toEqual([])

  const commandPromise = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && /\/commands$/.test(new URL(response.url()).pathname)
  ))
  await page.getByRole('button', { name: '下移 故宫博物院' }).click()
  expect((await commandPromise).status()).toBe(200)
  await expect(page.getByText('行程已修改，路线尚未更新')).toBeVisible()
  await expect(page.getByText(`整程住宿已选择：${selectedName}`)).toBeVisible()
  expect(mapRenderPosts).toHaveLength(1)

  const finalResultResponse = await page.request.get(`${resourcePath}/result`)
  const finalResult = await finalResultResponse.json()
  expect(finalResult.map.status).toBe('NEEDS_UPDATE')
  expect(finalResult.stay.candidates[0].selected).toBe(true)
  expect(collectKeys(finalResult).filter((key) => FORBIDDEN_PUBLIC_KEYS.has(key))).toEqual([])
  const publicText = await page.locator('body').innerText()
  expect(publicText).not.toContain(accepted.public_resource_id)
  expect(publicText).not.toMatch(/source span|confidence|Provider|revision|receipt|RunSpec|Evidence|Audit|Repair|Postcheck|UNKNOWN/i)
  expect(browserRouteProviderCalls).toEqual([])
})
