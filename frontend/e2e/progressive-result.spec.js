const { expect, test } = require('@playwright/test')


const STOP_RESOURCE = 'progressive-stop-result-001'
const RESUME_RESOURCE = 'progressive-resume-result-001'


function card(token, name, status = 'NEEDS_CONFIRMATION') {
  return {
    activity_token: token,
    name,
    category: '景点',
    area_or_address: '北京市',
    time_hint: '上午',
    status,
    available_actions: status === 'READY'
      ? ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE']
      : ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE'],
  }
}


function resultView(name, status = 'PARTIAL_RESULT') {
  return {
    status,
    assumptions: [
      { key: 'destination', label: '目的地', value: '北京', editable: true },
      { key: 'calendar', label: '日期', value: 'Day 1', editable: true },
      { key: 'party_size', label: '人数', value: '2 人', editable: true },
    ],
    days: [{ label: 'Day 1', activities: [card(`token-${name}`, name)] }],
    map: {
      status: 'UNAVAILABLE',
      message: '路线暂时无法显示，行程卡片不受影响。',
      available_actions: ['RENDER_MAP'],
    },
    stay: {
      status: 'UNAVAILABLE',
      message: '住宿待选择',
      area_summary: null,
      searched_scopes: [],
      candidates: [],
      available_actions: [],
    },
    available_actions: ['EDIT_ASSUMPTIONS', 'EDIT_CARDS'],
  }
}


function progressView({ cursor, phase, snapshot = null, checked = 0 }) {
  return {
    status: 'PROCESSING',
    message:
      phase === 'RECEIVED'
        ? '已收到文字，正在整理日期。'
        : '正在核对地点。',
    retry_after_ms: 25,
    phase,
    event_cursor: cursor,
    progress: {
      day_count: snapshot ? 1 : 0,
      card_count: snapshot ? 1 : 0,
      places_checked: checked,
      places_total: snapshot ? 1 : 0,
    },
    snapshot,
  }
}


function sse(id, payload, event = 'progress') {
  return `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(payload)}\n\n`
}


async function installSession(page, resource) {
  await page.addInitScript((reference) => {
    sessionStorage.setItem('bt_active_trip_ref', reference)
    sessionStorage.setItem('bt_active_trip_mode', 'FULL')
    sessionStorage.setItem('bt_trip_event_cursor:unrelated-trip', '99')
  }, resource)
}


async function fulfillSettledDependencies(route, etag) {
  const pathname = new URL(route.request().url()).pathname
  if (pathname.endsWith('/map-renders/latest')) {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'UNAVAILABLE',
        message: '路线暂时无法显示，行程卡片不受影响。',
        days: [],
        available_actions: ['RENDER_MAP'],
      }),
    })
    return true
  }
  if (pathname.endsWith('/stay-suggestions')) {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(resultView('占位').stay),
    })
    return true
  }
  if (pathname.endsWith('/supplementary')) {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'UNAVAILABLE', days: [] }),
    })
    return true
  }
  if (pathname.endsWith('/materialize')) {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      headers: { ETag: etag },
      body: JSON.stringify({
        status: 'READY',
        message: '检查已准备',
        calendar: 'Day 1',
        party_size: 2,
        checks_available: true,
      }),
    })
    return true
  }
  if (pathname.endsWith('/checks')) {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        message: '暂无需要优先调整的项目。',
        items: [],
        remaining_must_adjust: 0,
        available_actions: [],
      }),
    })
    return true
  }
  return false
}


test('progress cards stay read-only until stop promotes the last snapshot to an editable result', async ({ page }) => {
  await installSession(page, STOP_RESOURCE)
  let resultReads = 0
  let eventReads = 0
  let cancelled = false
  let releaseWaitingStream
  const waitingStream = new Promise((resolve) => {
    releaseWaitingStream = resolve
  })
  const cancelHeaders = []
  const partial = resultView('故宫博物院')

  await page.route(`**/api/v3/trip-understandings/${STOP_RESOURCE}/**`, async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (pathname.endsWith('/result')) {
      resultReads += 1
      if (cancelled) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          headers: { ETag: 'tu3_stopped_partial' },
          body: JSON.stringify(partial),
        })
      } else {
        await route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: JSON.stringify(progressView({
            cursor: eventReads ? 1 : 0,
            phase: eventReads ? 'CARDS_AVAILABLE' : 'RECEIVED',
            snapshot: eventReads ? partial : null,
          })),
        })
      }
      return
    }
    if (pathname.endsWith('/events')) {
      eventReads += 1
      if (eventReads === 1) {
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          body: sse(1, {
            status: 'PROCESSING',
            message: '已经整理出一天卡片，正在核对地点。',
            phase: 'CARDS_AVAILABLE',
            progress: progressView({ cursor: 1, phase: 'CARDS_AVAILABLE', snapshot: partial }).progress,
            snapshot: partial,
          }),
        })
      } else {
        await waitingStream
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          body: '',
        }).catch(() => undefined)
      }
      return
    }
    if (pathname.endsWith('/cancel')) {
      cancelHeaders.push(request.headers())
      cancelled = true
      releaseWaitingStream()
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: 'tu3_stopped_partial' },
        body: JSON.stringify({
          status: 'STOPPED_WITH_DRAFT',
          message: '已停止整理，当前卡片可继续编辑。',
          has_editable_result: true,
        }),
      })
      return
    }
    if (await fulfillSettledDependencies(route, 'tu3_stopped_partial')) return
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/trip/result')
  await expect(page.getByText('临时预览 · 只读')).toBeVisible()
  await expect(page.getByText('故宫博物院', { exact: true })).toBeVisible()
  await expect(page.getByText('待确认', { exact: true })).toBeVisible()
  await expect(page.locator('.e-progress-days button')).toHaveCount(0)
  await expect(page.getByTestId('itinerary-workspace')).toHaveCount(0)

  await page.getByRole('button', { name: '停止整理并编辑' }).click()
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  await expect(page.getByRole('button', { name: '移动 故宫博物院 到其他天或位置' })).toBeEnabled()
  expect(cancelHeaders).toHaveLength(1)
  expect(cancelHeaders[0]['idempotency-key']).toBeTruthy()
  expect(cancelHeaders[0]['if-match']).toBeUndefined()
  expect(resultReads).toBeGreaterThanOrEqual(3)
})


test('a closed event stream resumes from its resource cursor and ignores duplicate events', async ({ page }) => {
  await installSession(page, RESUME_RESOURCE)
  let resultReads = 0
  const eventHeaders = []
  const first = resultView('第一版地点')
  const second = resultView('第二版地点')
  const final = resultView('最终确认地点', 'READY')

  await page.route(`**/api/v3/trip-understandings/${RESUME_RESOURCE}/**`, async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (pathname.endsWith('/result')) {
      resultReads += 1
      if (resultReads >= 3) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          headers: { ETag: 'tu3_resumed_ready' },
          body: JSON.stringify(final),
        })
      } else if (resultReads === 2) {
        // Older or partially deployed readers can still return a sparse 202.
        // It must not erase the newer cumulative SSE cursor or snapshot.
        await route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: '{}',
        })
      } else {
        await route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: JSON.stringify(progressView({
            cursor: 0,
            phase: 'RECEIVED',
            snapshot: null,
            checked: 0,
          })),
        })
      }
      return
    }
    if (pathname.endsWith('/events')) {
      eventHeaders.push(request.headers())
      if (eventHeaders.length === 1) {
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          body: sse(6, {
            status: 'PROCESSING',
            message: '第一次连接已收到卡片。',
            phase: 'CARDS_AVAILABLE',
            progress: progressView({ cursor: 6, phase: 'CARDS_AVAILABLE', snapshot: first }).progress,
            snapshot: first,
          }),
        })
      } else {
        const duplicate = sse(6, {
          status: 'PROCESSING',
          message: '这条重复事件不应生效。',
          phase: 'CARDS_AVAILABLE',
          progress: progressView({ cursor: 6, phase: 'CARDS_AVAILABLE', snapshot: resultView('错误重复地点') }).progress,
          snapshot: resultView('错误重复地点'),
        })
        const updated = sse(7, {
          status: 'PROCESSING',
          message: '继续核对新地点。',
          phase: 'CHECKING_PLACES',
          progress: progressView({ cursor: 7, phase: 'CHECKING_PLACES', snapshot: second, checked: 1 }).progress,
          snapshot: second,
        })
        const ready = sse(8, {
          status: 'READY',
          message: '行程卡片已准备好。',
          phase: 'CHECKING_PLACES',
          progress: progressView({ cursor: 8, phase: 'CHECKING_PLACES', snapshot: second, checked: 1 }).progress,
          snapshot: second,
        }, 'result_available')
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          body: duplicate + updated + ready,
        })
      }
      return
    }
    if (await fulfillSettledDependencies(route, 'tu3_resumed_ready')) return
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/trip/result')
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  await expect(page.getByRole('heading', { name: '最终确认地点' })).toBeVisible()
  await expect(page.getByText('错误重复地点')).toHaveCount(0)
  expect(eventHeaders).toHaveLength(2)
  expect(eventHeaders[0]['last-event-id']).toBeUndefined()
  expect(eventHeaders[1]['last-event-id']).toBe('6')
  expect(await page.evaluate((resource) =>
    sessionStorage.getItem(`bt_trip_event_cursor:${resource}`), RESUME_RESOURCE)).toBe('8')
  expect(await page.evaluate(() =>
    sessionStorage.getItem('bt_trip_event_cursor:unrelated-trip'))).toBe('99')
})


test('an idle event stream falls back to an authoritative read instead of loading forever', async ({ page }) => {
  const resource = 'progressive-idle-stream-001'
  await page.clock.install()
  await installSession(page, resource)
  let resultReads = 0
  let eventReads = 0
  let releaseStream
  const heldStream = new Promise((resolve) => {
    releaseStream = resolve
  })
  const final = resultView('空闲后确认地点', 'READY')

  await page.route(`**/api/v3/trip-understandings/${resource}/**`, async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname.endsWith('/result')) {
      resultReads += 1
      if (resultReads >= 2) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          headers: { ETag: 'tu3_idle_recovered' },
          body: JSON.stringify(final),
        })
      } else {
        await route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: JSON.stringify(progressView({ cursor: 0, phase: 'RECEIVED' })),
        })
      }
      return
    }
    if (pathname.endsWith('/events')) {
      eventReads += 1
      await heldStream
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: '',
      }).catch(() => undefined)
      return
    }
    if (await fulfillSettledDependencies(route, 'tu3_idle_recovered')) return
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  try {
    await page.goto('/trip/result')
    await expect.poll(() => eventReads).toBe(1)
    await expect(page.getByRole('button', { name: '停止整理' })).toBeVisible()
    await page.clock.fastForward(45_100)
    await expect.poll(() => resultReads).toBeGreaterThanOrEqual(2)
    await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
    await expect(page.getByRole('heading', { name: '空闲后确认地点' })).toBeVisible()
  } finally {
    releaseStream()
  }
})
