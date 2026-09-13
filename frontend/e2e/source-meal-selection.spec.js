const {test, expect} = require('@playwright/test')
const {execFileSync} = require('node:child_process')
const path = require('node:path')

test.use({actionTimeout: 15000})
const RESOURCE = 'fixed-source-meal-selection-browser'
function replay(data) {
  return JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-X', 'utf8', path.resolve(__dirname, 'fixtures/source-meal-replay.py')], {
    cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8', input: JSON.stringify({...data, resource: RESOURCE}),
    env: {...process.env, PYTHONPATH: '.', RUNTIME_PROFILE: 'test', PYTHONIOENCODING: 'utf-8'},
  }))
}
async function show(page, width, options = {}) {
  const state = {result: replay({operation: 'initial', ...options}), commands: [], searches: [], external: [], pending: !!options.pending,
    failSearch: !!options.failSearch, emptySearch: !!options.emptySearch, stale: !!options.stale, expired: !!options.expired}
  const etag = () => `"fixed-source-meal-version-${state.commands.length}"`
  await page.setViewportSize({width, height: width === 390 ? 844 : 1000})
  await page.emulateMedia({reducedMotion: 'reduce'})
  await page.route('**/*', route => {
    const url = new URL(route.request().url())
    // A labelled test image verifies the supplier-image layout, not this restaurant's appearance.
    if (url.hostname === 'store.is.autonavi.com') return route.fulfill({contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="420" height="240" viewBox="0 0 420 240"><rect width="420" height="240" fill="#d8eaf0"/><circle cx="210" cy="105" r="55" fill="#fff"/><circle cx="210" cy="105" r="35" fill="#adcdbf"/><text x="210" y="205" text-anchor="middle" font-size="22" fill="#264c58">固定照片 · 仅验证显示</text></svg>'})
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) {state.external.push(url.hostname); return route.abort()}
    return route.continue()
  })
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url())
    const reply = (json, tag = etag()) => route.fulfill({json, headers: {ETag: tag}})
    if (url.pathname.endsWith('/source-meal-candidates')) {
      expect(request.headers()['if-match']).toBe(etag())
      const body = request.postDataJSON()
      state.searches.push(body)
      if (state.failSearch) {state.failSearch = false; return route.fulfill({status: 503, json: {}})}
      if (state.stale) return route.fulfill({status: 409, json: {}})
      const view = replay({operation: 'search', result: state.result, body, etag: etag()})
      if (state.emptySearch) {view.status = 'EMPTY'; view.message = '附近未找到名称或供应商标签与搜索词对应的门店，没有改选其他餐厅。'; view.candidates = []}
      return reply(view)
    }
    if (url.pathname.endsWith('/place-candidates')) return reply(replay({operation: 'confirm-search', result: state.result, body: request.postDataJSON(), etag: etag()}))
    if (url.pathname.endsWith('/commands')) {
      const command = request.postDataJSON()
      expect(request.headers()['if-match']).toBe(etag())
      expect(request.headers()['idempotency-key']).toBeTruthy()
      if (state.expired) return route.fulfill({status: 422, json: {detail: {code: 'COMMAND_TARGET_CHANGED'}}})
      const previous = structuredClone(state.result)
      state.result = replay({operation: 'command', result: state.result, command, etag: etag(), undo: state.undo, redo: state.redo})
      if (command.command_type === 'UNDO') state.redo = previous
      else {state.undo = previous; state.redo = null}
      state.commands.push(command)
      await new Promise(resolve => setTimeout(resolve, 100))
      return reply({status: 'APPLIED', changed_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE'})
    }
    if (url.pathname.endsWith('/result')) return reply(state.result)
    if (url.pathname.endsWith('/daily-dining')) return reply({status: 'UNAVAILABLE', message: '固定外部服务，本片不准备系统推荐', days: []})
    if (url.pathname.endsWith('/map-renders/latest')) return reply({...state.result.map, points: [], days: []})
    if (url.pathname.endsWith('/stay-suggestions')) return reply(state.result.stay)
    if (url.pathname.endsWith('/supplementary')) return reply({status: 'AVAILABLE', days: []})
    if (url.pathname.endsWith('/materialize')) return reply({status: 'READY', message: '固定检查', calendar: '相对日序', party_size: 2, checks_available: true})
    if (url.pathname.endsWith('/checks')) return reply({status: 'STILL_NEEDS_CONFIRMATION', message: '固定检查', items: [], remaining_must_adjust: 0, available_actions: []})
    return route.fulfill({status: 404, json: {}})
  })
  await page.goto(`/trip/result#trip=${RESOURCE}`)
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  return state
}

for (const width of [1440, 390]) test(`source dinner search, authoritative anchor, refresh undo redo at ${width}`, async ({page}, info) => {
  const state = await show(page, width, {pending: width === 390})
  const dinner = page.getByTestId('source-meal-1-1')
  await expect(dinner).toContainText('本帮菜，想吃红烧肉')
  await dinner.getByRole('button', {name: '为晚餐选餐厅'}).click()
  if (width === 390) {
    await expect(dinner).toContainText('先确认「景山公园」')
    await expect(dinner.getByLabel('手动搜索词')).toHaveCount(0)
    expect(state.searches).toHaveLength(0)
    await dinner.getByRole('button', {name: '确认用餐位置：景山公园'}).click()
    const dropdown = dinner.getByTestId('pending-place-dropdown')
    await dropdown.getByRole('button', {name: '搜索', exact: true}).click()
    await dropdown.getByRole('button', {name: /景山公园.*固定地点地址/}).click()
    await dropdown.getByRole('button', {name: '使用这个地点'}).click()
    await expect.poll(() => state.commands.length).toBe(1)
    await expect(dinner.getByLabel('手动搜索词')).toBeVisible()
  }
  expect(state.searches).toHaveLength(0)
  await dinner.getByLabel('手动搜索词').fill('红烧肉')
  await dinner.getByRole('button', {name: '查找本餐门店'}).click()
  const candidate = dinner.getByTestId('source-meal-candidate')
  await expect(candidate).toHaveCount(1)
  await expect(candidate).toContainText('固定本帮菜馆')
  await expect(candidate).toContainText('评分 4.6/5')
  await expect(candidate).toContainText('参考人均 ¥92')
  await expect(candidate).toContainText('红烧肉、油爆虾')
  await expect(candidate.locator('img')).toBeVisible()
  await expect(dinner).toContainText('不表示整句偏好、忌口或菜品供应已经核验')
  await page.screenshot({path: info.outputPath(`source-dinner-candidates-${width}.png`), fullPage: true})
  const before = state.commands.length
  await candidate.getByRole('button', {name: '选择这家晚餐餐厅'}).dblclick()
  await expect.poll(() => state.commands.length).toBe(before + 1)
  expect(state.commands.at(-1)).toMatchObject({command_type: 'DINING_INSERT', meal_role: 'DINNER', meal_slot: {day_index: 1, slot_index: 1}, insert_before: false})
  expect(state.result.days[0].activities.map(card => card.name)).toEqual(['故宫博物院', '景山公园', '固定本帮菜馆'])
  expect(state.result.days[0].meal_slots.map(slot => slot.selection_status)).toEqual(['UNSELECTED', 'SELECTED'])
  expect(state.result.days[1].meal_slots[0].selection_status).toBe('UNSELECTED')
  await expect(dinner).toContainText('晚餐 · 已安排：「固定本帮菜馆」')
  await expect(candidate).toHaveCount(0)
  await page.reload()
  await expect(dinner).toContainText('晚餐 · 已安排：「固定本帮菜馆」')
  await dinner.getByRole('button', {name: '查看用餐'}).click()
  await expect(dinner).toContainText('这餐已安排，未重复生成新餐位')
  await expect(dinner.getByLabel('手动搜索词')).toHaveCount(0)
  await page.screenshot({path: info.outputPath(`source-dinner-adopted-${width}.png`), fullPage: true})
  await page.getByRole('button', {name: '撤销', exact: true}).click()
  await expect(dinner).toContainText('晚餐 · 餐厅待选择')
  await expect(dinner.getByTestId('source-meal-candidate')).toHaveCount(0)
  await page.getByRole('button', {name: '重做', exact: true}).click()
  await expect(dinner).toContainText('晚餐 · 已安排：「固定本帮菜馆」')
  expect(state.searches).toHaveLength(1)
  expect(state.external).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('missing source position is explicit; failed and empty search leave original meal intact', async ({page}) => {
  const state = await show(page, 390, {no_anchor: true, failSearch: true})
  const dinner = page.getByTestId('source-meal-1-1')
  await dinner.getByRole('button', {name: '为晚餐选餐厅'}).click()
  await dinner.getByLabel('手动搜索词').fill('红烧肉')
  await expect(dinner.getByRole('button', {name: '查找本餐门店'})).toBeDisabled()
  await dinner.getByRole('combobox').selectOption({label: '故宫博物院 之前'})
  await dinner.getByRole('button', {name: '查找本餐门店'}).click()
  await expect(dinner).toContainText('餐厅暂时无法查询')
  state.emptySearch = true
  await dinner.getByRole('button', {name: '查找本餐门店'}).click()
  await expect(dinner).toContainText('没有改选其他餐厅')
  await expect(dinner.getByTestId('source-meal-candidate')).toHaveCount(0)
  expect(state.commands).toEqual([])
  expect(state.searches[1].position).toMatchObject({activity_token: state.result.days[0].activities[0].activity_token, insert_before: true})
  await expect(dinner).toContainText('晚餐 · 餐厅待选择')
})

test('version conflict and expired adoption never consume an original dinner or retain old candidates', async ({page}) => {
  const state = await show(page, 390, {stale: true})
  const dinner = page.getByTestId('source-meal-1-1')
  await dinner.getByRole('button', {name: '为晚餐选餐厅'}).click()
  await dinner.getByLabel('手动搜索词').fill('红烧肉')
  await dinner.getByRole('button', {name: '查找本餐门店'}).click()
  await expect(dinner).toContainText('行程已有变化')
  await expect(dinner.getByTestId('source-meal-candidate')).toHaveCount(0)
  state.stale = false
  state.expired = true
  await dinner.getByRole('button', {name: '查找本餐门店'}).click()
  await dinner.getByRole('button', {name: '选择这家晚餐餐厅'}).click()
  await expect(dinner).toContainText('已读取最新行程，请核对餐厅是否加入')
  await expect(dinner.getByTestId('source-meal-candidate')).toHaveCount(0)
  await expect(dinner).toContainText('晚餐 · 餐厅待选择')
  expect(state.commands).toEqual([])
})
