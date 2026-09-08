const {test, expect} = require('@playwright/test')
const {execFileSync} = require('node:child_process')
const path = require('node:path')

// Execute the actual provider, pipeline and public projection. The first two
// model replies are synthetic; the hotel and missing-name replies are saved real
// model outputs. All external place identities are fixed simulations. This
// checks downstream preservation and the page, not current live model accuracy.
let results
test.beforeAll(() => {
  results = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-m', 'tests.semantic_page_replays'], {
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

  test(`saved real hotel response to page: nine visits retain order and both hotel purposes at ${width}px`, async ({page}, info) => {
    await page.setViewportSize({width, height: 900})
    await show(page, 'lodging')
    await expect(page.getByText('部分待补全', {exact: true})).toBeVisible()
    const expectedDays = [
      ['断桥残雪', '浙江省博物馆孤山馆区', '楼外楼孤山路店', '曲院风荷', '杭州西湖国宾馆'],
      ['灵隐寺', '龙井村', '杭州西湖国宾馆', '杭州东站'],
    ]
    for (const [index, names] of expectedDays.entries()) {
      const lane = page.getByTestId(`day-lane-${index + 1}`)
      await expect(lane.getByTestId('activity-card').getByRole('heading')).toHaveText(names)
    }
    await expect(page.getByTestId('activity-card')).toHaveCount(9)
    const overnight = page.getByTestId('day-lane-1').getByTestId('activity-card').filter({hasText: '杭州西湖国宾馆'})
    const pickup = page.getByTestId('day-lane-2').getByTestId('activity-card').filter({hasText: '杭州西湖国宾馆'})
    await overnight.scrollIntoViewIfNeeded()
    await expect(overnight.getByRole('button', {name: /杭州西湖国宾馆 入住 已确认/})).toBeVisible()
    await pickup.scrollIntoViewIfNeeded()
    await expect(pickup.getByRole('button', {name: /杭州西湖国宾馆 取行李 已确认/})).toBeVisible()
    await expect(pickup).toBeInViewport()
    // An unnamed checkout remains unfinished; it cannot borrow last night's
    // hotel name or create a third confirmed hotel card.
    await expect(page.getByTestId('day-unprocessed-1')).toHaveCount(0)
    await expect(page.getByTestId('day-unprocessed-2')).toContainText('1 处原文内容尚未整理完成')
    await expect(page.getByTestId('unmatched-places-note')).toContainText('尚未完整整理')
    await expect(page.getByRole('button', {name: /杭州西湖国宾馆 退房 已确认/})).toHaveCount(0)
    expect(results.lodging.coverage.complete).toBe(false)
    expect(results.lodging.coverage.unprocessed_count).toBe(1)
    expect(results.lodging.days.map(day => day.unprocessed_count)).toEqual([0, 1])
    await page.screenshot({path: info.outputPath('real-hotel-replay.png'), fullPage: true})

    await page.getByRole('button', {name: '切换为列表', exact: true}).click()
    for (const [index, names] of expectedDays.entries()) {
      const list = page.getByRole('list', {name: `${results.lodging.days[index].label} 地点列表`, exact: true})
      const details = list.getByRole('button', {name: /已确认 · 可更改$/})
      await expect(details).toHaveCount(names.length)
      const accessibleNames = await details.allTextContents()
      expect(accessibleNames.map((text, i) => text.includes(names[i]))).toEqual(names.map(() => true))
    }
    await page.screenshot({path: info.outputPath('real-hotel-replay-list.png'), fullPage: true})
  })

  test(`saved real missing-name response to page: both days remain unfinished at ${width}px`, async ({page}, info) => {
    await page.setViewportSize({width, height: 900})
    await show(page, 'missing_name')
    await expect(page.getByText('部分待补全', {exact: true})).toBeVisible()
    expect(results.missing_name.status).toBe('PARTIAL_RESULT')
    expect(results.missing_name.coverage.complete).toBe(false)
    expect(results.missing_name.coverage.unprocessed_count).toBe(12)
    expect(results.missing_name.days.map(day => day.unprocessed_count)).toEqual([5, 7])
    await expect(page.getByTestId('activity-card')).toHaveCount(0)
    for (const day of [1, 2]) {
      await expect(page.getByTestId(`day-lane-${day}`)).toBeVisible()
      const warning = page.getByTestId(`day-unprocessed-${day}`)
      await warning.scrollIntoViewIfNeeded()
      await expect(warning).toContainText(`${day === 1 ? 5 : 7} 处原文内容尚未整理完成`)
      await expect(warning).toBeInViewport()
    }
    await expect(page.getByTestId('unmatched-places-note')).toContainText('尚未完整整理')
    await page.screenshot({path: info.outputPath('real-missing-name-replay.png'), fullPage: true})
  })

  for (const [kind, limit] of [['item_limit', '160 项'], ['day_limit', '14 天']]) {
    test(`provider capacity failure to page: ${limit} limit explains splitting and retains input at ${width}px`, async ({page}, info) => {
      const resource = `synthetic-capacity-${kind}`, original = results[kind].source
      await page.setViewportSize({width, height: 900})
      await page.addInitScript(({resource, original}) => {
        if (!sessionStorage.getItem('bt_input_draft')) sessionStorage.setItem('bt_input_draft', JSON.stringify({
          text: original, demo: false, key: 'synthetic-capacity-input', resource, expires: Date.now() + 600000,
        }))
      }, {resource, original})
      let writes = 0
      await page.route('**/api/**', route => {
        if (route.request().method() !== 'GET') writes++
        if (new URL(route.request().url()).pathname.endsWith('/result')) {
          return route.fulfill({status: 409, json: results[kind].failure})
        }
        return route.fulfill({status: 401, json: {}})
      })
      await page.goto(`/trip/result#trip=${resource}`)
      await expect(page.getByRole('heading', {name: '这次没有整理完成', exact: true})).toBeVisible()
      await expect(page.getByRole('status')).toContainText(`${limit}上限`)
      await expect(page.getByRole('status')).toContainText('请分成多份行程')
      await expect(page.getByRole('button', {name: '继续等待', exact: true})).toHaveCount(0)
      await page.screenshot({path: info.outputPath(`${kind}-explained.png`), fullPage: true})
      await page.getByRole('link', {name: '返回首页', exact: true}).click()
      await expect(page.locator('textarea')).toHaveValue(original)
      expect(writes).toBe(0)
    })
  }
}
