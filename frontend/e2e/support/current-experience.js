const { expect, test } = require('@playwright/test')
const { randomUUID } = require('node:crypto')

async function openDemo(page) {
  if (!test.info().title.includes('@live')) {
    // The ordinary suites exercise fixed server routes; SDK/POI networking is
    // reserved for their explicitly labelled live map/candidate tests.
    await page.route('**/webapi.amap.com/**', route => route.abort())
    await page.route('**/restapi.amap.com/**', route => route.abort())
  }
  // The public examples now submit real FULL/TEXT. Seed these deterministic
  // editing tests explicitly through the real anonymous DEMO API instead.
  // page.request shares the browser cookie jar; no capability or result is faked.
  const response = await page.request.post('/api/v3/trip-understandings', {
    headers: {'Idempotency-Key': randomUUID()}, data: {mode: 'DEMO'},
  })
  expect(response.status()).toBe(202)
  const accepted = await response.json()
  expect(accepted.public_resource_id).toMatch(/^[A-Za-z0-9_-]{20,80}$/)
  expect(accepted.status).toBe('PROCESSING')
  await page.goto(`/trip/result#trip=${accepted.public_resource_id}`)
  await expect(page).toHaveURL(/\/trip\/result#trip=/)
  await expect(page.getByTestId('activity-card').first()).toContainText('故宫博物院', { timeout: 60000 })
  await expect(page.getByTestId('drag-handle-1-0')).toBeEnabled()
  const result = await readResult(page)
  expect(result.body.is_demo).toBe(true)
  expect(result.body.ownership).toBe('ANONYMOUS')
  expect(result.etag).toBeTruthy()
  return accepted.public_resource_id
}

async function view(page, name) {
  const prefix = page.viewportSize().width < 1024 ? 'mobile' : 'desktop'
  await page.getByTestId(`${prefix}-nav-${name}`).click()
}

async function moveFirst(page) {
  const handle = page.getByTestId('drag-handle-1-0')
  await expect(handle).toBeEnabled()
  await handle.press('Enter')
  await page.keyboard.press('ArrowRight')
  await page.keyboard.press('Enter')
}

async function expectFirst(page, name) {
  await expect(page.getByTestId('activity-card').first().getByRole('heading')).toHaveText(name, { timeout: 30000 })
  await expect(page.getByTestId('drag-handle-1-0')).toBeEnabled()
}

async function readResult(page) {
  return page.evaluate(async () => {
    const reference = sessionStorage.getItem('bt_active_trip_ref')
    const token = localStorage.getItem('authToken')
    const response = await fetch(`/api/v3/trip-understandings/${encodeURIComponent(reference)}/result`, {
      credentials: 'include', cache: 'no-store', headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!response.ok) throw new Error('Owned result read failed')
    return { etag: response.headers.get('etag'), body: await response.json() }
  })
}

async function openSuggestions(page) {
  const toggle = page.getByTestId('journey-suggestions-toggle')
  if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click()
}

module.exports = { openDemo, view, moveFirst, expectFirst, readResult, openSuggestions }
