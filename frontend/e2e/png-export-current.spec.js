const {test, expect} = require('@playwright/test')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')

test.use({viewport: {width: 1440, height: 1000}, actionTimeout: 15000})
const RESOURCE = 'fixed-current-png-resource-0001'
const PIXEL = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aMZkAAAAASUVORK5CYII='
const card = (index, changes = {}) => ({activity_token: `fixed-png-card-${index}`, name: `完整名称公园${index}`,
  category: '景点', city: '北京', area_or_address: '固定地点地址', status: 'READY', photo_url: PIXEL,
  time_hint: null, available_actions: ['VIEW_DETAILS', 'REPLACE', 'DELETE', 'MOVE'], ...changes})

function fixture(capacity = false) {
  const result = {status: capacity ? 'READY' : 'PARTIAL_RESULT', can_undo: true, can_redo: false, ownership: 'ANONYMOUS',
    assumptions: [{key: 'destination', label: '目的地', value: '北京', editable: true}],
    coverage: {recognized_place_count: capacity ? 160 : 3, confirmed_place_count: capacity ? 160 : 2,
      unresolved_place_count: capacity ? 0 : 1, unprocessed_count: capacity ? 0 : 2, unclassified_mention_count: 0, complete: capacity},
    days: capacity ? Array.from({length: 14}, (_, day) => ({label: `Day ${day + 1}`,
      activities: Array.from({length: day < 6 ? 12 : 11}, (_, position) => card(day < 6 ? day * 12 + position : 72 + (day - 6) * 11 + position))})) : [
      {label: 'Day 1', unprocessed_count: 2, activities: [card(0, {name: '馆内餐厅', category: '餐饮', meal_role: 'LUNCH',
        dining_access: {status: 'DURING_VISIT', parent_name: '固定博物馆'}, meal_evidence_status: 'LIGHT_FOOD_ITEMS_ONLY',
        source_details: [{name: '内部展厅', optional: false}, {name: '花园支线', optional: true}]}), card(1, {status: 'NEEDS_CONFIRMATION'})],
        meal_slots: [{meal_role: 'LUNCH', selection_status: 'SELECTED', selected_activity_token: 'fixed-png-card-0', preference_text: '清淡不辣'}],
        alternatives: [{name: '雨天美术馆', category: '景点', replaces_name: '完整名称公园1', replacement_condition: '下雨时',
          branch_label: '方案 B', source_details: [{name: '馆内备选展厅', optional: true}]}]},
      {label: 'Day 2', activities: [card(2)]},
    ],
    map: {status: 'NEEDS_UPDATE', message: '请更新路线', available_actions: ['RENDER_MAP']},
    stay: {status: 'LIMITED', message: '通勤资料不完整', candidates: [], searched_scopes: [], area_summary: null, available_actions: [],
      segments: [{segment_token: 'fixed-png-stay', city: '北京', overnight_days: ['Day 1'], status: 'LIMITED', message: '部分通勤',
        candidates: [{candidate_token: 'selected-hotel', name: '已经选择的完整酒店名称', category: '住宿', brand: '固定品牌',
          area_or_address: '北京固定酒店完整地址', selected: true, commute_summary: '旧不完整资料 999 分钟',
          max_single_leg_minutes: null, transfer_count: 0, reason: '固定酒店', available_actions: []},
        {candidate_token: 'unselected-hotel', name: '未选择酒店不能导出', selected: false}]}]},
    available_actions: ['EDIT_ASSUMPTIONS', 'EDIT_CARDS'],
  }
  return result
}

async function show(page, options = {}) {
  const state = {result: fixture(options.capacity), version: 0, writes: []}
  if (options.stall) state.result.days.forEach(day => day.activities.forEach(card => {card.photo_url = `https://photos.invalid/${card.activity_token}.jpg`}))
  await page.addInitScript(() => {
    window.pngDrawn = []
    window.pngImages = {stall: true, attempted: 0, active: 0, maximum: 0, cancelled: 0, drawn: 0}
    const fill = CanvasRenderingContext2D.prototype.fillText
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y, ...rest) {
      const point = this.getTransform().transformPoint({x, y})
      window.pngDrawn.push({text: String(text), x: point.x, y: point.y, width: this.measureText(String(text)).width * this.getTransform().a,
        canvasWidth: this.canvas.width, canvasHeight: this.canvas.height})
      return fill.call(this, text, x, y, ...rest)
    }
    const draw = CanvasRenderingContext2D.prototype.drawImage
    CanvasRenderingContext2D.prototype.drawImage = function(...args) {window.pngImages.drawn++; return draw.apply(this, args)}
    const NativeImage = window.Image
    const src = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src')
    window.Image = function(...args) {
      const image = new NativeImage(...args)
      let held = false
      Object.defineProperty(image, 'src', {get() {return src.get.call(image)}, set(value) {
        if (window.pngImages.stall && (value.startsWith('https://photos.invalid/') || value.includes('/photo?'))) {
          held = true; window.pngImages.attempted++; window.pngImages.active++
          window.pngImages.maximum = Math.max(window.pngImages.maximum, window.pngImages.active)
          return
        }
        src.set.call(image, value)
      }})
      const remove = image.removeAttribute.bind(image)
      image.removeAttribute = name => {
        if (name === 'src' && held) {held = false; window.pngImages.active--; window.pngImages.cancelled++}
        remove(name)
      }
      return image
    }
  })
  await page.route('**/*', route => {
    const request = route.request(), url = new URL(request.url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.fallback()
    const reply = json => route.fulfill({json, headers: {ETag: `"fixed-png-${state.version}"`}})
    if (request.method() !== 'GET') state.writes.push(url.pathname)
    if (url.pathname.endsWith('/result')) return reply(state.result)
    if (url.pathname.endsWith('/commands')) {
      expect(request.postDataJSON()).toEqual({command_type: 'UNDO'})
      state.version++; state.result.days[0].activities[0].name = '撤销后的当前地点'
      return reply({status: 'APPLIED', changed_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE'})
    }
    if (url.pathname.endsWith('/supplementary')) return reply(options.supplementary || {status: 'AVAILABLE', days: [
      {day_index: null, day_label: '未指定日期', items: [{name: '未分配日序的动物园', role: 'OPTIONAL', time_hint: '18:00'}]},
      {day_index: 1, day_label: 'Day 1', items: [{name: '原文取消的购物街', role: 'EXCLUDED', time_hint: null}]},
    ]})
    if (url.pathname.endsWith('/map-renders/latest')) return reply({...state.result.map, days: [], points: []})
    if (url.pathname.endsWith('/stay-suggestions')) return reply(state.result.stay)
    if (url.pathname.endsWith('/daily-dining')) return reply({status: 'UNAVAILABLE', message: '未准备餐饮', days: []})
    if (url.pathname.endsWith('/materialize')) return reply({status: 'READY', calendar: '相对日序', party_size: 2, checks_available: true})
    if (url.pathname.endsWith('/checks')) return reply({status: 'STILL_NEEDS_CONFIRMATION', message: '尚有待确认内容', items: [], remaining_must_adjust: 0, available_actions: []})
    return route.fulfill({status: 404, json: {}})
  })
  await page.goto(`/trip/result#trip=${RESOURCE}`)
  await expect(page.getByTestId('export-itinerary-png')).toBeEnabled()
  return state
}

async function capture(page, info, filename) {
  await expect(page.getByTestId('png-preview')).toBeVisible({timeout: 15000})
  const drawn = await page.evaluate(() => window.pngDrawn)
  for (const row of drawn) {
    expect(row.x, row.text).toBeGreaterThanOrEqual(0)
    expect(row.x + row.width, row.text).toBeLessThanOrEqual(row.canvasWidth)
    expect(row.y, row.text).toBeGreaterThan(0)
    expect(row.y, row.text).toBeLessThan(row.canvasHeight)
  }
  const download = page.waitForEvent('download')
  await page.getByTestId('download-itinerary-png').click()
  await (await download).saveAs(info.outputPath(filename))
  return drawn.map(row => row.text).join('\n')
}

test('PNG contains actual confirmation counts, dining, lodging and source limitations', async ({page}, info) => {
  await show(page)
  await page.getByTestId('export-itinerary-png').click()
  const text = await capture(page, info, 'complete-arrangements.png')
  for (const value of ['3 个安排 · 2 个已确认', '原文尚未完整整理', '待确认地点：1 处', '原文未整理：2 处',
    '路线需要更新', '1. 内部展厅', '2. 花园支线（备选）', '未单独核验', '场所归属：固定博物馆', '离开前用餐',
    '进入条件、可用门区与营业情况尚未核实', '正餐供应尚未核实', '午餐 · 已安排：「馆内餐厅」', '清淡不辣',
    '备选 · 方案 B：雨天美术馆（下雨时替换完整名称公园1）', '馆内备选展厅（备选）', '原文已取消：原文取消的购物街',
    '未指定日期', '未分配日序的动物园', '已经选择的完整酒店名称', '北京固定酒店完整地址',
    '第 1 晚（Day 1 → Day 2）', '住宿通勤仅有部分资料，尚未完整核实']) expect(text).toContain(value)
  expect(text).not.toContain('未选择酒店不能导出')
  expect(text).not.toContain('999')
  expect(text).not.toContain('18:00')
})

for (const stall of [false, true]) test(`all 160 visits and 14 days export with ${stall ? 'stalled' : 'working'} photographs`, async ({page}, info) => {
  const state = await show(page, {capacity: true, stall})
  if (stall) await page.clock.install()
  const writes = state.writes.length
  await page.getByTestId('export-itinerary-png').click()
  if (stall) {
    await expect.poll(() => page.evaluate(() => window.pngImages.active)).toBe(6)
    await page.clock.fastForward(8000)
  }
  const text = await capture(page, info, `capacity-${stall ? 'stalled' : 'photos'}.png`)
  expect(text.split('\n').filter(line => /^完整名称公园\d+$/.test(line)))
    .toEqual(Array.from({length: 160}, (_, index) => `完整名称公园${index}`))
  for (let day = 1; day <= 14; day++) expect(text).toContain(`Day ${day}\n`)
  expect(text).toContain('160 个安排 · 160 个已确认')
  const images = await page.evaluate(() => window.pngImages)
  if (stall) expect(images).toMatchObject({attempted: 6, maximum: 6, active: 0, cancelled: 6, drawn: 0})
  else expect(images.drawn).toBe(160)
  expect(state.writes.slice(writes)).toEqual([])
})

test('cancelling photo preparation releases requests and a later export succeeds', async ({page}, info) => {
  await show(page, {stall: true})
  await page.getByTestId('export-itinerary-png').click()
  await expect.poll(() => page.evaluate(() => window.pngImages.active)).toBe(3)
  await expect(page.getByRole('status').filter({hasText: '正在准备照片'})).toBeVisible()
  await page.getByTestId('cancel-itinerary-png').click()
  await expect(page.getByRole('alert').filter({hasText: '已取消图片生成'})).toBeVisible()
  expect(await page.evaluate(() => window.pngImages.active)).toBe(0)
  await expect(page.getByTestId('png-preview')).toHaveCount(0)
  await page.evaluate(() => {window.pngImages.stall = false})
  await page.getByTestId('export-itinerary-png').click()
  expect(await capture(page, info, 'after-cancel.png')).toContain('馆内餐厅')
})

test('an edit cancels the old export and only the new itinerary can be downloaded', async ({page}, info) => {
  await show(page, {stall: true})
  await page.getByTestId('export-itinerary-png').click()
  await expect.poll(() => page.evaluate(() => window.pngImages.active)).toBe(3)
  await page.getByTestId('undo-trip-command').click()
  await expect(page.getByRole('alert').filter({hasText: '行程已经更新'})).toBeVisible()
  await expect(page.getByTestId('png-preview')).toHaveCount(0)
  expect(await page.evaluate(() => window.pngImages.active)).toBe(0)
  await page.evaluate(() => {window.pngImages.stall = false})
  await page.getByTestId('export-itinerary-png').click()
  expect(await capture(page, info, 'current-version.png')).toContain('撤销后的当前地点')
})

test('unmounting the result page stops photo work without opening a late preview', async ({page}) => {
  await show(page, {stall: true})
  await page.getByTestId('export-itinerary-png').click()
  await expect.poll(() => page.evaluate(() => window.pngImages.active)).toBe(3)
  await page.getByRole('link', {name: '行程查首页', exact: true}).click()
  await expect(page).toHaveURL(/\/$/)
  await expect.poll(() => page.evaluate(() => window.pngImages.active)).toBe(0)
  await page.goBack()
  await expect(page.getByTestId('png-preview')).toHaveCount(0)
  await expect(page.getByTestId('export-itinerary-png')).toBeEnabled()
})

test('unavailable source supplement is explicit in the downloaded image', async ({page}, info) => {
  await show(page, {supplementary: {status: 'UNAVAILABLE', days: []}})
  await page.getByTestId('export-itinerary-png').click()
  expect(await capture(page, info, 'unavailable-supplement.png')).toContain('补充安排暂时无法读取，本图可能缺少原文备选或取消内容')
})

function componentBundle() {
  const names = ['itinerary-png-export', 'share-image', 'share-image-content', 'share-image-photos', 'lodging-export',
    'source-meals', 'result-presentation', 'dining-access']
  const files = names.map(name => [`./${name}`, path.resolve(__dirname, '../src/app/trip/result', `${name}.${['itinerary-png-export', 'dining-access'].includes(name) ? 'tsx' : 'ts'}`)])
  files.push(['@/lib/place-type-photo', path.resolve(__dirname, '../src/lib/place-type-photo.ts')])
  const modules = files.map(([name, file]) => `${JSON.stringify(name)}:function(module,exports,require){${ts.transpileModule(fs.readFileSync(file, 'utf8'),
    {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX}}).outputText}\n}`).join(',')
  return `(function(){const modules={${modules}},cache={};function require(name){
    if(name==='react')return React;
    if(name==='react/jsx-runtime')return {jsx:(type,props,key)=>React.createElement(type,{...props,key}),jsxs:(type,props,key)=>React.createElement(type,{...props,key}),Fragment:React.Fragment};
    if(name==='lucide-react')return {Download:()=>null,Image:()=>null,X:()=>null};
    if(name==='./accessible-dialog')return {default:({children})=>React.createElement('div',{role:'dialog'},children)};
    if(name.endsWith('.css'))return {};
    if(!cache[name]){const module={exports:{}};cache[name]=module;modules[name](module,module.exports,require)}
    return cache[name].exports;
  }window.PngExport=require('./itinerary-png-export').default;})();`
}

test('same-version route and lodging detail updates invalidate the exported preview', async ({page}, info) => {
  await page.setContent('<div id="root"></div>')
  await page.addScriptTag({path: path.resolve(__dirname, '../node_modules/react/umd/react.development.js')})
  await page.addScriptTag({path: path.resolve(__dirname, '../node_modules/react-dom/umd/react-dom.development.js')})
  await page.addScriptTag({content: componentBundle()})
  const result = fixture()
  result.map.status = 'AVAILABLE'
  result.days[0].activities[1].status = 'READY'
  result.days[0].alternatives[0] = {...result.days[0].alternatives[0], branch_token: 'branch-b', choice_group_token: 'group-b'}
  result.days[0].choice_selections = [{branch_token: 'branch-b', choice_group_token: 'group-b', activity_tokens: ['fixed-png-card-0'], status: 'MODIFIED'}]
  const map = {status: 'AVAILABLE', message: '已准备', available_actions: [], days: [{label: 'Day 1', routes: [{
    from_activity_token: 'fixed-png-card-0', to_activity_token: 'fixed-png-card-1', from_name: '馆内餐厅', to_name: '完整名称公园1',
    selected_mode: 'walking', walking: {status: 'AVAILABLE', duration_minutes: 10, distance_meters: 500, connection_status: 'VERIFIED', geometry: [], geometry_break_indices: []},
    transit: {status: 'UNAVAILABLE'},
  }]}]}
  await page.evaluate(({result, map}) => {
    window.pngDrawn = []
    const fill = CanvasRenderingContext2D.prototype.fillText
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y, ...rest) {
      window.pngDrawn.push({text: String(text), x, y, width: this.measureText(String(text)).width, canvasWidth: this.canvas.width, canvasHeight: this.canvas.height})
      return fill.call(this, text, x, y, ...rest)
    }
    window.pngProps = {result, resource: 'fixed', etag: 'same-version', disabled: false, mapView: map,
      supplementary: {status: 'AVAILABLE', days: []}, stayView: structuredClone(result.stay)}
    window.pngProps.stayView.status = 'AVAILABLE'
    window.pngProps.stayView.segments[0].status = 'AVAILABLE'
    window.pngProps.stayView.segments[0].candidates[0].max_single_leg_minutes = 10
    window.pngProps.stayView.segments[0].candidates[0].commute_summary = '已核实酒店通勤 10 分钟'
    window.pngRoot = ReactDOM.createRoot(document.getElementById('root'))
    window.renderPng = () => window.pngRoot.render(React.createElement(window.PngExport, window.pngProps))
    window.renderPng()
  }, {result, map})
  await page.getByTestId('export-itinerary-png').click()
  const before = await capture(page, info, 'same-version-before.png')
  expect(before).toContain('已核实酒店通勤 10 分钟')
  expect(before).toContain('已调整，原方案供对照 · 方案 B')
  expect(before).not.toContain('已选方案 · 方案 B')
  await page.evaluate(() => {
    window.pngProps = {...window.pngProps, mapView: structuredClone(window.pngProps.mapView)}
    window.pngProps.mapView.days[0].routes[0].walking.duration_minutes = 20
    window.renderPng()
  })
  await expect(page.getByTestId('png-preview')).toHaveCount(0)
  await page.getByTestId('export-itinerary-png').click()
  expect(await capture(page, info, 'same-version-route.png')).toContain('步行 · 20 分钟')
  await page.evaluate(() => {
    window.pngProps = {...window.pngProps, stayView: structuredClone(window.pngProps.stayView)}
    window.pngProps.stayView.segments[0].candidates[0].commute_summary = '已核实酒店通勤 25 分钟'
    window.renderPng()
  })
  await expect(page.getByTestId('png-preview')).toHaveCount(0)
  await page.getByTestId('export-itinerary-png').click()
  expect(await capture(page, info, 'same-version-stay.png')).toContain('已核实酒店通勤 25 分钟')
})

test('selected lodging returns to PNG after an empty preparing summary', async ({page}, info) => {
  await page.setContent('<div id="root"></div>')
  await page.addScriptTag({path: path.resolve(__dirname, '../node_modules/react/umd/react.development.js')})
  await page.addScriptTag({path: path.resolve(__dirname, '../node_modules/react-dom/umd/react-dom.development.js')})
  await page.addScriptTag({content: componentBundle()})
  const result = fixture()
  const restoredStay = structuredClone(result.stay)
  result.stay = {...result.stay, status: 'PREPARING', candidates: [], segments: []}
  await page.evaluate(({result}) => {
    window.pngDrawn = []
    const fill = CanvasRenderingContext2D.prototype.fillText
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y, ...rest) {
      window.pngDrawn.push({text: String(text), x, y, width: this.measureText(String(text)).width, canvasWidth: this.canvas.width, canvasHeight: this.canvas.height})
      return fill.call(this, text, x, y, ...rest)
    }
    window.pngProps = {result, resource: 'fixed', etag: 'same-version', disabled: false, mapView: null,
      supplementary: {status: 'AVAILABLE', days: []}, stayView: structuredClone(result.stay)}
    window.pngRoot = ReactDOM.createRoot(document.getElementById('root'))
    window.renderPng = () => window.pngRoot.render(React.createElement(window.PngExport, window.pngProps))
    window.renderPng()
  }, {result})
  await expect(page.getByTestId('export-itinerary-png')).toBeEnabled()
  await page.evaluate(stay => {
    window.pngProps = {...window.pngProps, stayView: stay}
    window.renderPng()
  }, restoredStay)
  await page.getByTestId('export-itinerary-png').click()
  const restored = await capture(page, info, 'restored-selected-stay.png')
  for (const value of ['已经选择的完整酒店名称', '北京固定酒店完整地址', '第 1 晚（Day 1 → Day 2）',
    '住宿通勤仅有部分资料，尚未完整核实']) expect(restored).toContain(value)
  expect(restored).not.toContain('未选择酒店不能导出')
  expect(restored).not.toContain('999')

  for (const status of ['PREPARING', 'UNAVAILABLE']) {
    await page.evaluate(({stay, status}) => {
      const known = structuredClone(stay)
      known.status = 'AVAILABLE'
      known.segments[0].status = 'AVAILABLE'
      known.segments[0].candidates[0].max_single_leg_minutes = 12
      known.segments[0].candidates[0].commute_summary = '旧酒店通勤 12 分钟'
      window.pngProps = {...window.pngProps, result: {...window.pngProps.result, stay: known},
        stayView: {...known, status, candidates: [], segments: []}}
      window.pngDrawn = []
      window.renderPng()
    }, {stay: restoredStay, status})
    await expect(page.getByTestId('png-preview')).toHaveCount(0)
    await page.getByTestId('export-itinerary-png').click()
    const unavailable = await capture(page, info, `retained-stay-${status.toLowerCase()}.png`)
    expect(unavailable).toContain('已经选择的完整酒店名称')
    expect(unavailable).toContain(status === 'PREPARING' ? '住宿建议仍在准备，通勤尚需核对' : '住宿通勤资料暂不可用')
    expect(unavailable).not.toContain('未选择酒店不能导出')
    expect(unavailable).not.toContain('12 分钟')
  }
})
