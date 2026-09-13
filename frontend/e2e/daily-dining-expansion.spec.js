const { test, expect } = require('@playwright/test')
const { execFileSync } = require('node:child_process')
const path = require('node:path')

test.use({ actionTimeout: 10000 })
const restaurantNames = ['京味轩北京传统家常菜与手工面食餐厅王府井步行街店', '固定胡同家常菜馆', '固定清香面食馆']
const candidates = restaurantNames.map((name, index) => ({
  candidate_token: `fixed-daily-dining-candidate-not-sent-to-service-${index}`, name, area_or_address: `固定测试地址 ${index + 1} 号`,
  reason: index === 0 ? '经此店前往下一站约多6分钟；营业情况请到店前确认。' : '附近候选；绕路时间及营业情况尚未确认。',
  extra_minutes: index === 0 ? 6 : null, recommended: index === 0,
}))
const card = (name, index, extra = {}) => ({ name, activity_token: `fixed-daily-dining-card-${index}`, category: '景点',
  city: '北京', area_or_address: '固定测试地址', status: 'READY',
  available_actions: ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE'], ...extra })
function initialResult(before) {
  const first = [card('景山公园', 0), card('北海公园', 1)]
  return { status: 'READY', can_undo: false,
    assumptions: [{ key: 'destination', label: '目的地', value: '北京', editable: true },
      { key: 'calendar', label: '日序', value: 'Day 1–2', editable: true }, { key: 'party_size', label: '人数', value: '2 人', editable: true }],
    days: [{ label: 'Day 1', activities: first, alternatives: [{name: '首都博物馆', category: '景点', city: '北京'}],
      meal_slots: [{meal_role: 'LUNCH', selection_status: 'UNSELECTED', preference_text: '原文保留：想吃炸酱面',
        ...(before ? {before_activity_token: first[0].activity_token} : {after_activity_token: first[0].activity_token, before_activity_token: first[1].activity_token})},
      {meal_role: 'DINNER', selection_status: 'UNSELECTED', preference_text: '原文保留：家常菜'}] },
    {label: 'Day 2', activities: [card('天坛公园', 2), card('原文已选午餐店', 3, {category: '餐饮', meal_role: 'LUNCH'})], alternatives: []}],
    map: {status: 'AVAILABLE', message: '固定地图未展开', available_actions: ['RENDER_MAP']},
    stay: {status: 'UNAVAILABLE', message: '固定不提供住宿', candidates: [], searched_scopes: [], available_actions: []},
    available_actions: ['EDIT_ASSUMPTIONS', 'EDIT_CARDS'],
  }
}
function replay(operation, result, command, undo) {
  return JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-X', 'utf8', path.resolve(__dirname, 'fixtures/daily-dining-replay.py')], {
    cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8', input: JSON.stringify({operation, result, command, undo, candidates}),
    env: {...process.env, PYTHONPATH: '.', RUNTIME_PROFILE: 'test', PYTHONIOENCODING: 'utf-8'},
  }))
}
async function show(page, width, {before = false, stale = false, noAnchor = false, expired = false, protectedStatus} = {}) {
  const state = {result: initialResult(before), commands: [], refreshes: [], reads: 0, routeWrites: 0, external: [], failRefresh: true, rejected: false}
  if (protectedStatus) {
    state.result.days[1].activities[1].status = 'NEEDS_CONFIRMATION'
    state.result.status = 'PARTIAL_RESULT'
  }
  state.dining = replay('daily', state.result)
  if (protectedStatus) state.dining.status = protectedStatus
  if (noAnchor) state.dining.days[0].after_activity_token = null
  const etag = () => `"fixed-dining-version-${state.commands.length}"`
  state.diningEtag = stale ? '"fixed-stale-version"' : etag()
  await page.setViewportSize({width, height: width === 390 ? 844 : 1000})
  await page.emulateMedia({reducedMotion: 'reduce'})
  await page.route('**/*', route => {
    if (!['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname)) {state.external.push(route.request().url()); return route.abort()}
    return route.continue()
  })
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url())
    const reply = (json, tag = etag()) => route.fulfill({json, headers: {ETag: tag}})
    if (url.pathname.endsWith('/commands')) {
      const command = request.postDataJSON()
      expect(request.headers()['if-match']).toBe(etag())
      expect(request.headers()['idempotency-key']).toBeTruthy()
      if (expired) {state.rejected = true; return route.fulfill({status: 422, json: {detail: {code: 'CANDIDATE_EXPIRED'}}})}
      const previous = structuredClone(state.result)
      state.result = replay('command', state.result, command, state.undo)
      state.undo = previous
      state.commands.push(command)
      if (protectedStatus) {
        state.dining = replay('daily', state.result)
        state.dining.status = protectedStatus
        state.diningEtag = etag()
      }
      await new Promise(resolve => setTimeout(resolve, 100))
      return reply({status: 'APPLIED', changed_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE'})
    }
    if (url.pathname.endsWith('/result')) return reply(state.result)
    if (url.pathname.endsWith('/place-candidates')) return reply({status: 'AVAILABLE', candidates: [{...candidates[0], category: '餐饮', position: {longitude: 116.398, latitude: 39.918, coordinate_system: 'GCJ02'}}]})
    if (url.pathname.endsWith('/daily-dining')) {
      state.reads++
      if (request.method() === 'POST') {
        state.refreshes.push(request.headers())
        if (state.failRefresh) {state.failRefresh = false; return route.fulfill({status: 503, json: {}})}
        state.dining = replay('daily', state.result)
        state.diningEtag = etag()
      }
      return reply(state.dining, state.diningEtag)
    }
    if (url.pathname.endsWith('/map-renders/latest')) return reply({...state.result.map, points: [], days: []})
    if (url.pathname.endsWith('/map-renders')) {state.routeWrites++; return route.fulfill({status: 409, json: {}})}
    if (url.pathname.endsWith('/stay-suggestions')) return reply(state.result.stay)
    if (url.pathname.endsWith('/supplementary')) return reply({status: 'AVAILABLE', days: []})
    if (url.pathname.endsWith('/materialize')) return reply({status: 'READY', message: '固定检查', calendar: '相对日序', party_size: 2, checks_available: true})
    if (url.pathname.endsWith('/checks')) return reply({status: 'STILL_NEEDS_CONFIRMATION', message: '固定检查', items: [], remaining_must_adjust: 0, available_actions: []})
    return route.fulfill({status: 404, json: {}})
  })
  await page.goto('/trip/result#trip=fixed-daily-meal-expansion-20260913')
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  return state
}

// The source insertion position is business coverage, independent of viewport size.
for (const width of [1440, 390]) for (const before of [false, true]) test(`daily restaurant expansion preserves source meals, adopts ${before ? 'before' : 'after'} the authoritative anchor and restores at ${width}`, {tag: width === 390 ? '@small-screen' : '@desktop'}, async ({page}, info) => {
  const state = await show(page, width, {before})
  const meal = page.getByTestId('daily-meal-card').first()
  const source = page.getByTestId('source-meals-1')
  await expect(source).toContainText('午餐 · 餐厅待选择')
  await expect(source).toContainText('想吃炸酱面')
  await expect(source).toContainText('晚餐 · 餐厅待选择')
  await expect(meal).toContainText(`加入「景山公园」${before ? '之前' : '之后'}`)
  await expect(meal).toContainText('直线约480米，步行路线待确认')
  await expect(meal.getByTestId('daily-dining-candidate')).toHaveCount(0)
  await expect(page.getByTestId('daily-meal-card').nth(1)).toContainText('已安排原文已选午餐店')
  await expect(page.getByTestId('daily-meal-card').nth(1).getByRole('button')).toHaveCount(0)
  await meal.getByRole('button', {name: '查看 3 家候选'}).click()
  const articles = meal.getByTestId('daily-dining-candidate')
  await expect(articles).toHaveCount(3)
  await expect(articles.locator('h3')).toHaveText(restaurantNames)
  await expect(articles.first().getByRole('button', {name: '加入行程'})).toHaveCSS('color', 'rgb(255, 255, 255)')
  expect(await articles.locator('h3').evaluateAll(nodes => nodes.every(node => node.scrollWidth <= node.clientWidth + 1))).toBe(true)
  await expect(articles.locator('img')).toHaveCount(0)
  await expect(meal).not.toContainText(/人均|评分|¥/)
  await page.screenshot({path: info.outputPath(`dining-expanded-${before ? 'before' : 'after'}-${width}.png`), fullPage: true})
  await meal.getByRole('button', {name: '收起候选'}).click()
  await expect(articles).toHaveCount(0)
  expect(state.commands).toHaveLength(0)
  await meal.getByRole('button', {name: '查看 3 家候选'}).click()
  await articles.nth(1).getByRole('button', {name: '加入行程'}).dblclick()
  await expect.poll(() => state.commands.length).toBe(1)
  expect(state.commands[0]).toMatchObject({command_type: 'DINING_INSERT', meal_role: 'LUNCH',
    after_activity_token: 'fixed-daily-dining-card-0', ...(before ? {insert_before: true} : {})})
  expect(state.result.days[0].activities.map(card => card.name)).toEqual(before
    ? [restaurantNames[1], '景山公园', '北海公园'] : ['景山公园', restaurantNames[1], '北海公园'])
  expect(state.result.days[0].meal_slots.map(slot => slot.selection_status)).toEqual(['SELECTED', 'UNSELECTED'])
  await expect(source).toContainText(`午餐 · 已安排：「${restaurantNames[1]}」`)
  await expect(source).toContainText('想吃炸酱面')
  await expect(source).toContainText('晚餐 · 餐厅待选择')
  await expect(meal).toContainText('行程已调整')
  await expect(meal.getByTestId('daily-dining-candidate')).toHaveCount(0)
  expect(state.refreshes).toHaveLength(0)
  const update = meal.getByRole('button', {name: '更新用餐建议'})
  await update.click()
  await expect(meal).toContainText('建议更新未确认')
  await update.click()
  await expect(meal).toContainText(`已安排${restaurantNames[1]}`)
  expect(state.refreshes).toHaveLength(2)
  expect(state.refreshes[0]['if-match']).toBe('"fixed-dining-version-1"')
  expect(state.refreshes[0]['idempotency-key']).toBe(state.refreshes[1]['idempotency-key'])
  await expect(meal.getByRole('button', {name: '加入行程'})).toHaveCount(0)
  await page.reload()
  await expect(source).toContainText(`午餐 · 已安排：「${restaurantNames[1]}」`)
  await expect(meal).toContainText(`已安排${restaurantNames[1]}`)
  await page.screenshot({path: info.outputPath(`dining-adopted-${before ? 'before' : 'after'}-${width}.png`), fullPage: true})
  await page.getByRole('button', {name: '撤销', exact: true}).click()
  await expect.poll(() => state.commands.length).toBe(2)
  expect(state.commands[1]).toEqual({command_type: 'UNDO'})
  await expect(source).toContainText('午餐 · 餐厅待选择')
  await expect(source).toContainText('晚餐 · 餐厅待选择')
  await expect(page.getByTestId('day-lane-1').getByTestId('activity-card').locator('h3')).toHaveText(['景山公园', '北海公园'])
  await meal.getByRole('button', {name: '更新用餐建议'}).click()
  await expect(meal.getByRole('button', {name: '查看 3 家候选'})).toBeVisible()
  expect(state.routeWrites).toBe(0)
  expect(state.external).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

for (const width of [1440, 390]) test(`stale version never exposes old candidates and missing anchor prevents adoption at ${width}`, {tag: width === 390 ? '@small-screen' : '@desktop'}, async ({page}) => {
  const state = await show(page, width, {stale: true})
  const meal = page.getByTestId('daily-meal-card').first()
  await expect(meal).toContainText('行程已调整')
  await expect(meal.getByRole('button', {name: '查看 3 家候选'})).toHaveCount(0)
  expect(state.commands).toHaveLength(0)
  await page.unroute('**/api/**')
  await page.goto('about:blank')
  const next = await show(page, width, {noAnchor: true})
  await meal.getByRole('button', {name: '查看 3 家候选'}).click()
  await expect(meal).toContainText('加入位置尚未确认')
  for (const button of await meal.getByRole('button', {name: '加入行程'}).all()) await expect(button).toBeDisabled()
  expect(next.commands).toHaveLength(0)
})

for (const width of [1440, 390]) test(`expired selection remains a failure with original lunch unconsumed at ${width}`, {tag: width === 390 ? '@small-screen' : '@desktop'}, async ({page}) => {
  const state = await show(page, width, {expired: true})
  const meal = page.getByTestId('daily-meal-card').first()
  await meal.getByRole('button', {name: '查看 3 家候选'}).click()
  await meal.getByRole('button', {name: '加入行程'}).first().click()
  await expect.poll(() => state.rejected).toBe(true)
  await expect(page.getByText('上次修改未保存', {exact: true})).toBeVisible()
  await expect(meal).toContainText('已读取最新行程，请核对餐厅是否加入')
  await expect(meal.getByRole('button', {name: '加入行程'})).toHaveCount(0)
  await expect(meal.getByRole('button', {name: '更新用餐建议'})).toBeEnabled()
  await expect(page.getByTestId('source-meals-1')).toContainText('午餐 · 餐厅待选择')
  expect(state.commands).toHaveLength(0)
  expect(state.result.days[0].activities).toHaveLength(2)
})

for (const width of [1440, 390]) for (const protectedStatus of ['NEEDS_UPDATE', 'PREPARING', 'UNAVAILABLE']) test(`protected source lunch remains confirmable during ${protectedStatus}, with candidates unavailable at ${width}`, {tag: width === 390 ? '@small-screen' : '@desktop'}, async ({page}) => {
  const state = await show(page, width, {protectedStatus})
  const meals = page.getByTestId('daily-meal-card'), pending = meals.nth(1)
  await expect(meals.getByRole('button', {name: '加入行程'})).toHaveCount(0)
  await expect(meals.getByRole('button', {name: '查看 3 家候选'})).toHaveCount(0)
  await expect(pending).toContainText('原文用餐地点原文已选午餐店尚未确认')
  await pending.getByRole('button', {name: '确认原文午餐'}).click()
  const dropdown = pending.getByTestId('pending-place-dropdown')
  await dropdown.getByRole('button', {name: '搜索', exact: true}).click()
  await dropdown.getByRole('button', {name: new RegExp(restaurantNames[0])}).click()
  await dropdown.getByRole('button', {name: '使用这个地点', exact: true}).click()
  await expect.poll(() => state.commands.length).toBe(1)
  expect(state.commands[0].command_type).toBe('PLACE_CONFIRM')
  expect(state.result.days[1].activities).toHaveLength(2)
  await expect(page.getByTestId('day-lane-2').getByTestId('activity-card').locator('h3')).toHaveText(['天坛公园', restaurantNames[0]])
  await expect(pending).toContainText(`已安排${restaurantNames[0]}`)
  await expect(pending.getByRole('button')).toHaveCount(0)
  expect(state.refreshes).toHaveLength(0)
  expect(state.routeWrites).toBe(0)
  expect(state.external).toEqual([])
})
