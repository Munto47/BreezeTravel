const { expect } = require('@playwright/test')
const { randomUUID } = require('node:crypto')

async function openDemo(page) {
  await page.goto('/')
  await expect(page.getByTestId('start-demo')).toBeEnabled({ timeout: 15000 })
  await page.getByTestId('start-demo').click()
  await expect(page.getByTestId('trip-source-text')).toHaveValue(/北京三日慢游/)
  await page.getByTestId('create-full-trip').click()
  await expect(page).toHaveURL(/\/trip\/result#trip=/)
  await expect(page.getByTestId('activity-card').first()).toContainText('故宫博物院', { timeout: 60000 })
  await expect(page.getByTestId('drag-handle-1-0')).toBeEnabled()
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

// Explicit API setup for time-conflict scenarios: time inputs are no longer on cards.
// The preview/adoption and concurrency assertions still use the real current UI/API.
async function setTime(page, index, start, end) {
  const { etag, body } = await readResult(page)
  const reference = await page.evaluate(() => sessionStorage.getItem('bt_active_trip_ref'))
  const minutes = value => Number(value.slice(0, 2)) * 60 + Number(value.slice(3))
  const response = await page.request.post(`/api/v3/trip-understandings/${reference}/commands`, {
    headers: { 'Idempotency-Key': randomUUID(), 'If-Match': etag },
    data: { command_type: 'ACTIVITY_TIME_SET', activity_token: body.days[0].activities[index].activity_token,
      start_time: start, ...(end ? { end_time: end, visit_duration_minutes: minutes(end) - minutes(start) } : {}) },
  })
  expect(response.status()).toBe(200)
}

async function openSuggestions(page) {
  const toggle = page.getByTestId('journey-suggestions-toggle')
  if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click()
}

module.exports = { openDemo, view, moveFirst, expectFirst, readResult, setTime, openSuggestions }
