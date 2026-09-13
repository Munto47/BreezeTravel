const {test, expect} = require('@playwright/test')
const {execFileSync} = require('node:child_process')
const path = require('node:path')
const {result: saved} = require('./fixtures/saved-shanghai-pending-public.json')
let states
let failedLive
test.beforeAll(() => {
  states = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-c',
    'import asyncio,json;from tests.test_alternative_insert import build_alternative_insert_states;print(json.dumps(asyncio.run(build_alternative_insert_states()),ensure_ascii=False))'], {
    cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8',
    env: {...process.env, RUNTIME_PROFILE: 'test', PYTHONPATH: '.', PYTHONIOENCODING: 'utf-8'},
  }))
  failedLive = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-c',
    'import asyncio;from tests.test_source_visit_optional_parents import build_shanghai_optional_parent_live_result;print(asyncio.run(build_shanghai_optional_parent_live_result()).public_result.model_dump_json())'], {
    cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8',
    env: {...process.env, RUNTIME_PROFILE: 'test', PYTHONPATH: '.', PYTHONIOENCODING: 'utf-8'},
  }))
})

// Saved failed real public result, with inert tokens. Layout/HTTP replay only:
// the anonymous pending meals remain deliberately unfixed in this regression.
async function show(page, result, width, state) {
  await page.setViewportSize({width, height: 1000})
  await page.route('**/*', route => {
    const url = new URL(route.request().url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.fallback()
    const reply = json => route.fulfill({json, headers: {ETag: '"fixed-alternative-export"'}})
    if (url.pathname.endsWith('/commands') && state) {
      const command = route.request().postDataJSON()
      const current = states[state.key]
      if (state.key === 'before') {
        expect(command).toEqual({command_type: 'ALTERNATIVE_INSERT', day_index: 1, position: 0,
          alternative_token: current.days[0].alternatives[0].activity_token})
        state.key = 'inserted'
      } else if (command.command_type === 'PLACE_CONFIRM' && state.key === 'inserted') {
        expect(command).toEqual({command_type: 'PLACE_CONFIRM', activity_token: current.days[0].activities[0].activity_token,
          candidate_token: 'fixed-parent-confirmation-candidate-00001'})
        state.key = 'confirmed'
      } else {
        expect(command).toEqual({command_type: 'UNDO'})
        expect(['inserted', 'confirmed']).toContain(state.key)
        state.key = state.key === 'inserted' ? 'undo_insert' : 'undo'
      }
      state.commands.push(command)
      return route.fulfill({json: {status: 'APPLIED', changed_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE'}, headers: {ETag: `"fixed-alternative-${state.key}"`}})
    }
    if (url.pathname === '/api/user/me') return route.fulfill({status: 401, json: {}})
    if (url.pathname.endsWith('/result')) return state
      ? route.fulfill({json: states[state.key], headers: {ETag: `"fixed-alternative-${state.key}"`}}) : reply(result)
    if (url.pathname.endsWith('/place-candidates') && state) return reply({status: 'AVAILABLE', candidates: [{
      candidate_token: 'fixed-parent-confirmation-candidate-00001', name: '青岚乐园', category: '景点', city: '上海', area_or_address: '固定验证地址',
    }]})
    if (url.pathname.endsWith('/map-renders/latest')) return reply({...result.map, points: [], days: []})
    if (url.pathname.endsWith('/stay-suggestions')) return reply(result.stay)
    if (url.pathname.endsWith('/daily-dining')) return reply({status: 'AVAILABLE', days: []})
    if (url.pathname.endsWith('/supplementary')) return reply({status: 'AVAILABLE', days: []})
    if (url.pathname.endsWith('/materialize')) return reply({status: 'READY', message: '已准备', calendar: '按日期', party_size: 2, checks_available: true})
    if (url.pathname.endsWith('/checks')) return reply({status: 'STILL_NEEDS_CONFIRMATION', message: '请核对安排', items: [], remaining_must_adjust: 0, available_actions: []})
    return route.fulfill({status: 404, json: {}})
  })
  await page.addInitScript(() => {
    window.exportText = []
    const original = CanvasRenderingContext2D.prototype.fillText
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y, ...rest) {
      const point = this.getTransform().transformPoint({x, y})
      window.exportText.push({text: String(text), x: point.x, y: point.y, font: this.font,
        width: this.measureText(String(text)).width, canvasWidth: this.canvas.width, canvasHeight: this.canvas.height})
      return original.call(this, text, x, y, ...rest)
    }
  })
  await page.goto('/trip/result#trip=fixed-alternative-export-result-0001')
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
}

async function exportPng(page, info, result) {
  await page.getByTestId('export-itinerary-png').click()
  const image = page.getByAltText('行程横链导出预览', {exact: true})
  await expect(image).toBeVisible()
  await expect.poll(() => image.evaluate(img => img.naturalWidth)).toBe(1440)
  const drawn = await page.evaluate(() => window.exportText)
  for (const row of drawn) {
    expect(row.x, row.text).toBeGreaterThanOrEqual(0)
    expect(row.x + row.width, row.text).toBeLessThanOrEqual(row.canvasWidth)
    expect(row.y, row.text).toBeGreaterThan(0)
    expect(row.y, row.text).toBeLessThan(row.canvasHeight)
  }
  const labels = result.days.map(day => drawn.find(row => row.text === day.label))
  expect(labels.every(Boolean)).toBe(true)
  const days = labels.map((label, i) => drawn.filter(row => row.y >= label.y - 43 &&
    row.y < (labels[i + 1] ? labels[i + 1].y - 43 : row.canvasHeight - 64)))
  for (const [i, day] of result.days.entries()) {
    for (const option of day.alternatives || []) expect(days[i].map(row => row.text).join('')).toContain(option.name)
  }
  const downloading = page.waitForEvent('download')
  await page.getByTestId('download-itinerary-png').click()
  const download = await downloading
  expect(await download.failure()).toBeNull()
  await download.saveAs(info.outputPath('alternatives.png'))
  await page.screenshot({path: info.outputPath('alternatives-preview.png')})
  await info.attach('fixed-public-and-canvas', {body: JSON.stringify({scope: 'FIXED_PUBLIC_LAYOUT_NO_NEW_REAL_CALLS', result, drawn}), contentType: 'application/json'})
  return {drawn, days: days.map(rows => rows.map(row => row.text))}
}

for (const width of [1440, 390]) test(`saved Shanghai export keeps seven pending cards and every alternative at ${width}px`, async ({page}, info) => {
  await show(page, saved, width)
  await expect(page.getByTestId('activity-card')).toHaveCount(13)
  await expect(page.getByTestId('day-lane-3').getByTestId('activity-card')).toHaveCount(0)
  await page.reload()
  await expect(page.getByTestId('activity-card')).toHaveCount(13)
  const {drawn, days} = await exportPng(page, info, saved)
  await expect(page.getByRole('dialog')).toContainText('待确认地点：7 处')
  expect(drawn.map(row => row.text)).toContain('待确认地点：7 处，请返回行程确认')
  expect(days[0].join('')).toContain('待确认地点：3 处')
  expect(days[1].join('')).toContain('待确认地点：4 处')
  expect(days[2].join('')).not.toContain('待确认地点')
  expect(days[0].join('')).toContain('上海中心118层')
  expect(days[2].join('')).toContain('朵云书院（上海中心52楼）')
  expect(days[2].join('')).toContain('尚无主线地点')
  expect(drawn.filter(row => /^(?:bold|700) 15px/.test(row.font) && row.x > 176).map(row => row.text).join('')).not.toContain('上海迪士尼')
})

for (const width of [1440, 390]) test(`authoritative optional details survive adding confirmation reload and undo at ${width}px`, async ({page}, info) => {
  const state = {key: 'before', commands: []}
  await show(page, states.before, width, state)
  await page.getByTestId('day-alternatives-1').click()
  await expect(page.getByTestId('alternative-source-details')).toContainText('云海航船')
  await page.getByRole('button', {name: '加入待确认', exact: true}).click()
  await expect.poll(() => state.key).toBe('inserted')
  await expect(page.getByTestId('journey-suggestions-toggle')).toBeEnabled()
  await page.getByLabel('关闭建议').click()
  await expect(page.getByTestId('activity-card')).toHaveCount(0)
  await page.reload()
  await page.getByTestId('unmatched-places-note').click()
  await page.getByRole('button', {name: '青岚乐园 · 上海 · 确认地点', exact: true}).click()
  const dropdown = page.getByTestId('pending-place-dropdown')
  await dropdown.getByRole('button', {name: '搜索', exact: true}).click()
  await dropdown.getByRole('button', {name: /青岚乐园/}).click()
  await page.getByRole('button', {name: '使用这个地点', exact: true}).click()
  await expect.poll(() => state.key).toBe('confirmed')
  await expect(page.getByTestId('activity-card')).toHaveCount(1)
  await expect(page.getByTestId('activity-card')).toContainText('原文安排 · 2 项')
  await page.reload()
  await expect(page.getByTestId('activity-card')).toContainText('青岚乐园')
  const {days} = await exportPng(page, info, states.confirmed)
  const source = days[0].join('')
  // In the main parent's source section, before the original alternatives.
  expect(source.split('原文备选与方案')[0]).toContain('1. 云海航船2. 星光环线')
  expect(days[1].join('')).not.toContain('云海航船')
  await page.getByLabel('关闭图片预览').click()
  await page.getByTestId('undo-trip-command').click()
  await expect.poll(() => state.key).toBe('undo')
  await expect(page.getByTestId('activity-card')).toHaveCount(0)
  await page.getByTestId('day-alternatives-1').click()
  await expect(page.getByTestId('alternative-source-details')).toContainText('星光环线')
  expect(state.commands.map(command => command.command_type)).toEqual(['ALTERNATIVE_INSERT', 'PLACE_CONFIRM', 'UNDO'])
  await info.attach('fixed-provider-real-command-path', {body: JSON.stringify({scope: 'FIXED_TWO_ANSWERS_AND_IDENTITY_REAL_COMMAND_NO_ACCOUNT_OR_NETWORK', states, commands: state.commands}), contentType: 'application/json'})
})

for (const width of [1440, 390]) test(`failed real second answer keeps original visits and truthful missing detail status at ${width}px`, async ({page}, info) => {
  expect(failedLive.coverage.complete).toBe(false)
  expect(failedLive.days.reduce((n, day) => n + day.activities.length, 0)).toBe(17)
  expect(failedLive.days.flatMap(day => [...day.activities, ...day.alternatives]).flatMap(card => card.source_details)).toEqual([])
  await show(page, failedLive, width)
  await expect(page.getByTestId('activity-card')).toHaveCount(13)
  await page.getByTestId('day-alternatives-3').click()
  await expect(page.getByTestId('itinerary-choice-group')).toContainText('上海迪士尼')
  await expect(page.getByTestId('alternative-source-details')).toHaveCount(0)
  await expect(page.getByTestId('itinerary-choice-group')).toContainText('暂时不能整组加入')
  await page.getByLabel('关闭建议').click()
  const {drawn, days} = await exportPng(page, info, failedLive)
  const text = drawn.map(row => row.text).join('')
  expect(text).toContain('原文尚未完整整理')
  expect(text).toContain('待确认地点：4 处')
  expect(text).not.toContain('飞跃地平线')
  expect(days[2].join('')).toContain('尚无主线地点')
})

// A public shape boundary, explicitly constructed rather than attributed to
// the saved real model. The provider/pipeline tests separately supply details.
for (const width of [1440, 390]) test(`optional parent details stay outside mainline in page and PNG at ${width}px`, async ({page}, info) => {
  const result = structuredClone(saved)
  const parent = result.days[2].alternatives.find(item => item.name === '上海迪士尼')
  parent.source_details = ['飞跃地平线', '加勒比海盗', '创极速光轮', '疯狂动物城园区'].map(name => ({name, optional: false}))
  await show(page, result, width)
  await page.getByTestId('day-alternatives-3').click()
  const details = page.getByTestId('alternative-source-details')
  await expect(details).toContainText('随「上海迪士尼」保留的原文安排')
  await expect(details).toContainText('尚未单独核验')
  for (const item of parent.source_details) await expect(details).toContainText(item.name)
  await expect(page.getByTestId('activity-card')).toHaveCount(13)
  await expect(page.getByTestId('day-lane-3').getByTestId('activity-card')).toHaveCount(0)
  await details.scrollIntoViewIfNeeded()
  await page.screenshot({path: info.outputPath('alternative-details-page.png')})
  await page.getByRole('button', {name: '关闭建议', exact: true}).click()
  const {days} = await exportPng(page, info, result)
  expect(days[2].join('')).toContain('原文备选与方案 · 未选择的内容不属于主线')
  expect(days[2].join('')).toContain('上海迪士尼（备选）')
  for (const [index, item] of parent.source_details.entries()) expect(days[2]).toContain(`${index + 1}. ${item.name}`)
  expect(days.slice(0, 2).flat().join('')).not.toContain('飞跃地平线')
})
