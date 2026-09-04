const { expect, test } = require('@playwright/test')


const RESOURCE_REF = 'g03r-race-safe-result'
const ETAG_A = 'tu3_race_generation_a'
const ETAG_B = 'tu3_race_generation_b'
const MAP_THEATER_MESSAGES = {
  PREPARING: '路线正在后台准备，可以先查看行程卡片。',
  AVAILABLE: '路线已准备，可以切换步行或公交查看。',
  UNAVAILABLE: '路线暂时无法显示，行程卡片不受影响。',
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
      message: mapStatus === 'PREPARING' ? '正在准备路线' : '路线已准备',
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
  message: '步行和公交路线已准备，出发前请再核对实时情况',
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
    message: status === 'NEEDS_UPDATE' ? '行程已调整，需要手动更新路线。' : '步行和公交路线已准备，出发前请再核对实时情况',
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
              message: preparing || mode === 'budget-preparing' ? `路线仍在准备 ${call}` : '新路线状态已读取',
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
  test(`a hanging ${hangingKind} read aborts at 3000ms without losing the other terminal enhancement`, async ({ page }) => {
    const fixture = await openEnhancementFixtureWithPausedClock(page, {
      mapMode: hangingKind === 'map' ? 'hang' : 'available',
      stayMode: hangingKind === 'stay' ? 'hang' : 'available',
    })

    try {
      await fixture.waitForReads(1, 1)
      await page.clock.runFor(3_001)
      await fixture.waitForAborts(hangingKind === 'map' ? 1 : 0, hangingKind === 'stay' ? 1 : 0)

      await expect(page.getByTestId('trip-check-item')).toHaveCount(3)
      await openResultView(page, 'map_stay')
      await expect(page.getByTestId('enhancement-read-recovery')).toBeVisible()
      if (hangingKind === 'map') {
        await expect(page.getByTestId('map-theater')).toContainText(MAP_THEATER_MESSAGES.UNAVAILABLE)
        await expect(page.getByTestId('stay-panel')).toContainText('新住宿状态已读取')
      } else {
        await expect(page.getByTestId('map-theater')).toContainText(MAP_THEATER_MESSAGES.AVAILABLE)
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
    await expect(page.getByTestId('map-theater')).toContainText(MAP_THEATER_MESSAGES.PREPARING)
    await expect(page.getByTestId('stay-panel')).toContainText('住宿仍在准备 1')
    await page.clock.runFor(799)
    expect(fixture.calls().reads).toEqual({ map: 1, stay: 1 })

    await page.clock.runFor(1)
    await fixture.waitForReads(2, 2)
    await expect(page.getByTestId('map-theater')).toContainText(MAP_THEATER_MESSAGES.AVAILABLE)
    await expect(page.getByTestId('stay-panel')).toContainText('新住宿状态已读取')
    await expect(page.getByTestId('trip-check-item')).toHaveCount(3)
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
    await expect(page.getByTestId('map-theater')).toContainText(MAP_THEATER_MESSAGES.PREPARING)
    for (let call = 2; call <= 7; call += 1) {
      await page.clock.runFor(800)
      await fixture.waitForReads(call, call)
      await expect(page.getByTestId('map-theater')).toContainText(MAP_THEATER_MESSAGES.PREPARING)
    }
    await page.clock.runFor(800)
    await fixture.waitForReads(8, 8)

    await expect(page.getByTestId('map-theater')).toContainText(MAP_THEATER_MESSAGES.UNAVAILABLE)
    await expect(page.getByTestId('stay-panel')).toContainText('住宿建议暂时不可用')
    await expect(page.getByTestId('trip-check-item')).toHaveCount(3)
    await openResultView(page, 'map_stay')
    await expect(page.getByTestId('enhancement-read-recovery')).toBeVisible()
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
        await expect(page.getByTestId(pendingPanel)).toContainText(
          pendingKind === 'map' ? preparingMessage : `${preparingMessage} ${call}`,
        )
        await expect(page.getByTestId(terminalPanel)).toContainText(terminalMessage)
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

      await expect(page.getByTestId(terminalPanel)).toContainText(terminalMessage)
      await expect(page.getByTestId(pendingPanel)).toContainText(fallbackMessage)
      await expect(page.getByTestId('trip-check-item')).toHaveCount(3)
      await openResultView(page, 'map_stay')
      await expect(page.getByTestId('retry-enhancements')).toBeVisible()

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
    await expect(page.getByTestId('trip-check-item')).toHaveCount(3)
    await openResultView(page, 'map_stay')
    await expect(page.getByTestId('retry-enhancements')).toBeVisible()
    const beforeRecovery = fixture.calls()
    fixture.setModes('available', 'available')

    await page.getByTestId('retry-enhancements').evaluate((button) => {
      button.click()
      button.click()
    })
    await fixture.waitForReads(2, 2)
    await expect(page.getByTestId('map-theater')).toContainText(MAP_THEATER_MESSAGES.AVAILABLE)
    await expect(page.getByTestId('stay-panel')).toContainText('新住宿状态已读取')
    await expect(page.getByTestId('enhancement-read-recovery')).toHaveCount(0)
    await expect(page.getByTestId('trip-check-item')).toHaveCount(3)

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
    await expect(page.getByTestId('map-theater')).toContainText(MAP_THEATER_MESSAGES.AVAILABLE)
    await expect(page.getByTestId('stay-panel')).toContainText('新住宿状态已读取')
    await expect(page.getByTestId('trip-check-item')).toHaveCount(3)

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


test('current checks generation survives result cleanup without duplicate materialize', async ({ page }) => {
  const fixture = await installRaceFixture(page)

  await page.goto('/trip/result')
  await expect(page.getByTestId('trip-days')).toBeVisible()
  await fixture.waitForMaterializeStart()
  await fixture.waitForSecondResultRead()
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('bt_active_trip_etag'))).toBe(ETAG_B)
  expect(fixture.calls().materializeCalls).toBe(1)
  fixture.releaseCompatibleMaterialize()
  await expect(page.getByTestId('trip-check-item')).toHaveCount(3, { timeout: 5_000 })
  await openResultView(page, 'checks')
  await expect(page.getByText('优先处理这三项，行程会更顺畅')).toBeVisible()
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
  await fixture.waitForSecondResultRead()
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('bt_active_trip_etag'))).toBe(ETAG_B)
  expect(fixture.calls()).toEqual({
    resultReads: 2,
    materializeCalls: 1,
    maxMaterializeInFlight: 1,
    checksCalls: 0,
    abortedMaterializeCalls: 0,
  })

  await page.clock.runFor(10_001)
  await expect(page.getByTestId('trip-check-item')).toHaveCount(3, { timeout: 5_000 })

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
  await expect(page.getByTestId('trip-check-item')).toHaveCount(3, { timeout: 5_000 })

  expect(fixture.calls()).toEqual({
    resultReads: 2,
    materializeCalls: 2,
    maxMaterializeInFlight: 1,
    checksCalls: 1,
    abortedMaterializeCalls: 0,
  })
  await page.close()
})


test('ordinary preparation failure is recoverable only after explicit retry', async ({ page }) => {
  const fixture = await installRaceFixture(page, 'failure')

  await page.goto('/trip/result')
  await openResultView(page, 'checks')
  const retry = page.getByRole('button', { name: '重新准备检查' })
  await expect(retry).toBeVisible()
  expect(fixture.calls().materializeCalls).toBe(1)
  expect(fixture.calls().checksCalls).toBe(0)

  await retry.click()
  await expect(page.getByTestId('trip-check-item')).toHaveCount(3, { timeout: 5_000 })
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
} = {}) {
  let revision = 0
  let etag = 'tu3_interaction_0'
  const view = interactionResult()
  if (exposeWrites) {
    view.map = {
      status: 'NEEDS_UPDATE',
      message: '卡片有调整，需要手动更新路线',
      available_actions: ['RENDER_MAP'],
    }
    view.stay = interactionStayView(true)
  }
  if (initialMapView) view.map = clone(initialMapView)
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
  const writes = { map: 0, stay: 0, adopt: 0, claim: 0, source: 0, trip: 0 }
  let mapRenderPosts = 0
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
  const sourceIdempotencyKeys = []
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
      if (holdReadbackAfterCommand && commands.length > 0 && !readbackHeld) {
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
      commands.push(command)
      if (commandGate) await commandGate
      if (delayMs) await new Promise((resolve) => setTimeout(resolve, delayMs))

      if (scenario === 'failure' && commands.length === 1) {
        await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
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
        rotateActivityTokens(view, revision)
        etag = `tu3_interaction_${revision}`
        await route.fulfill({ status: 409, contentType: 'application/json', body: '{}' })
        return
      }

      applyCommandToResult(view, command)
      revision += 1
      rotateActivityTokens(view, revision)
      etag = `tu3_interaction_${revision}`
      if (scenario === 'readback-failure' && commands.length === 1) readbackBlocked = true
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: etag },
        body: JSON.stringify({ status: 'APPLIED', changed_days: view.days.map((day) => day.label), map_readiness: 'NEEDS_UPDATE' }),
      })
      commandCompleted.resolve()
      return
    }

    if (pathname.endsWith('/map-renders/latest')) {
      mapReads += 1
      if (failInitialEnhancements && mapReads === 1) {
        await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
        return
      }
      const status = view.map.status
      const currentMap = status === 'AVAILABLE'
        ? { ...clone(mapSnapshot), status, available_actions: view.map.available_actions }
        : { status, message: view.map.message, days: [], available_actions: view.map.available_actions }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(currentMap),
      })
      return
    }

    if (pathname.endsWith('/map-renders') && request.method() === 'POST') {
      mapRenderPosts += 1
      if (holdMapWrite) await releaseMapWrite.promise
      await route.fulfill({ status: 202, contentType: 'application/json', body: '{}' })
      return
    }

    if (pathname.endsWith('/stay-suggestions')) {
      stayReads += 1
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
      mapRenderPosts,
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
  test(`closing a pending preview aborts its late ${latePreviewOutcome} without reopening or polluting state`, async ({ page }) => {
    await installPausedClock(page)
    const fixture = await installInteractionFixture(page, { latePreviewOutcome })

    try {
      await page.goto('/trip/result')
      await expect(page.getByTestId('trip-check-item')).toHaveCount(3)
      await openResultView(page, 'checks')
      await page.getByTestId('preview-change').first().click()
      await expect(page.getByTestId('change-preview')).toContainText('补充午餐时间')

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
  await expect.poll(() => fixture.calls().commands.length).toBe(1)
  expect(fixture.calls().commands[0]).toMatchObject({
    command_type: 'ACTIVITY_MOVE',
    target_day_index: 1,
    target_position: 1,
  })
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['景山公园', '故宫博物院'])
  await expect(page.locator('[data-day-heading="1"]')).toBeFocused()
  await expect(page.getByText('需要手动更新', { exact: true }).first()).toBeVisible()
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


test('desktop keyboard activation of a drag handle opens the equivalent move panel', async ({ page }) => {
  await page.setViewportSize({ width: 1680, height: 938 })
  const fixture = await installInteractionFixture(page)
  await page.goto('/trip/result')
  const handle = page.getByTestId('drag-handle-1-0')
  await handle.focus()
  await page.keyboard.press('Enter')

  await expect(page.getByRole('dialog', { name: /把“故宫博物院”移到哪里/ })).toBeVisible()
  expect(fixture.calls().commands).toHaveLength(0)
})


test('mobile and keyboard controls move within and across days with accessible targets', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.setViewportSize({ width: 390, height: 844 })
  const fixture = await installInteractionFixture(page, { exposeWrites: true, racePreview: true })
  await page.goto('/trip/result')
  await expect(page.getByTestId('itinerary-workspace')).toHaveAttribute('data-reduced-motion', 'true')
  await expect(page.getByTestId('drag-handle-1-0')).toBeHidden()

  const down = page.getByRole('button', { name: '下移 故宫博物院' })
  await expectMinimumTarget(down)
  await expectMinimumTarget(page.getByRole('button', { name: '移动 故宫博物院 到其他天或位置' }))
  await expectMinimumTarget(page.getByRole('button', { name: '删除 故宫博物院' }))
  await expectMinimumTarget(page.getByTestId('day-1-add'))
  await openResultView(page, 'map_stay')
  await expectMinimumTarget(page.getByTestId('render-map'))
  await expectMinimumTarget(page.getByTestId('choose-stay'))
  await openResultView(page, 'itinerary')
  await down.focus()
  await page.keyboard.press('Enter')
  await expect.poll(() => fixture.calls().commands.length).toBe(1)
  expect(fixture.calls().commands[0]).toMatchObject({ target_day_index: 1, target_position: 1 })

  const move = page.getByRole('button', { name: '移动 故宫博物院 到其他天或位置' })
  await expect(move).toBeEnabled()
  await move.focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: /把“故宫博物院”移到哪里/ })
  await expect(dialog).toBeVisible()
  await expectMinimumTarget(dialog.getByRole('button', { name: '关闭移动面板' }))
  await expectMinimumTarget(page.getByTestId('confirm-move'))
  await page.getByTestId('move-target-day').selectOption('3')
  await page.getByTestId('move-target-position').selectOption('0')
  await page.getByTestId('confirm-move').click()

  await expect.poll(() => fixture.calls().commands.length).toBe(2)
  expect(fixture.calls().commands[1]).toMatchObject({ target_day_index: 3, target_position: 0 })
  await expect(dialog).toBeHidden()
  await expect.poll(() => dayCardNames(page, 3)).toEqual(['故宫博物院'])
  await expect(page.locator('[data-day-heading="3"]')).toBeFocused()
  await expect(page.getByTestId('day-lane-4')).toHaveCount(0)
  expect(fixture.calls().mapRenderPosts).toBe(0)

  await expect(page.getByTestId('trip-check-item')).toHaveCount(3)
  await openResultView(page, 'checks')
  await page.getByTestId('preview-change').evaluateAll((buttons) => {
    buttons[0].click()
    buttons[1].click()
  })
  await fixture.waitForTwoPreviews()
  expect(fixture.calls().previewPosts).toBe(2)
  fixture.releaseFirstPreview()
  await expect(page.getByTestId('preview-change').first()).toHaveText('正在准备预览…')
  await expect(page.getByText('这项建议已经变化，请刷新后再试。')).toHaveCount(0)
  fixture.releaseSecondPreview()
  await expect(page.getByTestId('change-preview')).toContainText('最新步行衔接')
  await expect(page.getByTestId('change-preview')).not.toContainText('补充午餐时间')
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
  const saveEditor = addEditor.getByTestId('save-card-editor')
  await expect(addEditor.getByTestId('card-editor-name')).toBeFocused()
  await closeEditor.focus()
  await page.keyboard.press('Shift+Tab')
  await expect(saveEditor).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(closeEditor).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(addEditor).toBeHidden()
  await expect(addButton).toBeFocused()

  const palaceCard = page.getByTestId('activity-card').filter({ hasText: '故宫博物院' })
  const palaceDetails = palaceCard.locator('button').filter({ hasText: '故宫博物院' })
  await palaceDetails.click()
  await expect(page.getByRole('button', { name: '删除这张卡片' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '删除 故宫博物院' })).toHaveCount(1)
  await page.getByRole('button', { name: '编辑文字' }).click()
  const editEditor = page.getByRole('dialog', { name: '编辑卡片文字' })
  await expect(editEditor).toBeVisible()
  await expect(editEditor.getByTestId('card-editor-name')).toBeFocused()
  await editEditor.getByRole('button', { name: '关闭编辑' }).click()
  await expect(editEditor).toBeHidden()
  await expect(page.locator('[data-day-heading="1"]')).toBeFocused()

  const refreshedPalaceCard = page.getByTestId('activity-card').filter({ hasText: '故宫博物院' })
  await refreshedPalaceCard.locator('button').filter({ hasText: '故宫博物院' }).click()
  await page.getByRole('button', { name: '替换地点' }).click()
  const replaceEditor = page.getByRole('dialog', { name: '替换地点' })
  await replaceEditor.getByTestId('card-editor-name').fill('北海公园')
  await replaceEditor.getByTestId('save-card-editor').click()
  await expect(replaceEditor).toBeHidden()
  await expect(page.locator('[data-day-heading="1"]')).toBeFocused()
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['北海公园', '景山公园'])

  const deleteButton = page.getByRole('button', { name: '删除 天坛公园' })

  await deleteButton.click()
  const dialog = page.getByRole('dialog', { name: '删除“天坛公园”？' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('button', { name: '取消' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(deleteButton).toBeFocused()
  expect(fixture.calls().commands).toHaveLength(1)

  await deleteButton.click()
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
  await page.getByTestId('choose-stay').click()
  await expect.poll(() => fixture.calls().writes.stay).toBe(1)
  expect(await fixture.browserWriteRace()).toEqual({
    materializeInFlight: 0,
    writesBeforeMaterializeSettled: 0,
  })
  await expect(page.getByTestId('trip-check-item')).toHaveCount(3)
  await openResultView(page, 'checks')
  await page.getByTestId('preview-change').first().click()
  await expect(page.getByTestId('change-preview')).toBeVisible()

  await openResultView(page, 'itinerary')
  const down = page.getByRole('button', { name: '下移 故宫博物院' })
  const conflictingWrites = [
    page.getByRole('button', { name: '移动 故宫博物院 到其他天或位置' }),
    page.getByRole('button', { name: '删除 故宫博物院' }),
    page.getByTestId('day-1-add'),
    page.getByRole('button', { name: /目的地.*北京/ }),
    page.getByTestId('render-map'),
    page.getByTestId('choose-stay'),
    page.getByTestId('adopt-change'),
    page.getByTestId('claim-demo-trip'),
    page.getByTestId('delete-entire-trip'),
  ]
  for (const control of conflictingWrites) await expect(control).toBeEnabled()
  await down.click()

  await expect.poll(() => fixture.calls().commands.length).toBe(1)
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['景山公园', '故宫博物院'])
  await expect(page.getByRole('button', { name: '上移 故宫博物院' })).toBeDisabled()
  for (const control of conflictingWrites) {
    await expect(control).toBeDisabled()
    await control.evaluate((element) => element.click())
  }
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
  })

  try {
    await page.goto('/trip/result')
    await page.getByRole('button', { name: '下移 故宫博物院' }).click()
    await expect.poll(() => fixture.calls().commands.length).toBe(1)

    await page.clock.runFor(6_000)
    fixture.releaseCommand()
    await fixture.waitForReadbackStart()
    await page.clock.runFor(3_999)
    await expect(page.getByTestId('result-operation-status')).toHaveCount(0)

    await page.clock.runFor(2)
    await expect(page.getByTestId('result-operation-status')).toContainText('保存结果暂时无法确认')
    const retry = page.getByTestId('retry-result-readback')
    await expect(retry).toBeVisible()
    expect(fixture.calls().commands).toHaveLength(1)
    expect(fixture.calls().mapRenderPosts).toBe(0)
    expect(fixture.calls().directProviderRequests).toBe(0)

    fixture.releaseReadback()
    await retry.click()
    await expect(retry).toBeHidden()
    await expect(page.getByRole('button', { name: '上移 故宫博物院' })).toBeEnabled()
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

    await page.clock.runFor(10_001)

    await expect(page.getByTestId('result-view-map-stay')).toBeVisible()
    await expect(page.getByTestId('result-operation-status')).toContainText('路线更新等待时间较长')
    const retry = page.getByTestId('retry-result-readback')
    await expect(retry).toBeVisible()
    await expect(renderMap).toBeDisabled()
    const readsBeforeRetry = fixture.calls().mapReads

    await retry.click()
    await expect.poll(() => fixture.calls().mapReads).toBe(readsBeforeRetry + 1)
    await expect(page.getByTestId('result-operation-status')).toContainText('不会重复提交路线请求')
    await expect(retry).toBeEnabled()
    expect(fixture.calls().writes.map).toBe(1)

    fixture.confirmMapJob()
    await retry.click()
    await expect(retry).toBeHidden()
    await expect(page.getByTestId('map-theater')).toContainText(MAP_THEATER_MESSAGES.PREPARING)
    expect(fixture.calls().writes.map).toBe(1)
    expect(fixture.calls().directProviderRequests).toBe(0)
  } finally {
    fixture.releaseMapWrite()
  }
})


for (const initialMapStatus of ['LIMITED', 'UNAVAILABLE']) {
  test(`an unacknowledged map write keeps the page locked when ${initialMapStatus} reads back unchanged`, async ({ page }) => {
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

      await page.clock.runFor(10_001)
      const retry = page.getByTestId('retry-result-readback')
      await expect(retry).toBeVisible()
      await expect(renderMap).toBeDisabled()

      for (let attempt = 0; attempt < 2; attempt += 1) {
        const readsBeforeRetry = fixture.calls().mapReads
        await retry.click()
        await expect.poll(() => fixture.calls().mapReads).toBe(readsBeforeRetry + 1)
        await expect(page.getByTestId('result-operation-status')).toContainText('路线任务仍未确认')
        await expect(page.getByTestId('result-operation-status')).toContainText('不会重复提交路线请求')
        await expect(retry).toBeVisible()
        await expect(retry).toBeEnabled()
        await expect(renderMap).toBeDisabled()
        await renderMap.evaluate((button) => button.click())
        expect(fixture.calls().writes.map).toBe(1)
        expect(fixture.calls().mapRenderPosts).toBe(1)
        expect(fixture.calls().directProviderRequests).toBe(0)
      }
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


test('a hanging editor write closes its modal and exposes the bounded recovery action', async ({ page }) => {
  await installPausedClock(page)
  const fixture = await installInteractionFixture(page, { holdCommand: true })

  try {
    await page.goto('/trip/result')
    await page.getByRole('heading', { name: '故宫博物院' }).click()
    await page.getByRole('button', { name: '编辑文字' }).click()
    const editor = page.getByRole('dialog', { name: '编辑卡片文字' })
    await editor.getByTestId('card-editor-name').fill('故宫博物院午门')
    await editor.getByTestId('save-card-editor').click()
    await expect.poll(() => fixture.calls().commands.length).toBe(1)

    await page.clock.runFor(10_001)

    await expect(editor).toBeHidden()
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
    await expect(page.getByRole('button', { name: '下移 故宫博物院午门' })).toBeEnabled()
    expect(fixture.calls().commands).toHaveLength(1)
  } finally {
    fixture.releaseCommand()
  }
})


test('a hanging suggestion preview stops at its deadline and leaves a retryable check', async ({ page }) => {
  await installPausedClock(page)
  const fixture = await installInteractionFixture(page, { racePreview: true })

  try {
    await page.goto('/trip/result')
    await expect(page.getByTestId('trip-check-item')).toHaveCount(3)
    await openResultView(page, 'checks')
    await page.getByTestId('preview-change').first().click()
    await expect.poll(() => fixture.calls().previewPosts).toBe(1)

    await page.clock.runFor(10_001)

    await expect(page.getByTestId('change-preview')).toHaveCount(0)
    await expect(page.getByTestId('trip-checks')).toContainText('建议预览等待时间较长，已安全停止；你可以重新预览。')
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
    await page.getByTestId('delete-trip-source').click()
    await page.getByTestId('confirm-delete-source').click()
    await expect.poll(() => fixture.calls().writes.source).toBe(1)

    await page.clock.runFor(10_001)
    const retry = page.getByTestId('retry-result-readback')
    await expect(retry).toBeVisible()
    await expect(page.getByTestId('result-operation-status')).toContainText('原文删除等待时间较长')
    expect(fixture.sourceIdempotencyKeys()).toHaveLength(1)

    await retry.click()
    await expect.poll(() => fixture.calls().writes.source).toBe(2)
    await expect(page.getByTestId('delete-trip-source')).toHaveText('原文已删除')
    await expect(retry).toBeHidden()
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

  await page.getByRole('button', { name: '下移 故宫博物院' }).click()
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
  const fixture = await installInteractionFixture(page, { scenario: 'failure' })
  await page.goto('/trip/result')
  await page.getByRole('button', { name: '下移 故宫博物院' }).click()

  await expect(page.getByText('调整请求未能确认，已按服务端最新行程恢复。')).toBeVisible()
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['故宫博物院', '景山公园'])
  await expect(page.locator('[data-day-heading="1"]')).toBeFocused()
  await expect(page.locator('body')).not.toContainText('没有保存')
  expect(fixture.calls().commands).toHaveLength(1)
  expect(fixture.calls().mapRenderPosts).toBe(0)
})


test('delete failure restores its card and returns focus to the original delete control', async ({ page }) => {
  const fixture = await installInteractionFixture(page, { scenario: 'failure' })
  await page.goto('/trip/result')
  const deleteButton = page.getByRole('button', { name: '删除 天坛公园' })
  await deleteButton.click()
  await page.getByTestId('confirm-delete').click()

  await expect.poll(() => dayCardNames(page, 2)).toEqual(['天坛公园'])
  await expect(deleteButton).toBeFocused()
  await expect(page.getByRole('dialog', { name: '删除“天坛公园”？' })).toBeHidden()
  expect(fixture.calls().commands).toHaveLength(1)
})


test('accepted command with failed readback stays locked until explicit recovery', async ({ page }) => {
  const fixture = await installInteractionFixture(page, { scenario: 'readback-failure' })
  await page.goto('/trip/result')
  await expect(page.getByTestId('trip-check-item')).toHaveCount(3)
  await openResultView(page, 'checks')
  await page.getByTestId('preview-change').first().click()
  await expect(page.getByTestId('change-preview')).toBeVisible()
  await openResultView(page, 'map_stay')
  await expect(page.getByTestId('map-theater')).toContainText('已准备')
  await expect(page.getByTestId('stay-panel')).toContainText('已准备')
  await openResultView(page, 'itinerary')
  await page.getByRole('button', { name: '下移 故宫博物院' }).click()

  const retryReadback = page.getByTestId('retry-result-readback')
  await expect(retryReadback).toBeVisible()
  await expect(page.getByText(/调整已提交，但保存结果暂时无法确认/)).toBeVisible()
  await expect(page.locator('body')).not.toContainText(/没有保存|未保存/)
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['景山公园', '故宫博物院'])
  await expect(page.getByTestId('change-preview')).toHaveCount(0)
  await expect(page.getByTestId('trip-check-item')).toHaveCount(0)
  await expect(page.getByTestId('map-theater')).not.toContainText('已准备')
  await expect(page.getByTestId('stay-panel')).not.toContainText('已准备')
  await expect(page.getByTestId('itinerary-workspace').getByText('行程已调整，需要手动更新路线。', { exact: true })).toBeVisible()
  await openResultView(page, 'map_stay')
  await expect(page.getByText('行程已调整，住宿建议需要重新确认。', { exact: true })).toBeVisible()
  await expect(page.getByTestId('render-map')).toBeDisabled()

  await openResultView(page, 'itinerary')
  await expect(page.getByRole('button', { name: '上移 故宫博物院' })).toBeDisabled()
  fixture.recoverReadback()
  await retryReadback.click()
  await expect(page.getByText('已读取服务端最新行程，可以继续调整。')).toBeVisible()
  await expect(retryReadback).toBeHidden()
  await expect(page.getByRole('button', { name: '上移 故宫博物院' })).toBeEnabled()
  await expect.poll(() => fixture.calls().resultReads).toBeGreaterThanOrEqual(3)
  expect(fixture.calls().commands).toHaveLength(1)
})


test('409 reads latest cards and invalidates an old available map without rendering', async ({ page }) => {
  const fixture = await installInteractionFixture(page, { scenario: 'conflict' })
  await page.goto('/trip/result')
  await expect(page.getByText('路线已准备', { exact: true }).first()).toBeVisible()
  await page.getByRole('button', { name: '下移 故宫博物院' }).click()

  await expect(page.getByText('卡片刚刚有更新，已为你读取最新版本，请再试一次。')).toBeVisible()
  await expect.poll(() => dayCardNames(page, 1)).toEqual(['最新同步地点', '故宫博物院', '景山公园'])
  await expect(page.getByText('需要手动更新', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('路线已准备', { exact: true })).toHaveCount(0)
  expect(fixture.calls().commands).toHaveLength(1)
  expect(fixture.calls().mapRenderPosts).toBe(0)
})


test('public result DOM contains no provider URL or internal implementation vocabulary', async ({ page }) => {
  const fixture = await installInteractionFixture(page, { failInitialEnhancements: true })
  await page.goto('/trip/result')
  await expect(page.getByTestId('trip-days')).toBeVisible()
  await expect(page.getByTestId('trip-check-item')).toHaveCount(3)
  await expect(page.getByTestId('map-theater')).toContainText(MAP_THEATER_MESSAGES.UNAVAILABLE)
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
  await expect(page.getByText('可以更好', { exact: true }).first()).toHaveClass(/bg-amber-50/)
  await expect(page.getByText('需要确认', { exact: true }).first()).toHaveClass(/bg-blue-50/)
})


test('three-view shell defaults to itinerary and preserves loaded map and checks state', async ({ page }) => {
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
  await page.getByTestId('map-mode-transit').click()
  await expect(page.getByTestId('map-mode-transit')).toHaveAttribute('aria-pressed', 'true')

  await openResultView(page, 'checks')
  await expect(page.getByTestId('trip-check-item')).toHaveCount(3)
  const labels = await page.getByTestId('trip-check-item').evaluateAll((items) => (
    items.map((item) => item.querySelector('span')?.textContent || '')
  ))
  expect(page.getByTestId('trip-check-item')).toHaveCount(3)
  expect(labels.every((label) => ['必须调整', '可以更好', '需要确认'].includes(label))).toBe(true)

  await openResultView(page, 'map_stay')
  await expect(page.getByTestId('map-mode-transit')).toHaveAttribute('aria-pressed', 'true')
  await openResultView(page, 'itinerary')
  await expect(page.getByRole('heading', { name: '故宫博物院' })).toBeVisible()
})


test('strict adjacent connector drops old minutes immediately after a semantic edit', async ({ page }) => {
  const fixture = await installInteractionFixture(page, { mapSnapshot: connectedMapView() })
  await page.goto('/trip/result')

  const connector = page.getByTestId('transport-connector').first()
  await expect(connector).toContainText('步行 · 12 分钟')
  await expect(connector).toHaveAttribute('data-connector-status', 'AVAILABLE')

  const palaceCard = page.getByTestId('activity-card').filter({ hasText: '故宫博物院' })
  await palaceCard.locator('button').filter({ hasText: '故宫博物院' }).click()
  await page.getByRole('button', { name: '编辑文字' }).click()
  const editor = page.getByRole('dialog', { name: '编辑卡片文字' })
  await editor.getByLabel('时间提示（可选）').fill('下午')
  await editor.getByTestId('save-card-editor').click()

  await expect.poll(() => fixture.calls().commands.length).toBe(1)
  await expect(connector).toContainText('路线需要更新')
  await expect(connector).not.toContainText('12 分钟')
  expect(fixture.calls().mapRenderPosts).toBe(0)
  expect(fixture.calls().directProviderRequests).toBe(0)
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
  await expect(page.getByTestId('map-theater')).toContainText('建议从故宫博物院步行前往景山公园')
  await expect(page.getByTestId('map-theater')).toContainText('步行 12 分钟')
  await expect(page.getByTestId('stay-panel')).toBeVisible()

  const toggle = page.getByTestId('map-directory-toggle')
  await toggle.focus()
  await page.keyboard.press('Space')
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await page.keyboard.press('Enter')
  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
})


test('assumption and privacy confirmations use one keyboard-safe dialog pattern', async ({ page }) => {
  const nativeDialogs = []
  page.on('dialog', async (dialog) => {
    nativeDialogs.push(dialog.type())
    await dialog.dismiss()
  })
  await installInteractionFixture(page, { mode: 'CLAIMED', withUser: true })
  await page.goto('/trip/result')

  const assumptionTrigger = page.getByTestId('edit-assumption-destination')
  await assumptionTrigger.click()
  const assumptionDialog = page.getByRole('dialog', { name: '修改目的地' })
  await expect(assumptionDialog).toHaveAttribute('aria-modal', 'true')
  await expect(assumptionDialog.getByTestId('assumption-editor-input')).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(assumptionTrigger).toBeFocused()

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
  await expect(sourceTrigger).toBeFocused()

  const tripTrigger = page.getByTestId('delete-entire-trip')
  await tripTrigger.click()
  await expect(page.getByRole('dialog', { name: '永久删除整份行程？' }).getByRole('button', { name: '取消' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(tripTrigger).toBeFocused()
  expect(nativeDialogs).toEqual([])
})


test('PROCESSING stops at sixty seconds and a manual retry can recover', async ({ page }) => {
  await installPausedClock(page)
  const fixture = await installProcessingFixture(page)
  await page.goto('/trip/result')
  await expect(page.getByTestId('trip-progress')).toBeVisible()
  await expect.poll(fixture.calls).toBeGreaterThan(0)

  await page.clock.runFor(59_999)
  await expect(page.getByTestId('trip-progress-recovery')).toHaveCount(0)
  await page.clock.runFor(2)
  await expect(page.getByTestId('trip-progress-recovery')).toBeVisible()
  await expect(page.getByRole('button', { name: '重试' })).toBeVisible()
  await expect(page.getByRole('button', { name: '返回首页' })).toBeVisible()
  const stoppedReads = fixture.calls()
  await page.clock.runFor(5_000)
  expect(fixture.calls()).toBe(stoppedReads)

  fixture.makeReady()
  await page.getByTestId('retry-initial-result').click()
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
    await openResultView(page, 'checks')
    await page.getByTestId('preview-change').first().click({ trial: true })
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    expect(overflow).toBeLessThanOrEqual(1)

    const mobile = viewport.width < 1024
    const nav = page.getByTestId(`${mobile ? 'mobile' : 'desktop'}-nav-checks`)
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


test('home is text-only and every legacy product entrance returns to it', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '把攻略变成 每天都能照着走的卡片' })).toBeVisible()
  await expect(page.locator('input[type="file"]')).toHaveCount(0)
  await expect(page.getByText(/截图|OCR|识别图片/)).toHaveCount(0)

  await page.getByRole('button', { name: '产品说明' }).first().click()
  await expect(page).toHaveURL(/\/about$/)
  await expect(page.getByText(/上传截图|截图处理|OCR|识别图片/)).toHaveCount(0)
  await page.getByRole('button', { name: '返回首页' }).click()

  for (const legacyPath of ['/history', '/import', '/intake', '/room/example', '/templates', '/workspace/example']) {
    await page.goto(legacyPath)
    await expect(page).toHaveURL(/\/$/)
  }
})


test('login network failures use safe non-red retry copy', async ({ page }) => {
  await page.route('**/api/auth/email-login', route => route.abort('failed'))
  await page.goto('/login')
  await expect(page.getByLabel('手机号')).toBeVisible()
  await page.getByTestId('auth-email-tab').click()
  await expect(page.getByLabel('邮箱地址')).toBeVisible()
  await expect(page.getByLabel('密码')).toBeVisible()
  await page.getByTestId('auth-email').fill('traveler@example.com')
  await page.getByTestId('auth-password').fill('Example123')
  await page.getByTestId('auth-email-submit').click()

  const feedback = page.getByText('暂时无法登录，请检查信息后重试。', { exact: true }).first()
  await expect(feedback).toBeVisible()
  await expect(feedback).toHaveAttribute('data-tone', 'neutral')
  await expect(feedback).not.toHaveClass(/text-red-/)
  await expect(page.getByTestId('auth-email')).toBeFocused()
  await expect(page.locator('main')).toHaveClass(/from-\[#f8f7f2\]/)
  await expect(page.locator('body')).not.toContainText(/Failed to fetch|NetworkError|ERR_FAILED|Provider|revision|receipt/i)
})


test('login stops at one 10000ms deadline when response headers arrive but the JSON body stalls', async ({ page }) => {
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
  await page.getByTestId('auth-email-tab').click()
  const email = page.getByTestId('auth-email')
  const submit = page.getByTestId('auth-email-submit')
  await email.fill('traveler@example.com')
  await page.getByTestId('auth-password').fill('Example123')
  await submit.click()
  await expect.poll(() => page.evaluate(() => window.__g03rAuthBodyStall)).toEqual({
    calls: 1,
    bodyReads: 1,
    aborts: 0,
  })
  await expect(submit).toBeDisabled()

  await page.clock.runFor(9_999)
  await expect(submit).toBeDisabled()
  await expect(page.locator('#auth-error')).toHaveCount(0)

  await page.clock.runFor(2)
  const feedback = page.getByText('暂时无法登录，请检查信息后重试。', { exact: true }).first()
  await expect(feedback).toBeVisible()
  await expect(feedback).toHaveAttribute('data-tone', 'neutral')
  await expect(submit).toBeEnabled()
  await page.clock.runFor(20)
  await expect(email).toBeFocused()
  await expect.poll(() => page.evaluate(() => window.__g03rAuthBodyStall.aborts)).toBe(1)

  await submit.click()
  await expect.poll(() => page.evaluate(() => window.__g03rAuthBodyStall.calls)).toBe(2)
  await expect.poll(() => page.evaluate(() => window.__g03rAuthBodyStall.bodyReads)).toBe(2)
  await expect(submit).toBeDisabled()
  await page.clock.runFor(10_001)
  await expect(submit).toBeEnabled()
})


test('OTP fields have permanent accessible names and support paste and arrow keys', async ({ page }) => {
  await page.route('**/api/auth/send-code', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ dev_bypass: false }) })
  })
  await page.route('**/api/auth/verify', route => route.abort('failed'))
  await page.goto('/login')
  await page.getByLabel('手机号').fill('13800138000')
  await page.getByRole('button', { name: /获取验证码/ }).click()

  const first = page.getByLabel('验证码第 1 位')
  const second = page.getByLabel('验证码第 2 位')
  await expect(first).toHaveAttribute('autocomplete', 'one-time-code')
  await first.focus()
  await page.keyboard.press('ArrowRight')
  await expect(second).toBeFocused()
  await second.press('ArrowLeft')
  await expect(first).toBeFocused()
  await first.evaluate((input) => {
    const clipboardData = new DataTransfer()
    clipboardData.setData('text/plain', '123456')
    input.dispatchEvent(new ClipboardEvent('paste', {
      bubbles: true,
      cancelable: true,
      clipboardData,
    }))
  })
  await expect(page.locator('#auth-error')).toHaveText('暂时无法完成验证，请检查验证码后重试。')
  await expect(first).toBeFocused()
})
