const {test, expect} = require('@playwright/test')
const {execFileSync} = require('node:child_process')
const path = require('node:path')
const fs = require('node:fs')
const ts = require('typescript')

test.use({actionTimeout: 15000})
const RESOURCE = 'fixed-dining-access-browser'
function replay(data) {
  return JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-X', 'utf8', path.resolve(__dirname, 'fixtures/dining-access-replay.py')], {
    cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8', input: JSON.stringify({...data, resource: RESOURCE}),
    env: {...process.env, PYTHONPATH: '.', RUNTIME_PROFILE: 'test', PYTHONIOENCODING: 'utf-8'},
  }))
}

async function show(page, width, family) {
  const state = {family, result: replay({operation: 'initial', family}), commands: [], searches: [], unexpected: [], dailyWrites: 0}
  const etag = () => `"fixed-dining-access-version-${state.commands.length}"`
  const call = data => replay({family, result: state.result, etag: etag(), ...data})
  let daily
  await page.setViewportSize({width, height: 1000})
  await page.emulateMedia({reducedMotion: 'reduce'})
  await page.addInitScript(() => {
    window.diningPngText = []
    const original = CanvasRenderingContext2D.prototype.fillText
    CanvasRenderingContext2D.prototype.fillText = function (text, x, y, ...rest) {
      if (this.canvas.width === 1440) window.diningPngText.push({text: String(text), x, y,
        width: this.measureText(String(text)).width, canvasWidth: this.canvas.width, canvasHeight: this.canvas.height})
      return original.call(this, text, x, y, ...rest)
    }
  })
  await page.route('**/*', route => {
    if (!['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname)) return route.abort()
    return route.continue()
  })
  await page.route('**/api/**', async route => {
    const request = route.request(), pathname = new URL(request.url()).pathname
    const reply = json => route.fulfill({json, headers: {ETag: etag()}})
    if (pathname.endsWith('/commands')) {
      expect(request.headers()['if-match']).toBe(etag())
      expect(request.headers()['idempotency-key']).toBeTruthy()
      const command = request.postDataJSON(), previous = structuredClone(state.result)
      state.result = call({operation: 'command', command, undo: state.undo, redo: state.redo})
      if (command.command_type === 'UNDO') state.redo = previous
      else {state.undo = previous; state.redo = null}
      state.commands.push(command); daily = null
      return reply({status: 'APPLIED', changed_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE'})
    }
    if (pathname.endsWith('/source-meal-candidates') || pathname.endsWith('/dining-candidates') || pathname.endsWith('/place-candidates')) {
      const body = request.postDataJSON()
      state.searches.push({path: pathname, body})
      return reply(call({operation: pathname.endsWith('/source-meal-candidates') ? 'source' : pathname.endsWith('/place-candidates') ? 'place' : 'nearby', body}))
    }
    if (pathname.endsWith('/daily-dining')) {
      if (request.method() !== 'GET') {state.dailyWrites++; return route.fulfill({status: 409, json: {}})}
      daily ||= call({operation: 'daily'})
      return reply(daily)
    }
    if (pathname.endsWith('/result')) return reply(state.result)
    if (pathname.endsWith('/map-renders/latest')) return reply({...state.result.map, days: [], points: []})
    if (pathname.endsWith('/stay-suggestions')) return reply(state.result.stay)
    if (pathname.endsWith('/supplementary')) return reply({status: 'AVAILABLE', days: []})
    if (pathname.endsWith('/materialize')) return reply({status: 'READY', calendar: '按 Day 编号安排', party_size: 2, checks_available: true, message: '固定检查'})
    if (pathname.endsWith('/checks')) return reply({status: 'STILL_NEEDS_CONFIRMATION', message: '固定检查', items: [], remaining_must_adjust: 0, available_actions: []})
    if (request.method() !== 'GET') state.unexpected.push(pathname)
    return route.fulfill({status: 404, json: {}})
  })
  await page.goto(`/trip/result#trip=${RESOURCE}`)
  await expect(page.getByTestId('activity-card')).toHaveCount(4)
  return state
}

async function exported(page, info, suffix, texts) {
  await page.evaluate(() => {window.diningPngText = []})
  await page.getByTestId('export-itinerary-png').click()
  await expect(page.getByAltText('行程横链导出预览', {exact: true})).toBeVisible()
  const drawn = await page.evaluate(() => window.diningPngText)
  const joined = drawn.map(row => row.text).join('')
  for (const text of texts) expect(joined).toContain(text)
  for (const row of drawn) {
    expect(row.x, row.text).toBeGreaterThanOrEqual(0)
    expect(row.x + row.width, row.text).toBeLessThanOrEqual(row.canvasWidth)
    expect(row.y, row.text).toBeLessThan(row.canvasHeight - 10)
  }
  const download = page.waitForEvent('download')
  await page.getByTestId('download-itinerary-png').click()
  await (await download).saveAs(info.outputPath(`${suffix}.png`))
  await page.getByRole('dialog').getByRole('button', {name: '关闭图片预览', exact: true}).click()
}

test('light-food evidence only blocks explicit main meals; unspecified is not silently lunch', {tag: '@desktop'}, () => {
  const source = fs.readFileSync(path.resolve(__dirname, '../src/app/trip/result/dining-access.tsx'), 'utf8')
  const js = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX}}).outputText
  const module = {exports: {}}
  new Function('module', 'exports', 'require', js)(module, module.exports, name => name.endsWith('.css') ? {} : require(name))
  const light = {meal_evidence_status: 'LIGHT_FOOD_ITEMS_ONLY', dining_access: null}
  for (const role of ['BREAKFAST', 'LUNCH', 'DINNER']) expect(module.exports.diningAdoptionBlocked(light, role)).toBe(true)
  for (const role of ['SNACK', 'UNSPECIFIED', null, undefined]) expect(module.exports.diningAdoptionBlocked(light, role)).toBe(false)
  expect(module.exports.diningAdoptionBlocked({...light, dining_access: {status: 'NEEDS_REVIEW', parent_name: '另一次到访'}}, 'SNACK')).toBe(true)
  expect(module.exports.diningAccessLines({dining_access: null}, true).join('')).toContain('尚未核实')
})

for (const width of [1440, 1280]) for (const family of ['saved', 'independent']) {
  test(`dining access ${family} candidates survive adoption refresh move undo and PNG at ${width}`, {tag: '@desktop'}, async ({page}, info) => {
    const state = await show(page, width, family)
    const parent = family === 'saved' ? '故宫博物院' : '青禾商场'
    const selectedName = family === 'saved' ? '冰窖餐厅' : '青禾商场云台传统家常菜与手工面食餐厅'
    const lightName = family === 'saved' ? '坤宁宫东院餐厅' : '陌上茶点铺'
    const daily = page.getByTestId('daily-meal-card').first()
    await daily.getByRole('button', {name: '查看 3 家候选'}).click()
    const dailyLight = daily.getByTestId('daily-dining-candidate').filter({has: page.getByRole('heading', {name: lightName, exact: true})})
    await expect(dailyLight).toContainText('当前资料仅有茶饮点心，正餐供应尚未核实')
    await expect(dailyLight.getByRole('button', {name: '正餐供应待核实'})).toBeDisabled()
    if (family === 'independent') {
      await expect(daily.getByTestId('daily-dining-candidate').filter({hasText: '南岸餐厅'}).getByRole('button', {name: '用餐位置需核对'})).toBeDisabled()
      await expect(daily).not.toContainText('游览')
    }
    await daily.getByRole('button', {name: '收起候选'}).click()
    const meal = page.getByTestId('source-meal-1-0')
    await meal.getByRole('button', {name: '为午餐选餐厅'}).click()
    await meal.getByLabel('手动搜索词').fill('餐厅')
    await meal.getByLabel('手动搜索词').press('Enter')
    const chosen = meal.getByTestId('source-meal-candidate').filter({has: page.getByRole('heading', {name: selectedName, exact: true})})
    await expect(chosen).toContainText(`场所归属：${parent}`)
    await expect(chosen).toContainText('离开前用餐')
    await expect(chosen).toContainText('可用门区与营业情况尚未核实')
    await expect(meal.getByTestId('source-meal-candidate').filter({hasText: lightName}).getByRole('button', {name: '正餐供应待核实'})).toBeDisabled()
    expect(state.commands).toEqual([])
    await page.screenshot({path: info.outputPath(`candidates-${family}-${width}.png`), fullPage: true})
    const button = chosen.getByRole('button', {name: `在「${parent}」到访期间用餐`})
    await button.focus(); await page.keyboard.press('Enter')
    await expect.poll(() => state.commands.length).toBe(1)
    expect(state.commands[0]).toMatchObject({command_type: 'DINING_INSERT', meal_role: 'LUNCH', meal_slot: {day_index: 1, slot_index: 0}})
    await expect(page.getByTestId('activity-card')).toHaveCount(5)
    await page.reload()
    const card = page.getByTestId('activity-card').filter({has: page.getByRole('heading', {name: selectedName, exact: true})})
    await expect(card).toContainText('用餐条件')
    await card.getByRole('button').filter({has: page.getByRole('heading', {name: selectedName, exact: true})}).click()
    const inline = page.getByTestId('pending-place-dropdown')
    await expect(inline.getByTestId('dining-access-note')).toContainText(`场所归属：${parent}`)
    await inline.getByRole('button', {name: '收起地点确认', exact: true}).click()
    await exported(page, info, `adopted-${family}-${width}`, [selectedName, `场所归属：${parent}`, '离开前用餐', '营业情况尚未核实'])
    const handle = page.getByTestId('drag-handle-1-1')
    await handle.press('Enter'); await page.keyboard.press('ArrowRight'); await page.keyboard.press('Enter')
    await expect.poll(() => state.commands.length).toBe(2)
    expect(state.commands[1].command_type).toBe('ACTIVITY_MOVE')
    expect(state.result.days[0].activities.map(item => item.name)).toEqual([parent, '景山公园', selectedName])
    await expect(card).toContainText('用餐位置需核对')
    expect(state.result.days[0].activities[2].dining_access.status).toBe('NEEDS_REVIEW')
    await page.reload()
    await expect(card).toContainText('用餐位置需核对')
    await exported(page, info, `review-needed-${family}-${width}`, [selectedName, '本次场所到访的关联需要核对', '进入条件'])
    await page.getByTestId('undo-trip-command').click()
    await expect(card).toContainText('用餐条件')
    await page.getByTestId('redo-trip-command').click()
    await expect(card).toContainText('用餐位置需核对')
    expect(state.result.days.flatMap(day => day.activities).filter(item => item.name !== selectedName)).toHaveLength(4)
    if (family === 'saved' && width === 1440) {
      // Both actual entry points must show the same conditions. This also
      // exercises real PLACE_CONFIRM validation; no client-supplied details.
      await page.getByTestId('undo-trip-command').click()
      await expect(card).toContainText('用餐条件')
      await card.getByRole('button').filter({has: page.getByRole('heading', {name: selectedName, exact: true})}).click()
      await inline.getByLabel('搜索地点名称').fill('餐厅')
      await inline.getByRole('button', {name: '搜索', exact: true}).click()
      const light = inline.locator('.pending-place-row').filter({hasText: '坤宁宫东院餐厅'})
      await expect(light).toContainText('正餐供应尚未核实')
      const count = state.commands.length
      await light.getByRole('button').first().click()
      await expect(light.getByRole('button', {name: '正餐供应待核实'})).toBeDisabled()
      // The row itself supports a second-click shortcut. It must be guarded too.
      await light.getByRole('button').first().press('Enter')
      expect(state.commands).toHaveLength(count)
      const replacement = inline.locator('.pending-place-row').filter({hasText: '景运门故宫餐厅'})
      await replacement.getByRole('button').first().click()
      await replacement.getByRole('button', {name: '在「故宫博物院」到访期间用餐', exact: true}).click()
      await expect.poll(() => state.commands.length).toBe(count + 1)
      expect(state.commands.at(-1).command_type).toBe('PLACE_CONFIRM')
      expect(state.result.days[0].activities[1]).toMatchObject({name: '景运门故宫餐厅', meal_role: 'LUNCH',
        dining_access: {status: 'DURING_VISIT', parent_name: '故宫博物院'}})
      await page.reload()
      await page.getByRole('button', {name: '景运门故宫餐厅更多操作', exact: true}).click()
      await page.getByRole('button', {name: '查看详情', exact: true}).click()
      // The current desktop "more > details" entry also opens the anchored
      // editor, rather than the legacy modal PlaceEditor.
      const editor = page.getByTestId('pending-place-dropdown')
      await expect(editor.getByTestId('dining-access-note')).toContainText('离开前用餐')
      await editor.getByLabel('搜索地点名称').fill('餐厅')
      await editor.getByRole('button', {name: '搜索', exact: true}).click()
      const editLight = editor.locator('.pending-place-row').filter({hasText: '坤宁宫东院餐厅'})
      await expect(editLight).toContainText('正餐供应尚未核实')
      await editLight.getByRole('button').first().click()
      await expect(editor.getByRole('button', {name: '正餐供应待核实'})).toBeDisabled()
      await editor.getByRole('button', {name: '收起地点确认', exact: true}).click()
      await exported(page, info, 'replaced-saved-1440', ['景运门故宫餐厅', '场所归属：故宫博物院', '离开前用餐'])
      expect(state.result.days.flatMap(day => day.activities)).toHaveLength(5)
    }
    expect(state.unexpected).toEqual([]); expect(state.dailyWrites).toBe(0)
    await expect(page.locator('body')).not.toContainText(/B000A8UIN8|BTESTQINGHE01|dining_parent_activity_token|provider_parent_place_id/)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  })
}

test('existing snack slot can adopt light food without occupying lunch at 1280', {tag: '@desktop'}, async ({page}) => {
  const state = await show(page, 1280, 'independent')
  expect(state.result.days[1].meal_slots[0].meal_role).toBe('SNACK')
  const meal = page.getByTestId('source-meal-2-0')
  await meal.getByRole('button', {name: '为加餐选餐厅'}).click()
  await meal.getByLabel('手动搜索词').fill('茶点')
  await meal.getByRole('button', {name: '查找本餐门店'}).click()
  await expect(meal).toContainText('正餐供应尚未核实')
  await meal.getByRole('button', {name: '在「天坛公园」到访期间用餐'}).click()
  await expect.poll(() => state.commands.length).toBe(1)
  expect(state.commands[0].meal_role).toBe('SNACK')
  expect(state.result.days[0].meal_slots[0].selection_status).toBe('UNSELECTED')
  expect(state.result.days[1].activities.find(card => card.name === '槐香茶点铺').meal_role).toBe('SNACK')
})

test('nearby light-food selection remains unspecified with visible conditions at 1440', {tag: '@desktop'}, async ({page}) => {
  const state = await show(page, 1440, 'saved')
  await page.getByTestId('desktop-nav-map_stay').click()
  await page.getByRole('button', {name: '用餐', exact: true}).click()
  await page.getByRole('button', {name: '找附近餐饮'}).click()
  const candidate = page.locator('#journey-suggestions article').filter({has: page.getByRole('heading', {name: '坤宁宫东院餐厅', exact: true})})
  await expect(candidate).toContainText('不会替代正餐安排')
  await candidate.getByRole('button', {name: '在「故宫博物院」到访期间用餐'}).click()
  await expect.poll(() => state.commands.length).toBe(1)
  expect(state.commands[0].meal_role).toBeUndefined()
  expect(state.result.days[0].meal_slots[0].selection_status).toBe('UNSELECTED')
  expect(state.result.days[0].activities.find(card => card.name === '坤宁宫东院餐厅').meal_role).toBeNull()
})
