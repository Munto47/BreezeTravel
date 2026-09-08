const {test, expect} = require('@playwright/test')
const {execFileSync} = require('node:child_process')
const path = require('node:path')

// Execute the actual provider, pipeline and public projection. Only the external
// model and place services are synthetic; this does not measure live accuracy.
let results
test.beforeAll(() => {
  const script = `
import asyncio,json,runpy
ns=runpy.run_path('tests/test_semantic_day_sections.py')
async def main():
    result={}
    for kind in ('partial','optional'):
        client=ns['ScopedClient'](second_fails=True) if kind=='partial' else ns['OptionalDayClient']()
        source=ns['source']()
        if kind=='optional': source=source.replace('Day2：月光桥。','Day2：月光桥作为备选。')
        output=await ns['TripUnderstandingPipeline'](ns['provider'](client),ns['RecordingPlaces']()).run(source)
        result[kind]=output.public_result.model_dump(mode='json')
    print(json.dumps(result,ensure_ascii=False))
asyncio.run(main())
`
  results = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-c', script], {
    cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8',
    env: {...process.env, RUNTIME_PROFILE: 'test', PYTHONPATH: '.', PYTHONIOENCODING: 'utf-8'},
  }))
})

async function show(page, kind) {
  const result = results[kind], resource = `synthetic-semantic-${kind}`
  await page.route('**/webapi.amap.com/**', r => r.abort())
  await page.route('**/restapi.amap.com/**', r => r.abort())
  await page.route('**/api/**', route => {
    const pathname = new URL(route.request().url()).pathname
    const reply = json => route.fulfill({json, headers: {ETag: '"synthetic-semantic-version"'}})
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
  await page.goto(`/trip/result#trip=${resource}`)
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
}

for (const width of [1440, 390]) {
  test(`provider to page: failed second day remains explicitly unfinished at ${width}px`, async ({page}, info) => {
    await page.setViewportSize({width, height: 900})
    await show(page, 'partial')
    await expect(page.getByTestId('day-lane-1')).toContainText('星河公园')
    await expect(page.getByTestId('day-lane-2')).toBeVisible()
    await expect(page.getByTestId('day-unprocessed-2')).toContainText('尚未整理完成')
    await expect(page.getByTestId('day-unprocessed-1')).toHaveCount(0)
    await expect(page.getByTestId('unmatched-places-note')).toContainText('尚未完整整理')
    expect(results.partial.coverage.complete).toBe(false)
    await page.screenshot({path: info.outputPath('failed-day-retained.png'), fullPage: true})
  })

  test(`provider to page: optional-only second day and its actual alternative survive at ${width}px`, async ({page}, info) => {
    await page.setViewportSize({width, height: 900})
    await show(page, 'optional')
    await expect(page.getByTestId('day-lane-1')).toContainText('星河公园')
    await expect(page.getByTestId('day-lane-2')).toContainText('仍是备选')
    await expect(page.getByTestId('day-unprocessed-2')).toHaveCount(0)
    await expect(page.getByTestId('day-alternatives-2')).toHaveText('备选 · 1')
    await page.getByTestId('day-alternatives-2').click()
    const panel = page.getByRole('complementary', {name: '检查与建议', exact: true})
    await expect(panel.getByLabel('建议所属日期')).toHaveValue('1')
    await expect(panel).toContainText('月光桥')
    await expect(panel.getByRole('button', {name: '加入待确认', exact: true})).toHaveCount(1)
    await expect(page.getByTestId('activity-card').filter({hasText: '月光桥'})).toHaveCount(0)
    expect(results.optional.coverage.complete).toBe(true)
    await page.screenshot({path: info.outputPath('optional-day-retained.png'), fullPage: true})
  })
}
