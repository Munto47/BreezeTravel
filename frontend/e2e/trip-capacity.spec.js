const {test, expect} = require('@playwright/test')
const {execFileSync} = require('node:child_process')
const path = require('node:path')

// Fixed external model/place responses pass through the real Python provider and
// pipeline. This verifies capacity and rendering, not live extraction accuracy.
let result, expectedDays
test.beforeAll(() => {
  const script = `
import asyncio,json,runpy
ns=runpy.run_path('tests/test_trip_capacity_readback.py')
async def main():
    output=await ns['build_capacity_result'](160,14)
    _,names=ns['capacity_source'](160,14)
    print(json.dumps({'result':output.public_result.model_dump(mode='json'),'names':names},ensure_ascii=False))
asyncio.run(main())
`
  const data = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-c', script], {
    cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8',
    env: {...process.env, RUNTIME_PROFILE: 'test', PYTHONPATH: '.', PYTHONIOENCODING: 'utf-8'},
  }))
  result = data.result
  expectedDays = data.names
})

for (const width of [1440, 390]) test(`14 days and 160 activities survive the page and PNG at ${width}px`, async ({page}, info) => {
  await page.setViewportSize({width, height: 900})
  await page.addInitScript(() => {
    window.exportText = []
    const original = CanvasRenderingContext2D.prototype.fillText
    CanvasRenderingContext2D.prototype.fillText = function (text, x, y, ...rest) {
      const position = this.getTransform().transformPoint({x, y})
      const metrics = this.measureText(String(text))
      window.exportText.push({text: String(text), x: position.x, y: position.y,
        width: metrics.width, canvasWidth: this.canvas.width, canvasHeight: this.canvas.height})
      return original.call(this, text, x, y, ...rest)
    }
  })
  await page.route('**/webapi.amap.com/**', r => r.abort())
  await page.route('**/restapi.amap.com/**', r => r.abort())
  await page.route('**/api/**', route => {
    const pathname = new URL(route.request().url()).pathname
    const reply = json => route.fulfill({json, headers: {ETag: '"capacity-version"'}})
    if (pathname === '/api/user/me') return route.fulfill({status: 401, json: {}})
    if (pathname.endsWith('/result')) return reply(result)
    if (pathname.endsWith('/map-renders/latest')) return reply({...result.map, points: [], days: []})
    if (pathname.endsWith('/stay-suggestions')) return reply(result.stay)
    if (pathname.endsWith('/daily-dining')) return reply({status: 'UNAVAILABLE', message: '未生成建议', days: []})
    if (pathname.endsWith('/supplementary')) return reply({status: 'AVAILABLE', days: []})
    if (pathname.endsWith('/materialize')) return reply({status: 'READY', message: '已准备', calendar: '按日期', party_size: 2, checks_available: true})
    if (pathname.endsWith('/checks')) return reply({status: 'STILL_NEEDS_CONFIRMATION', message: '请核对安排', items: [], remaining_must_adjust: 0, available_actions: []})
    return route.fulfill({status: 404, json: {}})
  })
  await page.goto('/trip/result#trip=synthetic-capacity-160')
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  await expect(page.getByTestId('activity-card')).toHaveCount(160)
  for (const [index, names] of expectedDays.entries()) {
    expect(await page.getByTestId(`day-lane-${index + 1}`).getByTestId('activity-card').getByRole('heading').allTextContents()).toEqual(names)
  }
  await page.getByRole('button', {name: '导出图片', exact: true}).click()
  const download = page.waitForEvent('download')
  await page.getByTestId('download-itinerary-png').click()
  const png = await download
  expect(await png.failure()).toBeNull()
  await png.saveAs(info.outputPath('complete-14-day-160-activity.png'))
  const rendered = await page.evaluate(() => window.exportText)
  for (const name of expectedDays.flat()) {
    const drawn = rendered.filter(item => item.text === name)
    expect(drawn, name).toHaveLength(1)
    expect(drawn[0].x).toBeGreaterThanOrEqual(0)
    expect(drawn[0].y).toBeGreaterThan(0)
    expect(drawn[0].x + drawn[0].width).toBeLessThan(drawn[0].canvasWidth)
    expect(drawn[0].y).toBeLessThan(drawn[0].canvasHeight)
  }
  for (let day = 1; day <= 14; day++) expect(rendered.some(item => item.text === `Day ${day}`)).toBe(true)
  await page.getByLabel('关闭图片预览').click()
  await page.getByTestId('activity-card').last().scrollIntoViewIfNeeded()
  await expect(page.getByTestId('activity-card').last()).toBeVisible()
  await page.screenshot({path: info.outputPath('last-day-visible.png')})
  await info.attach('capacity-rendering', {body: JSON.stringify({days: 14, activities: 160,
    pngWidth: rendered[0].canvasWidth, pngHeight: rendered[0].canvasHeight}), contentType: 'application/json'})
})
