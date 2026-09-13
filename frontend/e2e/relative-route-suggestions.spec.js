const {test, expect} = require('@playwright/test')
const {execFileSync} = require('node:child_process')
const path = require('node:path')

test.use({actionTimeout: 15000})
const RESOURCE = 'fixed-relative-route-browser'
function replay(data) {
  return JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-X', 'utf8', path.resolve(__dirname, 'fixtures/relative-route-replay.py')], {
    cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8', input: JSON.stringify({...data, resource: RESOURCE}),
    env: {...process.env, PYTHONPATH: '.', RUNTIME_PROFILE: 'test', PYTHONIOENCODING: 'utf-8'},
  }))
}
async function show(page, width, options = {}) {
  const state = {result: replay({operation: 'initial'}), comparisons: [], adoptions: [], commands: [], mapWrites: 0, external: [], ...options}
  const etag = () => `"fixed-relative-route-version-${state.commands.length+state.adoptions.length}"`
  await page.setViewportSize({width, height: width === 390 ? 844 : 1000})
  await page.emulateMedia({reducedMotion: 'reduce'})
  await page.route('**/*', route => {
    if (!['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname)) {state.external.push(route.request().url()); return route.abort()}
    return route.continue()
  })
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url())
    const reply = (json, tag = etag()) => route.fulfill({json, headers: {ETag: tag}})
    if (url.pathname.endsWith('/changes/preview')) {
      expect(request.headers()['if-match']).toBe(etag())
      expect(request.headers()['idempotency-key']).toBeTruthy()
      const body = request.postDataJSON()
      state.comparisons.push({body, headers: request.headers()})
      if (state.failCompare) {state.failCompare = false; return route.fulfill({status: 503, json: {}})}
      if (state.comparePending) return route.fulfill({status: 409, json: {detail: {code: 'REQUEST_IN_PROGRESS'}}})
      if (state.compareConflict) return route.fulfill({status: 409, json: {}})
      const result = replay({operation: 'compare', result: state.result, etag: etag()})
      if (state.noImprovement) Object.assign(result, {status: 'NO_IMPROVEMENT', message: '没有已核验的更省路方案，原行程保留。', options: []})
      if (state.deferCompare) await state.deferCompare
      return reply(result, state.wrongEtag ? '"another-version"' : etag())
    }
    if (url.pathname.endsWith('/changes/adopt')) {
      const token = request.postDataJSON().change_token
      const key = request.headers()['idempotency-key']
      if (state.adoptions.some(item => item.key === key)) return reply({status: 'APPLIED', message: '同一选择已保存', changed_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE'})
      expect(request.headers()['if-match']).toBe(etag())
      if (state.expired) return route.fulfill({status: 409, json: {detail: {code: 'PREVIEW_STALE'}}})
      state.undo = structuredClone(state.result)
      state.result = replay({operation: 'adopt', result: state.result, token, etag: etag()})
      state.adoptions.push({key, token})
      if (state.loseResponse) {state.loseResponse = false; return route.abort('failed')}
      await new Promise(resolve => setTimeout(resolve, 120))
      return reply({status: 'APPLIED', message: '固定比较已采纳', changed_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE'})
    }
    if (url.pathname.endsWith('/commands')) {
      const command = request.postDataJSON(), previous = structuredClone(state.result)
      state.result = replay({operation: 'command', result: state.result, command, undo: state.undo, redo: state.redo})
      if (command.command_type === 'UNDO') state.redo = previous
      else {state.undo = previous; state.redo = null}
      state.commands.push(command)
      return reply({status: 'APPLIED', changed_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE'})
    }
    if (url.pathname.endsWith('/result')) return reply(state.result)
    if (url.pathname.endsWith('/daily-dining')) return reply({status: 'UNAVAILABLE', message: '固定未准备餐饮', days: []})
    if (url.pathname.endsWith('/map-renders/latest')) return reply({...state.result.map, points: [], days: []})
    if (url.pathname.endsWith('/map-renders')) {state.mapWrites++; return route.fulfill({status: 409, json: {}})}
    if (url.pathname.endsWith('/stay-suggestions')) return reply(state.result.stay)
    if (url.pathname.endsWith('/supplementary')) return reply({status: 'AVAILABLE', days: []})
    if (url.pathname.endsWith('/materialize')) return reply({status: 'READY', message: '固定检查', calendar: '相对日序', party_size: 2, checks_available: true})
    if (url.pathname.endsWith('/checks')) return reply({status: 'STILL_NEEDS_CONFIRMATION', message: '固定检查', items: [], remaining_must_adjust: 0, available_actions: []})
    return route.fulfill({status: 404, json: {}})
  })
  await page.goto(`/trip/result#trip=${RESOURCE}`)
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  await page.getByTestId('journey-suggestions-toggle').click()
  await page.getByRole('button', {name: '顺路优化', exact: true}).click()
  await expect(page.getByTestId('relative-route-suggestions')).toBeVisible()
  return state
}

for (const width of [1440, 1280, 390]) test(`route comparison is explicit and adopts only after confirmation at ${width}`, async ({page}, info) => {
  test.skip(width < 1024, 'Owner scope: small-screen verification is paused; keep its prior implementation and evidence.')
  const state = await show(page, width)
  const otherDays = structuredClone(state.result.days.slice(1))
  const section = page.getByTestId('relative-route-suggestions')
  expect(state.comparisons).toHaveLength(0)
  expect(state.adoptions).toHaveLength(0)
  await section.getByRole('button', {name: '比较当天顺路方案'}).click()
  const option = section.getByTestId('relative-route-option')
  await expect(option).toHaveCount(1)
  await expect(option).toContainText('变化路段可节省 41 分钟')
  await expect(option).toContainText('75 → 34 分钟')
  await expect(option).toContainText('不是全天交通总时长')
  await expect(option.getByLabel('完整日序比较').locator('ol').first().locator('li')).toHaveText(['固定景点0','固定景点1','固定景点2','固定景点3'])
  await expect(option.getByLabel('完整日序比较').locator('ol').last().locator('li')).toHaveText(['固定景点0','固定景点2','固定景点1','固定景点3'])
  expect(await option.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true)
  await option.getByText('查看变化路段', {exact: true}).click()
  await expect(option).toContainText('公共交通 · 14 分钟 · 1.4 公里')
  await expect(option).not.toContainText(/\b\d\d:\d\d\b|停留|全天交通可节省/)
  await option.getByRole('button', {name: '选择这个方案'}).click()
  expect(state.adoptions).toHaveLength(0)
  await option.getByRole('button', {name: '取消', exact: true}).click()
  expect(state.adoptions).toHaveLength(0)
  await option.getByRole('button', {name: '选择这个方案'}).click()
  await page.locator('#journey-suggestions').evaluate(node=>{node.scrollTop=0})
  await page.screenshot({path: info.outputPath(`relative-route-comparison-${width}.png`)})
  await option.getByRole('button', {name: '确认调整顺序'}).scrollIntoViewIfNeeded()
  const confirmBounds = await option.getByRole('button', {name: '确认调整顺序'}).boundingBox()
  expect(confirmBounds.x).toBeGreaterThanOrEqual(0)
  expect(confirmBounds.x + confirmBounds.width).toBeLessThanOrEqual(width)
  expect(confirmBounds.y).toBeGreaterThanOrEqual(0)
  expect(confirmBounds.y + confirmBounds.height).toBeLessThanOrEqual(page.viewportSize().height)
  await page.screenshot({path: info.outputPath(`relative-route-confirm-${width}.png`)})
  await option.getByRole('button', {name: '确认调整顺序'}).dblclick()
  await expect.poll(() => state.adoptions.length).toBe(1)
  await expect(option).toHaveCount(0)
  expect(state.result.days[0].activities.map(card => card.name)).toEqual(['固定景点0','固定景点2','固定景点1','固定景点3'])
  expect(state.result.days.slice(1).map(day => day.activities.map(card => card.name))).toEqual(otherDays.map(day => day.activities.map(card => card.name)))
  await page.getByRole('button', {name: '关闭建议'}).click()
  await page.reload()
  await expect(page.getByTestId('day-lane-1').getByTestId('activity-card').locator('h3')).toHaveText(['固定景点0','固定景点2','固定景点1','固定景点3'])
  await page.getByRole('button', {name: '撤销', exact: true}).click()
  await expect(page.getByTestId('day-lane-1').getByTestId('activity-card').locator('h3')).toHaveText(['固定景点0','固定景点1','固定景点2','固定景点3'])
  await page.getByRole('button', {name: '重做', exact: true}).click()
  await expect(page.getByTestId('day-lane-1').getByTestId('activity-card').locator('h3')).toHaveText(['固定景点0','固定景点2','固定景点1','固定景点3'])
  expect(state.mapWrites).toBe(0)
  expect(state.external).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('failed comparison retries with the same key; no-improvement never offers adoption', async ({page}) => {
  const state = await show(page, 1440, {failCompare: true})
  const section = page.getByTestId('relative-route-suggestions')
  await section.getByRole('button', {name: '比较当天顺路方案'}).click()
  await expect(section).toContainText('暂时无法完成比较，行程没有改动')
  state.noImprovement = true
  await section.getByRole('button', {name: '比较当天顺路方案'}).click()
  await expect(section).toContainText('没有已核验的更省路方案')
  expect(state.comparisons[0].headers['idempotency-key']).toBe(state.comparisons[1].headers['idempotency-key'])
  await expect(section.getByRole('button', {name: '选择这个方案'})).toHaveCount(0)
  expect(state.adoptions).toHaveLength(0)
})

test('changing days discards pending or displayed comparisons; expired adoption preserves order', async ({page}) => {
  let release
  const state = await show(page, 1440, {deferCompare: new Promise(resolve => {release = resolve})})
  const section = page.getByTestId('relative-route-suggestions')
  await section.getByRole('button', {name: '比较当天顺路方案'}).click()
  await expect.poll(() => state.comparisons.length).toBe(1)
  await page.getByLabel('建议所属日期').selectOption('1')
  release()
  await expect(section).toContainText('Day 2 · 比较顺路方案')
  await expect(section.getByTestId('relative-route-option')).toHaveCount(0)
  state.deferCompare = null
  await page.getByLabel('建议所属日期').selectOption('0')
  await section.getByRole('button', {name: '比较当天顺路方案'}).click()
  await section.getByRole('button', {name: '选择这个方案'}).click()
  state.expired = true
  await section.getByRole('button', {name: '确认调整顺序'}).click()
  await expect(section.getByTestId('relative-route-option')).toHaveCount(0)
  await expect(page.getByText('上次修改未保存', {exact: true})).toBeVisible()
  expect(state.adoptions).toHaveLength(0)
  expect(state.result.days[0].activities.map(card => card.name)).toEqual(['固定景点0','固定景点1','固定景点2','固定景点3'])
})

test('lost adoption response uses existing global save recovery and never applies twice', async ({page}) => {
  const state = await show(page, 1440, {loseResponse: true})
  const section = page.getByTestId('relative-route-suggestions')
  await section.getByRole('button', {name: '比较当天顺路方案'}).click()
  await section.getByRole('button', {name: '选择这个方案'}).click()
  await section.getByRole('button', {name: '确认调整顺序'}).click()
  await expect.poll(() => state.adoptions.length).toBe(1)
  await page.getByRole('button', {name: '关闭建议'}).click()
  const recover = page.getByRole('button', {name: /确认.*保存|确认保存/})
  await expect(recover).toBeVisible()
  await expect(page.getByRole('button', {name: '撤销', exact: true})).toBeDisabled()
  await recover.click()
  await expect(page.getByTestId('day-lane-1').getByTestId('activity-card').locator('h3')).toHaveText(['固定景点0','固定景点2','固定景点1','固定景点3'])
  expect(state.adoptions).toHaveLength(1)
  await expect(recover).toHaveCount(0)
  expect(state.mapWrites).toBe(0)
})

test('a still-running comparison differs from a stale response and both preserve the original order', async ({page}) => {
  const state = await show(page, 1440, {comparePending:true})
  const section=page.getByTestId('relative-route-suggestions')
  await section.getByRole('button', {name:'比较当天顺路方案'}).click()
  await expect(section).toContainText('同一次比较仍在处理中')
  state.comparePending=false
  state.wrongEtag=true
  await section.getByRole('button', {name:'比较当天顺路方案'}).click()
  await expect(section).toContainText('行程或路线已有变化')
  await expect(section.getByTestId('relative-route-option')).toHaveCount(0)
  expect(state.comparisons[0].headers['idempotency-key']).toBe(state.comparisons[1].headers['idempotency-key'])
  expect(state.adoptions).toHaveLength(0)
})
