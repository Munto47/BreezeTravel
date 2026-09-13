const {test, expect} = require('@playwright/test')
const {execFileSync} = require('node:child_process')
const path = require('node:path')

let result
test.beforeAll(() => {
  result = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-c', `
import asyncio
from tests.test_anonymous_meal_context import build_shanghai_meal_context_result
print(asyncio.run(build_shanghai_meal_context_result()).public_result.model_dump_json())
`], {cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8',
    env: {...process.env, RUNTIME_PROFILE: 'test', PYTHONPATH: '.', PYTHONIOENCODING: 'utf-8'}}))
})

for (const width of [1440, 390]) test(`saved Shanghai anonymous meal content stays separate from places at ${width}px`, async ({page}, info) => {
  await page.setViewportSize({width, height: 1000})
  await page.route('**/*', route => {
    const url = new URL(route.request().url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.fallback()
    const reply = json => route.fulfill({json, headers: {ETag: '"fixed-anonymous-meal-context"'}})
    if (url.pathname === '/api/user/me') return route.fulfill({status: 401, json: {}})
    if (url.pathname.endsWith('/result')) return reply(result)
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
      window.exportText.push({text: String(text), x: point.x, y: point.y,
        width: this.measureText(String(text)).width, canvasWidth: this.canvas.width, canvasHeight: this.canvas.height})
      return original.call(this, text, x, y, ...rest)
    }
  })
  await page.goto('/trip/result#trip=fixed-shanghai-anonymous-meals-0001')
  const meal1 = '大壶春生煎、沈大成条头糕、鲜得来排骨年糕'
  const meal2 = '乌鲁木齐中路 / 安福路吃本帮面、咖啡简餐'
  const meal3 = '人和馆、老正兴，尝红烧肉、响油鳝糊、油爆虾'
  async function checkPage() {
    await expect(page.getByTestId('activity-card')).toHaveCount(14)
    await expect(page.getByTestId('unmatched-places-note')).toContainText('3 项待确认')
    await expect(page.getByTestId('source-meals-1')).toContainText('午餐 · 餐厅待选择')
    await expect(page.getByTestId('source-meals-1')).toContainText(meal1)
    await expect(page.getByTestId('source-meals-2')).toContainText(meal2)
    await expect(page.getByTestId('source-meals-2')).toContainText(meal3)
    await expect(page.getByTestId('source-meals-2')).toContainText('晚餐 · 餐厅待选择')
    await expect(page.getByTestId('source-meals-2')).toContainText('在「进贤路」（地点待确认）之后')
    await expect(page.getByTestId('source-meals-2')).not.toContainText('用餐 ·')
    await expect(page.getByTestId('source-meals-3')).toHaveCount(0)
    await expect(page.getByTestId('activity-card').getByRole('heading')).not.toContainText(['地点待确认'])
  }
  await checkPage()
  await page.reload()
  await checkPage()
  await page.getByTestId('source-meals-2').scrollIntoViewIfNeeded()
  await page.screenshot({path: info.outputPath('anonymous-meal-context-page.png')})
  await page.getByTestId('export-itinerary-png').click()
  await expect(page.getByAltText('行程横链导出预览', {exact: true})).toBeVisible()
  const drawn = await page.evaluate(() => window.exportText)
  const text = drawn.map(row => row.text).join('')
  for (const meal of [meal1, meal2, meal3]) expect(text).toContain(meal)
  expect(text).toContain('晚餐 · 餐厅待选择')
  expect(text).toContain('在「进贤路」（地点待确认）之后')
  expect(text).not.toContain('用餐 ·')
  for (const row of drawn) {
    expect(row.x + row.width, row.text).toBeLessThanOrEqual(row.canvasWidth)
    expect(row.y, row.text).toBeLessThan(row.canvasHeight)
  }
  const downloading = page.waitForEvent('download')
  await page.getByTestId('download-itinerary-png').click()
  const download = await downloading
  expect(await download.failure()).toBeNull()
  await download.saveAs(info.outputPath('anonymous-meal-context.png'))
  await info.attach('saved-actual-answer-and-poi-replay-public', {
    body: JSON.stringify({scope: 'SAVED_REAL_FIRST_ANSWER_AND_TWENTY_PLACE_REPLIES_ZERO_EXTERNAL_CALLS', result, drawn}),
    contentType: 'application/json',
  })
})
