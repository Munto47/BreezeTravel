const { test, expect } = require('@playwright/test')
const { execFileSync } = require('node:child_process')
const path = require('node:path')

test.use({ actionTimeout: 10000 })
const longName = '深圳市当代艺术与城市规划馆公共艺术与城市历史专题展览交流中心'
const names = [longName, '故宫博物院', '景山公园', '北海公园', '天坛公园', '颐和园', '圆明园遗址公园',
  '中国国家博物馆', '什刹海', '北京天文馆', '中国科学技术馆', '北京动物园', '香山公园']
const card = (name, index, status = 'READY') => ({ name, activity_token: `fixed-card-for-daily-${index}`, city: '北京',
  category: '景点', area_or_address: '固定视觉用例地点', time_hint: null, status,
  source_details: index === 0 ? [{ name: '原文内部安排', optional: false }] : [], available_actions: ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE'] })

function fixture() {
  const activities = names.map((name, index) => card(name, index))
  activities.splice(1, 0, card('故宫旁待确认入口', 'pending', 'NEEDS_CONFIRMATION'))
  return {
    status: 'PARTIAL_RESULT', can_undo: false,
    assumptions: [{ key: 'destination', label: '目的地', value: '北京', editable: true },
      { key: 'calendar', label: '日序', value: 'Day 1–3', editable: true }, { key: 'party_size', label: '人数', value: '2 人', editable: true }],
    coverage: { recognized_place_count: 16, confirmed_place_count: 15, unresolved_place_count: 1,
      unclassified_mention_count: 0, unprocessed_count: 3, complete: false },
    days: [
      { label: 'Day 1', activities, unprocessed_count: 1,
        meal_slots: [{ meal_role: 'LUNCH', selection_status: 'UNSELECTED', after_activity_token: 'fixed-card-for-daily-2' }],
        alternatives: [{ name: '首都博物馆', category: '景点', city: '北京' }] },
      { label: 'Day 2', activities: [card('奥林匹克森林公园', 20), card('中国美术馆', 21)], alternatives: [], unprocessed_count: 0 },
      { label: 'Day 3', activities: [], unprocessed_count: 2, alternatives: [{ name: '北京园博园', category: '景点', city: '北京' }] },
    ],
    map: { status: 'AVAILABLE', message: '固定已核验路线', available_actions: ['VIEW_MAP', 'RENDER_MAP'] },
    stay: { status: 'UNAVAILABLE', message: '固定未生成建议', area_summary: null, searched_scopes: [], candidates: [], available_actions: [] },
    available_actions: ['EDIT_ASSUMPTIONS', 'EDIT_CARDS'],
  }
}

async function show(page, width) {
  const state = { result: fixture(), commands: [], renders: 0, external: [] }
  const initial = structuredClone(state.result)
  const map = { status: 'AVAILABLE', message: '固定路线，仅用于界面验证', points: [], available_actions: ['RENDER_MAP'],
    days: initial.days.map(day => ({ label: day.label, routes: day.activities.filter(card => card.status === 'READY').slice(0, -1).map((card, index) => {
      const next = day.activities.filter(card => card.status === 'READY')[index + 1]
      return { from_activity_token: card.activity_token, to_activity_token: next.activity_token,
        from_name: card.name, to_name: next.name, selected_mode: 'walking', message: '固定路段',
        walking: { status: 'AVAILABLE', duration_minutes: 10, distance_meters: 650, transfer_count: 0, geometry: [] },
        transit: { status: 'UNAVAILABLE', duration_minutes: null, distance_meters: null, transfer_count: null, geometry: [] } }
    }) })) }
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.route('**/*', route => {
    const url = new URL(route.request().url())
    if (!['localhost', '127.0.0.1'].includes(url.hostname)) { state.external.push(url.origin); return route.abort() }
    return route.continue()
  })
  await page.route('**/api/**', route => {
    const request = route.request(), url = new URL(request.url())
    const tag = () => `"fixed-daily-cards-${state.commands.length}"`
    const reply = json => route.fulfill({ json, headers: { ETag: tag() } })
    if (url.pathname.endsWith('/commands')) {
      const command = request.postDataJSON()
      expect(command.command_type).toBe('ACTIVITY_MOVE')
      expect(request.headers()['if-match']).toBe(tag())
      expect(request.headers()['idempotency-key']).toBeTruthy()
      // Actual current command implementation owns all movement/token changes.
      state.result = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-X', 'utf8', '-c', `
import json,sys
from pydantic import TypeAdapter
from app.trip_understanding.models import UserFacingTripResult,TripUnderstandingCommand
from app.trip_understanding.commands import apply_public_command
value=json.load(sys.stdin)
result=apply_public_command(UserFacingTripResult.model_validate(value['result']),TypeAdapter(TripUnderstandingCommand).validate_python(value['command'])).result
print(result.model_dump_json())
`], { cwd: path.resolve(__dirname, '../../backend'), input: JSON.stringify({ result: state.result, command }), encoding: 'utf8',
        env: { ...process.env, RUNTIME_PROFILE: 'test', PYTHONIOENCODING: 'utf-8', PYTHONPATH: '.' } }))
      state.commands.push(command)
      return reply({ status: 'APPLIED', changed_days: state.result.days.map(day => day.label), map_readiness: 'NEEDS_UPDATE' })
    }
    if (url.pathname.endsWith('/result')) return reply(state.result)
    if (url.pathname.endsWith('/map-renders/latest')) return reply(state.commands.length ? { ...map, status: 'NEEDS_UPDATE' } : map)
    if (url.pathname.endsWith('/map-renders')) { state.renders++; return route.fulfill({ status: 409, json: {} }) }
    if (url.pathname.endsWith('/stay-suggestions')) return reply(state.result.stay)
    if (url.pathname.endsWith('/daily-dining')) return reply({ status: 'UNAVAILABLE', message: '固定未生成建议', days: [] })
    if (url.pathname.endsWith('/supplementary')) return reply({ status: 'AVAILABLE', days: [] })
    if (url.pathname.endsWith('/materialize')) return reply({ status: 'READY', message: '固定检查', calendar: '相对日序', party_size: 2, checks_available: true })
    if (url.pathname.endsWith('/checks')) return reply({ status: 'STILL_NEEDS_CONFIRMATION', message: '仍有内容待确认', items: [], remaining_must_adjust: 0, available_actions: [] })
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/trip/result#trip=fixed-four-page-daily-cards-20260913')
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  return state
}

async function expectFit(lane) {
  await expect.poll(() => lane.getByTestId('activity-card').evaluateAll(cards => cards.every(card => {
    const bounds = card.getBoundingClientRect(), copy = card.querySelector('.four-card-copy').getBoundingClientRect()
    const name = card.querySelector('h3')
    return copy.bottom <= bounds.bottom + 1 && name.scrollWidth <= name.clientWidth + 1
  }))).toBe(true)
}

for (const width of [1440, 390]) {
  test(`daily panel collapse keeps long names, pending, source meal and alternatives; keyboard move at ${width}`, async ({ page }, info) => {
    const state = await show(page, width)
    const lane = page.getByTestId('day-lane-1')
    await expect(lane.getByTestId('activity-card')).toHaveCount(13)
    await expectFit(lane)
    const handle = await lane.getByTestId('drag-handle-1-0').boundingBox()
    expect(handle.width).toBeGreaterThanOrEqual(48)
    expect(handle.height).toBeGreaterThanOrEqual(48)
    await expect(lane.locator('.four-day-statistics')).toContainText('待确认 1')
    await expect(lane.locator('.four-day-statistics')).toContainText('未整理 1')
    await expect(lane.locator('.four-day-route-summary')).toHaveText('步行 7.8 公里 · 120 分钟')
    const long = lane.getByRole('heading', { name: longName, exact: true })
    await expect(long).toHaveCSS('white-space', 'normal')
    expect(await long.evaluate(element => element.clientHeight)).toBeGreaterThan(25)
    await page.screenshot({ path: info.outputPath(`daily-expanded-${width}.png`), fullPage: true })
    await page.getByTestId('toggle-day-1').click()
    await expect(page.getByTestId('serpentine-canvas-1')).toBeHidden()
    const overview = page.getByTestId('day-overview-1')
    await expect(overview.locator('.four-mini-copy strong')).toHaveText(names)
    await expect(overview).toContainText('1 个地点待确认')
    await expect(overview).toContainText('1 处原文尚未整理')
    await expect(overview).toContainText('另有 1 个备选')
    await expect(overview).toContainText('午餐')
    await page.getByTestId('toggle-day-3').click()
    await expect(page.getByTestId('day-overview-3')).toContainText('2 处原文尚未整理')
    await expect(page.getByTestId('day-overview-3')).toContainText('另有 1 个备选')
    await expect(page.getByTestId('day-overview-3')).toBeVisible()
    await page.getByTestId('day-lane-3').screenshot({ path: info.outputPath(`empty-day-collapsed-${width}.png`) })
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({ path: info.outputPath(`daily-collapsed-${width}.png`), fullPage: true })
    expect(state.commands).toEqual([])
    await page.getByTestId('toggle-day-1').click()
    await expect(lane.getByTestId('activity-card').locator('h3')).toHaveText(names)
    await expectFit(lane)
    await page.getByTestId('toggle-day-2').click()
    const more = lane.getByRole('button', { name: `${longName}更多操作`, exact: true })
    await more.click()
    await lane.getByRole('button', { name: '删除地点', exact: true }).click()
    await expect(page.getByRole('dialog')).toContainText(longName)
    await page.getByRole('dialog').getByRole('button', { name: '取消', exact: true }).click()
    await more.click()
    await lane.getByRole('button', { name: '移动地点', exact: true }).click()
    await page.keyboard.press('Escape')
    expect(state.commands).toEqual([])
    await more.click()
    await lane.getByRole('button', { name: '移动地点', exact: true }).click()
    await page.keyboard.press('ArrowDown')
    await expect(page.getByTestId('toggle-day-2')).toHaveAttribute('aria-expanded', 'true')
    await page.keyboard.press('Enter')
    await expect.poll(() => state.commands.length).toBe(1)
    await expect(page.getByTestId('day-lane-2').getByTestId('activity-card').locator('h3')).toHaveText([longName, '奥林匹克森林公园', '中国美术馆'])
    await expect(lane.getByTestId('activity-card')).toHaveCount(12)
    await expect(lane.locator('.four-day-statistics')).toContainText('待确认 1')
    await expect(page.getByTestId('day-alternatives-1')).toHaveText('备选 · 1')
    await expect(page.getByTestId('unmatched-places-note')).toContainText('1 项待确认')
    expect(state.renders).toBe(0)
    expect(state.external).toEqual([])
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: info.outputPath(`daily-moved-${width}.png`), fullPage: true })
  })

  test(`measured long card height keeps reverse-row pointer drop and truthful routes at ${width}`, async ({ page }, info) => {
    const state = await show(page, width)
    const lane = page.getByTestId('day-lane-1'), canvas = page.getByTestId('serpentine-canvas-1')
    await expectFit(lane)
    const columns = Number(await canvas.getAttribute('data-columns'))
    expect(columns).toBeGreaterThanOrEqual(width === 390 ? 2 : 4)
    const boxes = await lane.getByTestId('activity-card').evaluateAll(nodes => nodes.map(node => {
      const rect = node.getBoundingClientRect(); return { x: rect.x, y: rect.y, right: rect.right, bottom: rect.bottom }
    }))
    expect(boxes[1].x).toBeGreaterThan(boxes[0].x)
    expect(boxes[columns + 1].x).toBeLessThan(boxes[columns].x)
    const bounds = await canvas.boundingBox()
    expect(boxes.every(box => box.right <= width && box.bottom <= bounds.y + bounds.height + 1)).toBe(true)
    await expect(lane.getByTestId('order-arc')).toHaveCount(12)
    await page.getByTestId('card-grab-1-0').evaluate(element => window.scrollBy(0, element.getBoundingClientRect().top - 130))
    const source = await page.getByTestId('card-grab-1-0').boundingBox()
    const target = await page.getByTestId(`card-grab-1-${columns + 1}`).boundingBox()
    await page.mouse.move(source.x + 28, source.y + 34)
    await page.mouse.down()
    await page.mouse.move(target.x + target.width - 16, target.y + 35, { steps: 12 })
    await expect(page.getByTestId('drop-preview')).toContainText(longName)
    expect(state.commands).toHaveLength(0)
    await page.mouse.up()
    await expect.poll(() => state.commands.length).toBe(1)
    await expect.poll(() => lane.getByTestId('activity-card').locator('h3').allTextContents()).toEqual([
      ...names.slice(1, columns + 1), longName, ...names.slice(columns + 1),
    ])
    await expectFit(lane)
    await expect(lane.locator('.four-day-route-summary')).toHaveText('路线需要更新')
    expect(state.result.days[0].activities.filter(card => card.status === 'NEEDS_CONFIRMATION')).toHaveLength(1)
    expect(state.renders).toBe(0)
    expect(state.external).toEqual([])
    await page.screenshot({ path: info.outputPath(`daily-pointer-move-${width}.png`), fullPage: true })
  })
}
