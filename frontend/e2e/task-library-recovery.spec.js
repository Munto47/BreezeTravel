const { test, expect } = require('@playwright/test')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')

const refs = Object.fromEntries(['PROCESSING', 'PARTIAL', 'READY', 'FAILED', 'CANCELLED'].map((state) => [state, `task_${state.toLowerCase()}_` + 'a'.repeat(24)]))
const rows = Object.entries(refs).map(([state, ref]) => ({ public_resource_id: ref,
  title: `${state === 'FAILED' ? '失败' : state === 'CANCELLED' ? '停止' : state === 'PROCESSING' ? '后台' : '已保存'}测试行程`,
  state, has_result: ['PARTIAL', 'READY'].includes(state), source_status: 'AVAILABLE', city: '广州',
  day_count: 0, is_demo: false, updated_at: new Date(Date.now() - 60000).toISOString(),
  expires_at: new Date(Date.now() + 86400000).toISOString() }))
const sourceText = '广州。先去沙面，再去越秀公园。'

// Every product API is intercepted; unmatched calls are visible failures, never live requests.
test.beforeEach(async ({ context }) => {
  await context.route('**/api/**', (route) => route.fulfill({ status: 404, json: { detail: { code: 'FIXED_TEST_ONLY' } } }))
})

async function account(page) {
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'fixed-ui-no-real-account')
    localStorage.setItem('authUser', JSON.stringify({ userId: 'fixed-user', nickname: '找回测试' }))
  })
  await page.route('**/api/v3/me/trips?*', (route) => route.fulfill({ json: { items: rows, next_cursor: null } }))
}

test('browser reference storage contains no source or secret, deduplicates and bounds entries', () => {
  const code = fs.readFileSync(path.join(__dirname, '../src/lib/browser-resource-ref.ts'), 'utf8')
  const compiled = ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
  const storage = new Map()
  const localStorage = { getItem: (key) => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) }
  const exports = {}
  vm.runInNewContext(compiled, { exports, window: {}, localStorage })
  for (let index = 0; index < 25; index++) exports.rememberBrowserTripReference(`r${index}_` + 'a'.repeat(24))
  expect(exports.readBrowserTripReferences()).toHaveLength(20)
  exports.rememberBrowserTripReference(refs.PROCESSING)
  exports.rememberBrowserTripReference(refs.PROCESSING)
  expect(exports.readBrowserTripReferences().filter((row) => row.resource === refs.PROCESSING)).toHaveLength(1)
  expect(exports.readBrowserTripReferences().every((row) => Object.keys(row).join() === 'resource')).toBe(true)
  exports.forgetBrowserTripReference(refs.PROCESSING)
  expect(exports.readBrowserTripReferences().some((row) => row.resource === refs.PROCESSING)).toBe(false)
  exports.rememberBrowserTripReference('https://other.invalid/private-source')
  expect(exports.readBrowserTripReferences()).toHaveLength(19)
})

test('account library distinguishes background, partial, failed and cancelled without reading source', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await account(page)
  const sourceRequests = []
  page.on('request', (request) => { if (new URL(request.url()).pathname.endsWith('/source')) sourceRequests.push(request.url()) })
  await page.goto('/my-trips')
  const list = page.getByRole('list', { name: '已保存行程', exact: true })
  await expect(list.getByRole('listitem')).toHaveCount(5)
  const pending = list.getByRole('listitem').filter({ hasText: '后台测试行程' })
  await expect(pending.getByRole('button', { name: '查看进度后台测试行程' })).toBeVisible()
  await expect(list.getByText('部分完成', { exact: true })).toBeVisible()
  for (const label of ['失败测试行程', '停止测试行程']) {
    const item = list.getByRole('listitem').filter({ hasText: label })
    await expect(item.getByRole('button', { name: '恢复原文再整理', exact: true })).toBeVisible()
    await expect(item.getByRole('button', { name: /继续编辑|查看部分結果|查看部分结果/ })).toHaveCount(0)
  }
  await page.reload()
  await expect(page.getByRole('list', { name: '已保存行程' }).getByRole('listitem')).toHaveCount(5)
  expect(sourceRequests).toEqual([])
  await expect(page.getByRole('navigation', { name: '全局导航' }).getByRole('link', { name: '我的行程' })).toHaveAttribute('aria-current', 'page')
  await page.screenshot({ path: testInfo.outputPath('task-library-desktop.png'), fullPage: true })
  await page.getByRole('button', { name: '查看进度后台测试行程', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/trip/result#trip=${refs.PROCESSING}$`))
})

test('explicit source recovery preserves a different home draft until user chooses replacement', async ({ page }) => {
  await account(page)
  let sourceReads = 0
  let creates = 0
  await page.route(`**/api/v3/trip-understandings/${refs.FAILED}/source`, (route) => {
    sourceReads++
    return route.fulfill({ json: { status: 'AVAILABLE', text: sourceText, activities: [] } })
  })
  await page.route('**/api/v3/trip-understandings', (route) => { creates++; return route.abort() })
  await page.goto('/my-trips')
  await page.evaluate(() => sessionStorage.setItem('bt_input_draft', JSON.stringify({ text: '另一份尚未提交的输入', demo: false, key: 'old-input-key', expires: Date.now() + 60000 })))
  const failed = page.getByRole('listitem').filter({ hasText: '失败测试行程' })
  await failed.getByRole('button', { name: '恢复原文再整理', exact: true }).click()
  await expect(page.getByText('首页还保留着另一份输入。是否用这次恢复的原文替换它？已保存的行程不会改变。')).toBeVisible()
  expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem('bt_input_draft')).text)).toBe('另一份尚未提交的输入')
  await page.getByRole('button', { name: '保留首页输入', exact: true }).click()
  await failed.getByRole('button', { name: '恢复原文再整理', exact: true }).click()
  await page.getByRole('button', { name: '替换输入并恢复原文', exact: true }).click()
  await expect(page).toHaveURL(/\/$/)
  expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem('bt_input_draft')).text)).toBe(sourceText)
  expect(sourceReads).toBe(2)
  expect(creates).toBe(0)
})

for (const width of [1440, 390]) test(`new tab finds only authorized anonymous references and drops expired ones at ${width}`, { tag: width === 390 ? '@small-screen' : '@desktop' }, async ({ context, page }, testInfo) => {
  await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 })
  const gone = 'gone_' + 'b'.repeat(24)
  const requests = []
  await context.route('**/api/v3/trip-understandings/*/result', (route) => {
    const url = route.request().url()
    requests.push(url)
    return url.includes(gone) ? route.fulfill({ status: 410, json: { detail: { code: 'RESOURCE_GONE' } } })
      : route.fulfill({ status: 202, json: { status: 'PROCESSING', message: '正在整理', phase: 'RECEIVED', progress: {}, snapshot: null } })
  })
  await page.goto('/my-trips')
  await page.evaluate(({ resource, gone }) => localStorage.setItem('bt_browser_trip_refs_v1', JSON.stringify([{ resource }, { resource: gone }])), { resource: refs.PROCESSING, gone })
  await page.close()
  const reopened = await context.newPage()
  await reopened.setViewportSize({ width, height: width === 390 ? 844 : 1000 })
  await reopened.goto('/my-trips')
  const local = reopened.getByRole('list', { name: '当前浏览器行程' })
  await expect(local.getByRole('listitem')).toHaveCount(1)
  await expect(local.getByRole('button', { name: '查看进度', exact: true })).toBeVisible()
  expect(await reopened.evaluate(() => JSON.parse(localStorage.getItem('bt_browser_trip_refs_v1')))).toEqual([{ resource: refs.PROCESSING }])
  expect(requests).toHaveLength(2)
  expect(await reopened.evaluate(() => sessionStorage.getItem('bt_input_draft'))).toBeNull()
  expect(await reopened.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await expect(reopened.getByRole('navigation', { name: '全局导航' }).getByRole('link', { name: '我的行程' })).toHaveAttribute('aria-current', 'page')
  await reopened.screenshot({ path: testInfo.outputPath(`task-library-anonymous-${width}.png`), fullPage: true })
  await local.getByRole('button', { name: '移除本机入口', exact: true }).click()
  await expect(local).not.toBeVisible()
  expect(await reopened.evaluate(() => localStorage.getItem('bt_browser_trip_refs_v1'))).toBeNull()
})

test('source deleted after listing cannot produce a restored input', async ({ page }) => {
  await account(page)
  await page.route(`**/api/v3/trip-understandings/${refs.FAILED}/source`, (route) => route.fulfill({ json: { status: 'DELETED', text: null, activities: [] } }))
  await page.goto('/my-trips')
  await page.getByRole('listitem').filter({ hasText: '失败测试行程' }).getByRole('button', { name: '恢复原文再整理', exact: true }).click()
  await expect(page.getByText('这份原文已删除。请重新输入攻略后再整理。')).toBeVisible()
  expect(await page.evaluate(() => sessionStorage.getItem('bt_input_draft'))).toBeNull()
  await expect(page).toHaveURL(/\/my-trips$/)
})
