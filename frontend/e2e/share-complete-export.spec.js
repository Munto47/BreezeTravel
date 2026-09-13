const {test, expect} = require('@playwright/test')
const {execFileSync} = require('node:child_process')
const path = require('node:path')

let fixture, capacity
test.beforeAll(() => {
  const script = 'import asyncio,json;from tests.test_share_complete_content import build_share_export_fixture;from app.trip_understanding.memory_share import build_share_projection;from tests.test_trip_capacity_readback import build_capacity_result\nasync def run():\n r,s=await build_share_export_fixture();c=await build_capacity_result(relative_only=True);print(json.dumps({"result":r.model_dump(mode="json"),"supplementary":s.model_dump(mode="json"),"share":build_share_projection(r,supplementary=s).model_dump(mode="json"),"capacity":c.public_result.model_dump(mode="json")},ensure_ascii=False))\nasyncio.run(run())'
  const data = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-X', 'utf8', '-c', script], {
    cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8', maxBuffer: 16 * 1024 * 1024,
    env: {...process.env, RUNTIME_PROFILE: 'test', PYTHONPATH: '.', PYTHONIOENCODING: 'utf-8'},
  }))
  fixture = data
  capacity = data.capacity
})

async function show(page, result, supplementary, width, options = {}) {
  const writes = []
  await page.setViewportSize({width, height: 1000})
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if (!['localhost', '127.0.0.1'].includes(url.hostname)) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.fallback()
    options.observedRequests?.push({method: request.method(), path: url.pathname})
    const reply = json => route.fulfill({json, headers: {ETag: '"fixed-complete-export"'}})
    if (request.method() !== 'GET') writes.push({path: url.pathname, method: request.method()})
    if (url.pathname === '/api/user/me') return route.fulfill({status: 401, json: {}})
    if (url.pathname.endsWith('/result')) return reply(result)
    if (url.pathname.endsWith('/supplementary')) return options.supplementaryHandler ? options.supplementaryHandler(route) : reply(supplementary)
    if (url.pathname.endsWith('/source') && request.method() === 'DELETE') return reply({status: 'DELETED'})
    if (url.pathname.endsWith('/map-renders/latest')) return reply({status: 'NEEDS_UPDATE', message: '请手动更新路线', days: [], points: [], available_actions: ['RENDER_MAP']})
    if (url.pathname.endsWith('/stay-suggestions')) return reply(result.stay)
    if (url.pathname.endsWith('/daily-dining')) return reply({status: 'UNAVAILABLE', days: []})
    if (url.pathname.endsWith('/materialize')) {
      await options.beforeMaterialize?.()
      return route.fulfill({json: {status: 'READY', message: '固定只读检查', calendar: '按天安排', party_size: 2, checks_available: true}, headers: {ETag: options.materializedEtag || '"fixed-complete-export"'}})
    }
    if (url.pathname.endsWith('/checks')) return reply({status: 'STILL_NEEDS_CONFIRMATION', message: '仍需补全', items: [], remaining_must_adjust: 0})
    return route.fulfill({status: 404, json: {}})
  })
  await page.addInitScript(() => {
    window.completeExportText = []
    const fill = CanvasRenderingContext2D.prototype.fillText
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y, ...rest) {
      const point = this.getTransform().transformPoint({x, y})
      window.completeExportText.push({text: String(text), x: point.x, y: point.y, width: this.measureText(String(text)).width,
        canvasWidth: this.canvas.width, canvasHeight: this.canvas.height})
      return fill.call(this, text, x, y, ...rest)
    }
  })
  const checked = page.waitForResponse(response => response.url().endsWith('/checks'))
  await page.goto('/trip/result#trip=fixed-complete-export-resource-0001')
  await checked
  await expect(page.getByTestId('itinerary-workspace')).toBeVisible()
  if (!options.skipSupplementaryReady) await expect(page.getByTestId('export-itinerary-png')).toBeEnabled()
  return writes
}

async function png(page, info, filename) {
  await page.getByTestId('export-itinerary-png').click()
  const image = page.getByAltText('行程横链导出预览', {exact: true})
  await expect(image).toBeVisible()
  await expect.poll(() => image.evaluate(img => img.naturalWidth)).toBe(1440)
  const drawn = await page.evaluate(() => window.completeExportText)
  for (const line of drawn) {
    expect(line.x, line.text).toBeGreaterThanOrEqual(0)
    expect(line.x + line.width, line.text).toBeLessThanOrEqual(line.canvasWidth)
    expect(line.y, line.text).toBeGreaterThan(0)
    expect(line.y, line.text).toBeLessThan(line.canvasHeight)
  }
  const downloading = page.waitForEvent('download')
  await page.getByTestId('download-itinerary-png').click()
  const download = await downloading
  expect(await download.failure()).toBeNull()
  await download.saveAs(info.outputPath(filename))
  await info.attach('drawn-content', {body: JSON.stringify({scope: 'FIXED_PROVIDER_AND_STRUCTURED_DISPLAY_FIXTURE_ZERO_LIVE_CALLS', drawn}), contentType: 'application/json'})
  return drawn
}

for (const width of [1440, 1280]) test(`complete source arrangements and undated alternatives survive desktop PNG at ${width}`, async ({page}, info) => {
  const writes = await show(page, fixture.result, fixture.supplementary, width)
  await expect(page.getByTestId('activity-card')).toHaveCount(3)
  const checked = page.waitForResponse(response => response.url().endsWith('/checks'))
  await page.reload()
  await checked
  await expect(page.getByTestId('export-itinerary-png')).toBeEnabled()
  const beforeWrites = writes.length
  const drawn = await png(page, info, `complete-${width}.png`)
  const text = drawn.map(line => line.text).join('')
  for (const name of ['Day 1', 'Day 2', 'Day 3', '中国国家博物馆', '古代中国展厅', '复兴之路展厅（备选）', '太和殿', '祈年殿',
    '早餐 · 餐厅待选择', '豆浆和包子', '午餐 · 餐厅待选择', '晚餐 · 餐厅待选择', '未指定日期', '北京动物园',
    '原文未整理：2 处', '原文未整理：1 处', '固定北京全程住宿酒店', '第1、2晚', '入住后寄放行李']) expect(text).toContain(name)
  const day3 = drawn.find(line => line.text === 'Day 3')
  expect(drawn.find(line => line.text === '北京动物园').y).toBeGreaterThan(day3.y)
  expect(writes.slice(beforeWrites)).toEqual([])
  await page.screenshot({path: info.outputPath(`preview-${width}.png`)})
})

for (const change of ['source deleted', 'resource switched', 'version changed']) test(`late supplementary cannot overwrite ${change} on desktop`, async ({page}, info) => {
  let held, reads = 0
  const observedRequests = []
  let releaseMaterialize
  const firstSupplementaryStarted = new Promise(resolve => { releaseMaterialize = resolve })
  const fresh = {status: 'AVAILABLE', days: [{day_index: null, day_label: '未指定日期', items: [{name: '新资源备选', role: 'OPTIONAL'}]}]}
  await show(page, fixture.result, fixture.supplementary, 1440, {
    skipSupplementaryReady: true,
    observedRequests,
    materializedEtag: change === 'version changed' ? '"changed-version"' : undefined,
    beforeMaterialize: change === 'version changed' ? () => firstSupplementaryStarted : undefined,
    supplementaryHandler: route => {
      reads++
      if (reads === 1) { held = route; releaseMaterialize(); return new Promise(() => {}) }
      return route.fulfill({json: fresh})
    },
  })
  expect(held).toBeTruthy()
  if (change !== 'version changed') await expect(page.getByTestId('export-itinerary-png')).toBeDisabled()
  if (change === 'source deleted') {
    await page.getByLabel('更多行程操作').click()
    await page.getByTestId('delete-trip-source').click()
    await page.getByRole('button', {name: '确认永久删除', exact: true}).click()
    await expect(page.getByTestId('export-itinerary-png')).toBeEnabled()
  } else if (change === 'resource switched') {
    await page.evaluate(() => { window.location.hash = 'trip=other-complete-export-resource-0002' })
    await expect.poll(() => reads).toBe(2)
    await expect(page.getByTestId('export-itinerary-png')).toBeEnabled()
  } else {
    await expect.poll(() => reads).toBe(2)
    await expect(page.getByTestId('export-itinerary-png')).toBeEnabled()
  }
  const settled = page.waitForResponse(response => response.url().endsWith('/supplementary'))
  await held.fulfill({json: fixture.supplementary})
  await settled
  // The new ETag starts its own read without reloading the page. Releasing the
  // original response afterwards cannot replace the current export content.
  if (change === 'version changed') {
    expect(reads).toBe(2)
    for (const suffix of ['/map-renders/latest', '/stay-suggestions', '/materialize'])
      expect(observedRequests.filter(item => item.path.endsWith(suffix)), suffix).toHaveLength(1)
    // useDailyDining already reads once per ETag. Preserve those two existing
    // GETs; the supplementary retry must not request restaurant regeneration.
    const diningReads = observedRequests.filter(item => item.path.endsWith('/daily-dining'))
    expect(diningReads).toHaveLength(2)
    expect(diningReads.every(item => item.method === 'GET')).toBe(true)
  }
  const drawn = await png(page, info, `late-${change.replaceAll(' ', '-')}.png`)
  const text = drawn.map(item => item.text).join('')
  expect(text).not.toContain('北京动物园')
  expect(text).toContain(change === 'source deleted' ? '补充安排已删除' : '新资源备选')
})

test('complete read-only share renders all days and preserves fragment exchange and revoked access', async ({page}, info) => {
  await page.setViewportSize({width: 1440, height: 1000})
  let revoked = false
  const requests = []
  await page.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.fallback()
    requests.push({method: req.method(), path: url.pathname})
    if (url.pathname.endsWith('/exchange')) {
      expect(new URL(page.url()).hash).toBe('')
      expect(req.postDataJSON()).toEqual({secret: 'fixed-share-secret'})
      return route.fulfill({status: revoked ? 404 : 200, json: {status: 'READY'}})
    }
    return route.fulfill({status: revoked ? 404 : 200, json: revoked ? {} : fixture.share})
  })
  await page.goto('/share/fixed-complete-share-00000001#s=fixed-share-secret')
  await expect(page.getByTestId('g06-shared-trip')).toBeVisible()
  for (const [day, text] of [[1, '太和殿'], [2, '祈年殿'], [3, '中国国家博物馆']])
    await expect(page.getByTestId(`shared-day-${day}`)).toContainText(text)
  await expect(page.getByTestId('shared-day-3')).toContainText('豆浆和包子')
  await expect(page.getByTestId('shared-day-2')).toContainText('待确认地点：1 处')
  await expect(page.getByTestId('shared-incomplete')).toBeVisible()
  await expect(page.getByTestId('shared-unassigned')).toHaveText(/北京动物园/)
  await expect(page.getByTestId('g06-shared-trip')).toContainText('固定北京全程住宿酒店 · 第1、2晚 · 已确认')
  await expect(page.getByTestId('g06-shared-trip').getByRole('button')).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({path: info.outputPath('complete-readonly-share-1440.png'), fullPage: true})
  await page.reload()
  await expect(page.getByTestId('shared-day-3')).toContainText('复兴之路展厅（备选）')
  expect(requests.filter(item => item.method !== 'GET')).toEqual([{method: 'POST', path: '/api/v3/shares/fixed-complete-share-00000001/exchange'}])
  revoked = true
  await page.reload()
  await expect(page.getByRole('alert').filter({hasText: '已被撤销'})).toBeVisible()
  await expect(page.getByTestId('g06-shared-trip')).toHaveCount(0)
})

test('all fourteen days and 160 full names remain inside the desktop PNG', async ({page}, info) => {
  const writes = await show(page, capacity, {status: 'AVAILABLE', days: []}, 1440)
  const beforeWrites = writes.length
  const drawn = await png(page, info, 'all-14-days-160.png')
  const text = drawn.map(line => line.text).join('')
  for (let index = 1; index <= 14; index++) expect(drawn.some(line => line.text === `Day ${index}`)).toBe(true)
  for (const day of capacity.days) for (const card of day.activities) expect(text).toContain(card.name)
  expect(drawn.find(line => line.text === '容量测试160公园').y).toBeGreaterThan(drawn.find(line => line.text === 'Day 14').y)
  expect(writes.slice(beforeWrites)).toEqual([])
})

test('pending whole-trip lodging keeps its real scope without joining confirmed cards or deduplicating by name', async ({page}, info) => {
  const result = structuredClone(fixture.result)
  result.lodging_constraints.push({...structuredClone(result.lodging_constraints[0]),
    activity_token:'fixed-pending-hotel-other-visit-0002',status:'NEEDS_CONFIRMATION',
    scope:'WHOLE_TRIP',overnight_days:[],source_details:[]})
  result.coverage.unresolved_place_count += 1
  await show(page, result, fixture.supplementary, 1440)
  await expect(page.getByTestId('activity-card')).toHaveCount(3)
  const drawn = await png(page, info, 'pending-whole-trip-lodging.png')
  const lines = drawn.filter(item => item.text.includes('固定北京全程住宿酒店 ·'))
  expect(lines).toHaveLength(2)
  expect(lines[0].text).toContain('第1、2晚 · 已确认')
  expect(lines[1].text).toContain('具体夜晚未列明 · 地点待确认')
})
