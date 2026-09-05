const { expect, test } = require('@playwright/test')

const API_ORIGIN = 'http://127.0.0.1:8999'

async function installAuth(page) {
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'dual-mode-token')
    localStorage.setItem(
      'authUser',
      JSON.stringify({ userId: 'user-42', nickname: '同行者' }),
    )
  })
}

function savedItinerary() {
  const place = (placeId, name, lng) => ({
    placeId,
    name,
    category: 'attraction',
    address: '杭州市',
    city: '杭州',
    coords: { lng, lat: 30.25 },
    source: 'amap_poi',
    tags: [],
    amapPhotos: [],
  })
  return {
    itineraryId: 'saved-itinerary-42',
    threadId: 'thread-42',
    city: '杭州',
    generatedAt: '2026-09-05T08:00:00Z',
    version: 2,
    days: [{
      dayIndex: 0,
      clusterId: 0,
      slots: [
        {
          placeId: 'place_001',
          startTime: '09:00',
          endTime: '11:00',
          tips: [],
          transport: { mode: 'driving', durationMins: 18, distanceKm: 7.2 },
          place: place('place_001', '西湖博物馆', 120.14),
        },
        {
          placeId: 'place_002',
          startTime: '13:00',
          endTime: '15:00',
          tips: [],
          place: place('place_002', '浙江省博物馆', 120.15),
        },
      ],
    }],
  }
}

function persistedRoomPlace(placeId, name, category, lng) {
  return {
    place_id: placeId,
    name,
    category,
    address: '杭州市',
    city: '杭州',
    district: '西湖区',
    coords: { lng, lat: 30.25 },
    source: 'amap_poi',
    tags: [],
    amap_photos: [],
    constraint_evidence: [],
    room_selected: true,
  }
}

function privateBoundaryResult() {
  return {
    status: 'READY',
    assumptions: [
      { key: 'destination', label: '目的地', value: '北京', editable: true },
      { key: 'calendar', label: '日期', value: 'Day 1', editable: true },
      { key: 'party_size', label: '同行人数', value: '2 人', editable: true },
    ],
    days: [{
      label: 'Day 1',
      activities: [{
        start_time: null,
        end_time: null,
        visit_duration_minutes: null,
        timing_source: 'UNSPECIFIED',
        locked: false,
        fixed_commitment: false,
        activity_token: 'private-activity-token-0001',
        name: '故宫博物院',
        category: '景点',
        area_or_address: '北京市东城区',
        time_hint: null,
        status: 'READY',
        available_actions: ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE'],
        knowledge_suggestions: [],
      }],
    }],
    map: { status: 'UNAVAILABLE', message: '路线暂不可用', available_actions: [] },
    stay: {
      status: 'UNAVAILABLE',
      message: '住宿待选择',
      area_summary: null,
      searched_scopes: [],
      candidates: [],
      available_actions: [],
    },
    available_actions: ['EDIT_ASSUMPTIONS', 'EDIT_CARDS'],
    can_undo: false,
    ownership: 'ACCOUNT',
    expires_at: null,
    is_demo: false,
    updated_at: '2026-09-05T08:00:00Z',
  }
}

test('invitation login preserves its room code and waits for explicit join', async ({ page }) => {
  let joinCalls = 0
  await page.route('**/api/auth/email-login', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ token: 'dual-mode-token', user_id: 'user-42', nickname: '同行者' }),
    })
  })
  await page.route(`${API_ORIGIN}/api/user/rooms`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  })
  await page.route(`${API_ORIGIN}/api/room/ROOM42/join`, async (route) => {
    joinCalls += 1
    await route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/collaborate?join=ROOM42')
  await expect(page).toHaveURL(/\/login$/)
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('bt_login_return')))
    .toBe('/collaborate?join=ROOM42')
  await expect(page.getByRole('link', { name: '返回首页' })).toHaveAttribute('href', '/')

  await page.getByLabel('邮箱', { exact: true }).fill('owner@example.com')
  await page.getByLabel('密码', { exact: true }).fill('password123')
  await page.getByRole('button', { name: '登录并继续' }).click()

  await expect(page).toHaveURL(/\/collaborate\?join=ROOM42$/)
  await expect(page.getByLabel('房间号')).toHaveValue('ROOM42')
  expect(joinCalls).toBe(0)
})

test('ordinary login returns to the text trip home', async ({ page }) => {
  await page.route('**/api/auth/email-login', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ token: 'dual-mode-token', user_id: 'user-42', nickname: '同行者' }),
    })
  })

  await page.goto('/login')
  await page.getByLabel('邮箱', { exact: true }).fill('owner@example.com')
  await page.getByLabel('密码', { exact: true }).fill('password123')
  await page.getByRole('button', { name: '登录并继续' }).click()

  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByRole('heading', { name: '把攻略，整理成走得明白的行程' })).toBeVisible()
})

test('result never reads or renders source mapping while privacy deletion stays usable', async ({ page }) => {
  const resource = 'private-result-resource-0001'
  const sourceSentinel = 'RAW-SOURCE-MUST-NEVER-ENTER-THE-DOM'
  const quoteSentinel = 'SOURCE-QUOTE-MUST-NEVER-ENTER-THE-DOM'
  let sourceReads = 0
  let sourceDeletes = 0
  await installAuth(page)
  await page.addInitScript(({ resourceRef }) => {
    sessionStorage.setItem('bt_active_trip_ref', resourceRef)
    sessionStorage.setItem('bt_active_trip_mode', 'FULL')
    sessionStorage.setItem('bt_active_trip_etag', 'tu3_private_boundary_1')
  }, { resourceRef: resource })
  await page.route(`**/api/v3/trip-understandings/${resource}/**`, async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (pathname.endsWith('/result')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: 'tu3_private_boundary_1' },
        body: JSON.stringify(privateBoundaryResult()),
      })
      return
    }
    if (pathname.endsWith('/source')) {
      if (request.method() === 'GET') {
        sourceReads += 1
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            status: 'AVAILABLE',
            text: sourceSentinel,
            activities: [{
              activity_token: 'private-activity-token-0001',
              name: '故宫博物院',
              quote: quoteSentinel,
            }],
          }),
        })
      } else {
        sourceDeletes += 1
        await route.fulfill({ status: 204, body: '' })
      }
      return
    }
    if (pathname.endsWith('/supplementary')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'AVAILABLE', days: [] }),
      })
      return
    }
    if (pathname.endsWith('/map-renders/latest')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          status: 'UNAVAILABLE',
          message: '路线暂不可用',
          points: [],
          days: [],
          available_actions: [],
        }),
      })
      return
    }
    if (pathname.endsWith('/stay-suggestions')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(privateBoundaryResult().stay),
      })
      return
    }
    if (pathname.endsWith('/materialize')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: 'tu3_private_boundary_1' },
        body: JSON.stringify({
          status: 'READY',
          message: '行程已可检查',
          calendar: 'Day 1',
          party_size: 2,
          checks_available: true,
        }),
      })
      return
    }
    if (pathname.endsWith('/checks')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          status: 'READY',
          message: '暂时没有需要优先处理的问题',
          items: [],
          remaining_must_adjust: 0,
          available_actions: [],
        }),
      })
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/trip/result')
  await expect(page.getByRole('heading', { name: '故宫博物院' })).toBeVisible()
  const card = page.getByTestId('activity-card').filter({ hasText: '故宫博物院' })
  await card.locator('button').filter({ hasText: '故宫博物院' }).click()
  await page.getByRole('button', { name: '编辑文字' }).click()
  await expect(page.getByText('查看原文中的地点名称')).toHaveCount(0)
  await page.getByRole('button', { name: '关闭编辑' }).click()

  await page.getByLabel('更多行程操作').click()
  await expect(page.getByRole('button', { name: '查看导入文字' })).toHaveCount(0)
  await expect(page.locator('body')).not.toContainText(sourceSentinel)
  await expect(page.locator('body')).not.toContainText(quoteSentinel)
  expect(sourceReads).toBe(0)

  await page.getByTestId('delete-trip-source').click()
  const firstDialog = page.getByRole('dialog', { name: '删除攻略原文？' })
  await firstDialog.getByRole('button', { name: '取消' }).click()
  expect(sourceDeletes).toBe(0)

  await page.getByLabel('更多行程操作').click()
  await page.getByTestId('delete-trip-source').click()
  await page.getByTestId('confirm-delete-source').click()
  await expect(page.getByText('导入文字已删除，现有行程与已确认地点仍保留。')).toBeVisible()
  expect(sourceDeletes).toBe(1)
  expect(sourceReads).toBe(0)
  await expect(page.getByRole('heading', { name: '故宫博物院' })).toBeVisible()

  await page.getByLabel('更多行程操作').click()
  await expect(page.getByTestId('delete-trip-source')).toHaveText('原文已删除')
  await expect(page.locator('body')).not.toContainText(sourceSentinel)
  await expect(page.locator('body')).not.toContainText(quoteSentinel)
  const publicMarkup = await page.evaluate(() => {
    const clone = document.body.cloneNode(true)
    clone.querySelectorAll('script, style, nextjs-portal').forEach((node) => node.remove())
    clone.querySelectorAll('[class]').forEach((node) => node.removeAttribute('class'))
    return clone.innerHTML
  })
  expect(publicMarkup).not.toMatch(
    /private-activity-token-0001|source span|offset|confidence/i,
  )
})

test('join and create trust the authenticated identity and use clean room URLs', async ({ page }) => {
  await installAuth(page)
  const requests = []
  await page.route(`${API_ORIGIN}/api/user/rooms`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  })
  await page.route(`${API_ORIGIN}/api/room/JOIN123/join`, async (route) => {
    requests.push({ kind: 'join', body: route.request().postDataJSON() })
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ room_id: 'JOIN123', thread_id: 'server-thread', trip_city: '杭州', trip_days: 3 }),
    })
  })
  await page.route(`${API_ORIGIN}/api/room/JOIN123/state`, async (route) => {
    await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/collaborate?join=JOIN123')
  await expect(page.getByRole('button', { name: '加入协同房间' })).toBeEnabled()
  await page.getByRole('button', { name: '加入协同房间' }).click()
  await expect(page).toHaveURL(/\/room\/JOIN123$/)
  expect(requests).toEqual([{ kind: 'join', body: { nickname: '同行者' } }])
  expect(new URL(page.url()).search).toBe('')

  const createPage = await page.context().newPage()
  await createPage.route(`${API_ORIGIN}/api/user/rooms`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  })
  await createPage.route(`${API_ORIGIN}/api/room`, async (route) => {
    requests.push({ kind: 'create', body: route.request().postDataJSON() })
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
  await createPage.route(`${API_ORIGIN}/api/room/*/state`, async (route) => {
    await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
  })
  await createPage.goto('/collaborate')
  await createPage.getByLabel('目的地（选填）').fill('上海')
  await createPage.getByLabel('行程天数').selectOption('2')
  await createPage.getByRole('button', { name: '创建协同房间' }).click()
  await expect(createPage).toHaveURL(/\/room\/[A-F0-9]{8}$/)

  const created = requests.find((request) => request.kind === 'create').body
  expect(created).toMatchObject({ trip_city: '上海', trip_days: 2, nickname: '同行者' })
  expect(created.user_id).toBeUndefined()
  expect(typeof created.room_id).toBe('string')
  expect(typeof created.thread_id).toBe('string')
  expect(new URL(createPage.url()).search).toBe('')
})

test('an uncertain room creation retries the same room and thread identifiers', async ({ page }) => {
  await installAuth(page)
  const attempts = []
  await page.route(`${API_ORIGIN}/api/user/rooms`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  })
  await page.route(`${API_ORIGIN}/api/room`, async (route) => {
    attempts.push(route.request().postDataJSON())
    if (attempts.length === 1) {
      await route.abort('failed')
      return
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
  await page.route(`${API_ORIGIN}/api/room/*/state`, async (route) => {
    await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/collaborate')
  await page.getByLabel('目的地（选填）').fill('苏州')
  await page.getByLabel('行程天数').selectOption('2')
  const create = page.getByRole('button', { name: '创建协同房间' })
  await create.click()
  await expect(page.getByText('房间暂时没有创建成功，请重试。')).toBeVisible()
  await expect(create).toBeEnabled()
  await create.click()
  await expect.poll(() => attempts.length).toBe(2)

  expect(attempts[1]).toEqual(attempts[0])
  await expect(page).toHaveURL(new RegExp(`/room/${attempts[0].room_id}$`))
})

test('room metadata failure keeps collaboration capabilities unmounted', async ({ page }) => {
  await installAuth(page)
  const downstream = []
  const sockets = []
  page.on('request', (request) => {
    const pathname = new URL(request.url()).pathname
    if (/\/(ws-token|places|itinerary|chat|optimize|weather)(?:\/|$)/.test(pathname)) {
      downstream.push(`${request.method()} ${pathname}`)
    }
  })
  page.on('websocket', (socket) => {
    if (socket.url().startsWith('ws://127.0.0.1:8998')) sockets.push(socket.url())
  })
  await page.route(`${API_ORIGIN}/api/room/LOCKED/state`, async (route) => {
    await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/room/LOCKED')
  await expect(page.getByRole('heading', { name: '这个房间暂时无法打开' })).toBeVisible()
  await expect(page.getByRole('button', { name: '重试' })).toBeVisible()
  await page.waitForTimeout(300)
  expect(downstream).toEqual([])
  expect(sockets).toEqual([])
  await expect(page.getByRole('button', { name: '智能排线' })).toHaveCount(0)
  await expect(page.getByText('AI 顾问')).toHaveCount(0)
})

test('an expired room login preserves the exact return path and mounts no capabilities', async ({ page }) => {
  await page.addInitScript(() => {
    if (sessionStorage.getItem('__stale_auth_seeded') === 'true') return
    sessionStorage.setItem('__stale_auth_seeded', 'true')
    localStorage.setItem('authToken', 'dual-mode-token')
    localStorage.setItem(
      'authUser',
      JSON.stringify({ userId: 'user-42', nickname: '同行者' }),
    )
    localStorage.setItem('userId', 'stale-user')
    localStorage.setItem('nickname', '旧昵称')
  })
  const downstream = []
  const sockets = []
  page.on('request', (request) => {
    const pathname = new URL(request.url()).pathname
    if (/\/(ws-token|places|itinerary|chat|optimize|weather)(?:\/|$)/.test(pathname)) {
      downstream.push(`${request.method()} ${pathname}`)
    }
  })
  page.on('websocket', (socket) => {
    if (socket.url().startsWith('ws://127.0.0.1:8998')) sockets.push(socket.url())
  })
  await page.route(`${API_ORIGIN}/api/room/EXPIRED/state`, async (route) => {
    await route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ detail: { code: 'AUTH_EXPIRED' } }),
    })
  })

  await page.goto('/room/EXPIRED?panel=chat#focus')
  await expect(page).toHaveURL(/\/login$/)
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('bt_login_return')))
    .toBe('/room/EXPIRED?panel=chat#focus')
  await expect.poll(() => page.evaluate(() => ({
    authToken: localStorage.getItem('authToken'),
    authUser: localStorage.getItem('authUser'),
    userId: localStorage.getItem('userId'),
    nickname: localStorage.getItem('nickname'),
  }))).toEqual({ authToken: null, authUser: null, userId: null, nickname: null })
  expect(downstream).toEqual([])
  expect(sockets).toEqual([])
})

test('authoritative saved route gates transfer and retries with one idempotency key', async ({ page }) => {
  await installAuth(page)
  const secret = 'LOCAL-CACHE-MUST-NOT-RENDER'
  await page.addInitScript(({ value }) => {
    localStorage.setItem('itinerary_cache_ROOM42', JSON.stringify({ city: value }))
  }, { value: secret })

  const transferCalls = []
  let transferAttempt = 0
  let optimizeCalls = 0
  let itineraryWrites = 0
  await page.route(`${API_ORIGIN}/api/**`, async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const { pathname } = url
    if (pathname === '/api/room/ROOM42/state') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ thread_id: 'thread-42', trip_city: '杭州', trip_days: 1 }),
      })
      return
    }
    if (pathname === '/api/room/ROOM42/places') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
      return
    }
    if (pathname === '/api/room/ROOM42/itinerary' && request.method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ itinerary_data: savedItinerary() }),
      })
      return
    }
    if (pathname === '/api/room/ROOM42/itinerary' && request.method() === 'POST') {
      itineraryWrites += 1
      await route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
      return
    }
    if (pathname === '/api/room/ROOM42/ws-token') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ token: 'short-lived-room-token', expires_in_seconds: 300 }),
      })
      return
    }
    if (pathname === '/api/weather') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ city: '杭州', days: [] }) })
      return
    }
    if (pathname === '/api/optimize') {
      optimizeCalls += 1
      await route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
      return
    }
    if (pathname === '/api/v3/trip-understandings/from-collaboration') {
      transferAttempt += 1
      transferCalls.push({
        body: request.postDataJSON(),
        key: request.headers()['idempotency-key'],
      })
      if (transferAttempt === 1) {
        await route.abort('failed')
        return
      }
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({
          public_resource_id: 'imported-trip-42',
          status: 'PROCESSING',
          message: '已接收',
          result_url: '/api/v3/trip-understandings/imported-trip-42/result',
          events_url: '/api/v3/trip-understandings/imported-trip-42/events',
        }),
      })
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/room/ROOM42')
  const transfer = page.getByRole('button', { name: '转入行程查' })
  await expect(transfer).toBeVisible()
  await expect(page.locator('body')).not.toContainText(secret)
  expect(optimizeCalls).toBe(0)
  expect(itineraryWrites).toBe(0)

  await transfer.click()
  await expect(page.getByText('暂时没有转入成功。再次尝试会安全地续用同一次请求。')).toBeVisible()
  await expect(transfer).toBeEnabled()
  await transfer.click()
  await expect.poll(() => transferCalls.length).toBe(2)
  await expect(page).toHaveURL(/\/trip\/result#trip=imported-trip-42$/)

  expect(transferCalls.map((call) => call.body)).toEqual([
    { room_id: 'ROOM42' },
    { room_id: 'ROOM42' },
  ])
  expect(transferCalls[0].key).toBeTruthy()
  expect(transferCalls[1].key).toBe(transferCalls[0].key)
  expect(optimizeCalls).toBe(0)
  expect(itineraryWrites).toBe(0)
})

test('a changed saved route requires a fresh transfer confirmation and idempotency key', async ({ page }) => {
  await installAuth(page)
  const original = savedItinerary()
  const latest = savedItinerary()
  latest.version = 3
  latest.days[0].slots[0].place.name = '曲院风荷'
  let itineraryReads = 0
  const transferCalls = []

  await page.route(`${API_ORIGIN}/api/**`, async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/room/CHANGED/state') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ thread_id: 'thread-changed', trip_city: '杭州', trip_days: 1 }),
      })
      return
    }
    if (pathname === '/api/room/CHANGED/places') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
      return
    }
    if (pathname === '/api/room/CHANGED/itinerary' && request.method() === 'GET') {
      itineraryReads += 1
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ itinerary_data: itineraryReads === 1 ? original : latest }),
      })
      return
    }
    if (pathname === '/api/room/CHANGED/ws-token') {
      await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
      return
    }
    if (pathname === '/api/weather') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ city: '杭州', days: [] }) })
      return
    }
    if (pathname === '/api/v3/trip-understandings/from-collaboration') {
      transferCalls.push(request.headers()['idempotency-key'])
      if (transferCalls.length === 1) {
        await route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({ detail: { code: 'IDEMPOTENCY_KEY_REUSED' } }),
        })
        return
      }
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({
          public_resource_id: 'imported-trip-changed',
          status: 'PROCESSING',
          message: '已接收',
          result_url: '/api/v3/trip-understandings/imported-trip-changed/result',
          events_url: '/api/v3/trip-understandings/imported-trip-changed/events',
        }),
      })
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/room/CHANGED')
  const transfer = page.getByRole('button', { name: '转入行程查' })
  await expect(transfer).toBeVisible()
  await transfer.click()
  await expect(page.getByText('已保存路线发生变化，请核对最新版后再次转入行程查。')).toBeVisible()
  await expect(page.locator('p:visible', { hasText: '曲院风荷' })).toBeVisible()
  expect(transferCalls).toHaveLength(1)

  await transfer.click()
  await expect.poll(() => transferCalls.length).toBe(2)
  expect(transferCalls[0]).toBeTruthy()
  expect(transferCalls[1]).toBeTruthy()
  expect(transferCalls[1]).not.toBe(transferCalls[0])
  await expect(page).toHaveURL(/\/trip\/result#trip=imported-trip-changed$/)
})

test('authorized itinerary read failure never falls back to private browser cache', async ({ page }) => {
  await installAuth(page)
  const secret = 'PREVIOUS-ACCOUNT-PRIVATE-TRIP'
  await page.addInitScript(({ value }) => {
    localStorage.setItem('itinerary_cache_PRIVATE', JSON.stringify({
      ...savedItinerary(),
      city: value,
    }))
  }, { value: secret })
  await page.route(`${API_ORIGIN}/api/room/PRIVATE/itinerary`, async (route) => {
    await route.fulfill({
      status: 403,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'SERVER-INTERNAL-MEMBERSHIP-DENIAL' }),
    })
  })

  await page.goto('/room/PRIVATE/itinerary')
  await expect(page.getByText('暂时无法读取已保存路线，请稍后重试。')).toBeVisible()
  await expect(page.locator('body')).not.toContainText(secret)
  await expect(page.locator('body')).not.toContainText('SERVER-INTERNAL-MEMBERSHIP-DENIAL')
  await expect(page.getByText('西湖博物馆')).toHaveCount(0)
})

test('saved itinerary detail discards legacy driving estimates and makes the missing route explicit', async ({ page }) => {
  await installAuth(page)
  let optimizeCalls = 0
  let providerCalls = 0
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/api/optimize') optimizeCalls += 1
    if (/amap|高德/i.test(request.url())) providerCalls += 1
  })
  await page.route(`${API_ORIGIN}/api/room/ROOM42/itinerary`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ itinerary_data: savedItinerary() }),
    })
  })

  await page.goto('/room/ROOM42/itinerary')
  await expect(page.getByText('西湖博物馆')).toBeVisible()
  await expect(page.getByTestId('collaboration-route-unavailable')).toHaveText('路线暂不可用')
  await expect(page.locator('body')).not.toContainText('18 分钟')
  await expect(page.locator('body')).not.toContainText('7.2 km')
  expect(optimizeCalls).toBe(0)
  expect(providerCalls).toBe(0)
})

test('malformed saved itinerary becomes a recoverable empty state instead of a blank page', async ({ page }) => {
  await installAuth(page)
  await page.route(`${API_ORIGIN}/api/room/BROKEN/itinerary`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ itinerary_data: {} }),
    })
  })

  await page.goto('/room/BROKEN/itinerary')
  await expect(page.getByText('暂时无法读取已保存路线，请稍后重试。')).toBeVisible()
  await expect(page.getByRole('button', { name: '返回工作台' }).last()).toBeVisible()
  await expect(page.locator('body')).not.toContainText('undefined')
})

for (const mobileWidth of [390, 360]) {
  test(`collaboration core controls remain reachable at ${mobileWidth}px`, async ({ page }) => {
    await page.setViewportSize({ width: mobileWidth, height: 760 })
    await installAuth(page)
    let optimizeCalls = 0
    let taskParseCalls = 0
    let transferCalls = 0

    await page.route(`${API_ORIGIN}/api/**`, async (route) => {
      const request = route.request()
      const { pathname } = new URL(request.url())
      if (pathname === '/api/room/MOBILE/state') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ thread_id: 'thread-mobile', trip_city: '杭州', trip_days: 1 }),
        })
        return
      }
      if (pathname === '/api/room/MOBILE/places' && request.method() === 'GET') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([
            persistedRoomPlace('mobile-attraction', '西湖博物馆', 'attraction', 120.14),
            persistedRoomPlace('mobile-food', '楼外楼', 'food', 120.15),
            persistedRoomPlace('mobile-hotel', '湖畔酒店', 'hotel', 120.16),
          ]),
        })
        return
      }
      if (pathname === '/api/room/MOBILE/places/sync') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) })
        return
      }
      if (pathname === '/api/room/MOBILE/itinerary' && request.method() === 'GET') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ itinerary_data: savedItinerary() }),
        })
        return
      }
      if (pathname === '/api/room/MOBILE/ws-token') {
        await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
        return
      }
      if (pathname === '/api/weather') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ city: '杭州', days: [] }) })
        return
      }
      if (pathname === '/api/room/MOBILE/task/parse') {
        taskParseCalls += 1
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ needs_clarification: false }),
        })
        return
      }
      if (pathname === '/api/optimize') {
        optimizeCalls += 1
        await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
        return
      }
      if (pathname === '/api/v3/trip-understandings/from-collaboration') {
        transferCalls += 1
        await route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: JSON.stringify({
            public_resource_id: `mobile-import-${mobileWidth}`,
            status: 'PROCESSING',
            message: '已接收',
            result_url: `/api/v3/trip-understandings/mobile-import-${mobileWidth}/result`,
            events_url: `/api/v3/trip-understandings/mobile-import-${mobileWidth}/events`,
          }),
        })
        return
      }
      await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
    })

    await page.goto('/room/MOBILE')
    const optimize = page.getByRole('button', { name: /智能排线/ })
    const transfer = page.getByRole('button', { name: '转入行程查' })
    await expect(optimize).toBeEnabled()
    await expect(transfer).toBeVisible()

    for (const control of [optimize, transfer]) {
      const box = await control.boundingBox()
      expect(box).not.toBeNull()
      expect(box.x).toBeGreaterThanOrEqual(0)
      expect(box.x + box.width).toBeLessThanOrEqual(mobileWidth + 0.5)
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(mobileWidth)

    const workspaceNav = page.getByRole('navigation', { name: '协同工作区' })
    await workspaceNav.getByRole('button', { name: 'AI 顾问' }).click()
    await expect(page.getByTestId('mobile-chat-panel')).toBeVisible()
    await expect(page.getByTestId('mobile-chat-panel').getByPlaceholder('描述你的旅行需求...')).toBeVisible()
    await workspaceNav.getByRole('button', { name: /地点/ }).click()
    await expect(page.getByTestId('mobile-place-panel')).toBeVisible()
    await expect(page.getByTestId('mobile-place-panel').getByText('候选地点').first()).toBeVisible()
    await workspaceNav.getByRole('button', { name: '地图' }).click()
    await expect(workspaceNav.getByRole('button', { name: '地图' })).toHaveAttribute('aria-pressed', 'true')

    await optimize.click()
    await expect.poll(() => taskParseCalls).toBe(1)
    await expect.poll(() => optimizeCalls).toBe(1)

    await transfer.click()
    await expect.poll(() => transferCalls).toBe(1)
    await expect(page).toHaveURL(new RegExp(`/trip/result#trip=mobile-import-${mobileWidth}$`))
  })
}
