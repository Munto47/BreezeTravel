const { expect, test } = require('@playwright/test')


const RESOURCE_REF = 'g03r-race-safe-result'
const ETAG_A = 'tu3_race_generation_a'
const ETAG_B = 'tu3_race_generation_b'
const MAP_THEATER_MESSAGES = {
  PREPARING: '准备中',
  AVAILABLE: '路线已准备',
  UNAVAILABLE: '路线暂不可用',
}


function deferred() {
  let resolvePromise
  let settled = false
  const promise = new Promise((resolve) => {
    resolvePromise = resolve
  })
  return {
    promise,
    resolve: () => {
      if (settled) return
      settled = true
      resolvePromise()
    },
  }
}


async function flushTwoAnimationFrames(page) {
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve))
  }))
}


async function openResultView(page, view) {
  const control = page.locator(
    `[data-testid="desktop-nav-${view}"]:visible, [data-testid="mobile-nav-${view}"]:visible`,
  )
  await expect(control).toHaveCount(1)
  await control.click()
  const panel = {
    itinerary: 'result-view-itinerary',
    map_stay: 'result-view-map-stay',
    checks: 'result-view-checks',
  }[view]
  await expect(page.getByTestId(panel)).toBeVisible()
}


function activity(token, name) {
  return {
    activity_token: token,
    name,
    category: '景点',
    area_or_address: '北京市',
    time_hint: '上午',
    status: 'READY',
    available_actions: ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE'],
  }
}


function resultView(mapStatus = 'AVAILABLE') {
  return {
    status: 'READY',
    assumptions: [
      { key: 'destination', label: '目的地', value: '北京', editable: true },
      { key: 'calendar', label: '日期', value: 'Day 1 ～ Day 3', editable: true },
      { key: 'party_size', label: '人数', value: '2 人', editable: true },
    ],
    days: [
      { label: 'Day 1', activities: [activity('activity-token-00000001', '故宫博物院')] },
      { label: 'Day 2', activities: [activity('activity-token-00000002', '天坛公园')] },
      { label: 'Day 3', activities: [] },
    ],
    map: {
      status: mapStatus,
      message:
        mapStatus === 'PREPARING'
          ? MAP_THEATER_MESSAGES.PREPARING
          : MAP_THEATER_MESSAGES.AVAILABLE,
      available_actions: mapStatus === 'AVAILABLE' ? ['VIEW_MAP'] : [],
    },
    stay: {
      status: 'AVAILABLE',
      message: '住宿建议已准备',
      area_summary: '建议住在东城区附近',
      searched_scopes: ['2公里'],
      candidates: [],
      available_actions: [],
    },
    available_actions: ['EDIT_ASSUMPTIONS', 'EDIT_CARDS'],
  }
}


const mapView = {
  status: 'AVAILABLE',
  message: MAP_THEATER_MESSAGES.AVAILABLE,
  days: [],
  available_actions: ['VIEW_MAP'],
}


function connectedMapView({ geometry = true, status = 'AVAILABLE' } = {}) {
  const walkingGeometry = geometry
    ? [{ longitude: 116.39, latitude: 39.92 }, { longitude: 116.40, latitude: 39.93 }]
    : []
  const transitGeometry = geometry
    ? [{ longitude: 116.391, latitude: 39.921 }, { longitude: 116.401, latitude: 39.931 }]
    : []
  return {
    status,
    message:
      status === 'NEEDS_UPDATE'
        ? '行程已调整，需要手动更新路线。'
        : MAP_THEATER_MESSAGES.AVAILABLE,
    days: [{
      label: 'Day 1',
      routes: [{
        from_name: '故宫博物院',
        to_name: '景山公园',
        selected_mode: 'walking',
        message: '建议从故宫博物院步行前往景山公园',
        walking: {
          status: 'AVAILABLE',
          duration_minutes: 12,
          distance_meters: 900,
          transfer_count: null,
          geometry: walkingGeometry,
        },
        transit: {
          status: 'AVAILABLE',
          duration_minutes: 18,
          distance_meters: 1600,
          transfer_count: 0,
          geometry: transitGeometry,
        },
      }],
    }],
    available_actions: status === 'NEEDS_UPDATE' ? ['RENDER_MAP'] : ['VIEW_MAP'],
  }
}


const stayView = {
  status: 'AVAILABLE',
  message: '住宿建议已准备',
  area_summary: '建议住在东城区附近',
  searched_scopes: ['2公里'],
  candidates: [],
  available_actions: [],
}


const checksView = {
  status: 'READY',
  message: '优先处理这三项，行程会更顺畅',
  items: [
    {
      check_token: 'check-token-000000000001',
      label: '可以更好',
      title: '午餐时间',
      message: '两段参观之间可以预留午餐时间。',
      affected_days: ['Day 1'],
      can_preview: true,
    },
    {
      check_token: 'check-token-000000000002',
      label: '需要确认',
      title: '出发时间',
      message: '出发前请再确认开放时间。',
      affected_days: ['Day 2'],
      can_preview: true,
    },
    {
      check_token: 'check-token-000000000003',
      label: '可以更好',
      title: '步行衔接',
      message: '相邻地点可以优先步行。',
      affected_days: ['Day 3'],
      can_preview: true,
    },
    {
      check_token: 'check-token-000000000004',
      label: '可以更好',
      title: '不会进入 Top-3',
      message: '异常的额外返回项不应出现在普通用户界面。',
      affected_days: ['Day 1'],
      can_preview: true,
    },
  ],
  remaining_must_adjust: 0,
  available_actions: ['PREVIEW_CHANGE'],
}


const ENHANCEMENT_REF = 'g03r-bounded-enhancements'
const ENHANCEMENT_ETAG_A = 'tu3_enhancement_generation_a'
const ENHANCEMENT_ETAG_B = 'tu3_enhancement_generation_b'
const ENHANCEMENT_CLOCK_START = Date.parse('2026-08-30T08:00:00.000Z')
const ENHANCEMENT_CLOCK_PAUSED = ENHANCEMENT_CLOCK_START + 60_000


function enhancementResultView() {
  const view = resultView('PREPARING')
  view.stay = {
    status: 'PREPARING',
    message: '正在准备住宿建议',
    area_summary: null,
    searched_scopes: [],
    candidates: [],
    available_actions: [],
  }
  return view
}


async function installEnhancementFixture(page, {
  mapMode = 'available',
  stayMode = 'available',
  switchGeneration = false,
  holdOldEnhancementRejections = false,
} = {}) {
  const modes = { map: mapMode, stay: stayMode }
  const reads = { map: 0, stay: 0 }
  const inFlight = { map: 0, stay: 0 }
  const maxInFlight = { map: 0, stay: 0 }
  const aborted = { map: 0, stay: 0 }
  const activeRequests = { map: new Set(), stay: new Set() }
  const barriers = { map: deferred(), stay: deferred() }
  const readBarriers = { map: new Map(), stay: new Map() }
  const waiters = []
  let materializeCalls = 0
  let checksCalls = 0
  let resultReads = 0
  let mapRenderPosts = 0
  let newGenerationStartedBeforeOldSettled = false

  const snapshot = () => ({
    reads: { ...reads },
    inFlight: { ...inFlight },
    maxInFlight: { ...maxInFlight },
    aborted: { ...aborted },
    materializeCalls,
    checksCalls,
    resultReads,
    mapRenderPosts,
    newGenerationStartedBeforeOldSettled,
  })
  const notify = () => {
    for (let index = waiters.length - 1; index >= 0; index -= 1) {
      if (!waiters[index].predicate(snapshot())) continue
      const [{ resolve }] = waiters.splice(index, 1)
      resolve()
    }
  }
  const waitFor = (predicate) => {
    if (predicate(snapshot())) return Promise.resolve()
    return new Promise((resolve) => waiters.push({ predicate, resolve }))
  }
  const settle = (kind, request) => {
    if (!activeRequests[kind].delete(request)) return
    inFlight[kind] -= 1
    notify()
  }

  page.on('requestfailed', (request) => {
    const pathname = new URL(request.url()).pathname
    const kind = pathname.endsWith('/map-renders/latest')
      ? 'map'
      : pathname.endsWith('/stay-suggestions')
        ? 'stay'
        : null
    if (!kind) return
    aborted[kind] += 1
    settle(kind, request)
    notify()
  })

  await page.addInitScript(({ resourceRef, etag, holdOldRejections }) => {
    if (holdOldRejections) {
      const originalFetch = window.fetch.bind(window)
      let releaseOldRejections
      let resolveOldRejectionsCaught
      let resolveOldRejectionsFinalized
      const oldRejectionBarrier = new Promise((resolve) => { releaseOldRejections = resolve })
      const oldRejectionsCaught = new Promise((resolve) => { resolveOldRejectionsCaught = resolve })
      const oldRejectionsFinalized = new Promise((resolve) => { resolveOldRejectionsFinalized = resolve })
      const state = {
        endpointCalls: { map: 0, stay: 0 },
        caught: 0,
        finalized: 0,
        newStartedBeforeOldFinalized: false,
      }
      window.__g03rOldEnhancementState = state
      window.__g03rOldRejectionsCaught = oldRejectionsCaught
      window.__g03rOldRejectionsFinalized = oldRejectionsFinalized
      window.__g03rReleaseOldRejections = () => releaseOldRejections()
      window.fetch = async (input, init) => {
        const requestUrl = typeof input === 'string' ? input : input.url
        const pathname = new URL(requestUrl, window.location.origin).pathname
        const kind = pathname.endsWith('/map-renders/latest')
          ? 'map'
          : pathname.endsWith('/stay-suggestions')
            ? 'stay'
            : null
        if (!kind) return originalFetch(input, init)
        state.endpointCalls[kind] += 1
        const oldRequest = state.endpointCalls[kind] === 1
        if (!oldRequest && state.finalized < 2) state.newStartedBeforeOldFinalized = true
        try {
          return await originalFetch(input, init)
        } catch (error) {
          if (oldRequest) {
            state.caught += 1
            if (state.caught === 2) resolveOldRejectionsCaught()
            await oldRejectionBarrier
          }
          throw error
        } finally {
          if (oldRequest) {
            state.finalized += 1
            if (state.finalized === 2) resolveOldRejectionsFinalized()
          }
        }
      }
    }
    sessionStorage.setItem('bt_active_trip_ref', resourceRef)
    sessionStorage.setItem('bt_active_trip_mode', 'DEMO')
    sessionStorage.setItem('bt_active_trip_etag', etag)
  }, {
    resourceRef: ENHANCEMENT_REF,
    etag: ENHANCEMENT_ETAG_A,
    holdOldRejections: holdOldEnhancementRejections,
  })

  const readBarrier = (kind, call) => {
    if (!readBarriers[kind].has(call)) readBarriers[kind].set(call, deferred())
    return readBarriers[kind].get(call)
  }

  const fulfillEndpoint = async (route, kind) => {
    const request = route.request()
    reads[kind] += 1
    const call = reads[kind]
    const mode = modes[kind]
    const perReadBarrier = mode === 'budget-preparing' ? readBarrier(kind, call) : null
    activeRequests[kind].add(request)
    inFlight[kind] += 1
    maxInFlight[kind] = Math.max(maxInFlight[kind], inFlight[kind])
    if (switchGeneration && call > 1 && (aborted.map < 1 || aborted.stay < 1)) {
      newGenerationStartedBeforeOldSettled = true
    }
    notify()

    if (mode === 'hang') {
      await barriers[kind].promise
      try {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(kind === 'map'
            ? { ...mapView, message: '旧代际迟到路线' }
            : { ...stayView, message: '旧代际迟到住宿' }),
        })
      } catch {
        // The browser has already aborted this stale request.
      }
      settle(kind, request)
      return
    }

    if (mode === 'slow' && call === 1) await barriers[kind].promise
    if (perReadBarrier) await perReadBarrier.promise
    if (mode === 'failure') {
      await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
      settle(kind, request)
      return
    }

    const preparing = mode === 'preparing' || (mode === 'slow' && call === 1)
    try {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(kind === 'map'
          ? {
              ...mapView,
              status: preparing || mode === 'budget-preparing' ? 'PREPARING' : 'AVAILABLE',
              message:
                preparing || mode === 'budget-preparing'
                  ? `${MAP_THEATER_MESSAGES.PREPARING} 路线仍在准备 ${call}`
                  : `${MAP_THEATER_MESSAGES.AVAILABLE} 新路线状态已读取`,
              available_actions: preparing || mode === 'budget-preparing' ? [] : ['VIEW_MAP'],
            }
          : {
              ...stayView,
              status: preparing || mode === 'budget-preparing' ? 'PREPARING' : 'AVAILABLE',
              message: preparing || mode === 'budget-preparing' ? `住宿仍在准备 ${call}` : '新住宿状态已读取',
              area_summary: preparing || mode === 'budget-preparing' ? null : stayView.area_summary,
            }),
      })
    } catch {
      // A bounded session can abort a route while its deterministic response barrier is still held.
    }
    settle(kind, request)
  }

  await page.route(`**/api/v3/trip-understandings/${ENHANCEMENT_REF}/**`, async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname

    if (pathname.endsWith('/events')) {
      if (switchGeneration) {
        await waitFor((calls) => calls.reads.map >= 1 && calls.reads.stay >= 1)
        modes.map = 'available'
        modes.stay = 'available'
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          body: 'id: 1\nevent: result_available\ndata: {"message":"服务端结果已更新"}\n\n',
        })
      } else {
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
      }
      return
    }

    if (pathname.endsWith('/result')) {
      resultReads += 1
      const etag = switchGeneration && resultReads > 1 ? ENHANCEMENT_ETAG_B : ENHANCEMENT_ETAG_A
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: etag },
        body: JSON.stringify(enhancementResultView()),
      })
      notify()
      return
    }

    if (pathname.endsWith('/map-renders/latest')) {
      await fulfillEndpoint(route, 'map')
      return
    }

    if (pathname.endsWith('/stay-suggestions')) {
      await fulfillEndpoint(route, 'stay')
      return
    }

    if (pathname.endsWith('/map-renders') && request.method() === 'POST') {
      mapRenderPosts += 1
      notify()
      await route.fulfill({ status: 202, contentType: 'application/json', body: '{}' })
      return
    }

    if (pathname.endsWith('/materialize')) {
      materializeCalls += 1
      notify()
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: switchGeneration ? ENHANCEMENT_ETAG_B : ENHANCEMENT_ETAG_A },
        body: JSON.stringify({
          status: 'READY',
          message: '行程已准备好检查',
          calendar: 'Day 1 ～ Day 3',
          party_size: 2,
          checks_available: true,
        }),
      })
      return
    }

    if (pathname.endsWith('/checks')) {
      checksCalls += 1
      notify()
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(checksView) })
      return
    }

    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  return {
    calls: snapshot,
    setModes: (nextMapMode, nextStayMode) => {
      modes.map = nextMapMode
      modes.stay = nextStayMode
    },
    waitForReads: (map, stay) => waitFor((calls) => calls.reads.map >= map && calls.reads.stay >= stay),
    waitForAborts: (map, stay) => waitFor((calls) => calls.aborted.map >= map && calls.aborted.stay >= stay),
    waitForIdle: () => waitFor((calls) => calls.inFlight.map === 0 && calls.inFlight.stay === 0),
    releaseRead: (kind, call) => readBarrier(kind, call).resolve(),
    waitForOldRejectionsCaught: () => page.evaluate(() => window.__g03rOldRejectionsCaught),
    releaseOldRejections: () => page.evaluate(() => window.__g03rReleaseOldRejections()),
    waitForOldRejectionsFinalized: () => page.evaluate(() => window.__g03rOldRejectionsFinalized),
    oldEnhancementState: () => page.evaluate(() => window.__g03rOldEnhancementState),
    releaseAll: () => {
      barriers.map.resolve()
      barriers.stay.resolve()
      for (const barrier of readBarriers.map.values()) barrier.resolve()
      for (const barrier of readBarriers.stay.values()) barrier.resolve()
    },
  }
}


async function installPausedClock(page) {
  await page.clock.install({ time: ENHANCEMENT_CLOCK_START })
  await page.clock.pauseAt(ENHANCEMENT_CLOCK_PAUSED)
}


async function openEnhancementFixtureWithPausedClock(page, options = {}) {
  await installPausedClock(page)
  const fixture = await installEnhancementFixture(page, options)
  await page.goto('/trip/result')
  return fixture
}


async function installRaceFixture(page, scenario = 'cleanup') {
  let resultReads = 0
  let materializeCalls = 0
  let materializeInFlight = 0
  let maxMaterializeInFlight = 0
  let checksCalls = 0
  let abortedMaterializeCalls = 0
  const activeMaterializeRequests = new Set()
  const materializeStarted = deferred()
  const secondResultRead = deferred()
  const releaseCompatibleMaterialize = deferred()
  const releaseHungMaterialize = deferred()

  const settleMaterialize = (request) => {
    if (!activeMaterializeRequests.delete(request)) return
    materializeInFlight -= 1
  }

  page.on('requestfailed', (request) => {
    if (!request.url().endsWith('/materialize')) return
    abortedMaterializeCalls += 1
    settleMaterialize(request)
    releaseHungMaterialize.resolve()
  })

  await page.addInitScript(({ resourceRef, etag }) => {
    sessionStorage.setItem('bt_active_trip_ref', resourceRef)
    sessionStorage.setItem('bt_active_trip_mode', 'DEMO')
    sessionStorage.setItem('bt_active_trip_etag', etag)
  }, { resourceRef: RESOURCE_REF, etag: ETAG_A })

  await page.route(`**/api/v3/trip-understandings/${RESOURCE_REF}/**`, async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname

    if (pathname.endsWith('/events')) {
      if (scenario === 'cleanup' || scenario === 'stale') {
        await materializeStarted.promise
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          body: 'id: 1\nevent: result_available\ndata: {"message":"服务端结果已更新"}\n\n',
        })
        return
      }
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: '',
      })
      return
    }

    if (pathname.endsWith('/result')) {
      resultReads += 1
      const initial = resultReads === 1
      const startsPolling = scenario === 'cleanup' || scenario === 'stale'
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: initial ? ETAG_A : ETAG_B },
        body: JSON.stringify(resultView(initial && startsPolling ? 'PREPARING' : 'AVAILABLE')),
      })
      if (!initial) secondResultRead.resolve()
      return
    }

    if (pathname.endsWith('/map-renders/latest')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(mapView) })
      return
    }

    if (pathname.endsWith('/stay-suggestions')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(stayView) })
      return
    }

    if (pathname.endsWith('/materialize')) {
      materializeCalls += 1
      activeMaterializeRequests.add(request)
      materializeInFlight += 1
      maxMaterializeInFlight = Math.max(maxMaterializeInFlight, materializeInFlight)
      const currentCall = materializeCalls
      materializeStarted.resolve()
      const fulfillReady = async (etag) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          headers: { ETag: etag },
          body: JSON.stringify({
            status: 'READY',
            message: '行程已准备好检查',
            calendar: 'Day 1 ～ Day 3',
            party_size: 2,
            checks_available: true,
          }),
        })
      }
      if (currentCall === 1 && scenario === 'stale') {
        await releaseHungMaterialize.promise
        return
      }
      if (currentCall === 1 && scenario === 'cleanup') {
        await releaseCompatibleMaterialize.promise
        if (!activeMaterializeRequests.has(request)) return
        await fulfillReady(ETAG_B)
      } else if (currentCall === 1 && scenario === 'conflict') {
        await route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({ detail: { message: '行程刚刚有更新' } }),
        })
      } else if (currentCall === 1 && scenario === 'failure') {
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: { message: '暂时不可用' } }),
        })
      } else if (scenario === 'failure') {
        await fulfillReady(ETAG_A)
      } else {
        await fulfillReady(ETAG_B)
      }
      settleMaterialize(request)
      return
    }

    if (pathname.endsWith('/checks')) {
      checksCalls += 1
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(checksView) })
      return
    }

    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  return {
    calls: () => ({
      resultReads,
      materializeCalls,
      maxMaterializeInFlight,
      checksCalls,
      abortedMaterializeCalls,
    }),
    waitForMaterializeStart: () => materializeStarted.promise,
    waitForSecondResultRead: () => secondResultRead.promise,
    releaseCompatibleMaterialize: () => releaseCompatibleMaterialize.resolve(),
  }
}


for (const hangingKind of ['map', 'stay']) {
  test(`a hanging ${hangingKind} read aborts at 15000ms without losing the other terminal enhancement`, async ({ page }) => {
    const fixture = await openEnhancementFixtureWithPausedClock(page, {
      mapMode: hangingKind === 'map' ? 'hang' : 'available',
      stayMode: hangingKind === 'stay' ? 'hang' : 'available',
    })

    try {
      await fixture.waitForReads(1, 1)
      await page.clock.runFor(15_001)
      await fixture.waitForAborts(hangingKind === 'map' ? 1 : 0, hangingKind === 'stay' ? 1 : 0)

      await expect(page.getByTestId('trip-check-item')).toHaveCount(0)
      await openResultView(page, 'map_stay')
      await expectEnhancementRecovery(page)
      if (hangingKind === 'map') {
        await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status','UNAVAILABLE')
        await expect(page.getByTestId('stay-panel')).toContainText('新住宿状态已读取')
      } else {
        await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status','AVAILABLE')
        await expect(page.getByTestId('stay-panel')).toContainText('住宿建议暂时不可用')
      }

      const stopped = fixture.calls()
      expect(stopped.maxInFlight).toEqual({ map: 1, stay: 1 })
      expect(stopped.mapRenderPosts).toBe(0)
      await page.clock.runFor(30_000)
      expect(fixture.calls().reads).toEqual(stopped.reads)
    } finally {
      fixture.releaseAll()
    }
  })
}


test('an enhancement round slower than 800ms stays single-flight before the next round', async ({ page }) => {
  const fixture = await openEnhancementFixtureWithPausedClock(page, { mapMode: 'slow', stayMode: 'slow' })

  try {
    await fixture.waitForReads(1, 1)
    await page.clock.runFor(801)
    expect(fixture.calls().reads).toEqual({ map: 1, stay: 1 })
    expect(fixture.calls().maxInFlight).toEqual({ map: 1, stay: 1 })

    fixture.releaseAll()
    await fixture.waitForIdle()
    await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status','PREPARING')
    await expect(page.getByTestId('stay-panel')).toContainText('住宿仍在准备 1')
    await page.clock.runFor(799)
    expect(fixture.calls().reads).toEqual({ map: 1, stay: 1 })

    await page.clock.runFor(1)
    await fixture.waitForReads(2, 2)
    await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status','AVAILABLE')
    await expect(page.getByTestId('stay-panel')).toContainText('新住宿状态已读取')
    await expect(page.getByTestId('trip-check-item')).toHaveCount(0)
    expect(fixture.calls().maxInFlight).toEqual({ map: 1, stay: 1 })
  } finally {
    fixture.releaseAll()
  }
})


test('continuous PREPARING responses stop after eight bounded rounds and release Top-3', async ({ page }) => {
  const fixture = await openEnhancementFixtureWithPausedClock(page, {
    mapMode: 'preparing',
    stayMode: 'preparing',
  })

  try {
    await fixture.waitForReads(1, 1)
    await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status','PREPARING')
    for (let call = 2; call <= 7; call += 1) {
      await page.clock.runFor(800)
      await fixture.waitForReads(call, call)
      await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status','PREPARING')
    }
    await page.clock.runFor(800)
    await fixture.waitForReads(8, 8)

    await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status','UNAVAILABLE')
    await expect(page.getByTestId('stay-panel')).toContainText('住宿建议暂时不可用')
    await expect(page.getByTestId('trip-check-item')).toHaveCount(0)
    await openResultView(page, 'map_stay')
    await expectEnhancementRecovery(page)
    const stopped = fixture.calls()
    expect(stopped.reads).toEqual({ map: 8, stay: 8 })
    expect(stopped.maxInFlight).toEqual({ map: 1, stay: 1 })

    await page.clock.runFor(20_001)
    expect(fixture.calls().reads).toEqual(stopped.reads)
  } finally {
    fixture.releaseAll()
  }
})


for (const pendingKind of ['map', 'stay']) {
  const terminalKind = pendingKind === 'map' ? 'stay' : 'map'
  const terminalMessage = terminalKind === 'map' ? MAP_THEATER_MESSAGES.AVAILABLE : '新住宿状态已读取'
  const preparingMessage = pendingKind === 'map' ? MAP_THEATER_MESSAGES.PREPARING : '住宿仍在准备'
  const fallbackMessage = pendingKind === 'map' ? MAP_THEATER_MESSAGES.UNAVAILABLE : '住宿建议暂时不可用'
  const terminalPanel = terminalKind === 'map' ? 'map-theater' : 'stay-panel'
  const pendingPanel = pendingKind === 'map' ? 'map-theater' : 'stay-panel'

  test(`the 10000ms budget stops a slow PREPARING ${pendingKind} before round eight and preserves terminal ${terminalKind}`, async ({ page }) => {
    const fixture = await openEnhancementFixtureWithPausedClock(page, {
      mapMode: pendingKind === 'map' ? 'budget-preparing' : 'available',
      stayMode: pendingKind === 'stay' ? 'budget-preparing' : 'available',
    })

    try {
      await fixture.waitForReads(1, 1)
      for (let call = 1; call <= 3; call += 1) {
        await page.clock.runFor(2_000)
        fixture.releaseRead(pendingKind, call)
        if(pendingKind === 'map')await expect(page.getByTestId(pendingPanel)).toHaveAttribute('data-map-status','PREPARING')
        else await expect(page.getByTestId(pendingPanel)).toContainText(`${preparingMessage} ${call}`)
        if (terminalKind === 'map') await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status', 'AVAILABLE')
      else await expect(page.getByTestId(terminalPanel)).toContainText(terminalMessage)
        await page.clock.runFor(800)
        const nextPendingReads = call + 1
        await fixture.waitForReads(
          pendingKind === 'map' ? nextPendingReads : 1,
          pendingKind === 'stay' ? nextPendingReads : 1,
        )
      }

      expect(fixture.calls().reads).toEqual(pendingKind === 'map'
        ? { map: 4, stay: 1 }
        : { map: 1, stay: 4 })
      await page.clock.runFor(1_599)
      expect(fixture.calls().aborted[pendingKind]).toBe(0)
      await page.clock.runFor(1)
      await fixture.waitForAborts(
        pendingKind === 'map' ? 1 : 0,
        pendingKind === 'stay' ? 1 : 0,
      )
      fixture.releaseRead(pendingKind, 4)

      if (terminalKind === 'map') await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status', 'AVAILABLE')
      else await expect(page.getByTestId(terminalPanel)).toContainText(terminalMessage)
      if(pendingKind === 'map')await expect(page.getByTestId(pendingPanel)).toHaveAttribute('data-map-status','UNAVAILABLE')
      else await expect(page.getByTestId(pendingPanel)).toContainText(fallbackMessage)
      await expect(page.getByTestId('trip-check-item')).toHaveCount(0)
      await openResultView(page, 'map_stay')
      await expectEnhancementRecovery(page)

      const stopped = fixture.calls()
      expect(stopped.reads[pendingKind]).toBe(4)
      expect(stopped.reads[terminalKind]).toBe(1)
      expect(stopped.reads[pendingKind]).toBeLessThan(8)
      expect(stopped.maxInFlight).toEqual({ map: 1, stay: 1 })
      expect(stopped.mapRenderPosts).toBe(0)
      await page.clock.runFor(20_001)
      expect(fixture.calls().reads).toEqual(stopped.reads)
    } finally {
      fixture.releaseAll()
    }
  })
}


test('manual enhancement recovery is GET-only, single-flight, and keeps completed Top-3', async ({ page }) => {
  const fixture = await openEnhancementFixtureWithPausedClock(page, {
    mapMode: 'failure',
    stayMode: 'available',
  })

  try {
    await fixture.waitForReads(1, 1)
    await expect(page.getByTestId('trip-check-item')).toHaveCount(0)
    await openResultView(page, 'map_stay')
    await expect(page.getByTestId('retry-enhancements')).toBeVisible()
    const beforeRecovery = fixture.calls()
    fixture.setModes('available', 'available')

    await page.getByTestId('retry-enhancements').evaluate((button) => {
      button.click()
      button.click()
    })
    await fixture.waitForReads(2, 2)
    await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status','AVAILABLE')
    await expect(page.getByTestId('stay-panel')).toContainText('新住宿状态已读取')
    await expect(page.getByTestId('retry-enhancements')).toHaveCount(0)
    await expect(page.getByTestId('retry-stay')).toHaveCount(0)
    await expect(page.getByTestId('trip-check-item')).toHaveCount(0)

    const recovered = fixture.calls()
    expect(recovered.reads).toEqual({ map: 2, stay: 2 })
    expect(recovered.materializeCalls).toBe(beforeRecovery.materializeCalls)
    expect(recovered.checksCalls).toBe(beforeRecovery.checksCalls)
    expect(recovered.mapRenderPosts).toBe(0)
    expect(recovered.maxInFlight).toEqual({ map: 1, stay: 1 })
  } finally {
    fixture.releaseAll()
  }
})


test('a generation change aborts old enhancement reads before the new session starts', async ({ page }) => {
  const fixture = await openEnhancementFixtureWithPausedClock(page, {
    mapMode: 'hang',
    stayMode: 'hang',
    switchGeneration: true,
    holdOldEnhancementRejections: true,
  })

  try {
    await fixture.waitForReads(1, 1)
    fixture.setModes('available', 'available')
    await page.evaluate(() =>
      window.dispatchEvent(new HashChangeEvent('hashchange')),
    )
    await fixture.waitForAborts(1, 1)
    await fixture.waitForOldRejectionsCaught()
    expect(fixture.calls().reads).toEqual({ map: 1, stay: 1 })
    expect(fixture.calls().inFlight).toEqual({ map: 0, stay: 0 })
    expect(await fixture.oldEnhancementState()).toEqual({
      endpointCalls: { map: 1, stay: 1 },
      caught: 2,
      finalized: 0,
      newStartedBeforeOldFinalized: false,
    })
    await page.clock.runFor(30_000)
    expect(fixture.calls().reads).toEqual({ map: 1, stay: 1 })

    await fixture.releaseOldRejections()
    await fixture.waitForOldRejectionsFinalized()
    await fixture.waitForReads(2, 2)
    await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status','AVAILABLE')
    await expect(page.getByTestId('stay-panel')).toContainText('新住宿状态已读取')
    await expect(page.getByTestId('trip-check-item')).toHaveCount(0)

    const current = fixture.calls()
    expect(current.newGenerationStartedBeforeOldSettled).toBe(false)
    expect(current.maxInFlight).toEqual({ map: 1, stay: 1 })
    expect(await fixture.oldEnhancementState()).toEqual({
      endpointCalls: { map: 2, stay: 2 },
      caught: 2,
      finalized: 2,
      newStartedBeforeOldFinalized: false,
    })
    fixture.releaseAll()
    await page.clock.runFor(30_000)
    expect(fixture.calls().reads).toEqual({ map: 2, stay: 2 })
    await expect(page.getByTestId('map-theater')).not.toContainText('旧代际迟到路线')
    await expect(page.getByTestId('stay-panel')).not.toContainText('旧代际迟到住宿')
  } finally {
    await fixture.releaseOldRejections().catch(() => {})
    fixture.releaseAll()
  }
})


test('a completed obsolete materialize drains before the current generation starts', async ({ page }) => {
  const fixture = await installRaceFixture(page)

  await page.goto('/trip/result')
  await expect(page.getByTestId('trip-days')).toBeVisible()
  await fixture.waitForMaterializeStart()
  await page.evaluate(() =>
    window.dispatchEvent(new HashChangeEvent('hashchange')),
  )
  await fixture.waitForSecondResultRead()
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('bt_active_trip_etag'))).toBe(ETAG_B)
  expect(fixture.calls().materializeCalls).toBe(1)
  fixture.releaseCompatibleMaterialize()
  await expect.poll(() => fixture.calls().checksCalls).toBe(1)
  await expect(page.getByTestId('result-view-checks')).toHaveCount(0)
  await flushTwoAnimationFrames(page)

  expect(fixture.calls()).toEqual({
    resultReads: 2,
    materializeCalls: 1,
    maxMaterializeInFlight: 1,
    checksCalls: 1,
    abortedMaterializeCalls: 0,
  })
  await page.close()
})


test('a hanging obsolete materialize is aborted before the current generation starts', async ({ page }) => {
  await installPausedClock(page)
  const fixture = await installRaceFixture(page, 'stale')

  await page.goto('/trip/result')
  await fixture.waitForMaterializeStart()
  await page.evaluate(() =>
    window.dispatchEvent(new HashChangeEvent('hashchange')),
  )
  await fixture.waitForSecondResultRead()
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('bt_active_trip_etag'))).toBe(ETAG_B)
  expect(fixture.calls()).toEqual({
    resultReads: 2,
    materializeCalls: 1,
    maxMaterializeInFlight: 1,
    checksCalls: 0,
    abortedMaterializeCalls: 0,
  })

  await page.clock.runFor(15_001)
  await expect.poll(() => fixture.calls().checksCalls).toBe(1)

  expect(fixture.calls()).toEqual({
    resultReads: 2,
    materializeCalls: 2,
    maxMaterializeInFlight: 1,
    checksCalls: 1,
    abortedMaterializeCalls: 1,
  })
  await page.close()
})


test('409 reads back the latest result before preparing checks again', async ({ page }) => {
  const fixture = await installRaceFixture(page, 'conflict')

  await page.goto('/trip/result')
  await expect.poll(() => fixture.calls().checksCalls).toBe(1)

  expect(fixture.calls()).toEqual({
    resultReads: 2,
    materializeCalls: 2,
    maxMaterializeInFlight: 1,
    checksCalls: 1,
    abortedMaterializeCalls: 0,
  })
  await page.close()
})


test.skip('RETIRED_CHECKS_PAGE: ordinary preparation failure is recoverable only after explicit retry', async ({ page }) => {
  const fixture = await installRaceFixture(page, 'failure')

  await page.goto('/trip/result')
  await openResultView(page, 'checks')
  const retry = page.getByRole('button', { name: '重新检查' })
  await expect(retry).toBeVisible()
  expect(fixture.calls().materializeCalls).toBe(1)
  expect(fixture.calls().checksCalls).toBe(0)

  await retry.click()
  await expect.poll(() => fixture.calls().checksCalls).toBe(1)
  expect(fixture.calls()).toEqual({
    resultReads: 1,
    materializeCalls: 2,
    maxMaterializeInFlight: 1,
    checksCalls: 1,
    abortedMaterializeCalls: 0,
  })
  await page.close()
})


const INTERACTION_REF = 'g03r-interaction-result'


function interactionResult() {
  const view = resultView('AVAILABLE')
  view.days = [
    {
      label: 'Day 1',
      activities: [
        activity('interaction-token-a', '故宫博物院'),
        { ...activity('interaction-token-b', '景山公园'), status: 'NEEDS_CONFIRMATION' },
      ],
    },
    { label: 'Day 2', activities: [activity('interaction-token-c', '天坛公园')] },
    { label: 'Day 3', activities: [] },
  ]
  return view
}


function interactionStayView(withCandidate = false) {
  return {
    ...clone(stayView),
    candidates: withCandidate ? [{
      candidate_token: 'stay-candidate-safe-token',
      name: '东城安心酒店',
      brand: '示例连锁',
      category: '酒店',
      area_or_address: '东城区中心区域',
      commute_summary: '通勤较均衡',
      max_single_leg_minutes: 28,
      transfer_count: 1,
      reason: '方便衔接每天首末站。',
      available_actions: ['CHOOSE_STAY'],
      selected: false,
    }] : [],
    available_actions: withCandidate ? ['CHOOSE_STAY'] : [],
  }
}


function clone(value) {
  return JSON.parse(JSON.stringify(value))
}


function rotateActivityTokens(view, revision) {
  view.days.forEach((day, dayIndex) => {
    day.activities.forEach((card, position) => {
      card.activity_token = `interaction-r${revision}-d${dayIndex + 1}-p${position + 1}`
    })
  })
}


function applyCommandToResult(view, command) {
  if (command.command_type === 'ACTIVITY_MOVE') {
    let moving = null
    for (const day of view.days) {
      const sourcePosition = day.activities.findIndex((card) => card.activity_token === command.activity_token)
      if (sourcePosition >= 0) {
        moving = day.activities.splice(sourcePosition, 1)[0]
        break
      }
    }
    if (!moving || !view.days[command.target_day_index - 1]) throw new Error('invalid move fixture command')
    const target = view.days[command.target_day_index - 1].activities
    target.splice(Math.max(0, Math.min(command.target_position, target.length)), 0, moving)
  } else if (command.command_type === 'ACTIVITY_DELETE') {
    for (const day of view.days) {
      day.activities = day.activities.filter((card) => card.activity_token !== command.activity_token)
    }
  } else if (command.command_type === 'ACTIVITY_INSERT') {
    view.days[command.day_index - 1].activities.splice(
      command.position,
      0,
      activity('inserted-interaction-token', command.name),
    )
  } else if (command.command_type === 'ACTIVITY_TEXT_EDIT') {
    for (const day of view.days) {
      const card = day.activities.find((item) => item.activity_token === command.activity_token)
      if (card) {
        card.name = command.name
        card.time_hint = command.time_hint
      }
    }
  } else if (command.command_type === 'PLACE_CONFIRM') {
    for(const day of view.days) {
      const card=day.activities.find(item=>item.activity_token===command.activity_token)
      if(card)Object.assign(card,{name:command.candidate_token.startsWith('fixture-place:')?decodeURIComponent(command.candidate_token.slice(14)):'故宫博物院',status:'READY',area_or_address:'北京市东城区景山前街4号'})
    }
  } else if (command.command_type === 'PLACE_REPLACE') {
    for (const day of view.days) {
      const card = day.activities.find((item) => item.activity_token === command.activity_token)
      if (card) Object.assign(card, command.replacement)
    }
  }
  view.map = {
    status: 'NEEDS_UPDATE',
    message: '卡片已调整，需要手动更新路线',
    available_actions: ['RENDER_MAP'],
  }
}


async function installInteractionFixture(page, {
  scenario = 'success',
  delayMs = 0,
  holdCommand = false,
  holdReadbackAfterCommand = false,
  holdMaterialize = false,
  racePreview = false,
  latePreviewOutcome = null,
  failInitialEnhancements = false,
  exposeWrites = false,
  holdMapWrite = false,
  holdSourceWrite = false,
  mode = 'DEMO',
  withUser = false,
  mapSnapshot = mapView,
  initialMapView = null,
  mapReadView = null,
  postCommandMapReadMode = 'normal',
  rolloverPreparing = false,
  longDay = false,
  pendingFirst = false,
} = {}) {
  let revision = 0
  let etag = 'tu3_interaction_0'
  const view = interactionResult()
  if(pendingFirst) view.days[0].activities[0].status='PLACE_PENDING'
  if(longDay) view.days[0].activities = Array.from({length:13},(_,i)=>({...activity('long-card-'+i,'景点'+(i+1)),status:i===8?'PLACE_PENDING':'READY'}))
  if (exposeWrites) {
    view.map = {
      status: 'NEEDS_UPDATE',
      message: '卡片有调整，需要手动更新路线',
      available_actions: ['RENDER_MAP'],
    }
    view.stay = interactionStayView(true)
  }
  if (initialMapView) view.map = clone(initialMapView)
  if (rolloverPreparing) {
    view.map = {
      status: 'NEEDS_UPDATE',
      message: '卡片已调整，需要手动更新路线',
      available_actions: ['RENDER_MAP'],
    }
    view.stay = {
      ...interactionStayView(false),
      status: 'PREPARING',
      message: '住宿仍在准备',
      area_summary: null,
      candidates: [],
    }
  }
  if (failInitialEnhancements) {
    view.map = {
      status: 'PREPARING',
      message: '正在准备路线',
      available_actions: [],
    }
    view.stay = {
      ...interactionStayView(false),
      status: 'PREPARING',
      message: '正在准备住宿建议',
    }
  }
  const commands = []
  const commandKeys = []
  const commandResponses = new Map()
  let commandApplications = 0
  const writes = { map: 0, stay: 0, adopt: 0, claim: 0, source: 0, trip: 0 }
  let mapRenderPosts = 0
  let mapRenderApplications = 0
  const mapRenderKeys = []
  const acceptedMapKeys = new Set()
  let directProviderRequests = 0
  let resultReads = 0
  let previewPosts = 0
  let previewAborts = 0
  let mapReads = 0
  let stayReads = 0
  let materializeCalls = 0
  let checksCalls = 0
  let materializeInFlight = false
  let writesBeforeMaterializeSettled = 0
  let readbackBlocked = false
  let readbackHeld = false
  let commandResponseCompleted = false
  let sourceDeleteCalls = 0
  let releaseCommand = null
  const materializeStarted = deferred()
  const releaseMaterialize = deferred()
  const twoPreviewsStarted = deferred()
  const releaseFirstPreview = deferred()
  const releaseSecondPreview = deferred()
  const latePreviewStarted = deferred()
  const latePreviewAborted = deferred()
  const releaseLatePreview = deferred()
  const latePreviewHandled = deferred()
  const releaseMapWrite = deferred()
  const readbackStarted = deferred()
  const releaseReadback = deferred()
  const releaseSourceWrite = deferred()
  const postCommandMapReadStarted = deferred()
  const postCommandMapReadFinished = deferred()
  const releasePostCommandMapRead = deferred()
  const sourceIdempotencyKeys = []
  let postCommandMapReadHandled = false
  const commandCompleted = deferred()
  const commandGate = holdCommand
    ? new Promise((resolve) => { releaseCommand = resolve })
    : null

  page.on('requestfailed', (request) => {
    if (request.url().endsWith('/changes/preview')) {
      previewAborts += 1
      latePreviewAborted.resolve()
      return
    }
    if (!request.url().endsWith('/materialize') || !materializeInFlight) return
    materializeInFlight = false
    releaseMaterialize.resolve()
  })

  page.on('request', (request) => {
    if (/amap|高德/i.test(request.url())) directProviderRequests += 1
    const pathname = new URL(request.url()).pathname
    let isWrite = false
    if (request.method() === 'POST' && pathname.endsWith('/map-renders')) { writes.map += 1; isWrite = true }
    if (request.method() === 'POST' && pathname.endsWith('/stay-selection')) { writes.stay += 1; isWrite = true }
    if (request.method() === 'POST' && pathname.endsWith('/changes/adopt')) { writes.adopt += 1; isWrite = true }
    if (request.method() === 'POST' && pathname.endsWith('/claim')) { writes.claim += 1; isWrite = true }
    if (request.method() === 'DELETE' && pathname.endsWith('/source')) { writes.source += 1; isWrite = true }
    if (request.method() === 'DELETE' && pathname.endsWith(`/${INTERACTION_REF}`)) { writes.trip += 1; isWrite = true }
    if (request.method() === 'POST' && pathname.endsWith('/commands')) isWrite = true
    if (isWrite && materializeInFlight) writesBeforeMaterializeSettled += 1
  })

  await page.addInitScript(({ resourceRef, initialEtag, activeMode, authenticated }) => {
    const originalFetch = window.fetch.bind(window)
    let firstWriteStarted = false
    window.__g03rWriteRace = { materializeInFlight: 0, writesBeforeMaterializeSettled: 0 }
    window.fetch = async (input, init) => {
      const requestUrl = typeof input === 'string' ? input : input.url
      const pathname = new URL(requestUrl, window.location.origin).pathname
      const method = (init?.method || (typeof input === 'string' ? 'GET' : input.method) || 'GET').toUpperCase()
      const isMaterialize = pathname.endsWith('/materialize')
      const isWrite = (
        (method === 'POST' && /\/(?:commands|map-renders|stay-selection|changes\/adopt|claim)$/.test(pathname))
        || (method === 'DELETE' && (pathname.endsWith('/source') || pathname.endsWith(`/${resourceRef}`)))
      )
      const tracksPreWriteMaterialize = isMaterialize && !firstWriteStarted
      if (isWrite && window.__g03rWriteRace.materializeInFlight > 0) {
        window.__g03rWriteRace.writesBeforeMaterializeSettled += 1
      }
      if (isWrite) firstWriteStarted = true
      if (tracksPreWriteMaterialize) window.__g03rWriteRace.materializeInFlight += 1
      try {
        return await originalFetch(input, init)
      } finally {
        if (tracksPreWriteMaterialize) window.__g03rWriteRace.materializeInFlight -= 1
      }
    }
    sessionStorage.setItem('bt_active_trip_ref', resourceRef)
    sessionStorage.setItem('bt_active_trip_mode', activeMode)
    sessionStorage.setItem('bt_active_trip_etag', initialEtag)
    if (authenticated) {
      localStorage.setItem('authToken', 'fixture-auth-token')
      localStorage.setItem('authUser', JSON.stringify({ userId: 'fixture-user', nickname: '测试用户' }))
    }
  }, { resourceRef: INTERACTION_REF, initialEtag: etag, activeMode: mode, authenticated: withUser })

  await page.route(`**/api/v3/trip-understandings/${INTERACTION_REF}/**`, async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname

    if (pathname.endsWith('/events')) {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
      return
    }

    if (pathname.endsWith('/result')) {
      resultReads += 1
      if (
        holdReadbackAfterCommand &&
        commandResponseCompleted &&
        !readbackHeld
      ) {
        readbackHeld = true
        readbackStarted.resolve()
        await releaseReadback.promise
      }
      if (readbackBlocked) {
        await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
        return
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: etag },
        body: JSON.stringify(clone(view)),
      })
      return
    }

    if (pathname.endsWith('/source') && request.method() === 'DELETE') {
      sourceDeleteCalls += 1
      sourceIdempotencyKeys.push(request.headers()['idempotency-key'])
      if (holdSourceWrite && sourceDeleteCalls === 1) await releaseSourceWrite.promise
      await route.fulfill({ status: 204, body: '' })
      return
    }

    if (pathname.endsWith('/commands') && request.method() === 'POST') {
      const command = request.postDataJSON()
      const commandKey = request.headers()['idempotency-key']
      commands.push(command)
      commandKeys.push(commandKey)
      if (commandGate) await commandGate
      if (delayMs) await new Promise((resolve) => setTimeout(resolve, delayMs))

      const replay = commandResponses.get(commandKey)
      if (replay) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          headers: { ETag: replay.etag, 'Idempotency-Replayed': 'true' },
          body: replay.body,
        })
        return
      }

      if (scenario === 'unacknowledged-concurrent-update') {
        if (commands.length === 1) {
          revision += 1
          view.days[0].activities.unshift(activity('server-concurrent-token', '服务端新增地点'))
          view.map = {
            status: 'NEEDS_UPDATE',
            message: '并发更新后需要手动更新路线',
            available_actions: ['RENDER_MAP'],
          }
          rotateActivityTokens(view, revision)
          etag = `tu3_interaction_${revision}`
          await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
          return
        }
        await route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({ detail: { code: 'REVISION_CONFLICT' } }),
        })
        return
      }

      if (scenario === 'unacknowledged-racing-conflict') {
        if (commands.length === 1) {
          await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
          return
        }
        revision += 1
        view.days[0].activities.unshift(activity('server-racing-token', '竞态后的服务端地点'))
        view.map = {
          status: 'NEEDS_UPDATE',
          message: '竞态更新后需要手动更新路线',
          available_actions: ['RENDER_MAP'],
        }
        rotateActivityTokens(view, revision)
        etag = `tu3_interaction_${revision}`
        readbackBlocked = true
        await route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({ detail: { code: 'REVISION_CONFLICT' } }),
        })
        return
      }

      if (scenario === 'failure' && commands.length === 1) {
        await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
        return
      }

      if (scenario === 'rejected' && commands.length === 1) {
        await route.fulfill({ status: 422, contentType: 'application/json', body: '{}' })
        return
      }

      if (scenario === 'rejected-readback-failure' && commands.length === 1) {
        readbackBlocked = true
        await route.fulfill({ status: 422, contentType: 'application/json', body: '{}' })
        return
      }

      if (scenario === 'conflict' && commands.length === 1) {
        revision += 1
        view.days[0].activities.unshift(activity('server-sync-token', '最新同步地点'))
        view.map = {
          status: 'NEEDS_UPDATE',
          message: '最新行程需要手动更新路线',
          available_actions: ['RENDER_MAP'],
        }
        view.stay = {
          ...view.stay,
          status: 'NEEDS_UPDATE',
          message: '最新行程需要重新准备住宿建议',
          area_summary: null,
          candidates: [],
          available_actions: [],
        }
        rotateActivityTokens(view, revision)
        etag = `tu3_interaction_${revision}`
        await route.fulfill({ status: 409, contentType: 'application/json', body: '{}' })
        return
      }

      applyCommandToResult(view, command)
      commandApplications += 1
      revision += 1
      rotateActivityTokens(view, revision)
      etag = `tu3_interaction_${revision}`
      if (scenario === 'readback-failure' && commands.length === 1) readbackBlocked = true
      commandResponseCompleted = true
      const responseBody = JSON.stringify({ status: 'APPLIED', changed_days: view.days.map((day) => day.label), map_readiness: 'NEEDS_UPDATE' })
      commandResponses.set(commandKey, { etag, body: responseBody })
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: etag },
        body: responseBody,
      })
      commandCompleted.resolve()
      return
    }

    if (pathname.endsWith('/map-renders/latest')) {
      mapReads += 1
      if (rolloverPreparing && mapRenderApplications > 0 && mapReads >= 3) {
        view.map = {
          status: 'AVAILABLE',
          message: '更新后的路线已准备',
          available_actions: ['VIEW_MAP'],
        }
      }
      const trackedPostCommandRead = (
        commands.length > 0
        && !postCommandMapReadHandled
        && postCommandMapReadMode !== 'normal'
      )
      if (trackedPostCommandRead) {
        postCommandMapReadHandled = true
        postCommandMapReadStarted.resolve()
        if (postCommandMapReadMode === 'delay') await releasePostCommandMapRead.promise
        if (postCommandMapReadMode === 'failure') {
          await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
          postCommandMapReadFinished.resolve()
          return
        }
      }
      if (failInitialEnhancements && mapReads === 1) {
        await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
        return
      }
      const authoritativeMap = mapReadView || view.map
      const status = authoritativeMap.status
      const currentMap = status === 'AVAILABLE'
        ? { ...clone(mapSnapshot), status, available_actions: authoritativeMap.available_actions }
        : { status, message: authoritativeMap.message, days: [], available_actions: authoritativeMap.available_actions }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(currentMap),
      })
      if (trackedPostCommandRead) postCommandMapReadFinished.resolve()
      return
    }

    if (pathname.endsWith('/map-renders') && request.method() === 'POST') {
      mapRenderPosts += 1
      const mapKey = request.headers()['idempotency-key']
      mapRenderKeys.push(mapKey)
      if (acceptedMapKeys.has(mapKey)) {
        await route.fulfill({
          status: 202,
          contentType: 'application/json',
          headers: { 'Idempotency-Replayed': 'true' },
          body: '{}',
        })
        return
      }
      acceptedMapKeys.add(mapKey)
      mapRenderApplications += 1
      if (rolloverPreparing) {
        view.map = {
          status: 'PREPARING',
          message: '更新后的路线正在准备',
          available_actions: [],
        }
      }
      if (holdMapWrite) await releaseMapWrite.promise
      await route.fulfill({ status: 202, contentType: 'application/json', body: '{}' })
      return
    }

    if (pathname.endsWith('/stay-suggestions')) {
      stayReads += 1
      if (rolloverPreparing && mapRenderApplications > 0 && stayReads >= 3)
        view.stay = interactionStayView(false)
      if (failInitialEnhancements && stayReads === 1) {
        await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
        return
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(view.stay) })
      return
    }

    if (pathname.endsWith('/stay-selection') && request.method() === 'POST') {
      revision += 1
      etag = `tu3_interaction_${revision}`
      view.stay.candidates = view.stay.candidates.map((candidate) => ({
        ...candidate,
        selected: candidate.candidate_token === 'stay-candidate-token-0001',
      }))
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: etag },
        body: JSON.stringify({ selected_stay: '东城安心酒店' }),
      })
      return
    }

    if (pathname.endsWith('/changes/preview') && request.method() === 'POST') {
      previewPosts += 1
      const previewCall = previewPosts
      if (latePreviewOutcome && previewCall === 2) {
        latePreviewStarted.resolve()
        await releaseLatePreview.promise
        try {
          if (latePreviewOutcome === 'failure') {
            await route.abort('failed')
          } else {
            await route.fulfill({
              status: 200,
              contentType: 'application/json',
              body: JSON.stringify({
                change_token: 'change-token-late',
                title: '迟到预览不应重开',
                summary: '用户已经关闭预览。',
                affected_days: ['Day 2'],
                before: ['旧安排'],
                after: ['迟到安排'],
                available_actions: ['ADOPT_CHANGE'],
              }),
            })
          }
        } catch {
          // The preview fetch was intentionally aborted when the user closed it.
        }
        latePreviewHandled.resolve()
        return
      }
      if (racePreview && previewCall === 1) {
        if (previewPosts === 2) twoPreviewsStarted.resolve()
        await releaseFirstPreview.promise
        await route.abort('failed')
        return
      }
      if (racePreview && previewCall === 2) {
        twoPreviewsStarted.resolve()
        await releaseSecondPreview.promise
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          change_token: previewCall === 2 ? 'change-token-new-safe' : 'change-token-safe',
          title: previewCall === 2 ? '最新步行衔接' : '补充午餐时间',
          summary: previewCall === 2 ? '优先保留新一次预览。' : '在两段参观之间留出午餐时间。',
          affected_days: [previewCall === 2 ? 'Day 3' : 'Day 1'],
          before: [previewCall === 2 ? '旧衔接' : '连续参观'],
          after: [previewCall === 2 ? '新衔接' : '中间预留午餐'],
          available_actions: ['ADOPT_CHANGE'],
        }),
      })
      return
    }

    if (pathname.endsWith('/materialize')) {
      materializeCalls += 1
      if (holdMaterialize && materializeCalls === 1) {
        materializeInFlight = true
        materializeStarted.resolve()
        await releaseMaterialize.promise
        if (!materializeInFlight) return
        materializeInFlight = false
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: etag },
        body: JSON.stringify({
          status: 'READY',
          message: '行程已准备好检查',
          calendar: 'Day 1 ～ Day 3',
          party_size: 2,
          checks_available: true,
        }),
      })
      return
    }

    if (pathname.endsWith('/checks')) {
      checksCalls += 1
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(checksView) })
      return
    }

    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  return {
    calls: () => ({
      commands: clone(commands),
      commandKeys: [...commandKeys],
      commandApplications,
      mapRenderPosts,
      mapRenderApplications,
      mapRenderKeys: [...mapRenderKeys],
      directProviderRequests,
      resultReads,
      previewPosts,
      previewAborts,
      mapReads,
      stayReads,
      materializeCalls,
      checksCalls,
      materializeInFlight,
      writesBeforeMaterializeSettled,
      writes: clone(writes),
    }),
    releaseCommand: () => releaseCommand?.(),
    recoverReadback: () => { readbackBlocked = false },
    waitForMaterializeStart: () => materializeStarted.promise,
    waitForTwoPreviews: () => twoPreviewsStarted.promise,
    releaseFirstPreview: () => releaseFirstPreview.resolve(),
    releaseSecondPreview: () => releaseSecondPreview.resolve(),
    waitForLatePreviewStart: () => latePreviewStarted.promise,
    waitForLatePreviewAbort: () => latePreviewAborted.promise,
    releaseLatePreview: () => releaseLatePreview.resolve(),
    waitForLatePreviewHandled: () => latePreviewHandled.promise,
    releaseMapWrite: () => releaseMapWrite.resolve(),
    waitForReadbackStart: () => readbackStarted.promise,
    releaseReadback: () => releaseReadback.resolve(),
    waitForCommandCompletion: () => commandCompleted.promise,
    releaseSourceWrite: () => releaseSourceWrite.resolve(),
    waitForPostCommandMapRead: () => postCommandMapReadStarted.promise,
    waitForPostCommandMapReadFinish: () => postCommandMapReadFinished.promise,
    releasePostCommandMapRead: () => releasePostCommandMapRead.resolve(),
    sourceIdempotencyKeys: () => [...sourceIdempotencyKeys],
    confirmMapJob: () => {
      view.map = {
        status: 'PREPARING',
        message: '路线任务已经开始，正在准备路线',
        available_actions: [],
      }
    },
    browserWriteRace: () => page.evaluate(() => window.__g03rWriteRace),
  }
}


async function installProcessingFixture(page) {
  const resourceRef = 'g03r-processing-result'
  let ready = false
  let resultReads = 0
  await page.addInitScript(({ reference }) => {
    sessionStorage.setItem('bt_active_trip_ref', reference)
    sessionStorage.setItem('bt_active_trip_mode', 'DEMO')
  }, { reference: resourceRef })

  await page.route(`**/api/v3/trip-understandings/${resourceRef}/**`, async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname.endsWith('/events')) {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
      return
    }
    if (pathname.endsWith('/result')) {
      resultReads += 1
      await route.fulfill({
        status: ready ? 200 : 202,
        contentType: 'application/json',
        headers: ready ? { ETag: 'tu3_processing_ready' } : {},
        body: JSON.stringify(ready
          ? resultView('AVAILABLE')
          : { status: 'PROCESSING', message: '正在整理行程', retry_after_ms: 1000 }),
      })
      return
    }
    if (pathname.endsWith('/map-renders/latest')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(mapView) })
      return
    }
    if (pathname.endsWith('/stay-suggestions')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(stayView) })
      return
    }
    if (pathname.endsWith('/materialize')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: 'tu3_processing_ready' },
        body: JSON.stringify({ status: 'READY', message: '已准备', calendar: 'Day 1 ～ Day 3', party_size: 2, checks_available: true }),
      })
      return
    }
    if (pathname.endsWith('/checks')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(checksView) })
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  return {
    calls: () => resultReads,
    makeReady: () => { ready = true },
  }
}


async function dayCardNames(page, dayIndex) {
  return page.getByTestId(`day-lane-${dayIndex}`).getByTestId('activity-card').locator('h3').allTextContents()
}


async function dispatchNativeDrag(page, source, target) {
  const dataTransfer = await page.evaluateHandle(() => new DataTransfer())
  await source.dispatchEvent('dragstart', { dataTransfer })
  await target.dispatchEvent('dragenter', { dataTransfer })
  await target.dispatchEvent('dragover', { dataTransfer })
  await target.dispatchEvent('drop', { dataTransfer })
  await source.dispatchEvent('dragend', { dataTransfer })
  await dataTransfer.dispose()
}


async function moveDownByDrag(page) {
  // Command-race tests use deterministic drag events, independent of dialog animation clocks.
  await dispatchNativeDrag(page, page.getByTestId('drag-handle-1-0'), page.getByTestId('drop-slot-1-2'))
}

async function expectMinimumTarget(locator, minimum = 48) {
  await expect(locator).toBeVisible()
  const size = await locator.evaluate((element) => {
    const box = element.getBoundingClientRect()
    return { width: box.width, height: box.height }
  })
  expect(size.width).toBeGreaterThanOrEqual(minimum)
  expect(size.height).toBeGreaterThanOrEqual(minimum)
}


for (const latePreviewOutcome of ['success', 'failure']) {
  test.skip(`RETIRED_CHECKS_PAGE: closing a pending preview aborts its late ${latePreviewOutcome} without reopening or polluting state`, async ({ page }) => {
    await installPausedClock(page)
    const fixture = await installInteractionFixture(page, { latePreviewOutcome })

    try {
      await page.goto('/trip/result')
      await expect(page.getByTestId('trip-check-item')).toHaveCount(0)
      await openResultView(page, 'checks')
      await page.getByTestId('preview-change').first().click()
      await expect(page.getByRole('region', { name: '补充午餐时间' })).toBeVisible()
      await page.getByRole('button', { name: '关闭改动预览' }).click()
      await openResultView(page, 'checks')

      await page.getByTestId('preview-change').nth(1).click()
      await fixture.waitForLatePreviewStart()
      await page.getByRole('button', { name: '关闭改动预览' }).click()
      await fixture.waitForLatePreviewAbort()
      await expect(page.getByTestId('change-preview')).toHaveCount(0)
      await expect(page.getByTestId('preview-change').first()).toBeEnabled()

      fixture.releaseLatePreview()
      await fixture.waitForLatePreviewHandled()
      await page.clock.runFor(30_000)
      await expect(page.getByTestId('change-preview')).toHaveCount(0)
      await expect(page.getByTestId('trip-checks')).not.toContainText('迟到预览不应重开')
      await expect(page.getByTestId('trip-checks')).not.toContainText('这项建议已经变化')
      const calls = fixture.calls()
      expect(calls.previewPosts).toBe(2)
      expect(calls.previewAborts).toBe(1)
      expect(calls.writes.adopt).toBe(0)
    } finally {
      fixture.releaseLatePreview()
    }
  })
}


test('desktop drag reorders within a day with one normalized command and no route render', async ({ page }) => {
  await page.setViewportSize({ width: 1680, height: 938 })
  const fixture = await installInteractionFixture(page)
  await page.goto('/trip/result')
  await expect(page.getByTestId('trip-days')).toBeVisible()

  await page.getByTestId('drag-handle-1-0').dragTo(page.getByTestId('drop-slot-1-2'))
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['景山公园', '故宫博物院'])
  await expect.poll(() => fixture.calls().commands.length).toBe(1)
  expect(fixture.calls().commands[0]).toMatchObject({
    command_type: 'ACTIVITY_MOVE',
    target_day_index: 1,
    target_position: 1,
  })
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['景山公园', '故宫博物院'])
  await expect(page.locator('[data-day-heading="1"]')).toBeFocused()
  await expect(page.getByTestId('transport-connector').first()).toContainText('路线需要更新')
  expect(fixture.calls().mapRenderPosts).toBe(0)
  expect(fixture.calls().directProviderRequests).toBe(0)
})


test('desktop drag moves a card into an existing empty day without creating another day', async ({ page }) => {
  await page.setViewportSize({ width: 1680, height: 938 })
  const fixture = await installInteractionFixture(page)
  await page.goto('/trip/result')
  await expect(page.getByTestId('trip-days')).toBeVisible()

  await dispatchNativeDrag(
    page,
    page.getByTestId('drag-handle-1-0'),
    page.getByTestId('drop-slot-3-0'),
  )
  await expect.poll(() => dayCardNames(page, 3)).toEqual(['故宫博物院'])
  await expect.poll(() => fixture.calls().commands.length).toBe(1)
  expect(fixture.calls().commands[0]).toMatchObject({
    command_type: 'ACTIVITY_MOVE',
    target_day_index: 3,
    target_position: 0,
  })
  await expect.poll(() => dayCardNames(page, 3)).toEqual(['故宫博物院'])
  await expect(page.getByTestId('day-lane-4')).toHaveCount(0)
  expect(fixture.calls().mapRenderPosts).toBe(0)
})


test('dropping beside the original position is a no-op and sends no command', async ({ page }) => {
  await page.setViewportSize({ width: 1680, height: 938 })
  const fixture = await installInteractionFixture(page)
  await page.goto('/trip/result')
  await expect(page.getByTestId('trip-days')).toBeVisible()

  await page.getByTestId('drag-handle-1-0').dragTo(page.getByTestId('drop-slot-1-1'))
  await expect(page.getByTestId('itinerary-live-status')).toContainText('仍在原位')
  expect(fixture.calls().commands).toHaveLength(0)
  expect(fixture.calls().mapRenderPosts).toBe(0)
})


test('drag cancellation outside every drop target keeps order and announces no request', async ({ page }) => {
  await page.setViewportSize({ width: 1680, height: 938 })
  const fixture = await installInteractionFixture(page)
  await page.goto('/trip/result')
  const handle = page.getByTestId('drag-handle-1-0')
  const dataTransfer = await page.evaluateHandle(() => new DataTransfer())
  await handle.dispatchEvent('dragstart', { dataTransfer })
  await handle.dispatchEvent('dragend', { dataTransfer })
  await dataTransfer.dispose()

  await expect(page.getByTestId('itinerary-live-status')).toContainText('拖动已取消')
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['故宫博物院', '景山公园'])
  expect(fixture.calls().commands).toHaveLength(0)
})


test('desktop keyboard drag previews locally and Escape cancels without a dialog', async ({ page }) => {
  await page.setViewportSize({ width: 1680, height: 938 })
  const fixture = await installInteractionFixture(page)
  await page.goto('/trip/result')
  const handle = page.getByTestId('drag-handle-1-0')
  await handle.focus()
  await page.keyboard.press('Enter')

  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByTestId('drop-preview')).toBeVisible()
  await page.keyboard.press('ArrowRight')
  expect(fixture.calls().commands).toHaveLength(0)
  await page.keyboard.press('Escape')
  await expect(handle).toBeFocused()
  await expect(page.getByTestId('drop-preview')).toHaveCount(0)
})


test('mobile and keyboard controls move within and across days with accessible targets', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.setViewportSize({ width: 390, height: 844 })
  const fixture = await installInteractionFixture(page, { exposeWrites: true })
  await page.goto('/trip/result')
  await expect(page.getByTestId('itinerary-workspace')).toHaveAttribute('data-reduced-motion', 'true')
  await expect(page.getByTestId('drag-handle-1-0')).toBeVisible()

  const down = page.getByRole('button', { name: '拖动 故宫博物院' })
  await expectMinimumTarget(down)
  await expectMinimumTarget(page.getByRole('button', { name: '拖动 故宫博物院' }))
  await expect(page.getByRole('button', { name: '删除 故宫博物院' })).toHaveCount(0)
  await expectMinimumTarget(page.getByTestId('day-1-add'))
  await openResultView(page, 'map_stay')
  await expectMinimumTarget(page.getByTestId('render-map'))
  await openStayTools(page)
  await expectMinimumTarget(page.getByTestId('choose-stay'))
  await openResultView(page, 'itinerary')
  await moveDownByDrag(page)
  await expect.poll(() => fixture.calls().commands.length).toBe(1)
  expect(fixture.calls().commands[0]).toMatchObject({ target_day_index: 1, target_position: 1 })

  const move = page.getByRole('button', { name: '拖动 故宫博物院' })
  await expect(move).toBeEnabled()
  await move.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await page.keyboard.press('ArrowDown')
  await page.keyboard.press('ArrowDown')
  await page.keyboard.press('Enter')

  await expect.poll(() => fixture.calls().commands.length).toBe(2)
  expect(fixture.calls().commands[1]).toMatchObject({ target_day_index: 3, target_position: 0 })
  await expect(page.getByTestId('drop-preview')).toHaveCount(0)
  await expect.poll(() => dayCardNames(page, 3)).toEqual(['故宫博物院'])
  await expect(page.locator('[data-day-heading="3"]')).toBeFocused()
  await expect(page.getByTestId('day-lane-4')).toHaveCount(0)
  expect(fixture.calls().mapRenderPosts).toBe(0)
})


test.skip('RETIRED_CHECKS_PAGE: mobile suggestion preview keeps accessible targets and reduced motion', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.setViewportSize({ width: 390, height: 844 })
  await installInteractionFixture(page)
  await page.goto('/trip/result')
  await expect(page.getByTestId('trip-check-item')).toHaveCount(0)
  await openResultView(page, 'checks')
  await expect(page.getByTestId('preview-change')).toHaveCount(3)
  await page.getByTestId('preview-change').first().click()
  await expect(page.getByTestId('change-preview')).toBeVisible()
  await expectMinimumTarget(page.getByRole('button', { name: '关闭改动预览' }))
  await expectMinimumTarget(page.getByTestId('adopt-change'))
  const runningMotionAnimations = await page.evaluate(() => document
    .getAnimations({ subtree: true })
    .filter((animation) => {
      if (animation.playState !== 'running' || !(animation.effect instanceof KeyframeEffect)) return false
      return animation.effect.getKeyframes().some((frame) => 'transform' in frame || 'opacity' in frame)
    }).length)
  expect(runningMotionAnimations).toBe(0)
})


test('reduced-motion navigation switches views without forced scrolling', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.setViewportSize({ width: 1680, height: 938 })
  await installInteractionFixture(page)
  await page.goto('/trip/result')
  await page.evaluate(() => {
    window.__g03rScrollBehavior = null
    Element.prototype.scrollIntoView = function scrollIntoView(options) {
      window.__g03rScrollBehavior = options?.behavior || 'auto'
    }
  })

  await openResultView(page, 'map_stay')
  await expect(page.getByTestId('desktop-nav-map_stay')).toHaveAttribute('aria-current', 'page')
  expect(await page.evaluate(() => window.__g03rScrollBehavior)).toBeNull()
})


test('card editor traps focus and restores it before accessible delete preserves an empty day', async ({ page }) => {
  const fixture = await installInteractionFixture(page)
  await page.goto('/trip/result')

  const addButton = page.getByTestId('day-3-add')
  await addButton.click()
  const addEditor = page.getByRole('dialog', { name: '新增地点' })
  const closeEditor = addEditor.getByRole('button', { name: '关闭编辑' })
  const nameEditor = addEditor.getByTestId('card-editor-name')
  await expect(nameEditor).toBeFocused()
  await closeEditor.focus()
  await page.keyboard.press('Shift+Tab')
  await expect(nameEditor).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(closeEditor).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(addEditor).toBeHidden()
  await expect(addButton).toBeFocused()

  const palaceCard = page.getByTestId('activity-card').filter({ hasText: '故宫博物院' })
  const palaceDetails = palaceCard.locator('button').filter({ hasText: '故宫博物院' })
  await palaceDetails.click()
  await expect(page.getByRole('button', { name: '删除这张卡片' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '删除 故宫博物院' })).toHaveCount(0)
  const inline = page.getByTestId('pending-place-dropdown')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(inline.getByRole('textbox')).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(inline).toHaveCount(0)
  await expect(palaceDetails).toBeFocused()
  await palaceDetails.click()
  await chooseInlinePlace(page,'北海公园')
  await inline.getByRole('button',{name:'使用这个地点'}).click()
  await expect(inline).toHaveCount(0)
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['北海公园', '景山公园'])

  const deleteButton = page.getByRole('button', { name: '拖动 天坛公园' })

  await deleteButton.press('Delete')
  const dialog = page.getByRole('dialog', { name: '删除“天坛公园”？' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('button', { name: '取消' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(deleteButton).toBeFocused()
  expect(fixture.calls().commands).toHaveLength(1)

  await deleteButton.press('Delete')
  await page.getByTestId('confirm-delete').click()
  await expect.poll(() => fixture.calls().commands.length).toBe(2)
  expect(fixture.calls().commands[1]).toMatchObject({ command_type: 'ACTIVITY_DELETE' })
  await expect.poll(() => dayCardNames(page, 2)).toEqual([])
  await expect(page.getByTestId('day-2-add')).toBeVisible()
  await expect(page.locator('[data-day-heading="2"]')).toBeFocused()
  expect(fixture.calls().mapRenderPosts).toBe(0)
})


test('one pending card command blocks every conflicting write surface', async ({ page }) => {
  const fixture = await installInteractionFixture(page, {
    exposeWrites: true,
    holdCommand: true,
    holdMaterialize: true,
    withUser: true,
  })
  await page.goto('/trip/result')
  await fixture.waitForMaterializeStart()
  await openResultView(page, 'map_stay')
  await expect(page.getByTestId('render-map')).toBeEnabled()
  await openStayTools(page)
  await page.getByTestId('choose-stay').click()
  await expect.poll(() => fixture.calls().writes.stay).toBe(1)
  expect(await fixture.browserWriteRace()).toEqual({
    materializeInFlight: 0,
    writesBeforeMaterializeSettled: 0,
  })
  await expect(page.getByTestId('trip-check-item')).toHaveCount(0)
  await openResultView(page, 'itinerary')
  await page.getByText('行程信息', { exact: true }).click()
  const down = page.getByRole('button', { name: '拖动 故宫博物院' })
  const stayControl = page.getByTestId('choose-stay')
  const conflictingWrites = [
    page.getByRole('button', { name: '拖动 故宫博物院' }),
    page.getByRole('button', { name: '拖动 天坛公园' }),
    page.getByTestId('day-1-add'),
    page.getByTestId('edit-assumption-destination'),
    page.getByRole('button', { name: '保存到账号' }),
    page.getByTestId('delete-entire-trip'),
  ]
  await expect(stayControl).toBeEnabled()
  for (const control of conflictingWrites) await expect(control).toBeEnabled()
  await moveDownByDrag(page)

  await expect.poll(() => fixture.calls().commands.length).toBe(1)
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['景山公园', '故宫博物院'])
  await expect(page.getByRole('button', { name: '拖动 故宫博物院' })).toBeDisabled()
  await expect(stayControl).toHaveCount(0)
  for (const control of conflictingWrites) {
    await expect(control).toBeDisabled()
    await control.evaluate((element) => element.click())
  }
  await openResultView(page, 'map_stay')
  await expect(page.getByTestId('render-map')).toBeDisabled()
  await page.getByTestId('render-map').evaluate((element) => element.click())
  expect(fixture.calls().commands).toHaveLength(1)
  expect(fixture.calls().writes).toEqual({ map: 0, stay: 1, adopt: 0, claim: 0, source: 0, trip: 0 })

  fixture.releaseCommand()
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['景山公园', '故宫博物院'])
  await expect(page.getByTestId('render-map')).toBeEnabled()
  expect(fixture.calls().commands).toHaveLength(1)
})


test('a write and its readback share one deadline before explicit GET-only recovery', async ({ page }) => {
  await installPausedClock(page)
  const fixture = await installInteractionFixture(page, {
    holdCommand: true,
    holdReadbackAfterCommand: true,
    mapSnapshot: connectedMapView(),
  })

  try {
    await page.goto('/trip/result')
    await moveDownByDrag(page)
    await expect.poll(() => fixture.calls().commands.length).toBe(1)

    await page.clock.runFor(6_000)
    fixture.releaseCommand()
    await fixture.waitForReadbackStart()
    await page.clock.runFor(8_999)
    await expect(page.getByTestId('result-operation-status')).toHaveCount(0)

    await page.clock.runFor(2)
    await expect(page.getByTestId('result-operation-status')).toContainText('保存结果暂时无法确认')
    const retry = page.getByTestId('retry-result-readback')
    await expect(retry).toBeVisible()
    expect(fixture.calls().commands).toHaveLength(1)
    expect(fixture.calls().mapRenderPosts).toBe(0)
    expect(fixture.calls().directProviderRequests).toBe(0)

    await openResultView(page, 'map_stay')
    await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status','NEEDS_UPDATE')
    await expect(page.getByTestId('map-route-summary')).toHaveCount(0)
    await expect(page.getByTestId('map-route-line')).toHaveCount(0)
    await expect(page.getByTestId('map-theater')).not.toContainText('12 分钟')
    await openRouteTools(page)
    await expect(page.getByRole('button', { name: '播放' })).toBeDisabled()
    expect(fixture.calls().mapRenderPosts).toBe(0)
    expect(fixture.calls().directProviderRequests).toBe(0)

    fixture.releaseReadback()
    await retry.click()
    await expect(retry).toBeHidden()
    await openResultView(page, 'itinerary')
    await expect(page.getByRole('button', { name: '拖动 故宫博物院' })).toBeEnabled()
    expect(fixture.calls().commands).toHaveLength(1)
  } finally {
    fixture.releaseCommand()
    fixture.releaseReadback()
  }
})


test('a hanging map write reaches a bounded recovery path without hiding it in another view', async ({ page }) => {
  await installPausedClock(page)
  const fixture = await installInteractionFixture(page, {
    exposeWrites: true,
    holdMapWrite: true,
  })

  try {
    await page.goto('/trip/result')
    await openResultView(page, 'map_stay')
    const renderMap = page.getByTestId('render-map')
    await expect(renderMap).toBeEnabled()
    await renderMap.click()
    await expect.poll(() => fixture.calls().writes.map).toBe(1)

    await page.clock.runFor(15_001)

    await expect(page.getByTestId('result-view-map-stay')).toBeVisible()
    await expect(page.getByTestId('map-route-summary')).toHaveCount(0)
    await expect(page.getByTestId('result-operation-status')).toContainText('路线更新等待时间较长')
    const retry = page.getByTestId('retry-result-readback')
    await expect(retry).toBeVisible()
    await expect(renderMap).toBeDisabled()
    await retry.click()
    await expect(retry).toBeHidden()
    await expect(page.getByTestId('result-operation-status')).toHaveCount(0)
    expect(fixture.calls().writes.map).toBe(2)
    expect(fixture.calls().mapRenderPosts).toBe(2)
    expect(fixture.calls().mapRenderApplications).toBe(1)
    expect(new Set(fixture.calls().mapRenderKeys).size).toBe(1)
    expect(fixture.calls().directProviderRequests).toBe(0)
  } finally {
    fixture.releaseMapWrite()
  }
})


for (const initialMapStatus of ['LIMITED', 'UNAVAILABLE']) {
  test(`an unacknowledged map write safely replays its key when ${initialMapStatus} reads back unchanged`, async ({ page }) => {
    await installPausedClock(page)
    const fixture = await installInteractionFixture(page, {
      holdMapWrite: true,
      initialMapView: {
        status: initialMapStatus,
        message: `${initialMapStatus} 路线状态尚未变化`,
        days: [],
        available_actions: ['RENDER_MAP'],
      },
    })

    try {
      await page.goto('/trip/result')
      await openResultView(page, 'map_stay')
      const renderMap = page.getByTestId('render-map')
      await expect(renderMap).toBeEnabled()
      await expect.poll(() => fixture.calls().mapReads).toBeGreaterThanOrEqual(1)
      await renderMap.click()
      await expect.poll(() => fixture.calls().writes.map).toBe(1)

      await page.clock.runFor(15_001)
      const retry = page.getByTestId('retry-result-readback')
      await expect(retry).toBeVisible()
      await expect(renderMap).toBeDisabled()

      await retry.click()
      await expect(retry).toBeHidden()
      await expect(page.getByTestId('result-operation-status')).toHaveCount(0)
      expect(fixture.calls().writes.map).toBe(2)
      expect(fixture.calls().mapRenderPosts).toBe(2)
      expect(fixture.calls().mapRenderApplications).toBe(1)
      expect(new Set(fixture.calls().mapRenderKeys).size).toBe(1)
      expect(fixture.calls().directProviderRequests).toBe(0)
    } finally {
      fixture.releaseMapWrite()
    }
  })
}


for (const initialMapView of [
  { status: 'LIMITED', message: '部分路线可查看', days: [], available_actions: ['VIEW_MAP'] },
  { status: 'UNAVAILABLE', message: '路线暂时不可用', days: [], available_actions: [] },
]) {
  test(`${initialMapView.status} without RENDER_MAP never exposes a map write`, async ({ page }) => {
    const fixture = await installInteractionFixture(page, { initialMapView })
    await page.goto('/trip/result')
    await openResultView(page, 'map_stay')

    await expect(page.getByTestId('map-theater')).toBeVisible()
    await expect(page.getByTestId('render-map')).toHaveCount(0)
    expect(fixture.calls().writes.map).toBe(0)
    expect(fixture.calls().mapRenderPosts).toBe(0)
  })
}


test('an acknowledged map write accepts the same authoritative LIMITED terminal state', async ({ page }) => {
  const fixture = await installInteractionFixture(page, {
    initialMapView: {
      status: 'LIMITED',
      message: '部分路线已经准备',
      days: [],
      available_actions: ['RENDER_MAP'],
    },
  })
  await page.goto('/trip/result')
  await openResultView(page, 'map_stay')
  const renderMap = page.getByTestId('render-map')
  await expect(renderMap).toBeEnabled()
  const readsBeforeRender = fixture.calls().mapReads

  await renderMap.click()
  await expect.poll(() => fixture.calls().writes.map).toBe(1)
  await expect.poll(() => fixture.calls().mapReads).toBeGreaterThan(readsBeforeRender)
  await expect(page.getByTestId('retry-result-readback')).toHaveCount(0)
  await expect(renderMap).toBeEnabled()
  expect(fixture.calls().mapRenderPosts).toBe(1)
  expect(fixture.calls().directProviderRequests).toBe(0)
})


test('a new map cycle keeps polling when stay was already preparing', async ({ page }) => {
  await installPausedClock(page)
  const fixture = await installInteractionFixture(page, {
    rolloverPreparing: true,
    mapSnapshot: connectedMapView(),
  })
  await page.goto('/trip/result')
  await openResultView(page, 'map_stay')
  await expect(page.getByTestId('stay-panel')).toContainText('住宿仍在准备')
  const renderMap = page.getByTestId('render-map')
  await expect(renderMap).toBeEnabled()

  await renderMap.click()
  await expect.poll(() => fixture.calls().mapRenderApplications).toBe(1)
  await expect.poll(() => fixture.calls().mapReads).toBeGreaterThanOrEqual(2)
  await expect.poll(() => fixture.calls().stayReads).toBeGreaterThanOrEqual(2)
  await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status','PREPARING')

  await page.clock.runFor(801)
  await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status', 'AVAILABLE')
  await expect(page.getByTestId('stay-panel')).toContainText('住宿建议已准备')
  expect(fixture.calls().mapReads).toBeGreaterThanOrEqual(3)
  expect(fixture.calls().stayReads).toBeGreaterThanOrEqual(3)
})


test('a hanging editor write recovers with the same key without applying the command twice', async ({ page }) => {
  await installPausedClock(page)
  const fixture = await installInteractionFixture(page, { holdCommand: true })

  try {
    await page.goto('/trip/result')
    await page.getByRole('heading', { name: '故宫博物院' }).click()
    const editor = await chooseInlinePlace(page,'故宫（北京）')
    await editor.getByRole('button',{name:'使用这个地点'}).click()
    await expect.poll(() => fixture.calls().commands.length).toBe(1)

    await page.clock.runFor(15_001)

    await expect(editor).toBeVisible()
    await expect(editor.getByRole('textbox')).toBeDisabled()
    await expect(page.getByTestId('result-operation-status')).toContainText('调整保存等待时间较长')
    const retry = page.getByTestId('retry-result-readback')
    await expect(retry).toBeVisible()
    await page.clock.runFor(40)
    await expect(retry).toBeFocused()

    await retry.click()
    await expect(page.getByTestId('result-operation-status')).toContainText('服务端版本尚未确认这次操作')
    await expect(retry).toBeVisible()
    expect(fixture.calls().commands).toHaveLength(1)

    fixture.releaseCommand()
    await fixture.waitForCommandCompletion()
    await retry.click()
    await expect(retry).toBeHidden()
    await expect(editor).toHaveCount(0)
    await expect(page.getByRole('button', { name: '拖动 故宫（北京）' })).toBeEnabled()
    expect(fixture.calls().commands).toHaveLength(2)
    expect(new Set(fixture.calls().commandKeys).size).toBe(1)
    expect(fixture.calls().commandApplications).toBe(1)
  } finally {
    fixture.releaseCommand()
  }
})


test.skip('RETIRED_CHECKS_PAGE: a hanging suggestion preview stops at its deadline and leaves a retryable check', async ({ page }) => {
  await installPausedClock(page)
  const fixture = await installInteractionFixture(page, { racePreview: true })

  try {
    await page.goto('/trip/result')
    await expect(page.getByTestId('trip-check-item')).toHaveCount(0)
    await openResultView(page, 'checks')
    await page.getByTestId('preview-change').first().click()
    await expect.poll(() => fixture.calls().previewPosts).toBe(1)

    await page.clock.runFor(15_001)

    await expect(page.getByTestId('change-preview')).toHaveCount(0)
    await expect(
      page
        .getByText(
          '建议预览等待时间较长，已安全停止；你可以重新预览。',
          { exact: true },
        )
        .first(),
    ).toBeVisible()
    await page.getByRole('button', { name: '返回行程' }).click()
    await openResultView(page, 'checks')
    await expect(page.getByTestId('preview-change').first()).toBeEnabled()
  } finally {
    fixture.releaseFirstPreview()
  }
})


test('source deletion timeout replays only after consent with the same idempotency key', async ({ page }) => {
  await installPausedClock(page)
  const fixture = await installInteractionFixture(page, {
    holdSourceWrite: true,
    mode: 'CLAIMED',
    withUser: true,
  })

  try {
    await page.goto('/trip/result')
    await page.getByLabel('更多行程操作').click()
    await page.getByTestId('delete-trip-source').click()
    await page.getByTestId('confirm-delete-source').click()
    await expect.poll(() => fixture.calls().writes.source).toBe(1)

    await page.clock.runFor(15_001)
    await expect(page.getByRole('dialog', { name: '删除攻略原文？' })).toContainText('尚未确认删除结果，请稍后重试。')
    await expect(page.getByTestId('delete-trip-source')).not.toHaveText('原文已删除')
    expect(fixture.sourceIdempotencyKeys()).toHaveLength(1)

    fixture.releaseSourceWrite()
    await page.getByTestId('confirm-delete-source').click()
    await expect.poll(() => fixture.calls().writes.source).toBe(2)
    await expect(page.getByTestId('delete-trip-source')).toHaveText('原文已删除')
    expect(fixture.sourceIdempotencyKeys()).toHaveLength(2)
    expect(fixture.sourceIdempotencyKeys()[1]).toBe(fixture.sourceIdempotencyKeys()[0])
  } finally {
    fixture.releaseSourceWrite()
  }
})


test('claimed-mode source and trip deletion stay blocked during card reconciliation', async ({ page }) => {
  const fixture = await installInteractionFixture(page, {
    holdCommand: true,
    mode: 'CLAIMED',
    withUser: true,
  })
  await page.goto('/trip/result')
  const sourceDelete = page.getByTestId('delete-trip-source')
  const tripDelete = page.getByTestId('delete-entire-trip')
  await expect(sourceDelete).toBeEnabled()
  await expect(tripDelete).toBeEnabled()

  await moveDownByDrag(page)
  await expect.poll(() => fixture.calls().commands.length).toBe(1)
  await expect(sourceDelete).toBeDisabled()
  await expect(tripDelete).toBeDisabled()
  await sourceDelete.evaluate((element) => element.click())
  await tripDelete.evaluate((element) => element.click())
  expect(fixture.calls().writes.source).toBe(0)
  expect(fixture.calls().writes.trip).toBe(0)

  fixture.releaseCommand()
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['景山公园', '故宫博物院'])
})


test('a rejected move restores authoritative order without claiming the server did not save', async ({ page }) => {
  const fixture = await installInteractionFixture(page, { scenario: 'rejected' })
  await page.goto('/trip/result')
  await moveDownByDrag(page)

  await expect(page.getByText('这次修改没有被接受，请检查时间、地点或安排后重试。')).toBeVisible()
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['故宫博物院', '景山公园'])
  await expect(page.locator('[data-day-heading="1"]')).toBeFocused()
  await expect(page.locator('body')).not.toContainText('没有保存')
  expect(fixture.calls().commands).toHaveLength(1)
  expect(fixture.calls().mapRenderPosts).toBe(0)
})


test('a rejected move restores its prior order even when the follow-up read fails', async ({ page }) => {
  const fixture = await installInteractionFixture(page, {
    scenario: 'rejected-readback-failure',
  })
  await page.goto('/trip/result')
  await moveDownByDrag(page)

  await expect(page.getByText('这次修改没有被接受，请检查时间、地点或安排后重试。')).toBeVisible()
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['故宫博物院', '景山公园'])
  await expect(page.getByRole('button', { name: '拖动 故宫博物院' })).toBeEnabled()
  expect(fixture.calls().commands).toHaveLength(1)
  expect(fixture.calls().commandApplications).toBe(0)
})


for (const { mode: postCommandMapReadMode, label } of [
  { mode: 'delay', label: 'is delayed' },
  { mode: 'failure', label: 'fails' },
]) {
  test(`a rejected card write cannot grant map rendering while its authoritative map read ${label}`, async ({ page }) => {
    const fixture = await installInteractionFixture(page, {
      scenario: 'rejected',
      mapSnapshot: connectedMapView(),
      initialMapView: {
        status: 'AVAILABLE',
        message: '结果摘要确认路线可用',
        available_actions: ['VIEW_MAP'],
      },
      mapReadView: {
        status: 'AVAILABLE',
        message: '权威地图仍是当前版本',
        days: [],
        available_actions: ['VIEW_MAP'],
      },
      postCommandMapReadMode,
    })

    try {
      await page.goto('/trip/result')
      await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status','AVAILABLE')
      await expect(page.getByTestId('render-map')).toHaveCount(0)
      await moveDownByDrag(page)

      await fixture.waitForPostCommandMapRead()
      await openResultView(page, 'map_stay')
      await expect(page.getByTestId('render-map')).toHaveCount(0)
      expect(fixture.calls().commands).toHaveLength(1)
      expect(fixture.calls().writes.map).toBe(0)
      expect(fixture.calls().mapRenderPosts).toBe(0)
      expect(fixture.calls().directProviderRequests).toBe(0)

      fixture.releasePostCommandMapRead()
      await fixture.waitForPostCommandMapReadFinish()
      await expect(page.getByText('这次修改没有被接受，请检查时间、地点或安排后重试。')).toBeVisible()
      await expect.poll(() => dayCardNames(page, 1)).toEqual(['故宫博物院', '景山公园'])
      if (postCommandMapReadMode === 'failure') {
        await expectEnhancementRecovery(page)
        await expect(page.getByTestId('map-route-summary')).toHaveCount(0)
        await expect(page.getByTestId('map-route-line')).toHaveCount(0)
        await expect(page.getByTestId('map-theater')).not.toContainText(/步行 \d+ 分钟/)
      }
      await expect(page.getByTestId('render-map')).toHaveCount(0)
      expect(fixture.calls().mapRenderPosts).toBe(0)
    } finally {
      fixture.releasePostCommandMapRead()
    }
  })
}


test('rejected delete restores its card and returns focus to the original delete control', async ({ page }) => {
  const fixture = await installInteractionFixture(page, { scenario: 'rejected' })
  await page.goto('/trip/result')
  const deleteButton = page.getByRole('button', { name: '拖动 天坛公园' })
  await deleteButton.press('Delete')
  await page.getByTestId('confirm-delete').click()

  await expect.poll(() => dayCardNames(page, 2)).toEqual(['天坛公园'])
  await expect(deleteButton).toBeFocused()
  await expect(page.getByRole('dialog', { name: '删除“天坛公园”？' })).toBeHidden()
  expect(fixture.calls().commands).toHaveLength(1)
})


test('accepted command with failed readback stays locked until explicit recovery', async ({ page }) => {
  const fixture = await installInteractionFixture(page, { scenario: 'readback-failure' })
  await page.goto('/trip/result')
  await expect(page.getByTestId('trip-check-item')).toHaveCount(0)
  await openResultView(page, 'map_stay')
  await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status','AVAILABLE')
  await expect(page.getByTestId('stay-panel')).toContainText('已准备')
  await openResultView(page, 'itinerary')
  await moveDownByDrag(page)

  const retryReadback = page.getByTestId('retry-result-readback')
  await expect(retryReadback).toBeVisible()
  await expect(page.getByText(/调整已提交，但保存结果暂时无法确认/)).toBeVisible()
  await expect(page.locator('body')).not.toContainText(/没有保存|未保存/)
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['景山公园', '故宫博物院'])
  await expect(page.getByTestId('change-preview')).toHaveCount(0)
  await expect(page.getByTestId('trip-check-item')).toHaveCount(0)
  await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status','NEEDS_UPDATE')
  await expect(page.getByTestId('map-route-summary')).toHaveCount(0)
  await expect(page.getByTestId('stay-panel')).not.toContainText('已准备')
  await expect(page.getByTestId('itinerary-workspace').getByText('路线需要更新', { exact: true }).first()).toBeVisible()
  await openResultView(page, 'map_stay')
  await page.getByTestId('stay-panel').locator('summary').click()
  await expect(page.getByText('行程已调整，住宿建议需要重新确认。', { exact: true })).toBeVisible()
  await expect(page.getByTestId('render-map')).toHaveCount(0)

  await openResultView(page, 'itinerary')
  await expect(page.getByRole('button', { name: '拖动 故宫博物院' })).toBeDisabled()
  fixture.recoverReadback()
  await retryReadback.click()
  await expect(page.getByText('已读取服务端最新行程，可以继续调整。')).toBeVisible()
  await expect(retryReadback).toBeHidden()
  await expect(page.getByRole('button', { name: '拖动 故宫博物院' })).toBeEnabled()
  await expect.poll(() => fixture.calls().resultReads).toBeGreaterThanOrEqual(3)
  expect(fixture.calls().commands).toHaveLength(1)
})


test('an unrelated ETag change cannot confirm an unacknowledged card write', async ({ page }) => {
  const fixture = await installInteractionFixture(page, {
    scenario: 'unacknowledged-concurrent-update',
  })
  await page.goto('/trip/result')
  await moveDownByDrag(page)

  const retryReadback = page.getByTestId('retry-result-readback')
  await expect(retryReadback).toBeVisible()
  await retryReadback.click()

  await expect(retryReadback).toBeHidden()
  await expect(page.getByText('卡片刚刚有更新，已显示服务端版本；请核对后再试。')).toBeVisible()
  await expect.poll(() => dayCardNames(page, 1)).toEqual([
    '服务端新增地点',
    '故宫博物院',
    '景山公园',
  ])
  expect(fixture.calls().commands).toHaveLength(2)
  expect(new Set(fixture.calls().commandKeys).size).toBe(1)
  expect(fixture.calls().commandApplications).toBe(0)
})


test('a conflict after the recovery pre-read stays locked until a fresh read succeeds', async ({ page }) => {
  const fixture = await installInteractionFixture(page, {
    scenario: 'unacknowledged-racing-conflict',
  })
  await page.goto('/trip/result')
  await moveDownByDrag(page)

  const retryReadback = page.getByTestId('retry-result-readback')
  await retryReadback.click()
  await expect(retryReadback).toBeVisible()
  expect(fixture.calls().commands).toHaveLength(1)

  await retryReadback.click()
  await expect(page.getByText('这次操作未被接受，但最新服务端版本暂时无法读取；请再次确认。')).toBeVisible()
  await expect(retryReadback).toBeVisible()
  await expect(page.getByRole('button', { name: '拖动 故宫博物院' })).toBeDisabled()
  expect(fixture.calls().commands).toHaveLength(2)

  fixture.recoverReadback()
  await retryReadback.click()
  await expect(retryReadback).toBeHidden()
  await expect.poll(() => dayCardNames(page, 1)).toEqual([
    '竞态后的服务端地点',
    '故宫博物院',
    '景山公园',
  ])
  expect(fixture.calls().commands).toHaveLength(2)
  expect(fixture.calls().commandApplications).toBe(0)
})


test('a lost claim response survives reload and expired login before same-key recovery', async ({ page }) => {
  const oldResource = 'claim-response-lost-old-001'
  const newResource = 'claim-response-lost-new-001'
  const claimKeys = []
  let oldResourceGone = false
  const anonymous = interactionResult()
  anonymous.ownership = 'ANONYMOUS'
  anonymous.is_demo = true
  const claimed = clone(anonymous)
  claimed.ownership = 'ACCOUNT'
  claimed.is_demo = false

  await page.addInitScript((reference) => {
    sessionStorage.setItem('bt_active_trip_ref', reference)
    sessionStorage.setItem('bt_active_trip_mode', 'DEMO')
    sessionStorage.setItem('bt_active_trip_etag', 'tu3_claim_before')
    localStorage.setItem('authToken', 'fixture-auth-token')
    localStorage.setItem('authUser', JSON.stringify({ userId: 'fixture-user', nickname: '测试用户' }))
  }, oldResource)

  await page.route('**/api/v3/trip-understandings/**', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    const isNew = pathname.includes(`/${newResource}/`)
    if (pathname.endsWith('/result')) {
      if (!isNew && oldResourceGone) {
        await route.fulfill({ status: 410, contentType: 'application/json', body: '{}' })
        return
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: isNew ? 'tu3_claim_after' : 'tu3_claim_before' },
        body: JSON.stringify(isNew ? claimed : anonymous),
      })
      return
    }
    if (pathname.endsWith('/claim') && request.method() === 'POST') {
      claimKeys.push(request.headers()['idempotency-key'])
      oldResourceGone = true
      if (claimKeys.length <= 2) {
        await route.abort('failed')
        return
      }
      if (claimKeys.length === 3) {
        await route.fulfill({ status: 401, contentType: 'application/json', body: '{}' })
        return
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: 'tu3_claim_after', 'Idempotency-Replayed': 'true' },
        body: JSON.stringify({ status: 'CLAIMED', public_resource_id: newResource }),
      })
      return
    }
    if (pathname.endsWith('/events')) {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
      return
    }
    if (pathname.endsWith('/map-renders/latest')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(mapView) })
      return
    }
    if (pathname.endsWith('/stay-suggestions')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(stayView) })
      return
    }
    if (pathname.endsWith('/supplementary')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'UNAVAILABLE', days: [] }) })
      return
    }
    if (pathname.endsWith('/materialize')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: isNew ? 'tu3_claim_after' : 'tu3_claim_before' },
        body: JSON.stringify({ status: 'READY', message: '检查已准备', calendar: 'Day 1 ～ Day 3', party_size: 2, checks_available: true }),
      })
      return
    }
    if (pathname.endsWith('/checks')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(checksView) })
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
  await page.route('**/api/auth/email-login', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        token: 'refreshed-fixture-token',
        user_id: 'fixture-user',
        nickname: '测试用户',
      }),
    })
  })

  await page.goto('/trip/result')
  await page.getByRole('button', { name: '保存到账号' }).click()
  await expect(page.getByTestId('retry-result-readback')).toBeVisible()
  await page.reload()

  await expect.poll(() => claimKeys.length).toBe(3)
  await expect(page.getByRole('button', { name: '重新登录并恢复保存' })).toBeVisible()
  expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem('bt_pending_operation')).key)).toBe(claimKeys[0])
  expect(new Set(claimKeys).size).toBe(1)
  await page.getByRole('button', { name: '重新登录并恢复保存' }).click()
  await expect(page).toHaveURL(/\/login$/)
  await page.getByLabel('邮箱', { exact: true }).fill('owner@example.com')
  await page.getByLabel('密码', { exact: true }).fill('password123')
  await page.getByRole('button', { name: '登录并继续' }).click()

  await expect.poll(() => claimKeys.length).toBeGreaterThanOrEqual(4)
  expect(new Set(claimKeys).size).toBe(1)
  await expect(page).toHaveURL(new RegExp(`#trip=${newResource}$`))
  await expect(page.getByRole('heading', { name: '故宫博物院' })).toBeVisible()
  await page.getByTestId('retry-result-readback').click()
  await expect(page.getByTestId('retry-result-readback')).toBeHidden()
  expect(new Set(claimKeys).size).toBe(1)
})


test('409 reads latest cards and invalidates an old available map without rendering', async ({ page }) => {
  const fixture = await installInteractionFixture(page, { scenario: 'conflict' })
  await page.goto('/trip/result')
  await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status', 'AVAILABLE')
  await moveDownByDrag(page)

  await expect(page.getByText('卡片刚刚有更新，已为你读取最新版本，请再试一次。')).toBeVisible()
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['最新同步地点', '故宫博物院', '景山公园'])
  await expect(page.getByText('路线需要更新', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('路线已准备', { exact: true })).toHaveCount(0)
  await openResultView(page, 'map_stay')
  await expect(page.getByTestId('map-route-summary')).toHaveCount(0)
  await expect(page.getByTestId('stay-panel')).toContainText('最新行程需要重新准备住宿建议')
  await expect(page.getByTestId('choose-stay')).toHaveCount(0)
  expect(fixture.calls().commands).toHaveLength(1)
  expect(fixture.calls().mapRenderPosts).toBe(0)
})


test('public result DOM contains no provider URL or internal implementation vocabulary', async ({ page }) => {
  const fixture = await installInteractionFixture(page, { failInitialEnhancements: true })
  await page.goto('/trip/result')
  await expect(page.getByTestId('trip-days')).toBeVisible()
  await expect(page.getByTestId('trip-check-item')).toHaveCount(0)
  await expect(page.getByTestId('map-theater')).toHaveAttribute('data-map-status','UNAVAILABLE')
  await expect(page.getByTestId('stay-panel')).toContainText('住宿建议暂时不可用')
  expect(fixture.calls().mapReads).toBe(1)
  expect(fixture.calls().stayReads).toBe(1)
  expect(fixture.calls().checksCalls).toBe(1)
  const publicDom = await page.evaluate(() => {
    const clone = document.body.cloneNode(true)
    clone.querySelectorAll('script, style').forEach((element) => element.remove())
    return clone.innerHTML
  })
  expect(publicDom).not.toContain(INTERACTION_REF)
  expect(publicDom).not.toMatch(/interaction-(?:token|r\d)|activity_token|public_resource_id|Provider|AMap|高德|revision|receipt|\bUID\b|\bhash\b/i)
  expect(publicDom).not.toMatch(/https:\/\/(?:restapi\.)?amap\.com|provider[_-]?(?:url|resource)/i)
  expect(fixture.calls().directProviderRequests).toBe(0)
  await expect(page.getByTestId('activity-card').filter({hasText:'待确认'})).not.toHaveCount(0)
  expect(publicDom).not.toMatch(/(?:text|bg|border)-red-/)
})


test('two-view shell defaults to cards and preserves the loaded map state', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  await installInteractionFixture(page, { mapSnapshot: connectedMapView() })
  await page.goto('/trip/result')

  await expect(page.getByTestId('result-view-itinerary')).toBeVisible()
  await expect(page.getByTestId('result-view-map-stay')).toBeHidden()
  await expect(page.getByTestId('result-view-checks')).toBeHidden()
  await expect(page.getByTestId('desktop-nav-itinerary')).toHaveAttribute('aria-current', 'page')
  expect((await page.getByTestId('result-desktop-nav').boundingBox()).width).toBeCloseTo(68, 0)
  await page.getByTestId('desktop-nav-map_stay').focus()
  await expect.poll(async () => (await page.getByTestId('result-desktop-nav').boundingBox()).width).toBeGreaterThanOrEqual(180)

  await openResultView(page, 'map_stay')
  await expect(page.getByTestId('map-place-directory')).toBeVisible()
  await expect(page.getByTestId('map-directory-toggle')).toHaveAttribute('aria-expanded', 'true')
  const dayColors = await page.evaluate(() => {
    const dot = document.querySelector('[data-testid="map-place-directory"] [data-day-index="0"] span')
    const line = document.querySelector('[data-testid="map-route-line"]')
    const itinerary = document.querySelector('[data-testid="itinerary-day-color"]')
    return {
      dot: getComputedStyle(dot).backgroundColor,
      line: getComputedStyle(line).stroke,
      itinerary: getComputedStyle(itinerary).backgroundColor,
    }
  })
  expect(dayColors.line).toBe(dayColors.dot)
  expect(dayColors.itinerary).toBe(dayColors.dot)
  await openRouteTools(page)
  await page.getByTestId('map-mode-transit').click()
  await expect(page.getByTestId('map-mode-transit')).toHaveAttribute('aria-pressed', 'true')

  await openResultView(page, 'itinerary')
  await expect(page.getByTestId('result-view-checks')).toHaveCount(0)
  await openResultView(page, 'map_stay')
  await expect(page.getByTestId('map-mode-transit')).toHaveAttribute('aria-pressed', 'true')
  await openResultView(page, 'itinerary')
  await expect(page.getByRole('heading', { name: '故宫博物院' })).toBeVisible()
})


test('strict adjacent connector drops old minutes and offers only manual rendering', async ({ page }) => {
  const fixture = await installInteractionFixture(page, {
    mapSnapshot: connectedMapView(),
    postCommandMapReadMode: 'delay',
  })

  try {
    await page.goto('/trip/result')

    const connector = page.getByTestId('transport-connector').first()
    await expect(connector).toContainText('步行 · 12 分钟')
    await expect(connector).toHaveAttribute('data-connector-status', 'AVAILABLE')

    const palaceCard = page.getByTestId('activity-card').filter({ hasText: '故宫博物院' })
    await palaceCard.locator('button').filter({ hasText: '故宫博物院' }).click()
    const editor=await chooseInlinePlace(page,'故宫（北京）')
    await editor.getByRole('button',{name:'使用这个地点'}).click()

    await expect.poll(() => fixture.calls().commands.length).toBe(1)
    await fixture.waitForPostCommandMapRead()
    const staleConnector = page.getByLabel('路线需要更新').first()
    await expect(staleConnector).toContainText('路线需要更新')
    await expect(staleConnector).not.toContainText('12 分钟')
    await expect(staleConnector).toHaveAttribute(
      'data-connector-status',
      'NEEDS_UPDATE',
    )
    await openResultView(page, 'map_stay')
    await expect(page.getByTestId('render-map')).toBeEnabled()
    expect(fixture.calls().mapRenderPosts).toBe(0)
    expect(fixture.calls().directProviderRequests).toBe(0)

    fixture.releasePostCommandMapRead()
    await expect(page.getByTestId('render-map')).toBeEnabled()
    expect(fixture.calls().mapRenderPosts).toBe(0)
  } finally {
    fixture.releasePostCommandMapRead()
  }
})


test('map keeps its directory and server summary when geometry is absent', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await installInteractionFixture(page, { mapSnapshot: connectedMapView({ geometry: false }) })
  await page.goto('/trip/result')
  await openResultView(page, 'map_stay')

  await expect(page.getByTestId('map-route-line')).toHaveCount(0)
  await expect(page.getByTestId('map-place-directory')).toContainText('故宫博物院')
  await expect(page.getByTestId('map-place-directory')).toContainText('景山公园')
  await expect(page.getByTestId('map-theater')).toContainText('故宫博物院 → 景山公园')
  await expect(page.getByTestId('route-map')).toBeVisible()
  await expect(page.getByTestId('map-theater')).toContainText('步行 12 分钟')
  await expect(page.getByTestId('stay-panel')).toBeVisible()

  const toggle = page.getByTestId('map-directory-toggle')
  await toggle.focus()
  await page.keyboard.press('Space')
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await page.keyboard.press('Enter')
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
})


test('route playback is paused by default and never requests another route', async ({ page }) => {
  const fixture = await installInteractionFixture(page, { mapSnapshot: connectedMapView() })
  await page.goto('/trip/result')
  await openResultView(page, 'map_stay')

  await openRouteTools(page)
  const playback = page.getByTestId('route-playback')
  const play = playback.getByRole('button', { name: '播放' })
  await expect(playback).toContainText('计划路线模拟')
  await expect(play).toHaveAttribute('aria-pressed', 'false')
  const before = fixture.calls()

  await play.click()
  await expect(playback.getByRole('button', { name: '暂停' })).toHaveAttribute('aria-pressed', 'true')
  await playback.getByRole('button', { name: '暂停' }).click()
  await expect(playback.getByRole('button', { name: '播放' })).toHaveAttribute('aria-pressed', 'false')

  expect(fixture.calls().mapRenderPosts).toBe(before.mapRenderPosts)
  expect(fixture.calls().directProviderRequests).toBe(0)
})


test('route playback keeps station controls when verified geometry is absent', async ({ page }) => {
  const fixture = await installInteractionFixture(page, {
    mapSnapshot: connectedMapView({ geometry: false }),
  })
  await page.goto('/trip/result')
  await openResultView(page, 'map_stay')

  await openRouteTools(page)
  const playback = page.getByTestId('route-playback')
  await expect(playback.getByRole('button', { name: '播放' })).toBeDisabled()
  await expect(playback).toContainText('地图动画不可用')
  await expect(playback.getByRole('button', {name:'播放'})).toBeDisabled()
  await expect(playback).not.toContainText('这是已核对计划路线')
  await playback.getByRole('button', { name: '下一站' }).click()
  await expect(playback).toContainText('第 2/2 站')
  expect(fixture.calls().mapRenderPosts).toBe(0)
  expect(fixture.calls().directProviderRequests).toBe(0)
})


test('PNG export renders the complete structured chain and downloads locally', async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 })
  await page.addInitScript(() => {
    window.__pngDrawnText = []
    const original = CanvasRenderingContext2D.prototype.fillText
    CanvasRenderingContext2D.prototype.fillText = function (text, ...args) {
      window.__pngDrawnText.push(String(text))
      return original.call(this, text, ...args)
    }
  })
  const fixture = await installInteractionFixture(page, { mapSnapshot: connectedMapView() })
  await page.goto('/trip/result')
  await expect(page.getByTestId('transport-connector').first()).toContainText('12 分钟')

  await page.getByTestId('export-itinerary-png').click()
  const preview = page.getByTestId('png-preview')
  await expect(preview).toBeVisible()
  const image = page.getByAltText('完整行程横链导出预览')
  await expect.poll(() => image.evaluate((node) => ({ width: node.naturalWidth, height: node.naturalHeight })))
    .toMatchObject({ width: 1440 })
  const drawn = await page.evaluate(() => window.__pngDrawnText)
  expect(drawn).toContain('故宫博物院')
  expect(drawn).toContain('景山公园')
  expect(drawn).toContain('步行约 12 分钟 · 900 米')
  expect(drawn.some((text) => text.includes('未包含'))).toBe(true)

  const downloadPromise = page.waitForEvent('download')
  await page.getByTestId('download-itinerary-png').click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toMatch(/^行程查-\d{4}-\d{2}-\d{2}\.png$/)
  expect(fixture.calls().mapRenderPosts).toBe(0)
  expect(fixture.calls().directProviderRequests).toBe(0)
})


test('assumption and privacy confirmations use one keyboard-safe dialog pattern', async ({ page }) => {
  const nativeDialogs = []
  page.on('dialog', async (dialog) => {
    nativeDialogs.push(dialog.type())
    await dialog.dismiss()
  })
  await installInteractionFixture(page, { mode: 'CLAIMED', withUser: true })
  await page.goto('/trip/result')

  await page.locator('.e-trip-information > summary').click()
  const assumptionTrigger = page.getByTestId('edit-assumption-destination')
  await assumptionTrigger.click()
  const assumptionDialog = page.getByRole('dialog', { name: '修改目的地' })
  await expect(assumptionDialog).toHaveAttribute('data-testid', 'nearby-assumption-editor')
  await expect(assumptionDialog.getByTestId('assumption-editor-input')).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(assumptionTrigger).toBeFocused()

  await page.getByRole('group').filter({ has: page.getByLabel('更多行程操作') }).getByLabel('更多行程操作').click()
  const sourceTrigger = page.getByTestId('delete-trip-source')
  await sourceTrigger.click()
  const sourceDialog = page.getByRole('dialog', { name: '删除攻略原文？' })
  const sourceCancel = sourceDialog.getByRole('button', { name: '取消' })
  const sourceClose = sourceDialog.getByRole('button', { name: '关闭删除确认' })
  const sourceConfirm = sourceDialog.getByTestId('confirm-delete-source')
  await expect(sourceCancel).toBeFocused()
  await sourceClose.focus()
  await page.keyboard.press('Shift+Tab')
  await expect(sourceConfirm).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(sourceClose).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByLabel('更多行程操作')).toBeFocused()

  await page.getByLabel('更多行程操作').click()
  const tripTrigger = page.getByTestId('delete-entire-trip')
  await tripTrigger.click()
  await expect(page.getByRole('dialog', { name: '永久删除整份行程？' }).getByRole('button', { name: '取消' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByLabel('更多行程操作')).toBeFocused()
  expect(nativeDialogs).toEqual([])
})


test('PROCESSING tolerates an older partial payload, pauses at ninety seconds, and can recover', async ({ page }) => {
  await installPausedClock(page)
  const fixture = await installProcessingFixture(page)
  await page.goto('/trip/result')
  await expect(page.getByTestId('generation-stages')).toBeVisible()
  await expect(page.getByRole('button', { name: '停止整理' })).toBeVisible()
  await expect.poll(fixture.calls).toBeGreaterThan(0)

  await page.clock.runFor(89_999)
  await expect(page.getByRole('button', { name: '继续等待' })).toHaveCount(0)
  await page.clock.runFor(2)
  await expect(page.getByRole('button', { name: '继续等待' })).toBeVisible()
  await expect(page.getByRole('link', { name: '重新整理' })).toBeVisible()
  const stoppedReads = fixture.calls()
  await page.clock.runFor(5_000)
  expect(fixture.calls()).toBe(stoppedReads)

  fixture.makeReady()
  await page.getByRole('button', { name: '继续等待' }).click()
  await expect(page.getByTestId('result-view-itinerary')).toBeVisible()
})


for (const viewport of [
  { width: 1440, height: 900 },
  { width: 1280, height: 720 },
  { width: 1024, height: 768 },
  { width: 390, height: 844 },
  { width: 360, height: 800 },
]) {
  test(`${viewport.width}x${viewport.height} keeps navigation and core actions reachable without page overflow`, async ({ page }) => {
    await page.setViewportSize(viewport)
    await installInteractionFixture(page, { mapSnapshot: connectedMapView({ geometry: false }) })
    await page.goto('/trip/result')

    await expect(page.getByTestId('day-1-add')).toBeVisible()
    await page.getByTestId('day-1-add').click({ trial: true })
    await openResultView(page, 'map_stay')
    await page.getByTestId('map-directory-toggle').click({ trial: true })
    await expect(page.getByTestId('result-view-checks')).toHaveCount(0)
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    expect(overflow).toBeLessThanOrEqual(1)

    const mobile = viewport.width < 1024
    const nav = page.getByTestId(`${mobile ? 'mobile' : 'desktop'}-nav-map_stay`)
    await expect(nav).toBeVisible()
    await expect(nav).toHaveAttribute('aria-current', 'page')
    const box = await nav.boundingBox()
    expect(box).not.toBeNull()
    expect(box.x).toBeGreaterThanOrEqual(0)
    expect(box.y).toBeGreaterThanOrEqual(0)
    expect(box.x + box.width).toBeLessThanOrEqual(viewport.width + 1)
    expect(box.y + box.height).toBeLessThanOrEqual(viewport.height + 1)
    if (mobile) {
      await openResultView(page, 'map_stay')
      await expect(page.getByTestId('map-directory-toggle')).toHaveAttribute('aria-expanded', 'false')
    }
  })
}


test('home stays text-only, frozen legacy entrances redirect, and collaboration requires login', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByTestId('trip-source-text')).toBeVisible()
  await expect(page.getByLabel('你的攻略或行程')).toBeVisible()
  await expect(page.getByRole('button', { name: '整理行程' })).toBeVisible()
  await expect(page.getByRole('link', { name: '协同规划' })).toBeVisible()
  await expect(page.locator('input[type="file"]')).toHaveCount(0)
  await expect(page.getByText(/OCR|识别图片/)).toHaveCount(0)

  await page.getByRole('link', { name: '隐私与数据' }).click()
  await expect(page).toHaveURL(/\/about#privacy$/)
  await expect(page.getByText('当前只接收主动粘贴的文字，不接收截图。')).toBeVisible()
  await page.getByRole('link', { name: '返回首页' }).click()

  for (const legacyPath of ['/history', '/import', '/intake', '/templates', '/workspace/example']) {
    await page.goto(legacyPath)
    await expect(page).toHaveURL(/\/$/)
  }

  await page.goto('/room/example')
  await expect(page).toHaveURL(/\/login$/)
  expect(await page.evaluate(() => sessionStorage.getItem('bt_login_return'))).toBe('/room/example')
})


test('login network failures use safe non-red retry copy', async ({ page }) => {
  await page.route('**/api/auth/email-login', route => route.abort('failed'))
  await page.goto('/login')
  const email = page.getByLabel('邮箱')
  const password = page.getByLabel('密码')
  const submit = page.locator('form.e-auth-form button[type="submit"]')
  await expect(email).toBeVisible()
  await expect(password).toBeVisible()
  await email.fill('traveler@example.com')
  await password.fill('Example123')
  await submit.click()

  const feedback = page.locator('p.e-message[role="alert"]')
  await expect(feedback).toBeVisible()
  await expect(feedback).toHaveText('暂时没有收到登录结果，请稍后重试。')
  await expect(feedback).toHaveClass(/e-message/)
  await expect(feedback).not.toHaveClass(/text-red-/)
  await expect(submit).toBeEnabled()
  await expect(page.locator('body')).not.toContainText(/Failed to fetch|NetworkError|ERR_FAILED|Provider|revision|receipt/i)
})


test('login stops at one 15000ms deadline when response headers arrive but the JSON body stalls', async ({ page }) => {
  await installPausedClock(page)
  await page.addInitScript(() => {
    const originalFetch = window.fetch.bind(window)
    window.__g03rAuthBodyStall = { calls: 0, bodyReads: 0, aborts: 0 }
    window.fetch = async (input, init) => {
      const requestUrl = typeof input === 'string' ? input : input.url
      const pathname = new URL(requestUrl, window.location.origin).pathname
      if (pathname !== '/api/auth/email-login') return originalFetch(input, init)

      window.__g03rAuthBodyStall.calls += 1
      let bodyController
      const body = new ReadableStream({
        start(controller) {
          bodyController = controller
          controller.enqueue(new TextEncoder().encode('{"token":'))
        },
      })
      const response = new Response(body, {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
      const readJson = response.json.bind(response)
      Object.defineProperty(response, 'json', {
        configurable: true,
        value: () => {
          window.__g03rAuthBodyStall.bodyReads += 1
          const signal = init?.signal
          const abortBody = () => {
            window.__g03rAuthBodyStall.aborts += 1
            bodyController.error(new DOMException('Aborted', 'AbortError'))
          }
          if (signal?.aborted) abortBody()
          else signal?.addEventListener('abort', abortBody, { once: true })
          return readJson()
        },
      })
      return response
    }
  })

  await page.goto('/login')
  const email = page.getByLabel('邮箱')
  const password = page.getByLabel('密码')
  const submit = page.locator('form.e-auth-form button[type="submit"]')
  await email.fill('traveler@example.com')
  await password.fill('Example123')
  await submit.click()
  await expect.poll(() => page.evaluate(() => window.__g03rAuthBodyStall)).toEqual({
    calls: 1,
    bodyReads: 1,
    aborts: 0,
  })
  await expect(submit).toBeDisabled()

  await page.clock.runFor(14_999)
  await expect(submit).toBeDisabled()
  await expect(page.locator('p.e-message[role="alert"]')).toHaveCount(0)

  await page.clock.runFor(2)
  const feedback = page.locator('p.e-message[role="alert"]')
  await expect(feedback).toBeVisible()
  await expect(feedback).toHaveText('暂时没有收到登录结果，请稍后重试。')
  await expect(submit).toBeEnabled()
  await expect.poll(() => page.evaluate(() => window.__g03rAuthBodyStall.aborts)).toBe(1)

  await submit.click()
  await expect.poll(() => page.evaluate(() => window.__g03rAuthBodyStall.calls)).toBe(2)
  await expect.poll(() => page.evaluate(() => window.__g03rAuthBodyStall.bodyReads)).toBe(2)
  await expect(submit).toBeDisabled()
  await page.clock.runFor(15_001)
  await expect(submit).toBeEnabled()
})


test('email registration has permanent accessible names and a keyboard-safe password check', async ({ page }) => {
  await page.goto('/login')
  const registerTab = page.getByRole('button', { name: '注册账号' })
  await registerTab.click()
  await expect(registerTab).toHaveAttribute('aria-pressed', 'true')
  const email = page.getByLabel('邮箱')
  const password = page.getByLabel('密码')
  const nickname = page.getByLabel('称呼（选填）')
  const submit = page.getByRole('button', { name: '注册并继续' })
  await expect(email).toHaveAttribute('autocomplete', 'email')
  await expect(password).toHaveAttribute('autocomplete', 'new-password')
  await expect(nickname).toHaveAttribute('autocomplete', 'nickname')
  await email.fill('traveler@example.com')
  await password.fill('abcdefgh')
  await submit.click()
  await expect(page.locator('p.e-message[role="alert"]')).toHaveText('密码请使用 8–64 位字符，并包含字母和数字。')
  await email.focus()
  await page.keyboard.press('Tab')
  await expect(password).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(nickname).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(submit).toBeFocused()
})

test('owner feedback: drag hover opens an insertion gap and trash drop saves once', async ({page}) => {
  await page.setViewportSize({width:1440,height:900})
  const fixture=await installInteractionFixture(page, {mapSnapshot:connectedMapView()})
  await page.goto('/trip/result')
  const handle=page.getByTestId('drag-handle-1-0')
  await expect(handle).toBeVisible()
  await expect(page.getByRole('button',{name:'下移 故宫博物院'})).toHaveCount(0)
  await expect(page.getByRole('button',{name:'删除 故宫博物院'})).toHaveCount(0)
  await expect(page.getByRole('complementary',{name:'行程概览'})).toHaveCount(0)
  const dt=await page.evaluateHandle(()=>new DataTransfer())
  await handle.dispatchEvent('dragstart',{dataTransfer:dt})
  const slot=page.getByTestId('drop-slot-2-0')
  await slot.dispatchEvent('dragenter',{dataTransfer:dt})
  await expect(page.getByTestId('drop-preview')).toContainText('故宫博物院')
  await expect.poll(async()=>(await page.getByTestId('drop-preview').boundingBox()).width).toBeGreaterThanOrEqual(128)
  expect(fixture.calls().commands).toHaveLength(0)
  await page.getByTestId('drag-trash').dispatchEvent('drop',{dataTransfer:dt})
  await expect.poll(()=>fixture.calls().commands.length).toBe(1)
  expect(fixture.calls().commands[0].command_type).toBe('ACTIVITY_DELETE')
  await expect.poll(()=>dayCardNames(page,1)).toEqual(['景山公园'])
  expect(fixture.calls().mapRenderPosts).toBe(0)
  await dt.dispose()
})

test('owner feedback: map overview keeps all days while selecting a day for playback', async({page})=>{
  await page.setViewportSize({width:1440,height:900})
  const fixture=await installInteractionFixture(page,{mapSnapshot:connectedMapView()})
  await page.goto('/trip/result')
  await openResultView(page,'map_stay')
  const directory=page.getByTestId('map-place-directory')
  await expect(directory).toContainText('故宫博物院')
  await expect(directory).toContainText('天坛公园')
  const colors=await directory.locator('[data-day-index] > span:first-child').evaluateAll(nodes=>nodes.map(n=>n.style.backgroundColor))
  expect(colors[0]).not.toBe(colors[2])
  await page.getByRole('button',{name:'Day 2',exact:true}).click()
  await expect(directory).toContainText('故宫博物院')
  await expect(directory).toContainText('天坛公园')
  expect(fixture.calls().mapRenderPosts).toBe(0)
})

for (const width of [1440,1280,390,360]) {
  test(`owner feedback: quiet rounded home and reachable canvas at ${width}px`,async({page},testInfo)=>{
    await page.setViewportSize({width,height:900})
    await installInteractionFixture(page)
    await page.goto('/')
    await expect(page.getByTestId('trip-source-text')).toHaveAttribute('placeholder','粘贴行程，帮你整理地点、核对路线，生成清晰的行程卡片。')
    await expect(page.getByText('把攻略，整理成走得明白的行程')).toHaveCount(0)
    await expect(page.locator('.e-input-panel')).toHaveCSS('border-radius',width<640?'28px':'36px')
    await page.screenshot({path:testInfo.outputPath('home.png'),fullPage:true})
    await page.goto('/trip/result')
    const handle=page.getByTestId('drag-handle-1-0')
    await expect(handle).toBeVisible()
    await expectMinimumTarget(handle,44)
    await expect(page.getByTestId('sagging-chain')).toHaveCount(0)
    await expect(page.getByTestId('order-arc').first()).toBeVisible()
    await handle.focus()
    await page.keyboard.press('Enter')
    await expect(page.getByTestId('drop-preview')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(handle).toBeFocused()
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
    await page.screenshot({path:testInfo.outputPath('canvas.png'),fullPage:true})
  })
}

test('owner feedback: real touch drag moves across days and touch cancellation writes nothing',async({browser,baseURL})=>{
  const context=await browser.newContext({baseURL,viewport:{width:390,height:1100},hasTouch:true,isMobile:true})
  try{
    const page=await context.newPage()
    const fixture=await installInteractionFixture(page)
    await page.goto('/trip/result')
    const handle=page.getByTestId('drag-handle-1-0')
    await expect(handle).toBeVisible()
    const source=await handle.boundingBox()
    const target=await page.getByTestId('drop-slot-2-0').boundingBox()
    await expect(handle).toHaveCSS('touch-action','none')
    const client=await context.newCDPSession(page)
    await client.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:source.x+24,y:source.y+24}]})
    await expect(page.getByTestId('drag-trash')).toBeVisible()
    await client.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:target.x+8,y:target.y+35}]})
    await client.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]})
    await expect.poll(()=>fixture.calls().commands.length).toBe(1)
    expect(fixture.calls().commands[0]).toMatchObject({command_type:'ACTIVITY_MOVE',target_day_index:2,target_position:0})
    await expect.poll(()=>dayCardNames(page,2)).toEqual(['故宫博物院','天坛公园'])
    const next=await page.getByTestId('drag-handle-1-0').boundingBox()
    await client.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:next.x+24,y:next.y+24}]})
    await client.send('Input.dispatchTouchEvent',{type:'touchCancel',touchPoints:[]})
    await expect(page.getByTestId('drag-trash')).toHaveCount(0)
    expect(fixture.calls().commands.length).toBe(1)
    expect(fixture.calls().mapRenderPosts).toBe(0)
  }finally{await context.close()}
})

test('owner feedback: map SDK receives all-day markers and real geometry only after visible',async({page})=>{
  await page.addInitScript(()=>{
    window.__mapLayers=[]
    window.__mapWidth=0
    window.__mapCenters=[]
    window.AMap={
      Map:class{
        constructor(container){window.__mapWidth=container.clientWidth}
        on(name,callback){if(name==='complete')queueMicrotask(callback)}
        add(items){window.__mapLayers.push(...items)}
        remove(items){window.__mapLayers=window.__mapLayers.filter(item=>!items.includes(item))}
        destroy(){} setCenter(point){window.__mapCenters.push(point)} setFitView(){} resize(){}
      },
      Marker:class{constructor(options){this.kind='marker';this.options=options}},
      Polyline:class{constructor(options){this.kind='line';this.options=options}},
    }
  })
  const snapshot=connectedMapView()
  snapshot.points=[
    {activity_token:'interaction-token-a',name:'故宫博物院',position:{longitude:116.397,latitude:39.918,coordinate_system:'GCJ02'}},
    {activity_token:'interaction-token-b',name:'景山公园',position:{longitude:116.397,latitude:39.925,coordinate_system:'GCJ02'}},
    {activity_token:'interaction-token-c',name:'天坛公园',position:{longitude:116.410,latitude:39.882,coordinate_system:'GCJ02'}},
  ]
  await installInteractionFixture(page,{mapSnapshot:snapshot})
  await page.goto('/trip/result')
  await expect(page.getByTestId('trip-days')).toBeVisible()
  expect(await page.evaluate(()=>window.__mapWidth)).toBe(0)
  await openResultView(page,'map_stay')
  await expect.poll(()=>page.evaluate(()=>window.__mapWidth)).toBeGreaterThan(300)
  await expect.poll(()=>page.evaluate(()=>window.__mapLayers.filter(x=>x.kind==='marker').length)).toBe(3)
  expect(await page.evaluate(()=>window.__mapCenters)).toEqual([])
  const colors=await page.evaluate(()=>window.__mapLayers.filter(x=>x.kind==='marker').map(x=>x.options.content.style.backgroundColor))
  expect(colors[0]).not.toBe(colors[2])
  const paths=await page.evaluate(()=>window.__mapLayers.filter(x=>x.kind==='line').map(x=>x.options.path))
  expect(paths).toEqual([[[116.39,39.92],[116.40,39.93]]])
  await page.getByRole('button',{name:'Day 2',exact:true}).click()
  expect(await page.evaluate(()=>window.__mapLayers.filter(x=>x.kind==='marker').length)).toBe(3)
})

for (const [width,height] of [[1440,900],[1280,720],[390,844],[360,800]]) {
 test(`fluid: two views and the complete map fit the first screen at ${width}`,async({page},testInfo)=>{
  await page.setViewportSize({width,height})
  await installInteractionFixture(page,{mapSnapshot:connectedMapView()})
  await page.goto('/trip/result')
  await expect(page.getByTestId('trip-days')).toBeVisible()
  const nav=page.getByTestId(width>=1024?'result-desktop-nav':'result-mobile-nav')
  await expect(nav.getByRole('button')).toHaveCount(2)
  await expect(page.getByText('优先检查',{exact:true})).toHaveCount(0)
  await openResultView(page,'map_stay')
  await expect(page.getByText('全程地图',{exact:true})).toHaveCount(0)
  await expect(page.getByText('重试住宿',{exact:true})).toHaveCount(0)
  const box=await page.getByTestId('route-map').boundingBox()
  const legend=await page.getByLabel('日期颜色与预演选择').boundingBox()
  expect(box.height).toBeGreaterThan(220)
  expect(box.y).toBeGreaterThanOrEqual(0)
  expect(box.y+box.height).toBeLessThanOrEqual(height)
  expect(legend.y).toBeGreaterThanOrEqual(box.y+box.height-1)
  expect(legend.y+legend.height).toBeLessThanOrEqual(height-(width<1024?60:0))
  const before=await page.evaluate(()=>scrollY)
  await page.getByRole('button',{name:'Day 2',exact:true}).click()
  expect(await page.evaluate(()=>scrollY)).toBe(before)
  await page.screenshot({path:testInfo.outputPath('map-first-screen.png'),fullPage:true})
 })
}

test('fluid: information edits stay beside their trigger and preserve canvas and scroll',async({page})=>{
 await installInteractionFixture(page)
 await page.goto('/trip/result')
 await page.locator('.e-trip-information > summary').click()
 const before=await page.getByTestId('edit-assumption-destination').boundingBox()
 const scroll=await page.evaluate(()=>scrollY)
 await page.getByTestId('edit-assumption-destination').click()
 const editor=page.getByTestId('nearby-assumption-editor')
 await expect(editor).toBeVisible()
 await expect(page.getByTestId('assumption-editor-input')).toBeFocused()
 await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
 expect(await page.evaluate(()=>scrollY)).toBe(scroll)
 const after=await editor.boundingBox()
 expect(Math.abs(after.y-before.y)).toBeLessThan(60)
 await page.getByTestId('assumption-editor-input').fill('上海')
 await page.keyboard.press('Escape')
 await expect(editor.getByRole('alert')).toBeVisible()
 await editor.getByRole('button',{name:'放弃修改'}).click()
 await expect(page.getByTestId('edit-assumption-destination')).toBeFocused()
})

test('fluid: dragging the photo previews insertion, squeezes neighbors and saves once across dates',async({page},testInfo)=>{
 await page.setViewportSize({width:1440,height:1000})
 const fixture=await installInteractionFixture(page)
 await page.goto('/trip/result')
 const photo=page.getByTestId('card-grab-1-0')
 await expect(photo).toBeVisible()
 const source=await photo.boundingBox()
 const next=page.getByTestId('day-lane-2').getByTestId('activity-card').first()
 const before=await next.boundingBox()
 await page.mouse.move(source.x+40,source.y+55)
 await page.mouse.down()
 await page.mouse.move(before.x+10,before.y+40,{steps:12})
 await expect(page.getByTestId('drop-preview')).toContainText('故宫博物院')
 await expect.poll(async()=> (await next.boundingBox()).x).toBeGreaterThan(before.x+140)
 await expect(page.locator('.fluid-card-ghost')).toBeVisible()
 expect(fixture.calls().commands).toHaveLength(0)
 await page.screenshot({path:testInfo.outputPath('drag-preview.png'),fullPage:true})
 await page.mouse.up()
 await expect.poll(()=>fixture.calls().commands.length).toBe(1)
 expect(fixture.calls().commands[0]).toMatchObject({command_type:'ACTIVITY_MOVE',target_day_index:2,target_position:0})
 expect(fixture.calls().mapRenderPosts).toBe(0)
})

test('fluid: Escape while dragging cancels without saving',async({page})=>{
 const fixture=await installInteractionFixture(page)
 await page.goto('/trip/result')
 const photo=page.getByTestId('card-grab-1-0')
 await expect(photo).toBeVisible()
 const box=await photo.boundingBox()
 await page.mouse.move(box.x+30,box.y+40)
 await page.mouse.down()
 await page.mouse.move(box.x+90,box.y+70)
 await page.keyboard.press('Escape')
 await page.mouse.up()
 await expect(page.locator('.fluid-card-ghost')).toHaveCount(0)
 expect(fixture.calls().commands).toHaveLength(0)
})
async function openStayTools(page) {
 const panel=page.getByTestId('stay-panel')
 if(await panel.getAttribute('open')===null) await panel.locator(':scope > summary').click()
}
async function openRouteTools(page) {
 const panel=page.locator('.fluid-map-popover').filter({has:page.getByTestId('map-route-tools')})
 if(await panel.getAttribute('open')===null) await panel.locator(':scope > summary').click()
}
async function expectEnhancementRecovery(page) {
 if(await page.getByTestId('retry-enhancements').count()) await expect(page.getByTestId('retry-enhancements')).toBeVisible()
 else { await openStayTools(page); await expect(page.getByTestId('retry-stay')).toBeVisible() }
}

for(const width of [1440,1280,390,360]) {
 test(`serpentine: compact chronological rows turn without horizontal scrolling at ${width}`,async({page},testInfo)=>{
  await page.setViewportSize({width,height:900})
  await page.emulateMedia({reducedMotion:'reduce'})
  const fixture=await installInteractionFixture(page,{longDay:true})
  await page.goto('/trip/result')
  const canvas=page.getByTestId('serpentine-canvas-1')
  await expect(canvas).toBeVisible()
  const columns=Number(await canvas.getAttribute('data-columns'))
  expect(columns).toBeGreaterThanOrEqual(width<600?2:4)
  const boxes=await page.getByTestId('day-lane-1').getByTestId('activity-card').evaluateAll(nodes=>nodes.map(n=>{const b=n.getBoundingClientRect();return {x:b.x,y:b.y,width:b.width,right:b.right,bottom:b.bottom}}))
  expect(boxes).toHaveLength(13)
  const canvasBounds=await canvas.boundingBox()
  expect(boxes.every(b=>b.bottom<=canvasBounds.y+canvasBounds.height)).toBe(true)
  const firstArc=await canvas.getByTestId('order-arc').first().getAttribute('d')
  const [,arcX,arcY]=firstArc.split(' ')
  expect(Math.abs(boxes[0].x+boxes[0].width/2-canvasBounds.x-Number(arcX))).toBeLessThan(2)
  expect(Math.abs(boxes[0].y-canvasBounds.y-Number(arcY))).toBeLessThan(2)
  expect(boxes[1].x).toBeGreaterThan(boxes[0].x)
  expect(boxes[columns].y).toBeGreaterThan(boxes[0].y)
  expect(boxes[columns+1].x).toBeLessThan(boxes[columns].x)
  expect(boxes[columns*2+1].x).toBeGreaterThan(boxes[columns*2].x)
  expect(boxes.every(b=>b.width<=185&&b.x>=0&&b.right<=width)).toBe(true)
  const overflow=await canvas.evaluate(n=>n.scrollWidth-n.clientWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  await expect(page.getByTestId('day-lane-1').getByTestId('order-arc')).toHaveCount(12)
  await expect(page.getByTestId('itinerary-workspace')).not.toContainText('时间待定')
  await expect(page.getByTestId('itinerary-workspace')).not.toContainText('上午')
  await page.screenshot({path:testInfo.outputPath('serpentine.png'),fullPage:true})
  expect(fixture.calls().mapRenderPosts).toBe(0)
 })
}
test('serpentine: reverse-row pointer insertion follows logical order and writes once',async({page},testInfo)=>{
 await page.setViewportSize({width:1280,height:1000})
 await page.emulateMedia({reducedMotion:'reduce'})
 const fixture=await installInteractionFixture(page,{longDay:true})
 await page.goto('/trip/result')
 const canvas=page.getByTestId('serpentine-canvas-1')
 const columns=Number(await canvas.getAttribute('data-columns'))
 const source=await page.getByTestId('card-grab-1-0').boundingBox()
 const target=await page.getByTestId(`card-grab-1-${columns+1}`).boundingBox()
 await page.mouse.move(source.x+30,source.y+35);await page.mouse.down()
 await page.mouse.move(target.x+target.width-15,target.y+35,{steps:12})
 await expect(page.getByTestId('drop-preview')).toContainText('景点1')
 expect(fixture.calls().commands).toHaveLength(0)
 await page.screenshot({path:testInfo.outputPath('reverse-drag.png'),fullPage:true})
 await page.mouse.up()
 await expect.poll(()=>fixture.calls().commands.length).toBe(1)
 expect(fixture.calls().commands[0]).toMatchObject({command_type:'ACTIVITY_MOVE',target_day_index:1,target_position:columns})
 expect(fixture.calls().mapRenderPosts).toBe(0)
})

test('serpentine: complete PNG keeps reverse rows, offscreen places and honest route states',async({page},testInfo)=>{
 await page.setViewportSize({width:360,height:800})
 await page.addInitScript(()=>{
  window.__pngText=[]
  const original=CanvasRenderingContext2D.prototype.fillText
  CanvasRenderingContext2D.prototype.fillText=function(text,x,y,...args){
   window.__pngText.push({text:String(text),x,y})
   return original.call(this,text,x,y,...args)
  }
 })
 const fixture=await installInteractionFixture(page,{longDay:true,exposeWrites:true,mapSnapshot:{...connectedMapView(),status:'NEEDS_UPDATE'}})
 await page.goto('/trip/result')
 await page.getByTestId('export-itinerary-png').click()
 await expect(page.getByTestId('png-preview')).toBeVisible()
 const drawn=await page.evaluate(()=>window.__pngText)
 const places=drawn.filter(t=>/^景点\d+$/.test(t.text))
 expect(places).toHaveLength(13)
 const row2=places.filter(t=>t.y===places[6].y)
 expect(row2).toHaveLength(6)
 expect(row2[1].x).toBeLessThan(row2[0].x)
 expect(places[12].y).toBeGreaterThan(places[6].y)
 expect(drawn.some(t=>t.text==='待确认')).toBe(true)
 expect(drawn.filter(t=>t.text==='路线需要更新')).toHaveLength(12)
 expect(drawn.some(t=>/上午|时间待定|停留/.test(t.text))).toBe(false)
 const download=page.waitForEvent('download')
 await page.getByTestId('download-itinerary-png').click()
 await (await download).saveAs(testInfo.outputPath('complete-serpentine.png'))
 expect(fixture.calls().mapRenderPosts).toBe(0)
 expect(fixture.calls().directProviderRequests).toBe(0)
})

for(const width of [1440,1280,390,360]) test(`outside: paired rows route above and below at ${width}`,async({page},testInfo)=>{
 await page.setViewportSize({width,height:900})
 await page.emulateMedia({reducedMotion:'reduce'})
 await installInteractionFixture(page,{longDay:true})
 await page.goto('/trip/result')
 const lane=page.getByTestId('day-lane-1'),canvas=lane.locator('.serpentine-canvas')
 const columns=Number(await canvas.getAttribute('data-columns'))
 const cards=await lane.getByTestId('activity-card').evaluateAll(nodes=>nodes.map(n=>{const b=n.getBoundingClientRect();return{top:b.top,bottom:b.bottom}}))
 const upper=await lane.locator('.serpentine-route-label').nth(0).boundingBox()
 const lower=await lane.locator('.serpentine-route-label').nth(columns).boundingBox()
 const turn=await lane.locator('.serpentine-route-label').nth(columns-1).boundingBox()
 expect(upper.y+upper.height).toBeLessThan(cards[0].top)
 expect(lower.y).toBeGreaterThan(cards[columns].bottom)
 expect(turn.y).toBeGreaterThan(cards[columns-1].bottom)
 expect(turn.y+turn.height).toBeLessThan(cards[columns].top)
 expect(await canvas.evaluate(n=>n.scrollWidth<=n.clientWidth)).toBe(true)
 await page.screenshot({path:testInfo.outputPath('outside.png'),fullPage:true})
})

for(const width of [1440,390,360]) test(`outside: pending place uses a nearby nonmodal dropdown at ${width}`,async({page},testInfo)=>{
 await page.setViewportSize({width,height:900})
 const fixture=await installInteractionFixture(page,{pendingFirst:true})
 let searches=0
 await page.route('**/place-candidates',async route=>{
  searches++
  await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({status:'AVAILABLE',candidates:[{candidate_token:'candidate-fixture',name:'故宫博物院',category:'景点',area_or_address:'北京市东城区景山前街4号',position:{longitude:116.397,latitude:39.918,coordinate_system:'GCJ02'}}]})})
 })
 await page.goto('/trip/result')
 const card=page.getByTestId('activity-card').first()
 const trigger=card.getByRole('button').filter({hasText:'故宫博物院'})
 await trigger.click()
 const dropdown=page.getByTestId('pending-place-dropdown')
 await expect(dropdown).toBeVisible()
 await expect(page.getByRole('dialog')).toHaveCount(0)
 expect(searches).toBe(0)
 await expect(dropdown.getByRole('textbox')).toBeFocused()
 const box=await dropdown.boundingBox(),cardBox=await card.boundingBox()
 expect(Math.abs(box.y-cardBox.y-cardBox.height)).toBeLessThan(15)
 expect(box.x).toBeGreaterThanOrEqual(0)
 expect(box.x+box.width).toBeLessThanOrEqual(width)
 await dropdown.getByRole('button',{name:'搜索',exact:true}).click()
 await dropdown.getByRole('button',{name:/故宫博物院.*北京市/}).click()
 expect(fixture.calls().commands).toHaveLength(0)
 await page.screenshot({path:testInfo.outputPath('dropdown.png'),fullPage:true})
 await dropdown.getByRole('button',{name:'使用这个地点'}).click()
 await expect(dropdown).toHaveCount(0)
 expect(fixture.calls().commands).toHaveLength(1)
 expect(fixture.calls().commands[0]).toMatchObject({command_type:'PLACE_CONFIRM',candidate_token:'candidate-fixture'})
 expect(fixture.calls().mapRenderPosts).toBe(0)
 await expect(page.getByTestId('update-card-routes')).toBeEnabled()
})

test('outside: walking is the default, with real distance; stale routes only update on click',async({page})=>{
 const map=connectedMapView()
 map.days[0].routes[0].selected_mode='transit'
 const fixture=await installInteractionFixture(page,{mapSnapshot:map})
 await page.goto('/trip/result')
 await expect(page.getByTestId('transport-connector').first()).toContainText('步行 · 12 分钟 · 900 米')
 await page.getByRole('heading',{name:'故宫博物院'}).click()
 const editor=await chooseInlinePlace(page,'故宫（北京）')
 await editor.getByRole('button',{name:'使用这个地点'}).click()
 await expect(page.getByTestId('transport-connector').first()).not.toContainText('12 分钟')
 expect(fixture.calls().mapRenderPosts).toBe(0)
 await page.getByTestId('update-card-routes').click()
 await expect.poll(()=>fixture.calls().mapRenderPosts).toBe(1)
})

test('outside: generation offers readable tips with slower motion and manual paging',async({page},testInfo)=>{
 await installPausedClock(page)
 await installProcessingFixture(page)
 await page.goto('/trip/result')
 const tips=page.getByTestId('generation-reading')
 await expect(tips).toContainText('先排顺序')
 await expect(page.locator('.soft-stage-dot .animate-spin')).toHaveCSS('animation-duration','3.5s')
 await page.clock.runFor(5000)
 await expect(tips).toContainText('先排顺序')
 await tips.getByRole('button',{name:/提示 2/}).click()
 await expect(tips).toContainText('同名地点')
 await page.clock.runFor(12000)
 await expect(tips).toContainText('同名地点')
 await page.screenshot({path:testInfo.outputPath('readable-generation.png'),fullPage:true,animations:'disabled'})
 await page.emulateMedia({reducedMotion:'reduce'})
 await expect(page.locator('.soft-stage-dot .animate-spin')).toHaveCSS('animation-name','none')
})

test('outside: failed place search stays local, Escape restores focus and writes nothing',async({page})=>{
 const fixture=await installInteractionFixture(page,{pendingFirst:true})
 await page.route('**/place-candidates',route=>route.fulfill({status:503,body:'{}'}))
 await page.goto('/trip/result')
 const trigger=page.getByTestId('activity-card').first().getByRole('button').filter({hasText:'故宫博物院'})
 await trigger.click()
 const dropdown=page.getByTestId('pending-place-dropdown')
 await dropdown.getByRole('button',{name:'搜索',exact:true}).click()
 await expect(dropdown.getByRole('status')).toContainText('请重试')
 await expect(dropdown.getByRole('button',{name:'使用这个地点'})).toHaveCount(0)
 await page.keyboard.press('Escape')
 await expect(dropdown).toHaveCount(0)
 await expect(trigger).toBeFocused()
 expect(fixture.calls().commands).toHaveLength(0)
 expect(fixture.calls().mapRenderPosts).toBe(0)
})

test('outside: drag keeps its preview but no blue insertion rail',async({page})=>{
 const fixture=await installInteractionFixture(page)
 await page.goto('/trip/result')
 const dt=await page.evaluateHandle(()=>new DataTransfer())
 await page.getByTestId('drag-handle-1-0').dispatchEvent('dragstart',{dataTransfer:dt})
 await page.getByTestId('drop-slot-2-0').dispatchEvent('dragenter',{dataTransfer:dt})
 await expect(page.getByTestId('drop-preview')).toBeVisible()
 await expect(page.getByTestId('drop-slot-2-0')).toHaveCSS('background-color','rgba(0, 0, 0, 0)')
 await expect(page.getByTestId('drop-slot-2-0')).toHaveCSS('box-shadow','none')
 await page.keyboard.press('Escape')
 expect(fixture.calls().commands).toHaveLength(0)
 await dt.dispose()
})
async function chooseInlinePlace(page,name) {
 await page.route('**/place-candidates',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({status:'AVAILABLE',candidates:[{candidate_token:'fixture-place:'+encodeURIComponent(name),name,category:'景点',area_or_address:'北京市东城区'}]})}))
 const editor=page.getByTestId('pending-place-dropdown')
 await editor.getByRole('textbox').fill(name)
 await editor.getByRole('button',{name:'搜索',exact:true}).click()
 await editor.getByRole('button',{name:name+' 北京市东城区 · 景点',exact:true}).click()
 return editor
}
for(const width of [1440,1280,390,360]) test(`simple place: ready card and map edit stay in place at ${width}`,async({page},testInfo)=>{
 await page.setViewportSize({width,height:900})
 const fixture=await installInteractionFixture(page)
 await page.goto('/trip/result')
 const url=page.url()
 if(width>=1024){
  const nav=page.getByTestId('result-desktop-nav')
  expect((await nav.boundingBox()).height).toBeLessThan(160)
  await nav.hover()
  expect((await nav.boundingBox()).height).toBeLessThan(160)
 }
 const card=page.getByTestId('activity-card').first()
 await card.getByRole('heading').click()
 await expect(page.getByTestId('pending-place-dropdown')).toBeVisible()
 await expect(page.getByRole('dialog')).toHaveCount(0)
 await expect(page.getByRole('button',{name:/编辑文字|替换地点|移动位置|移到后一天/})).toHaveCount(0)
 await expect(page.getByTestId('context-workspace')).toHaveCount(0)
 await chooseInlinePlace(page,'北海公园')
 await expect(page.getByTestId('pending-place-dropdown').getByRole('button',{name:'使用这个地点'})).toBeVisible()
 expect(fixture.calls().commands).toHaveLength(0)
 await page.screenshot({path:testInfo.outputPath('ready-place.png'),fullPage:true})
 await page.keyboard.press('Escape')
 await openResultView(page,'map_stay')
 await page.getByTestId('map-directory-toggle').click()
 if(!(await page.getByTestId('map-place-directory').isVisible()))await page.getByTestId('map-directory-toggle').click()
 await page.getByTestId('map-place-directory').getByRole('button').filter({hasText:'故宫博物院'}).click()
 await page.locator('.fluid-map-place-edit > button').click()
 const dropdown=page.getByTestId('pending-place-dropdown')
 await expect(dropdown).toBeVisible()
 await expect(page.getByRole('dialog')).toHaveCount(0)
 await expect(page.getByTestId('context-workspace')).toHaveCount(0)
 await chooseInlinePlace(page,'北海公园')
 await page.screenshot({path:testInfo.outputPath('map-place.png'),fullPage:true})
 await dropdown.getByRole('button',{name:'使用这个地点'}).click()
 await expect(dropdown).toHaveCount(0)
 expect(page.url()).toBe(url)
 expect(fixture.calls().commands).toHaveLength(1)
 expect(fixture.calls().mapRenderPosts).toBe(0)
 await expect(page.getByTestId('result-view-map-stay')).toBeVisible()
})
test('simple place: home explains the text action only inside the input',async({page})=>{
 await page.goto('/')
 await expect(page.getByPlaceholder('粘贴行程，帮你整理地点、核对路线，生成清晰的行程卡片。')).toBeVisible()
})
for(const width of [1440,1280,390,360]) test(`compact map: successful edit leaves one toolbar and map near heading at ${width}`,async({page},testInfo)=>{
 await page.setViewportSize({width,height:900})
 const fixture=await installInteractionFixture(page,{mode:'CLAIMED',withUser:true})
 await page.goto('/trip/result')
 await page.getByTestId('activity-card').first().getByRole('heading').click()
 const editor=await chooseInlinePlace(page,'北海公园')
 const row=editor.locator('.pending-place-row[data-selected="true"]')
 const option=await row.locator('.pending-place-option').boundingBox()
 const confirm=await row.getByRole('button',{name:'使用这个地点'}).boundingBox()
 expect(confirm.x).toBeGreaterThan(option.x+option.width-1)
 expect(Math.abs((confirm.y+confirm.height/2)-(option.y+option.height/2))).toBeLessThan(3)
 await row.getByRole('button',{name:'使用这个地点'}).click()
 await expect(editor).toHaveCount(0)
 await expect(page.getByText('修改已保留，路线需要更新时请主动更新。')).toHaveCount(0)
 await openResultView(page,'map_stay')
 await expect(page.getByTestId('result-action-bar').getByTestId('render-map')).toBeVisible()
 await expect(page.getByTestId('result-action-bar')).toContainText('路线需要更新')
 await page.getByLabel('更多行程操作').click()
 await expect(page.getByTestId('delete-entire-trip')).toBeVisible()
 await page.getByLabel('更多行程操作').click()
 await expect(page.locator('.fluid-map-stage').getByTestId('render-map')).toHaveCount(0)
 const head=await page.locator('.e-trip-head').boundingBox()
 const stage=await page.locator('.fluid-map-stage').boundingBox()
 expect(stage.y-head.y-head.height).toBeLessThanOrEqual(10)
 expect(stage.y+stage.height).toBeLessThanOrEqual(900)
 expect(fixture.calls().commands).toHaveLength(1)
 expect(fixture.calls().mapRenderPosts).toBe(0)
 await page.screenshot({path:testInfo.outputPath('compact-map.png'),fullPage:true})
})
test('inline confirmation: select another candidate is read-only, repeat selection commits once',async({page})=>{
 const fixture=await installInteractionFixture(page,{holdCommand:true})
 await page.route('**/place-candidates',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({status:'AVAILABLE',candidates:Array.from({length:6},(_,i)=>({candidate_token:'fixture-place:'+encodeURIComponent('地点'+i),name:'地点'+i,category:'景点',area_or_address:'北京市东城区'}))})}))
 try {
 await page.goto('/trip/result')
 await page.getByTestId('activity-card').first().getByRole('heading').click()
 const editor=page.getByTestId('pending-place-dropdown')
 await editor.getByRole('button',{name:'搜索',exact:true}).click()
 const options=editor.locator('.pending-place-option')
 await options.nth(0).click()
 await options.nth(1).click()
 expect(fixture.calls().commands).toHaveLength(0)
 const selected=editor.locator('.pending-place-row[data-selected="true"]')
 await expect(selected).toContainText('地点1')
 await options.nth(1).dblclick()
 await expect.poll(()=>fixture.calls().commands.length).toBe(1)
 await expect(options.nth(1)).toBeDisabled()
 await expect(selected.getByRole('button',{name:'使用这个地点'})).toBeDisabled()
 expect(fixture.calls().commands[0].candidate_token).toBe('fixture-place:'+encodeURIComponent('地点1'))
 fixture.releaseCommand()
 await fixture.waitForCommandCompletion()
 await expect(editor).toHaveCount(0)
 expect(fixture.calls().commandApplications).toBe(1)
 expect(fixture.calls().mapRenderPosts).toBe(0)
 } finally {fixture.releaseCommand()}
})
test('real SDK lifecycle: unmount never removes overlays from an already destroyed map',async({page})=>{
 const failures=[]
 page.on('pageerror',e=>failures.push(e.message))
 await page.addInitScript(()=>{
  window.__destroyedMaps=0
  window.AMap={
   Map:class{
    destroyed=false
    on(name,cb){if(name==='complete')queueMicrotask(cb)}
    add(){} remove(){if(this.destroyed)throw new Error('overlay removal after map destruction')}
    destroy(){this.destroyed=true;window.__destroyedMaps++}
    setCenter(){} setFitView(){} resize(){}
   },
   Marker:class{},Polyline:class{},
  }
 })
 await installInteractionFixture(page,{mapSnapshot:connectedMapView()})
 await page.goto('/trip/result')
 await openResultView(page,'map_stay')
 await expect(page.getByTestId('route-map')).toBeVisible()
 await page.getByLabel('更多行程操作').click()
 await page.getByTestId('delete-trip-source').click()
 await expect.poll(()=>page.evaluate(()=>window.__destroyedMaps)).toBeGreaterThan(0)
 await expect(page.getByRole('button',{name:'确认永久删除',exact:true})).toBeVisible()
 await page.keyboard.press('Escape')
 await expect(page.getByTestId('route-map')).toBeVisible()
 expect(failures).toEqual([])
})
