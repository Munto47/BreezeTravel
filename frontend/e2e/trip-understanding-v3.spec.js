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
  expect(result.map.status).toBe('UNAVAILABLE')
  expect(result.stay.status).toBe('UNAVAILABLE')

  await expect(page).toHaveURL(/\/trip\/result$/)
  await expect(page.getByTestId('trip-days').locator('section')).toHaveCount(3)
  for (const names of EXPECTED_DAYS) {
    for (const name of names) await expect(page.getByRole('heading', { name })).toBeVisible()
  }
  const visibleText = await page.locator('body').innerText()
  expect(visibleText).not.toContain(accepted.public_resource_id)
  expect(visibleText).not.toMatch(/原文映射|source span|confidence|Provider|revision|receipt|RunSpec|Audit|Repair|postcheck/i)
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
})
