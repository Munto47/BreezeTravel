const {test, expect} = require('@playwright/test')
const {execFileSync} = require('node:child_process')
const path = require('node:path')

// Saved candidate replies -> current provider/pipeline with fixed identities.
// Adoption uses the actual public command mutation, without an account or DB.
// The remaining cases are explicit public-view layout/anchor boundary fixtures.
let original, adopted, capacity
test.beforeAll(() => {
  const data = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-c', `
import asyncio,json
from tests.test_meal_slot_selection import build_meal_result,adopt
from tests.test_trip_capacity_readback import build_capacity_result
async def main():
    output=await build_meal_result()
    current=output.public_result
    assert [[c.name for c in d.activities] for d in current.days]==[
        ['莲花山公园','深圳市当代艺术与城市规划馆'],['深圳博物馆历史民俗馆','莲花山公园']]
    assert [len(d.meal_slots) for d in current.days]==[1,1]
    selected=adopt(current)
    assert selected.days[0].meal_slots[0].selection_status=='SELECTED'
    assert selected.days[0].activities[1].meal_role=='LUNCH'
    large=await build_capacity_result(160,14)
    print(json.dumps(dict(original=current.model_dump(mode='json'),
        adopted=selected.model_dump(mode='json'),capacity=large.public_result.model_dump(mode='json')),ensure_ascii=False))
asyncio.run(main())
`], {cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8',
    env: {...process.env, RUNTIME_PROFILE: 'test', PYTHONPATH: '.', PYTHONIOENCODING: 'utf-8'},
  }))
  ;({original, adopted, capacity} = data)
})

async function show(page, result, width) {
  await page.setViewportSize({width, height: 900})
  await page.route('**/*', route => {
    const url = new URL(route.request().url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.fallback()
    const reply = json => route.fulfill({json, headers: {ETag: '"meal-export-fixed-version"'}})
    if (url.pathname === '/api/user/me') return route.fulfill({status: 401, json: {}})
    if (url.pathname.endsWith('/result')) return reply(result)
    if (url.pathname.endsWith('/map-renders/latest')) return reply({...result.map, points: [], days: []})
    if (url.pathname.endsWith('/stay-suggestions')) return reply(result.stay)
    if (url.pathname.endsWith('/daily-dining')) return reply({status: 'AVAILABLE', days: [{
      day_index: 1, label: result.days[0].label, status: 'AVAILABLE', message: '固定推荐，不是原文安排',
      candidates: [{candidate_token: 'fixed-menu-candidate', name: '候选餐厅不应导出', category: '餐饮',
        area_or_address: '固定候选', reason: '模拟推荐'}],
    }]})
    if (url.pathname.endsWith('/supplementary')) return reply({status: 'AVAILABLE', days: []})
    if (url.pathname.endsWith('/materialize')) return reply({status: 'READY', message: '已准备',
      calendar: '按日期', party_size: 2, checks_available: true})
    if (url.pathname.endsWith('/checks')) return reply({status: 'STILL_NEEDS_CONFIRMATION',
      message: '请核对安排', items: [], remaining_must_adjust: 0, available_actions: []})
    return route.fulfill({status: 404, json: {}})
  })
  await page.addInitScript(() => {
    window.exportText = []
    const originalFill = CanvasRenderingContext2D.prototype.fillText
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y, ...rest) {
      const point = this.getTransform().transformPoint({x, y})
      window.exportText.push({text: String(text), x: point.x, y: point.y, font: this.font,
        width: this.measureText(String(text)).width, canvasWidth: this.canvas.width, canvasHeight: this.canvas.height})
      return originalFill.call(this, text, x, y, ...rest)
    }
  })
  await page.goto('/trip/result#trip=synthetic-meal-export-result-0001')
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  await expect(page.getByTestId('activity-card')).toHaveCount(result.days.reduce((n, day) => n + day.activities.filter(card => card.status === 'READY').length, 0))
}

async function exportPng(page, info, result) {
  await page.getByTestId('export-itinerary-png').click()
  const image = page.getByAltText('行程横链导出预览', {exact: true})
  await expect(image).toBeVisible()
  await expect.poll(() => image.evaluate(img => img.naturalWidth)).toBe(1440)
  const downloading = page.waitForEvent('download')
  await page.getByTestId('download-itinerary-png').click()
  const download = await downloading
  expect(await download.failure()).toBeNull()
  await download.saveAs(info.outputPath('meal-export.png'))
  await page.screenshot({path: info.outputPath('meal-export-preview.png')})
  const drawn = await page.evaluate(() => window.exportText)
  for (const line of drawn) {
    expect(line.x, line.text).toBeGreaterThanOrEqual(0)
    expect(line.x + line.width, line.text).toBeLessThan(line.canvasWidth)
    expect(line.y, line.text).toBeGreaterThan(0)
    expect(line.y, line.text).toBeLessThan(line.canvasHeight)
  }
  expect(drawn.some(line => line.text.includes('候选餐厅不应导出'))).toBe(false)
  // Reconstruct wrapped names, keeping each repeated visit and full suffix.
  const names = []
  let name = ''
  for (const line of [...drawn, {}]) {
    if (/^(?:bold|700) 15px/.test(line.font || '') && line.x > 176) name += line.text
    else if (name) {names.push(name); name = ''}
  }
  expect(names).toEqual(result.days.flatMap(day => day.activities.filter(card => card.status === 'READY').map(card => card.name)))
  const labels = result.days.map(day => drawn.find(line => line.text === day.label))
  expect(labels.every(Boolean)).toBe(true)
  const days = labels.map((label, i) => drawn.filter(line => line.y >= label.y - 43 &&
    line.y < (labels[i + 1] ? labels[i + 1].y - 43 : drawn[0].canvasHeight)))
  for (const [i, day] of result.days.entries()) for (const slot of day.meal_slots || []) {
    if (slot.preference_text) expect(days[i].map(line => line.text).join('')).toContain(slot.preference_text)
  }
  // Content of a day must finish above the next day panel, not just inside PNG.
  for (let i = 0; i < days.length - 1; i++) {
    for (const line of days[i].filter(line => /餐厅待选择|之后|之前|原文用餐/.test(line.text))) {
      expect(line.y, line.text).toBeLessThan(labels[i + 1].y - 43)
    }
  }
  await info.attach('fixed-public-and-canvas', {body: JSON.stringify({scope: 'FIXED_HTTP_PUBLIC_VIEW_NO_ACCOUNT_NO_LIVE_CALLS',
    result, drawn}), contentType: 'application/json'})
  return days.map(lines => lines.map(line => line.text))
}

for (const width of [1440, 390]) test(`saved Shenzhen two source lunches stay in each PNG day at ${width}px`, async ({page}, info) => {
  await show(page, original, width)
  const days = await exportPng(page, info, original)
  expect(days.map(lines => lines.filter(text => text.startsWith('午餐 · 餐厅待选择')).length)).toEqual([1, 1])
  expect(days[0].join('')).toContain('在「莲花山公园」之后，在「深圳市当代艺术与城市规划馆」之前')
  expect(days[1].join('')).toContain('在「深圳博物馆历史民俗馆」之后，在「莲花山公园」之前')
  expect(days.flat().filter(text => /^\d+\. /.test(text))).toEqual([
    '1. 入口：从南门进', '2. 风筝广场', '3. 邓小平雕像', '4. 桃花林（备选）', '5. 出口：从南门出', '1. 仅看外观，不入内部',
  ])
  expect(days[1]).toContain('原文未整理：2 处')
})

test('actual fixed adoption consumes only its lunch, leaving other meals and days', async ({page}, info) => {
  const result = structuredClone(adopted)
  result.days[0].meal_slots.unshift({meal_role: 'BREAKFAST', selection_status: 'UNSELECTED', before_activity_token: result.days[0].activities[0].activity_token})
  result.days[0].meal_slots.push({meal_role: 'DINNER', selection_status: 'UNSELECTED', after_activity_token: result.days[0].activities.at(-1).activity_token})
  await show(page, result, 1440)
  const days = await exportPng(page, info, result)
  expect(days[0].filter(text => text.includes('餐厅待选择')).map(text => text.split(' · ')[0])).toEqual(['早餐', '晚餐'])
  expect(days[1].filter(text => text.startsWith('午餐 · 餐厅待选择'))).toHaveLength(1)
  expect(days.flat().filter(text => text === '合成采纳餐厅')).toHaveLength(1)
  expect(days[0].join('')).toContain('午餐 · 已安排：「合成采纳餐厅」')
})

test('one restaurant cannot consume two source lunches in the same interval', async ({page}, info) => {
  const result = structuredClone(adopted)
  result.days[0].meal_slots.push({...result.days[0].meal_slots[0], selection_status: 'UNSELECTED', selected_activity_token: null})
  await show(page, result, 390)
  const days = await exportPng(page, info, result)
  expect(days.map(lines => lines.filter(text => text.startsWith('午餐 · 餐厅待选择')).length)).toEqual([1, 1])
})

for (const state of ['moved', 'pending', 'legacy']) test(`export reads ${state} meal selection without re-matching by position`, async ({page}, info) => {
  const result = structuredClone(adopted)
  const day = result.days[0]
  if (state === 'moved') {
    const [restaurant] = day.activities.splice(1, 1)
    day.activities.unshift(restaurant)
  } else if (state === 'pending') {
    day.activities[1].status = 'NEEDS_CONFIRMATION'
    result.coverage.unresolved_place_count = 1
  } else {
    for (const day of result.days) for (const slot of day.meal_slots) {
      delete slot.selection_status
      delete slot.selected_activity_token
    }
  }
  await show(page, result, 390)
  const days = await exportPng(page, info, result)
  expect(days[0].some(text => text.startsWith('午餐 · 餐厅待选择'))).toBe(false)
  if (state === 'moved') expect(days[0].join('')).toContain('午餐 · 已安排：「合成采纳餐厅」')
  if (state === 'pending') {
    expect(days[0].join('')).toContain('午餐 · 餐厅需确认：「合成采纳餐厅」')
    expect(days[0].some(text => text.includes('午餐 · 已安排'))).toBe(false)
  }
  if (state === 'legacy') {
    expect(days.map(lines => lines.filter(text => text.startsWith('原文有午餐安排')).length)).toEqual([1, 1])
    expect(days.flat().some(text => /餐厅待选择|午餐 · 已安排/.test(text))).toBe(false)
  }
})

test('empty activity days retain every meal without inventing anchors or times', async ({page}, info) => {
  const result = structuredClone(original)
  result.days[0].activities = []
  result.days[0].meal_slots = ['BREAKFAST', 'LUNCH', 'DINNER', 'SNACK'].map(meal_role => ({meal_role, selection_status: 'UNSELECTED'}))
  await show(page, result, 390)
  const days = await exportPng(page, info, result)
  expect(days[0].filter(text => text.includes('餐厅待选择'))).toEqual([
    '早餐 · 餐厅待选择', '午餐 · 餐厅待选择', '晚餐 · 餐厅待选择', '加餐 · 餐厅待选择',
  ])
  expect(days[0]).toContain('尚无主线地点')
  expect(days[0].some(text => /之后|之前|\d\d:\d\d/.test(text))).toBe(false)
})

test('stale, cross-day and reversed anchors do not invent positions or suppress meals', async ({page}, info) => {
  const result = structuredClone(adopted)
  const [first, , last] = result.days[0].activities
  result.days[0].meal_slots = [
    {meal_role: 'LUNCH', selection_status: 'UNSELECTED', after_activity_token: 'removed-token', before_activity_token: result.days[1].activities[0].activity_token},
    {meal_role: 'DINNER', selection_status: 'UNSELECTED', after_activity_token: last.activity_token, before_activity_token: first.activity_token},
    {meal_role: 'BREAKFAST', selection_status: 'UNSELECTED', before_activity_token: first.activity_token},
  ]
  await show(page, result, 1440)
  const days = await exportPng(page, info, result)
  expect(days[0].filter(text => text.includes('餐厅待选择'))).toEqual([
    '午餐 · 餐厅待选择', '晚餐 · 餐厅待选择', '早餐 · 餐厅待选择（在「莲花山公园」之前）',
  ])
  expect(days.flat().join('')).not.toContain('removed-token')
})

test('14 days and 160 cards keep meals, long names and final-day content inside PNG', async ({page}, info) => {
  const result = structuredClone(capacity)
  for (const [index, day] of result.days.entries()) {
    // 40-character synthetic names force five lines without smaller fonts.
    day.activities[0].name = `第${String(index + 1).padStart(2, '0')}日${'长名称导出布局验证'.repeat(4)}`.slice(0, 40)
    day.meal_slots = [
      {meal_role: 'BREAKFAST', selection_status: 'UNSELECTED', before_activity_token: day.activities[0].activity_token},
      {meal_role: 'LUNCH', selection_status: 'UNSELECTED', after_activity_token: day.activities[0].activity_token, before_activity_token: day.activities[1].activity_token},
      {meal_role: 'DINNER', selection_status: 'UNSELECTED', after_activity_token: day.activities.at(-1).activity_token},
    ]
  }
  await show(page, result, 1440)
  const days = await exportPng(page, info, result)
  expect(days.map(lines => lines.filter(text => /^(早餐|午餐|晚餐) · 餐厅待选择/.test(text)).length)).toEqual(Array(14).fill(3))
  expect(days.at(-1).join('')).toContain(`在「${result.days.at(-1).activities.at(-1).name}」之后`)
})
