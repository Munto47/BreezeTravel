const {test, expect} = require('@playwright/test')
const {execFileSync} = require('node:child_process')
const path = require('node:path')

let result
test.beforeAll(() => {
  result = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-c', `
import asyncio,json
from tests.test_meal_preferences import build_owner_meal_result
output=asyncio.run(build_owner_meal_result())
print(output.public_result.model_dump_json())
`], {cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8',
    env: {...process.env, RUNTIME_PROFILE: 'test', PYTHONPATH: '.', PYTHONIOENCODING: 'utf-8'}}))
})

for (const width of [1440, 390]) test(`saved owner dinner preferences remain visible and export at ${width}px`, async ({page}, info) => {
  await page.setViewportSize({width, height: 1000})
  await page.route('**/*', route => {
    const url = new URL(route.request().url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.fallback()
    const reply = json => route.fulfill({json, headers: {ETag: '"fixed-owner-meal-version"'}})
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
  await page.goto('/trip/result#trip=fixed-owner-meal-preferences-0001')
  const texts = ['晚餐：铜锅涮肉（南门涮肉）、炸酱面', '晚餐：烤鸭（四季民福、紫光园）']
  async function checkPage() {
    await expect(page.getByTestId('activity-card')).toHaveCount(12)
    for (const [i, text] of texts.entries()) {
      await expect(page.getByTestId(`source-meals-${i + 1}`)).toContainText(text)
      await expect(page.getByTestId(`source-meals-${i + 1}`)).toContainText('餐厅待选择')
    }
    await expect(page.getByTestId('source-meals-3')).toHaveCount(0)
    await expect(page.getByTestId('itinerary-workspace')).not.toContainText('护国寺小吃')
  }
  await checkPage()
  await page.reload()
  await checkPage()
  await page.screenshot({path: info.outputPath('dinner-preferences-page.png'), fullPage: true})
  await page.getByTestId('export-itinerary-png').click()
  await expect(page.getByAltText('行程横链导出预览', {exact: true})).toBeVisible()
  const drawn = await page.evaluate(() => window.exportText)
  const text = drawn.map(row => row.text).join('')
  for (const dinner of texts) expect(text).toContain(dinner)
  expect(text).not.toContain('护国寺小吃')
  for (const row of drawn) {
    expect(row.x + row.width, row.text).toBeLessThanOrEqual(row.canvasWidth)
    expect(row.y, row.text).toBeLessThan(row.canvasHeight)
  }
  const downloading = page.waitForEvent('download')
  await page.getByTestId('download-itinerary-png').click()
  const download = await downloading
  expect(await download.failure()).toBeNull()
  await download.saveAs(info.outputPath('dinner-preferences.png'))
  await info.attach('fixed-owner-public-and-canvas', {body: JSON.stringify({scope: 'SAVED_REAL_MODEL_AND_POI_REPLAY_ZERO_NETWORK', result, drawn}), contentType: 'application/json'})
})
