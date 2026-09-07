const { test, expect } = require('@playwright/test')

const API = '/api/v3/trip-understandings'
const RESOURCE = 'synthetic-create-retry-trip-00001'
const OWNER_COOKIE = 'synthetic-create-retry-owner'
const TEXT = '上海两日游。Day 1：外滩、豫园。Day 2：武康大楼、思南公馆。'
const UNAVAILABLE = '整理服务暂时不可用，文字仍在这里。请稍后重试，重试会确认同一次请求。'
const UNKNOWN = '暂时没有收到整理结果，文字仍在这里。重试会确认同一次请求。'
const LIMITED = '当前体验次数已用完，或已有行程正在整理。可以继续已有行程，稍后再来。'

// The fixture models anonymous creation and cookie-authorized progress reads.
// A synthetic 202 acknowledges acceptance only, never successful generation.
async function installFixture(page, firstResponse) {
  const state = { submissions: [], progressReads: 0, unauthorizedReads: 0, unexpected: [] }
  const progress = {
    status: 'PROCESSING', message: '已收到文字，正在整理日期。',
    retry_after_ms: 1500, phase: 'RECEIVED', event_cursor: 1,
    progress: { day_count: 0, card_count: 0, places_checked: 0, places_total: 0 },
    snapshot: null,
  }
  await page.route('**/api/**', async route => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (pathname === '/api/user/me') return route.fulfill({ status: 401, json: {} })
    if (pathname === API && request.method() === 'POST') {
      state.submissions.push({ body: request.postDataJSON(), key: request.headers()['idempotency-key'] })
      if (state.submissions.length === 1) {
        if (firstResponse === 'TIMEOUT') return route.abort('timedout')
        if (firstResponse === 'INVALID_JSON')
          return route.fulfill({ status: 202, contentType: 'application/json', body: '{' })
        return route.fulfill({ status: firstResponse, contentType: 'text/plain', body: 'Synthetic service response' })
      }
      return route.fulfill({
        status: 202,
        headers: { 'Set-Cookie': `bt_demo_capability=${OWNER_COOKIE}; Path=${API}; HttpOnly; SameSite=Lax` },
        json: {
          public_resource_id: RESOURCE, status: 'PROCESSING', message: progress.message,
          result_url: `${API}/${RESOURCE}/result`, events_url: `${API}/${RESOURCE}/events`,
        },
      })
    }
    if (pathname.startsWith(`${API}/${RESOURCE}/`)) {
      if (!request.headers().cookie?.split(';').some(item => item.trim() === `bt_demo_capability=${OWNER_COOKIE}`)) {
        state.unauthorizedReads++
        return route.fulfill({ status: 404, json: {} })
      }
      if (request.method() === 'GET' && pathname.endsWith('/result')) {
        state.progressReads++
        return route.fulfill({ status: 202, json: progress })
      }
      if (request.method() === 'GET' && pathname.endsWith('/events'))
        return route.fulfill({ status: 200, contentType: 'text/event-stream', body: `id: 1\nevent: progress\ndata: ${JSON.stringify(progress)}\n\n` })
    }
    state.unexpected.push(`${request.method()} ${pathname}`)
    return route.fulfill({ status: 404, json: {} })
  })
  return state
}

async function submitAndObserveFailure(page, state, expectedMessage, { demo = false } = {}) {
  await page.goto('/')
  const input = page.getByTestId('trip-source-text')
  await expect(input).toBeEnabled()
  if (demo) await page.getByTestId('start-demo').click()
  else await input.fill(TEXT)
  const text = await input.inputValue()
  await page.getByTestId('create-full-trip').click()
  await expect(page.locator('main').getByRole('alert')).toHaveText(expectedMessage)
  await expect(input).toHaveValue(text)
  await expect(input).toBeEnabled()
  await expect(page.getByTestId('create-full-trip')).toBeEnabled()
  expect(state.submissions).toHaveLength(1)
  const draft = await page.evaluate(() => JSON.parse(sessionStorage.getItem('bt_input_draft')))
  expect(draft.text).toBe(text)
  expect(draft.key).toBe(state.submissions[0].key)
  expect(draft.key).toMatch(/^[a-f0-9]{32}$/)
  expect(draft.resource).toBeUndefined()
  expect(draft.failedResource).toBeUndefined()
  expect(await page.evaluate(() => sessionStorage.getItem('bt_active_trip_ref'))).toBeNull()
  // Observe an idle interval: neither a timer nor the failure rendering retries.
  await page.waitForTimeout(650)
  expect(state.submissions).toHaveLength(1)
  return draft
}

async function retrySameRequest(page, state, draft) {
  await page.getByTestId('create-full-trip').click()
  await expect(page).toHaveURL(new RegExp(`/trip/result#trip=${RESOURCE}$`))
  await expect.poll(() => state.progressReads).toBeGreaterThan(0)
  expect(state.submissions).toHaveLength(2)
  expect(state.submissions[1]).toEqual(state.submissions[0])
  const retained = await page.evaluate(() => JSON.parse(sessionStorage.getItem('bt_input_draft')))
  expect(retained.text).toBe(draft.text)
  expect(retained.key).toBe(draft.key)
  expect(retained.resource).toBe(RESOURCE)
  expect(state.unauthorizedReads).toBe(0)
  expect(state.unexpected).toEqual([])
  await page.waitForTimeout(650)
  expect(state.submissions).toHaveLength(2)
}

for (const status of [502, 503, 504]) {
  test(`${status}: service feedback retains text; one explicit retry reuses the request and receives 202`, async ({ page }) => {
    const state = await installFixture(page, status)
    const draft = await submitAndObserveFailure(page, state, UNAVAILABLE)
    expect(state.submissions[0].body).toEqual({ mode: 'FULL', source: { type: 'TEXT', text: TEXT } })
    await retrySameRequest(page, state, draft)
  })
}

for (const outcome of ['TIMEOUT', 'INVALID_JSON']) {
  test(`${outcome}: unknown acceptance retains cautious wording and the same request identity`, async ({ page }) => {
    const state = await installFixture(page, outcome)
    const draft = await submitAndObserveFailure(page, state, UNKNOWN)
    await retrySameRequest(page, state, draft)
  })
}

for (const [status, message] of [[429, LIMITED], [401, UNKNOWN]]) {
  test(`${status}: existing feedback and request identity stay intact`, async ({ page }) => {
    const state = await installFixture(page, status)
    await submitAndObserveFailure(page, state, message)
    expect(state.unexpected).toEqual([])
  })
}

test('demo 503 also distinguishes an unavailable service without creating a second request automatically', async ({ page }) => {
  const state = await installFixture(page, 503)
  const draft = await submitAndObserveFailure(page, state, UNAVAILABLE, { demo: true })
  expect(state.submissions[0].body).toEqual({ mode: 'DEMO' })
  await retrySameRequest(page, state, draft)
})
