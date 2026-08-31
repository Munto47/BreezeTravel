const { expect, test } = require('@playwright/test')


const RESOURCE_REF = 'g06-browser-resource-ref'
const SHARE_REF = 'g06BrowserShareRef12345678901234'
const SHARE_SECRET = 'g06BrowserSecretValue123456789012345678901'
const ETAG = 'tu3_g06_browser_itinerary_generation'


function resultView() {
  return {
    status: 'READY',
    assumptions: [
      { key: 'destination', label: '目的地', value: '北京', editable: true },
      { key: 'calendar', label: '日期', value: 'Day 1–Day 2', editable: true },
      { key: 'party_size', label: '人数', value: '2 人', editable: true },
    ],
    days: [{
      label: 'Day 1',
      activities: [{
        activity_token: 'g06-browser-activity-token-0001',
        name: '故宫博物院',
        category: '景点',
        area_or_address: '北京市东城区',
        time_hint: '上午',
        status: 'READY',
        available_actions: ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE'],
        knowledge_suggestions: [],
      }],
    }],
    map: { status: 'AVAILABLE', message: '路线已准备', available_actions: ['VIEW_MAP'] },
    stay: { status: 'UNAVAILABLE', message: '住宿待选择', area_summary: null, searched_scopes: [], candidates: [], available_actions: [] },
    available_actions: ['EDIT_ASSUMPTIONS', 'EDIT_CARDS'],
  }
}


const mapView = { status: 'AVAILABLE', message: '路线已准备', days: [], available_actions: ['VIEW_MAP'] }
const stayView = { status: 'UNAVAILABLE', message: '住宿待选择', area_summary: null, searched_scopes: [], candidates: [], available_actions: [] }
const checksView = { status: 'READY', message: '当前没有需要优先处理的问题', items: [], remaining_must_adjust: 0, available_actions: [] }


async function signIn(page) {
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'g06-browser-token')
    localStorage.setItem('authUser', JSON.stringify({ userId: 'g06-user', nickname: '旅行者' }))
  })
}


async function installTripFixture(page) {
  const state = { revoked: false, feedbackEnabled: false, exchangeBodies: [], feedbackBodies: [] }
  await signIn(page)
  await page.addInitScript(({ resourceRef, etag }) => {
    sessionStorage.setItem('bt_active_trip_ref', resourceRef)
    sessionStorage.setItem('bt_active_trip_mode', 'FULL')
    sessionStorage.setItem('bt_active_trip_etag', etag)
  }, { resourceRef: RESOURCE_REF, etag: ETAG })
  await page.route('**/api/v3/me/data-consents', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ memory_enabled: false, feedback_enabled: state.feedbackEnabled, training_eval_enabled: false }) })
  })
  await page.route('**/api/v3/me/shares', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state.revoked ? [] : []) })
  })
  await page.route(`**/api/v3/me/shares/${SHARE_REF}`, async (route) => {
    state.revoked = true
    await route.fulfill({ status: 204 })
  })
  await page.route(`**/api/v3/trip-understandings/${RESOURCE_REF}/**`, async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (pathname.endsWith('/events')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    if (pathname.endsWith('/result')) return route.fulfill({ status: 200, contentType: 'application/json', headers: { ETag: ETAG }, body: JSON.stringify(resultView()) })
    if (pathname.endsWith('/map-renders/latest')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(mapView) })
    if (pathname.endsWith('/stay-suggestions')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(stayView) })
    if (pathname.endsWith('/materialize')) return route.fulfill({ status: 200, contentType: 'application/json', headers: { ETag: ETAG }, body: JSON.stringify({ status: 'READY', message: '可以检查', calendar: 'Day 1', party_size: 2, checks_available: true }) })
    if (pathname.endsWith('/checks')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(checksView) })
    if (pathname.endsWith('/shares') && request.method() === 'POST') return route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ share_url: `/share/${SHARE_REF}#s=${SHARE_SECRET}`, expires_at: '2026-09-07T04:00:00Z' }) })
    if (pathname.endsWith('/feedback') && request.method() === 'POST') {
      state.feedbackBodies.push(request.postDataJSON())
      return route.fulfill({ status: state.feedbackEnabled ? 202 : 409, contentType: 'application/json', body: JSON.stringify(state.feedbackEnabled ? { status: 'RECORDED' } : { detail: { code: 'FEEDBACK_NOT_ENABLED' } }) })
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
  await page.route(`**/api/v3/shares/${SHARE_REF}/exchange`, async (route) => {
    const request = route.request()
    state.exchangeBodies.push(request.postDataJSON())
    expect(request.url()).not.toContain(SHARE_SECRET)
    await route.fulfill({ status: state.revoked ? 404 : 204, headers: { 'Set-Cookie': 'bt_g06_share=session-capability; HttpOnly; Path=/api/v3/shares; SameSite=Lax' } })
  })
  await page.route(`**/api/v3/shares/${SHARE_REF}`, async (route) => {
    if (state.revoked) return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: { code: 'SHARE_UNAVAILABLE' } }) })
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      title: '北京行程', destination: '北京', schedule: 'Day 1–Day 2', party_size: '2 人',
      days: [{ label: 'Day 1', activities: [{ name: '故宫博物院', area_or_address: '北京市东城区', time_hint: '上午', note: '可直接查看' }] }],
      accommodation: null, message: '这是朋友分享的只读行程。',
    }) })
  })
  return state
}


test('memory, feedback and training choices start off and stay separate', async ({ page }) => {
  await signIn(page)
  const state = {
    consents: { memory_enabled: false, feedback_enabled: false, training_eval_enabled: false },
    preference: null,
  }
  await page.route('**/api/user/me', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ user_id: 'g06-user', nickname: '旅行者', phone: null, avatar_url: null, birthday: null, created_at: '2026-08-31T00:00:00Z' }) }))
  await page.route('**/api/v3/me/data-consents', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state.consents) }))
  await page.route('**/api/v3/me/data-consents/*', async (route) => {
    const purpose = new URL(route.request().url()).pathname.split('/').at(-1)
    const enabled = route.request().postDataJSON().enabled
    if (purpose === 'memory') state.consents.memory_enabled = enabled
    if (purpose === 'feedback') state.consents.feedback_enabled = enabled
    if (purpose === 'training-eval') state.consents.training_eval_enabled = enabled
    if (purpose === 'memory' && !enabled) state.preference = null
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state.consents) })
  })
  await page.route('**/api/v3/me/travel-preferences', async (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state.preference) })
    if (route.request().method() === 'DELETE') { state.preference = null; return route.fulfill({ status: 204 }) }
    state.preference = route.request().postDataJSON()
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state.preference) })
  })

  await page.goto('/profile')
  const panel = page.getByTestId('g06-memory-settings')
  await expect(panel).toContainText('全部默认关闭')
  for (const label of ['记住结构化偏好', '允许保存产品反馈', '允许用于训练或评测']) {
    await expect(page.getByRole('button', { name: `切换${label}` })).toHaveAttribute('aria-pressed', 'false')
  }
  await page.getByRole('button', { name: '切换记住结构化偏好' }).click()
  await page.getByTestId('walking-tolerance').fill('25')
  await page.getByTestId('preferred-start-time').fill('08:30')
  await page.getByTestId('trip-intensity').selectOption('BALANCED')
  await page.getByRole('button', { name: '当地风味' }).click()
  await page.getByRole('button', { name: '靠近公交' }).click()
  await page.getByTestId('save-preferences').click()
  expect(state.preference).toMatchObject({ walking_tolerance_minutes: 25, preferred_start_time: '08:30', intensity: 'BALANCED', dining_preferences: ['LOCAL'], hotel_preferences: ['NEAR_TRANSIT'] })
  expect(state.consents).toEqual({ memory_enabled: true, feedback_enabled: false, training_eval_enabled: false })
  await page.getByRole('button', { name: '切换允许保存产品反馈' }).click()
  expect(state.consents.training_eval_enabled).toBe(false)
  await page.getByTestId('clear-preferences').click()
  expect(state.preference).toBeNull()
})


test('owner creates a fragment link; recipient clears it before exchange and sees only minimal read-only cards', async ({ page, context }) => {
  const state = await installTripFixture(page)
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.goto('/trip/result')
  await expect(page.getByTestId('g06-memory-share-panel')).toBeVisible()
  await page.getByTestId('create-readonly-share').click()
  await expect(page.getByText(/7天有效/)).toBeVisible()
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain(`#s=${SHARE_SECRET}`)
  expect(await page.locator('body').innerText()).not.toContain(SHARE_SECRET)

  await page.goto(`/share/${SHARE_REF}#s=${SHARE_SECRET}`)
  await expect(page).toHaveURL(new RegExp(`/share/${SHARE_REF}$`))
  await expect(page.getByTestId('g06-shared-trip')).toBeVisible()
  await expect(page.getByText('故宫博物院')).toBeVisible()
  expect(state.exchangeBodies).toEqual([{ secret: SHARE_SECRET }])
  const html = await page.getByTestId('g06-shared-trip').evaluate((element) => element.outerHTML)
  expect(html).not.toContain(SHARE_SECRET)
  expect(html).not.toMatch(/activity_token|candidate_token|revision|receipt|confidence|license|provider|audit|finding/i)
})


test('revocation removes recipient access and feedback never enables training', async ({ page }) => {
  const state = await installTripFixture(page)
  state.feedbackEnabled = true
  await page.goto('/trip/result')
  await page.getByRole('button', { name: '这份行程对我有帮助' }).click()
  expect(state.feedbackBodies).toEqual([{ event_type: 'ADOPTED', subject_type: 'TRIP' }])

  await page.getByTestId('create-readonly-share').click()
  await page.reload()
  await expect(page.getByTestId('g06-memory-share-panel')).toBeVisible()
  // Supply the active listing after reload so the owner can revoke it.
  await page.unroute('**/api/v3/me/shares')
  await page.route('**/api/v3/me/shares', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([{ share_ref: SHARE_REF, expires_at: '2026-09-07T04:00:00Z', status: 'ACTIVE' }]) }))
  await page.reload()
  await page.getByTestId('revoke-share').click()
  expect(state.revoked).toBe(true)
  await page.goto(`/share/${SHARE_REF}#s=${SHARE_SECRET}`)
  await expect(page.getByText('此链接不存在、已过期或已被撤销。')).toBeVisible()
})
