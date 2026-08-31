const { expect, test } = require('@playwright/test')


const RESOURCE_REF = 'g05-browser-knowledge-resource'
const ETAG_A = 'tu3_g05_itinerary_generation_a'
const ETAG_B = 'tu3_g05_itinerary_generation_b'


const KNOWLEDGE_CASES = [
  {
    city: '北京',
    place: '故宫博物院',
    type: 'RESERVATION_ADVICE',
    text: '建议至少提前一天实名预约，并在出发前复核官方预约规则。',
    sourceName: '北京市人民政府国际版门户网站',
    sourceUrl: 'https://english.beijing.gov.cn/latest/news/202306/t20230629_3150036.html',
  },
  {
    city: '上海',
    place: '外滩',
    type: 'NIGHT_VIEW',
    text: '如果行程允许，可把外滩安排在傍晚至入夜，兼顾白天江景与亮灯后的建筑群。',
    sourceName: '上海市人民政府英文门户网站',
    sourceUrl: 'https://english.shanghai.gov.cn/en-ScenicSpots/20231205/584672cc6d044eabb5f7f6fc9049a19f.html',
  },
  {
    city: '杭州',
    place: '雷峰塔',
    type: 'SUITABLE_TIME',
    text: '若想看雷峰夕照，可优先考虑傍晚前抵达；开放时段会随季节变化，请再看当日官方公告。',
    sourceName: '杭州市文化广电旅游局',
    sourceUrl: 'https://wgly.hangzhou.gov.cn/art/2022/12/1/art_1229696389_58943150.html',
  },
]


function suggestion(item) {
  return {
    type: item.type,
    text: item.text,
    source_name: item.sourceName,
    source_url: item.sourceUrl,
    freshness: '更新于 2026-08-31；有效至 2026-11-29',
  }
}


function resultView(item, { withKnowledge = true, mapStatus = 'AVAILABLE' } = {}) {
  return {
    status: 'READY',
    assumptions: [
      { key: 'destination', label: '目的地', value: item.city, editable: true },
      { key: 'calendar', label: '日期', value: 'Day 1', editable: true },
      { key: 'party_size', label: '人数', value: '2 人', editable: true },
    ],
    days: [{
      label: 'Day 1',
      activities: [{
        activity_token: 'activity-token-g05-browser-0001',
        name: item.place,
        category: '景点',
        area_or_address: `${item.city}市`,
        time_hint: '时间可调整',
        status: 'READY',
        available_actions: ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE'],
        knowledge_suggestions: withKnowledge ? [suggestion(item)] : [],
      }],
    }],
    map: {
      status: mapStatus,
      message: mapStatus === 'UNAVAILABLE' ? '路线服务暂时不可用，行程卡片仍可查看' : '路线已准备',
      available_actions: mapStatus === 'AVAILABLE' ? ['VIEW_MAP'] : [],
    },
    stay: {
      status: 'UNAVAILABLE',
      message: '住宿建议暂时不可用',
      area_summary: null,
      searched_scopes: [],
      candidates: [],
      available_actions: [],
    },
    available_actions: ['EDIT_ASSUMPTIONS', 'EDIT_CARDS'],
  }
}


const mapView = (status = 'AVAILABLE') => ({
  status,
  message: status === 'UNAVAILABLE' ? '路线服务暂时不可用，行程卡片仍可查看' : '路线已准备',
  days: [],
  available_actions: status === 'AVAILABLE' ? ['VIEW_MAP'] : [],
})


const stayView = {
  status: 'UNAVAILABLE',
  message: '住宿建议暂时不可用',
  area_summary: null,
  searched_scopes: [],
  candidates: [],
  available_actions: [],
}


const checksView = {
  status: 'READY',
  message: '当前没有需要优先处理的问题',
  items: [],
  remaining_must_adjust: 0,
  available_actions: [],
}


async function installFixture(page, initial) {
  const state = {
    item: initial.item,
    withKnowledge: initial.withKnowledge !== false,
    mapStatus: initial.mapStatus || 'AVAILABLE',
    etag: ETAG_A,
    commandBodies: [],
  }
  await page.addInitScript(({ resourceRef, etag }) => {
    sessionStorage.setItem('bt_active_trip_ref', resourceRef)
    sessionStorage.setItem('bt_active_trip_mode', 'DEMO')
    sessionStorage.setItem('bt_active_trip_etag', etag)
  }, { resourceRef: RESOURCE_REF, etag: ETAG_A })

  await page.route(`**/api/v3/trip-understandings/${RESOURCE_REF}/**`, async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (pathname.endsWith('/events')) {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
      return
    }
    if (pathname.endsWith('/result')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: state.etag },
        body: JSON.stringify(resultView(state.item, state)),
      })
      return
    }
    if (pathname.endsWith('/map-renders/latest')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mapView(state.mapStatus)),
      })
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
        headers: { ETag: state.etag },
        body: JSON.stringify({
          status: 'READY',
          message: '行程已准备好检查',
          calendar: 'Day 1',
          party_size: 2,
          checks_available: true,
        }),
      })
      return
    }
    if (pathname.endsWith('/checks')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(checksView) })
      return
    }
    if (pathname.endsWith('/commands') && request.method() === 'POST') {
      const body = request.postDataJSON()
      state.commandBodies.push(body)
      state.item = { ...state.item, place: body.replacement.name }
      state.withKnowledge = false
      state.mapStatus = 'NEEDS_UPDATE'
      state.etag = ETAG_B
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { ETag: ETAG_B },
        body: JSON.stringify({
          status: 'APPLIED',
          changed_days: ['Day 1'],
          map_readiness: 'NEEDS_UPDATE',
        }),
      })
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
  return state
}


async function openDetails(page, place) {
  await expect(page.getByRole('heading', { name: place })).toBeVisible()
  await page.getByRole('heading', { name: place }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
}


test('Beijing, Shanghai and Hangzhou cards show bounded sourced advice in place details', async ({ page }) => {
  const state = await installFixture(page, { item: KNOWLEDGE_CASES[0] })
  for (const item of KNOWLEDGE_CASES) {
    state.item = item
    state.withKnowledge = true
    state.mapStatus = 'AVAILABLE'
    await page.goto('/trip/result')
    await openDetails(page, item.place)
    const panel = page.getByTestId('knowledge-suggestions')
    await expect(panel).toBeVisible()
    await expect(panel).toContainText(item.text)
    await expect(panel).toContainText('更新于 2026-08-31；有效至 2026-11-29')
    await expect(panel.getByRole('link', { name: new RegExp(item.sourceName) })).toHaveAttribute('href', item.sourceUrl)
    expect(await panel.locator('li').count()).toBeLessThanOrEqual(3)
    const dom = await panel.evaluate((element) => element.outerHTML)
    expect(dom).not.toMatch(/claim[_-]?id|receipt|license_status|confidence|revision|provider/i)
    await page.getByRole('button', { name: '关闭地点详情' }).click()
  }
})


test('other-city, missing, expired, withdrawn and provider-unavailable states remain neutral', async ({ page }) => {
  const cases = [
    { item: { ...KNOWLEDGE_CASES[0], city: '成都', place: '宽窄巷子' }, label: '其他城市基础卡片' },
    { item: { ...KNOWLEDGE_CASES[0], place: '无知识地点' }, label: '知识缺失' },
    { item: { ...KNOWLEDGE_CASES[0], place: '过期内容已隐藏' }, label: '过期内容' },
    { item: { ...KNOWLEDGE_CASES[0], place: '撤回内容已隐藏' }, label: '撤回内容' },
  ]
  const state = await installFixture(page, {
    item: cases[0].item,
    withKnowledge: false,
    mapStatus: 'UNAVAILABLE',
  })
  for (const scenario of cases) {
    state.item = scenario.item
    state.withKnowledge = false
    state.mapStatus = 'UNAVAILABLE'
    await page.goto('/trip/result')
    await openDetails(page, scenario.item.place)
    await expect(page.getByTestId('knowledge-suggestions')).toHaveCount(0)
    await expect(page.getByText('路线服务暂时不可用，行程卡片仍可查看')).toBeVisible()
    const text = await page.getByRole('dialog').innerText()
    expect(text).not.toMatch(/错误|失败|冲突|内部|claim|receipt|license|confidence/i)
    await page.getByRole('button', { name: '关闭地点详情' }).click()
  }
})


test('replacing a place removes derived advice without a route-provider call', async ({ page }) => {
  const externalRouteCalls = []
  page.on('request', (request) => {
    if (new URL(request.url()).hostname.endsWith('amap.com')) externalRouteCalls.push(request.url())
  })
  const state = await installFixture(page, { item: KNOWLEDGE_CASES[0] })
  await page.goto('/trip/result')
  await openDetails(page, '故宫博物院')
  await expect(page.getByTestId('knowledge-suggestions')).toBeVisible()
  await page.getByRole('button', { name: '替换地点' }).click()
  await page.getByTestId('card-editor-name').fill('无知识地点')
  await page.getByTestId('save-card-editor').click()

  await expect(page.getByRole('heading', { name: '无知识地点' })).toBeVisible()
  await openDetails(page, '无知识地点')
  await expect(page.getByTestId('knowledge-suggestions')).toHaveCount(0)
  expect(state.commandBodies).toHaveLength(1)
  expect(state.commandBodies[0].command_type).toBe('PLACE_REPLACE')
  expect(externalRouteCalls).toEqual([])
})
