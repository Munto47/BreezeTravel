const {test, expect} = require('@playwright/test')
const {execFileSync} = require('node:child_process')
const path = require('node:path')

// Execute the actual provider, pipeline and public projection. The first two
// model replies are synthetic; hotel, missing-name and truncated replies are saved real
// model outputs. All external place identities are fixed simulations. This
// checks downstream preservation and the page, not current live model accuracy.
let results
test.beforeAll(() => {
  results = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-m', 'tests.semantic_page_replays'], {
    cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8',
    env: {...process.env, RUNTIME_PROFILE: 'test', PYTHONPATH: '.', PYTHONIOENCODING: 'utf-8'},
  }))
})

async function show(page, kind, result = results[kind]) {
  const resource = `synthetic-semantic-${kind}`
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

async function exportObservedPng(page, info, filename) {
  await page.getByRole('button', {name: '导出图片', exact: true}).click()
  const preview = page.getByAltText('行程横链导出预览', {exact: true})
  await expect(preview).toBeVisible()
  await expect.poll(() => preview.evaluate(image => image.naturalWidth)).toBe(1440)
  await expect(page.getByRole('dialog')).not.toContainText('完整行程')
  const download = page.waitForEvent('download')
  await page.getByTestId('download-itinerary-png').click()
  const png = await download
  expect(await png.failure()).toBeNull()
  await png.saveAs(info.outputPath(`${filename}.png`))
  await page.screenshot({path: info.outputPath(`${filename}-preview.png`)})
  const rendered = await page.evaluate(() => window.exportText)
  expect(rendered.some(item => item.text === '行程查 · 行程概览')).toBe(true)
  expect(rendered.some(item => item.text.includes('完整行程') || item.text.includes('仅展示已匹配'))).toBe(false)
  expect(rendered.some(item => item.text === '地点状态见各卡片；原文备选与方案另列。路线时效与参观条件需另行核对。')).toBe(true)
  for (const item of rendered.filter(item => /尚未完整整理|待确认地点|原文未整理|尚无主线地点|未纳入主线/.test(item.text))) {
    expect(item.x, item.text).toBeGreaterThanOrEqual(0)
    expect(item.x + item.width, item.text).toBeLessThan(item.canvasWidth)
    expect(item.y, item.text).toBeGreaterThan(0)
    expect(item.y, item.text).toBeLessThan(item.canvasHeight)
  }
  await info.attach('png-drawn-text', {body: JSON.stringify(rendered), contentType: 'application/json'})
  return rendered.map(item => item.text)
}

test.describe('owner weak route modality through provider to page', () => {
  let replay
  const names = ['青溪公园', '星河博物馆', '云岭古镇', '望星塔', '月光公园']
  test.beforeAll(() => {
    // Owner rule 2026-09-08. A fixed model supplies PLANNED roles; no test
    // promotes its own OPTIONAL or REFERENCE response to make this pass.
    replay = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-c', `
import asyncio, json
from tests.test_experience_inference import Client
from tests.test_semantic_day_sections import provider
from tests.semantic_page_replays import FixedReplayPlaces
from app.trip_understanding.pipeline import TripUnderstandingPipeline

async def replay():
    source = ('北京一日游。\\nDay1：\\n上午先去青溪公园。\\n随后可以去星河博物馆。\\n'
        '中午：云岭古镇可顺路参观。\\n下午：望星塔可以打卡。\\n傍晚建议前往月光公园。\\n'
        '如果有空，可以去紫岚公园。\\n路线外资料举例：远山博物馆。')
    names = ['青溪公园', '星河博物馆', '云岭古镇', '望星塔', '月光公园']
    rows = [dict(source_quote=name, place_name=name, role=role, day_index=day,
        category='景点', city='北京', city_evidence='北京一日游')
        for name, role, day in [(name, 'PLANNED', 1) for name in names]
            + [('紫岚公园', 'OPTIONAL', 1), ('远山博物馆', 'REFERENCE', None)]]
    class Places(FixedReplayPlaces):
        def __init__(self):
            self.calls = []
        async def resolve(self, **query):
            self.calls.append(query['atomic_place_name'])
            return await super().resolve(**query)
    places = Places()
    model = Client(json.dumps(dict(destination='北京', day_labels=['Day1'], activities=rows), ensure_ascii=False))
    output = await TripUnderstandingPipeline(provider(model), places).run(source)
    assert len(model.calls) == 1
    assert places.calls == names
    assert [mention.role.value for mention in output.proposal.mentions] == ['PLANNED'] * 5 + ['OPTIONAL', 'REFERENCE']
    result = output.public_result.model_dump(mode='json')
    assert [card['name'] for card in result['days'][0]['activities']] == names
    assert [item['name'] for item in result['days'][0]['alternatives']] == ['紫岚公园']
    return dict(result=result, fixed_model_calls=len(model.calls), fixed_place_queries=places.calls,
        external_calls=0, evidence='FIXED_RAW_CURRENT_PROVIDER_PIPELINE_FIXED_IDENTITIES_NOT_LIVE_ACCURACY')

print(json.dumps(asyncio.run(replay()), ensure_ascii=False))
`], {cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8',
      env: {...process.env, RUNTIME_PROFILE: 'test', PYTHONPATH: '.', PYTHONIOENCODING: 'utf-8'},
    }))
  })

  for (const width of [1440, 390]) {
    test(`weak route advice remains ordinary main cards and PNG at ${width}px`, async ({page}, info) => {
      await page.setViewportSize({width, height: 900})
      await page.addInitScript(() => {
        window.exportText = []
        const original = CanvasRenderingContext2D.prototype.fillText
        CanvasRenderingContext2D.prototype.fillText = function (text, x, y, ...rest) {
          const position = this.getTransform().transformPoint({x, y})
          window.exportText.push({text: String(text), x: position.x, y: position.y,
            width: this.measureText(String(text)).width, canvasWidth: this.canvas.width, canvasHeight: this.canvas.height})
          return original.call(this, text, x, y, ...rest)
        }
      })
      expect(replay.external_calls).toBe(0)
      expect(replay.fixed_model_calls).toBe(1)
      expect(replay.fixed_place_queries).toEqual(names)
      await show(page, 'weak_route', replay.result)
      const cards = page.getByTestId('activity-card')
      await expect(cards).toHaveCount(5)
      await expect(cards.getByRole('heading')).toHaveText(names)
      for (const card of await cards.all()) {
        await expect(card).toContainText('已确认')
        await expect(card).not.toContainText(/备选|推荐|建议|可选/)
      }
      await expect(page.getByTestId('day-alternatives-1')).toHaveText('备选 · 1')
      await expect(page.getByTestId('itinerary-workspace')).not.toContainText('远山博物馆')
      await page.screenshot({path: info.outputPath(`weak-route-main-${width}.png`), fullPage: true})
      await page.getByTestId('day-alternatives-1').click()
      const panel = page.getByRole('complementary', {name: '检查与建议', exact: true})
      await expect(panel).toContainText('紫岚公园')
      await expect(panel.getByRole('button', {name: '加入待确认', exact: true})).toHaveCount(1)
      await expect(cards.filter({hasText: '紫岚公园'})).toHaveCount(0)
      await expect(page.locator('body')).not.toContainText('远山博物馆')
      await page.screenshot({path: info.outputPath(`weak-route-condition-${width}.png`), fullPage: true})
      await page.reload()
      await expect(cards.getByRole('heading')).toHaveText(names)
      const texts = await exportObservedPng(page, info, `weak-route-export-${width}`)
      for (const name of names) expect(texts).toContain(name)
      expect(texts).toContain('共 1 天 · 5 个地点 · 生成时地图底图未包含')
      expect(texts).toContain('备选与方案：1 处')
      expect(texts).not.toContain('紫岚公园')
      expect(texts).not.toContain('远山博物馆')
      expect(texts.filter(text => text === '已确认')).toHaveLength(5)
      await info.attach('weak-route-generated-public-result', {body: JSON.stringify(replay), contentType: 'application/json'})
    })
  }
})

test.describe('parent scope provider to page', () => {
  let invalidParentResult
  test.beforeAll(() => {
    // Reuse the known wrong-parent raw fixture, not a hand-written public result.
    // Both the actual provider adapter and the complete pipeline execute here;
    // Client and FixedReplayPlaces cannot call a model or a place service.
    invalidParentResult = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-c', `
import asyncio, json
from tests.test_internal_parent_scope import invalid_cases
from tests.test_experience_inference import Client
from tests.test_semantic_day_sections import provider
from tests.semantic_page_replays import FixedReplayPlaces
from app.trip_understanding.pipeline import TripUnderstandingPipeline

async def replay():
    source, rows, rejected_name = next(invalid_cases())
    assert rejected_name == '万春亭'
    client = Client(json.dumps(dict(destination='北京', activities=rows), ensure_ascii=False))
    output = await TripUnderstandingPipeline(provider(client), FixedReplayPlaces()).run(source)
    assert len(client.calls) == 1
    assert output.resolution_receipt['attempted_count'] == 2
    assert output.resolution_receipt['semantic_diagnostic_counts']['PARENT_RELATION_UNRESOLVED'] == 1
    return output.public_result.model_dump(mode='json')

print(json.dumps(asyncio.run(replay()), ensure_ascii=False))
`], {cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8',
      env: {...process.env, RUNTIME_PROFILE: 'test', PYTHONPATH: '.', PYTHONIOENCODING: 'utf-8'},
    }))
  })

  for (const width of [1440, 390]) {
    test(`wrong parent stays unfinished without attaching an unrelated inner visit at ${width}px`, async ({page}, info) => {
      await page.setViewportSize({width, height: 900})
      expect(invalidParentResult.status).toBe('PARTIAL_RESULT')
      expect(invalidParentResult.coverage.complete).toBe(false)
      expect(invalidParentResult.coverage.unprocessed_count).toBeGreaterThan(0)
      expect(invalidParentResult.days[0].activities.map(card => card.name)).toEqual(['故宫博物院', '景山公园'])
      expect(invalidParentResult.days[0].activities.map(card => card.source_details)).toEqual([
        [{name: '太和殿', optional: false}], [],
      ])
      expect(invalidParentResult.days[0].alternatives).toEqual([])
      await show(page, 'invalid_parent', invalidParentResult)
      await expect(page.getByTestId('activity-card')).toHaveCount(2)
      await expect(page.getByText('部分待补全', {exact: true})).toBeVisible()
      await expect(page.getByTestId('unmatched-places-note')).toContainText('尚未完整整理')
      await expect(page.getByTestId('day-unprocessed-1')).toContainText('1 处原文内容尚未整理完成')
      const parent = page.getByTestId('activity-card').filter({has: page.getByRole('heading', {name: '故宫博物院', exact: true})})
      await expect(parent).toContainText('原文安排 · 1 项')
      await parent.getByRole('button', {name: /故宫博物院.*查看详情/}).click()
      const details = page.getByTestId('source-internal-details')
      await expect(details.getByRole('listitem')).toHaveText(['太和殿'])
      await expect(details).toContainText('地点身份与开放情况未单独核验')
      await expect(page.getByTestId('itinerary-workspace')).not.toContainText('万春亭')
      await expect(page.locator('body')).not.toContainText('PARENT_RELATION_UNRESOLVED')
      await details.scrollIntoViewIfNeeded()
      await page.screenshot({path: info.outputPath(`parent-scope-details-${width}.png`)})
      await page.getByRole('button', {name: '收起地点确认', exact: true}).click()
      await page.reload()
      await expect(page.getByTestId('activity-card')).toHaveCount(2)
      await expect(page.getByTestId('day-unprocessed-1')).toContainText('1 处原文内容尚未整理完成')
      await expect(parent).toContainText('原文安排 · 1 项')
      await page.getByTestId('day-unprocessed-1').scrollIntoViewIfNeeded()
      await page.screenshot({path: info.outputPath(`parent-scope-unfinished-${width}.png`)})
      await info.attach('parent-scope-public-result', {body: JSON.stringify({
        evidence: 'synthetic raw through current provider and pipeline; fixed simulated place identities',
        result: invalidParentResult,
      }), contentType: 'application/json'})
    })
  }
})

test.describe('source visit second answer to saved page', () => {
  let replay
  test.beforeAll(() => {
    replay = JSON.parse(execFileSync(process.env.EXPERIENCE_PYTHON || 'python', ['-m', 'tests.source_visit_page_replay'], {
      cwd: path.resolve(__dirname, '../../backend'), encoding: 'utf8',
      env: {...process.env, RUNTIME_PROFILE: 'test', PYTHONPATH: '.', PYTHONIOENCODING: 'utf-8'},
    }))
  })

  for (const width of [1440, 390]) {
    test(`gates and visit purposes retain order, failed occurrence and saved readback at ${width}px`, async ({page}, info) => {
      await page.setViewportSize({width, height: 900})
      await page.addInitScript(() => {
        window.exportText = []
        const original = CanvasRenderingContext2D.prototype.fillText
        CanvasRenderingContext2D.prototype.fillText = function (text, x, y, ...rest) {
          const position = this.getTransform().transformPoint({x, y})
          window.exportText.push({text: String(text), x: position.x, y: position.y,
            width: this.measureText(String(text)).width, canvasWidth: this.canvas.width, canvasHeight: this.canvas.height})
          return original.call(this, text, x, y, ...rest)
        }
      })
      expect(replay.model_calls).toBe(2)
      expect(replay.external_calls).toBe(0)
      expect(replay.readback).toBe('MEMORY_API_PERSISTED_AND_REREAD')
      const result = replay.result
      expect(result.coverage.complete).toBe(false)
      expect(result.coverage.recognized_place_count).toBe(5)
      expect(result.days.map(day => day.unprocessed_count)).toEqual([0, 1])
      const checks = [
        [1, '故宫博物院', ['入口：午门', '乾清宫', '太和殿', '珍宝馆备选', '出口：神武门']],
        [1, '景山公园', ['仅看外观，不入内部']],
        [2, '故宫博物院', ['仅取物，不参观']],
        [2, '天坛公园', ['出口：北门']],
      ]
      await show(page, 'source_visit_second_answer', result)
      await expect(page.getByTestId('activity-card')).toHaveCount(5)
      await expect(page.getByTestId('day-lane-1').getByTestId('activity-card').getByRole('heading')).toHaveText([
        '南门涮肉', '故宫博物院', '景山公园',
      ])
      await expect(page.getByTestId('day-lane-2').getByTestId('activity-card').getByRole('heading')).toHaveText([
        '故宫博物院', '天坛公园',
      ])
      await expect(page.getByTestId('day-unprocessed-1')).toHaveCount(0)
      await expect(page.getByTestId('day-unprocessed-2')).toContainText('1 处原文内容尚未整理完成')

      async function inspectInstructions(day, name, expected, screenshot) {
        const parent = page.getByTestId(`day-lane-${day}`).getByTestId('activity-card')
          .filter({has: page.getByRole('heading', {name, exact: true})})
        await expect(parent).toContainText(`原文安排 · ${expected.length} 项`)
        await parent.scrollIntoViewIfNeeded()
        await parent.getByRole('button', {name: new RegExp(`${name}.*查看详情`)}).click()
        const details = page.getByTestId('source-internal-details')
        await expect(details.getByRole('listitem')).toHaveText(expected)
        await expect(details).toContainText('地点身份与开放情况未单独核验')
        await expect(details).not.toContainText('入口：南门')
        await details.scrollIntoViewIfNeeded()
        if (screenshot) await page.screenshot({path: info.outputPath(`source-visit-day${day}-${name}-${width}.png`)})
        await page.getByRole('button', {name: '收起地点确认', exact: true}).click()
      }

      for (const check of checks) await inspectInstructions(...check, true)
      await expect(page.locator('body')).not.toContainText('SOURCE_VISIT_UNRESOLVED')
      await expect(page.locator('body')).not.toContainText('parent_index')
      await page.reload()
      await expect(page.getByTestId('activity-card')).toHaveCount(5)
      await expect(page.getByTestId('day-unprocessed-2')).toContainText('1 处原文内容尚未整理完成')
      for (const check of checks) await inspectInstructions(...check, false)

      await page.getByRole('button', {name: '切换为列表', exact: true}).click()
      for (const [day, name, expected] of checks) {
        const list = page.getByRole('list', {name: `${result.days[day - 1].label} 地点列表`, exact: true})
        await list.getByRole('button', {name: new RegExp(`${name}.*已确认 · 可更改$`)}).click()
        await expect(page.getByTestId('source-internal-details').getByRole('listitem')).toHaveText(expected)
        await page.getByRole('button', {name: '收起地点确认', exact: true}).click()
      }
      await page.getByRole('button', {name: '切换为横链', exact: true}).click()
      const texts = await exportObservedPng(page, info, `source-visit-purposes-${width}`)
      expect(texts).toContain('共 2 天 · 5 个地点 · 生成时地图底图未包含')
      expect(texts.filter(text => text === '已确认')).toHaveLength(5)
      expect(texts).toContain('原文尚未完整整理，请返回行程补全')
      expect(texts).toContain('原文未整理：1 处')
      expect(texts.filter(text => text === '原文安排 · 门口及内部地点未单独核验')).toHaveLength(2)
      expect(texts.filter(text => /^\d+\. /.test(text))).toEqual([
        '1. 入口：午门', '2. 乾清宫', '3. 太和殿', '4. 珍宝馆（备选）', '5. 出口：神武门',
        '1. 仅看外观，不入内部', '1. 仅取物，不参观', '1. 出口：北门',
      ])
      expect(texts.some(text => text.includes('入口：南门'))).toBe(false)
      const drawn = await page.evaluate(() => window.exportText.filter(item => /^\d+\. |原文安排/.test(item.text)))
      for (const item of drawn) {
        expect(item.x, item.text).toBeGreaterThanOrEqual(0)
        expect(item.x + item.width, item.text).toBeLessThan(item.canvasWidth)
        expect(item.y, item.text).toBeGreaterThan(0)
        expect(item.y, item.text).toBeLessThan(item.canvasHeight - 64)
      }
      await info.attach('source-visit-two-answer-memory-readback', {
        body: JSON.stringify(replay), contentType: 'application/json',
      })
    })
  }
})

for (const width of [1440, 390]) {
  test(`source internal arrangements: parent details survive page, refresh and PNG at ${width}px`, async ({page}, info) => {
    await page.setViewportSize({width, height: 900})
    await page.addInitScript(() => {
      window.exportText = []
      const original = CanvasRenderingContext2D.prototype.fillText
      CanvasRenderingContext2D.prototype.fillText = function (text, x, y, ...rest) {
        const position = this.getTransform().transformPoint({x, y})
        window.exportText.push({text: String(text), x: position.x, y: position.y,
          width: this.measureText(String(text)).width, canvasWidth: this.canvas.width, canvasHeight: this.canvas.height})
        return original.call(this, text, x, y, ...rest)
      }
    })
    const result = results.source_details
    expect(result.days[0].activities.map(card => card.name)).toEqual(['故宫博物院', '景山公园'])
    expect(result.days[0].alternatives).toEqual([])
    expect(result.coverage.recognized_place_count).toBe(2)
    expect(result.coverage.complete).toBe(true)
    const expected = [{name: '太和殿', optional: false}, {name: '乾清宫', optional: false}, {name: '珍宝馆', optional: true}]
    expect(result.days[0].activities[0].source_details).toEqual(expected)
    await show(page, 'source_details')
    const parent = page.getByTestId('activity-card').filter({has: page.getByRole('heading', {name: '故宫博物院', exact: true})})
    await expect(page.getByTestId('activity-card')).toHaveCount(2)
    await expect(parent).toContainText('原文安排 · 3 项')
    await parent.getByRole('button', {name: /故宫博物院.*查看详情/}).click()
    const details = page.getByTestId('source-internal-details')
    await expect(details).toBeVisible()
    await expect(details.getByRole('listitem')).toHaveText(['太和殿', '乾清宫', '珍宝馆备选'])
    await expect(details).toContainText('地点身份与开放情况未单独核验')
    await details.scrollIntoViewIfNeeded()
    await page.screenshot({path: info.outputPath(`source-details-${width}.png`)})
    await page.getByRole('button', {name: '收起地点确认', exact: true}).click()
    await page.reload()
    await expect(page.getByTestId('activity-card')).toHaveCount(2)
    await parent.getByRole('button', {name: /故宫博物院.*查看详情/}).click()
    await expect(details.getByRole('listitem')).toHaveText(['太和殿', '乾清宫', '珍宝馆备选'])
    await page.getByRole('button', {name: '收起地点确认', exact: true}).click()
    await page.getByRole('button', {name: '切换为列表', exact: true}).click()
    const list = page.getByRole('list', {name: `${result.days[0].label} 地点列表`, exact: true})
    await expect(list.getByRole('button', {name: /已确认 · 可更改$/})).toHaveCount(2)
    await list.getByRole('button', {name: /故宫博物院/}).click()
    await expect(details.getByRole('listitem')).toHaveText(['太和殿', '乾清宫', '珍宝馆备选'])
    await page.getByRole('button', {name: '收起地点确认', exact: true}).click()
    await page.getByRole('button', {name: '切换为横链', exact: true}).click()
    const texts = await exportObservedPng(page, info, `source-arrangements-${width}`)
    expect(texts).toContain('共 1 天 · 2 个地点 · 生成时地图底图未包含')
    expect(texts.filter(text => text === '已确认')).toHaveLength(2)
    expect(texts).toContain('原文安排 · 门口及内部地点未单独核验')
    expect(texts).toContain('故宫博物院：')
    const sourceNames = texts.filter(text => /^\d+\. /.test(text))
    expect(sourceNames).toEqual(['1. 太和殿', '2. 乾清宫', '3. 珍宝馆（备选）'])
    const drawn = await page.evaluate(() => window.exportText.filter(item => /^\d+\. |原文安排|故宫博物院：/.test(item.text)))
    for (const item of drawn) {
      expect(item.x + item.width, item.text).toBeLessThan(item.canvasWidth)
      expect(item.y, item.text).toBeLessThan(item.canvasHeight - 64)
    }
  })
}

for (const [kind, width] of [['truncated_whole', 1440], ['pending_semantics', 390], ['optional', 390], ['long_names', 1440]]) {
  test(`PNG completion status: ${kind} at ${width}px`, async ({page}, info) => {
    await page.setViewportSize({width, height: 900})
    // Observe the actual canvas without replacing drawing, encoding or download.
    await page.addInitScript(() => {
      window.exportText = []
      const original = CanvasRenderingContext2D.prototype.fillText
      CanvasRenderingContext2D.prototype.fillText = function (text, x, y, ...rest) {
        const position = this.getTransform().transformPoint({x, y})
        window.exportText.push({text: String(text), x: position.x, y: position.y,
          font: this.font, width: this.measureText(String(text)).width, canvasWidth: this.canvas.width, canvasHeight: this.canvas.height})
        return original.call(this, text, x, y, ...rest)
      }
    })
    const result = structuredClone(results[['pending_semantics', 'long_names'].includes(kind) ? 'lodging' : kind])
    if (kind === 'long_names') {
      // Synthetic public-layout stress only: eight 40-character labels force
      // wrapped names and a second snake row. These are not real place claims.
      const template = result.days[0].activities.find(card => card.status === 'READY')
      result.days[0].activities = Array.from({length: 8}, (_, index) => ({...template,
        activity_token: `synthetic-export-long-${index}`,
        name: '长名称布局验证'.repeat(6).slice(0, 39) + String(index), source_details: [],
      }))
    }
    if (kind === 'pending_semantics') {
      // Fixed public-state counterexample, not a claim about the saved raw:
      // independent semantic and identity warnings must both survive export.
      result.days[0].activities[0].status = 'NEEDS_CONFIRMATION'
      result.days.forEach(day => { day.unprocessed_count = 0 })
      result.coverage = {...result.coverage, confirmed_place_count: 8, unresolved_place_count: 1,
        unprocessed_count: 0, unclassified_mention_count: 1, complete: false}
      result.map.status = 'NEEDS_UPDATE'
    }
    await show(page, kind, result)
    const texts = await exportObservedPng(page, info, `honest-${kind}-${width}`)
    const drawn = await page.evaluate(() => window.exportText)
    const drawnNames = []
    let name = ''
    for (const item of [...drawn, {}]) {
      if (/^(?:bold|700) 15px/.test(item.font || '') && item.x > 176) {
        name += item.text
        expect(item.text).not.toContain('…')
        expect(item.width).toBeLessThanOrEqual(152)
        expect(item.y).toBeLessThan(item.canvasHeight - 64)
      } else if (name) {
        drawnNames.push(name)
        name = ''
      }
    }
    expect(drawnNames).toEqual(result.days.flatMap(day => day.activities.filter(card => card.status === 'READY').map(card => card.name)))
    for (const day of result.days) {
      expect(texts.filter(text => text === day.label)).toHaveLength(1)
    }
    if (kind === 'truncated_whole') {
      expect(result.days).toHaveLength(14)
      expect(result.days.flatMap(day => day.activities)).toHaveLength(63)
      expect(result.coverage.unprocessed_count).toBe(54)
      expect(texts).toContain('原文尚未完整整理，请返回行程补全')
      expect(texts).toContain('原文未整理：5 处')
      expect(texts.filter(text => text === '原文未整理：6 处 · 尚无主线地点')).toHaveLength(8)
      expect(texts.filter(text => text === '已确认')).toHaveLength(63)
    } else if (kind === 'pending_semantics') {
      expect(texts).toContain('原文尚未完整整理，请返回行程补全')
      expect(texts).toContain('待确认地点：1 处，请返回行程确认')
      expect(texts).not.toContain('待确认地点：2 处，请返回行程确认')
      // The real page passes only confirmed mainline cards to the exporter;
      // coverage must still disclose the pending place that is not in its cards.
      expect(texts).not.toContain(result.days[0].activities[0].name)
      expect(texts.filter(text => text === '待确认')).toHaveLength(0)
      expect(texts.filter(text => text === '已确认')).toHaveLength(8)
      expect(texts).toContain('路线状态：行程已调整，需要更新')
      expect(texts).toContain('路线需要更新')
    } else if (kind === 'optional') {
      expect(result.days).toHaveLength(2)
      expect(result.days[1].activities).toHaveLength(0)
      expect(result.days[1].alternatives).toHaveLength(1)
      expect(texts).toContain('备选与方案：1 处 · 尚无主线地点')
      expect(texts).not.toContain('月光桥')
      expect(texts).not.toContain('原文尚未完整整理，请返回行程补全')
      expect(texts.filter(text => text === '已确认')).toHaveLength(1)
    } else {
      expect(drawnNames.slice(0, 8).every(name => name.length === 40)).toBe(true)
      const lastLines = drawn.filter(item => /^(?:bold|700) 15px/.test(item.font || '') && item.x > 176)
      expect(lastLines.length).toBeGreaterThan(drawnNames.length)
    }
  })
}

for (const width of [1440, 390]) {
  test(`saved truncated whole response to page: all fourteen days retain honest completion counts at ${width}px`, async ({page}, info) => {
    await page.setViewportSize({width, height: 900})
    await show(page, 'truncated_whole')
    const result = results.truncated_whole
    const fixture = require('../../backend/tests/fixtures/live_capacity_day_structure.json')
    const names = fixture.source.split('\n')[1].split('：')[1].replace(/。$/, '').split('、')
    expect(names).toHaveLength(12)
    expect(result.status).toBe('PARTIAL_RESULT')
    expect(result.coverage.complete).toBe(false)
    expect(result.coverage.unprocessed_count).toBe(54)
    expect(result.days).toHaveLength(14)
    expect(result.days.map(day => day.unprocessed_count)).toEqual([0, 0, 0, 0, 0, 5, 6, 6, 6, 6, 6, 6, 6, 6])
    await expect(page.getByText('部分待补全', {exact: true})).toBeVisible()
    await expect(page.getByTestId('activity-card')).toHaveCount(63)
    await expect(page.getByTestId('unmatched-places-note')).toContainText('尚未完整整理')
    for (let day = 1; day <= 14; day++) {
      const lane = page.getByTestId(`day-lane-${day}`)
      await expect(lane).toBeVisible()
      await expect(lane.getByTestId('activity-card').getByRole('heading')).toHaveText(day <= 5 ? names : day === 6 ? names.slice(0, 3) : [])
      const warning = page.getByTestId(`day-unprocessed-${day}`)
      if (day <= 5) await expect(warning).toHaveCount(0)
      else await expect(warning).toContainText(`${day === 6 ? 5 : 6} 处原文内容尚未整理完成`)
    }
    await page.getByTestId('day-unprocessed-6').scrollIntoViewIfNeeded()
    await expect(page.getByTestId('day-unprocessed-6')).toBeInViewport()
    await page.screenshot({path: info.outputPath('truncated-day-six-warning.png')})
    await page.getByTestId('day-unprocessed-14').scrollIntoViewIfNeeded()
    await expect(page.getByTestId('day-unprocessed-14')).toBeInViewport()
    await expect(page.getByTestId('day-lane-14').getByTestId('activity-card')).toHaveCount(0)
    await page.screenshot({path: info.outputPath('truncated-last-empty-day-warning.png')})
    await info.attach('saved-truncation-page-counts', {body: JSON.stringify({
      source: 'saved real truncated response; fixed simulated place identities',
      confirmedCards: 63, days: 14, globalUnprocessed: result.coverage.unprocessed_count,
      dayUnprocessed: result.days.map(day => day.unprocessed_count), complete: result.coverage.complete,
    }), contentType: 'application/json'})
  })

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
