const { expect, test } = require('@playwright/test')


const RESOURCE_REF = 'g03r-race-safe-result'
const ETAG_A = 'tu3_race_generation_a'
const ETAG_B = 'tu3_race_generation_b'
const ETAG_C = 'tu3_obsolete_generation_c'


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
  ],
  remaining_must_adjust: 0,
  available_actions: ['PREVIEW_CHANGE'],
}


async function installRaceFixture(page, scenario = 'cleanup') {
  let resultReads = 0
  let materializeCalls = 0
  let materializeInFlight = 0
  let maxMaterializeInFlight = 0
  let checksCalls = 0

  await page.addInitScript(({ resourceRef, etag }) => {
    sessionStorage.setItem('bt_active_trip_ref', resourceRef)
    sessionStorage.setItem('bt_active_trip_mode', 'DEMO')
    sessionStorage.setItem('bt_active_trip_etag', etag)
  }, { resourceRef: RESOURCE_REF, etag: ETAG_A })

  await page.route(`**/api/v3/trip-understandings/${RESOURCE_REF}/**`, async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname

    if (pathname.endsWith('/events')) {
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
      materializeInFlight += 1
      maxMaterializeInFlight = Math.max(maxMaterializeInFlight, materializeInFlight)
      const currentCall = materializeCalls
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
      if (currentCall === 1 && (scenario === 'cleanup' || scenario === 'stale')) {
        await new Promise((resolve) => setTimeout(resolve, 700))
        await fulfillReady(scenario === 'cleanup' ? ETAG_B : ETAG_C)
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
      materializeInFlight -= 1
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
    calls: () => ({ resultReads, materializeCalls, maxMaterializeInFlight, checksCalls }),
  }
}


test('current checks generation survives result cleanup without duplicate materialize', async ({ page }) => {
  const fixture = await installRaceFixture(page)

  await page.goto('/trip/result')
  await expect(page.getByTestId('trip-days')).toBeVisible()
  await expect(page.getByTestId('trip-check-item')).toHaveCount(3, { timeout: 5_000 })
  await expect(page.getByText('优先处理这三项，行程会更顺畅')).toBeVisible()

  expect(fixture.calls()).toEqual({
    resultReads: 2,
    materializeCalls: 1,
    maxMaterializeInFlight: 1,
    checksCalls: 1,
  })
  await page.close()
})


test('obsolete materialize response yields to the current generation sequentially', async ({ page }) => {
  const fixture = await installRaceFixture(page, 'stale')

  await page.goto('/trip/result')
  await expect(page.getByTestId('trip-check-item')).toHaveCount(3, { timeout: 5_000 })

  expect(fixture.calls()).toEqual({
    resultReads: 2,
    materializeCalls: 2,
    maxMaterializeInFlight: 1,
    checksCalls: 1,
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
  })
  await page.close()
})


test('ordinary preparation failure is recoverable only after explicit retry', async ({ page }) => {
  const fixture = await installRaceFixture(page, 'failure')

  await page.goto('/trip/result')
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
  })
  await page.close()
})
