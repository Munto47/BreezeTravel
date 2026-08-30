const { expect, test } = require('@playwright/test')


const PNG = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  Buffer.from('g04-browser-fixture'),
])

const BATCH_REF = 'S'.repeat(43)
const FORBIDDEN_PUBLIC_TEXT = /batch_ref|bbox|confidence|provider|receipt|runspec|source span|revision|audit|repair|postcheck/i


function screenshot(name) {
  return { name, mimeType: 'image/png', buffer: PNG }
}


async function installAuthenticatedUser(page, owner = 'g04-browser-owner') {
  await page.addInitScript(({ userId }) => {
    localStorage.setItem('authToken', 'g04-browser-token')
    localStorage.setItem('authUser', JSON.stringify({ userId, nickname: '截图验收用户' }))
  }, { userId: owner })
}


test('selector preserves explicit 1/3/6 ordering and deletion', async ({ page }) => {
  await installAuthenticatedUser(page)
  await page.goto('/')
  const input = page.getByLabel('选择行程截图')
  const list = page.locator('ol[aria-label^="已选择"] > li')

  await input.setInputFiles([screenshot('one.png')])
  await expect(list).toHaveCount(1)
  await input.setInputFiles([screenshot('two.png'), screenshot('three.png')])
  await expect(list).toHaveCount(3)
  await input.setInputFiles([
    screenshot('four.png'),
    screenshot('five.png'),
    screenshot('six.png'),
  ])
  await expect(list).toHaveCount(6)

  await page.getByRole('button', { name: '将 two.png 上移' }).click()
  await expect(list.first()).toContainText('two.png')
  await page.getByRole('button', { name: '移除 three.png' }).click()
  await expect(list).toHaveCount(5)
  await expect(page.getByText('已从本次选择中移除图片。')).toBeVisible()
})


test('extension-validated empty MIME is normalized before multipart upload', async ({ page }) => {
  await installAuthenticatedUser(page)
  let multipart = ''
  await page.route('**/api/v3/screenshot-batches', async (route) => {
    multipart = route.request().postDataBuffer()?.toString('latin1') ?? ''
    return route.fulfill({
      status: 422,
      contentType: 'application/json',
      body: JSON.stringify({
        detail: { code: 'SCREENSHOT_TEXT_NOT_FOUND', message: '没有找到文字' },
      }),
    })
  })

  await page.goto('/')
  await page.getByLabel('选择行程截图').setInputFiles({
    name: 'empty-mime.png',
    mimeType: '',
    buffer: PNG,
  })
  await page.getByTestId('create-screenshot-trip').click()
  await expect(page.getByText(/没有在这些截图中找到可用文字/)).toBeVisible()
  expect(multipart).toContain('Content-Type: image/png')
})


test('unknown create response survives refresh and never uploads pixels twice', async ({ page }) => {
  await installAuthenticatedUser(page)
  let uploadCalls = 0
  const createKeys = []
  let createCalls = 0
  await page.route('**/api/v3/screenshot-batches', async (route) => {
    uploadCalls += 1
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({
        batch_ref: BATCH_REF,
        expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
        outcome: 'COMPLETE',
        message: '截图已读取，可以生成行程卡片',
      }),
    })
  })
  await page.route('**/api/v3/trip-understandings', async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    createCalls += 1
    createKeys.push(route.request().headers()['idempotency-key'])
    expect(route.request().postDataJSON().source.batch_ref).toBe(BATCH_REF)
    if (createCalls === 1) return route.abort('connectionreset')
    return route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({
        public_resource_id: 'g04-public-resource',
        status: 'PROCESSING',
        message: '正在整理每天行程',
        result_url: '/api/v3/trip-understandings/g04-public-resource/result',
        events_url: '/api/v3/trip-understandings/g04-public-resource/events',
      }),
    })
  })

  await page.goto('/')
  await page.getByLabel('选择行程截图').setInputFiles([screenshot('refresh.png')])
  await page.getByTestId('create-screenshot-trip').click()
  await expect(page.getByText(/网络中断，结果暂时未知/)).toBeVisible()
  expect(await page.locator('body').innerText()).not.toContain(BATCH_REF)

  await page.reload()
  await expect(page.getByText('截图已读取，可以继续生成行程卡片。')).toBeVisible()
  await page.getByRole('button', { name: '再试一次' }).click()
  await expect(page).toHaveURL(/\/trip\/result$/)

  expect(uploadCalls).toBe(1)
  expect(createKeys).toHaveLength(2)
  expect(createKeys[1]).toBe(createKeys[0])
  expect(await page.evaluate(() => sessionStorage.getItem('bt_g04_pending_screenshot_attempt'))).toBeNull()
})


test('terminal upload rotates its key while partial state and busy copy stay accurate', async ({ page }) => {
  await installAuthenticatedUser(page)
  const uploadKeys = []
  let uploadCalls = 0
  let releaseCreate
  const createBlocked = new Promise((resolve) => { releaseCreate = resolve })
  await page.route('**/api/v3/screenshot-batches', async (route) => {
    uploadCalls += 1
    uploadKeys.push(route.request().headers()['idempotency-key'])
    if (uploadCalls === 1) {
      return route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: { code: 'SCREENSHOT_TEXT_NOT_FOUND', message: '没有找到文字' },
        }),
      })
    }
    return route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({
        batch_ref: BATCH_REF,
        expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
        outcome: 'PARTIAL',
        message: '部分截图未能读取，已保留其余内容',
      }),
    })
  })
  await page.route('**/api/v3/trip-understandings', async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    await createBlocked
    return route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({
        public_resource_id: 'g04-partial-resource',
        status: 'PROCESSING',
        message: '正在整理每天行程',
        result_url: '/api/v3/trip-understandings/g04-partial-resource/result',
        events_url: '/api/v3/trip-understandings/g04-partial-resource/events',
      }),
    })
  })

  await page.goto('/')
  await page.getByLabel('选择行程截图').setInputFiles([screenshot('partial.png')])
  await page.getByTestId('create-screenshot-trip').click()
  await expect(page.getByText(/没有在这些截图中找到可用文字/)).toBeVisible()
  await page.getByRole('button', { name: '再试一次' }).click()

  await expect(page.getByText('部分图片暂未整理完成，你可以继续使用已完成的内容。')).toBeVisible()
  await expect(page.getByTestId('create-screenshot-trip')).toHaveText(/正在读取并整理截图/)
  await expect(page.getByTestId('create-full-trip')).toHaveText(/生成逐日卡片/)
  expect(uploadKeys).toHaveLength(2)
  expect(uploadKeys[1]).not.toBe(uploadKeys[0])
  expect(await page.locator('body').innerText()).not.toContain(BATCH_REF)
  releaseCreate()
  await expect(page).toHaveURL(/\/trip\/result$/)
})


test('live fixture runs screenshot through cards, map, stay and top-3 without public leakage', async ({ page, request }) => {
  const login = await request.post('/api/auth/test-login')
  expect(login.ok()).toBe(true)
  const auth = await login.json()
  await page.addInitScript(({ token, userId, nickname }) => {
    localStorage.setItem('authToken', token)
    localStorage.setItem('authUser', JSON.stringify({ userId, nickname }))
  }, { token: auth.token, userId: auth.user_id, nickname: auth.nickname })

  await page.goto('/')
  await page.getByLabel('选择行程截图').setInputFiles([screenshot('live.png')])
  const uploadResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/v3/screenshot-batches'
  ))
  const createResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/v3/trip-understandings'
  ))
  await page.getByTestId('create-screenshot-trip').click()
  const uploadedHttp = await uploadResponse
  expect(uploadedHttp.status()).toBe(201)
  const uploaded = await uploadedHttp.json()
  expect(Object.keys(uploaded).sort()).toEqual(['batch_ref', 'expires_at', 'message', 'outcome'])
  expect(uploaded.outcome).toBe('COMPLETE')
  expect((await createResponse).status()).toBe(202)

  await expect(page).toHaveURL(/\/trip\/result$/)
  await expect(page.getByTestId('trip-days').locator('section')).toHaveCount(3, { timeout: 30_000 })
  await expect(page.getByTestId('map-theater')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByTestId('stay-candidate')).toHaveCount(3, { timeout: 30_000 })
  await expect(page.getByTestId('trip-check-item')).toHaveCount(3, { timeout: 30_000 })

  const publicText = await page.locator('body').innerText()
  const publicDom = await page.locator('body').evaluate((element) => element.outerHTML)
  expect(publicText).not.toContain(uploaded.batch_ref)
  expect(publicText).not.toMatch(FORBIDDEN_PUBLIC_TEXT)
  expect(publicDom).not.toContain(uploaded.batch_ref)
  expect(publicDom).not.toMatch(FORBIDDEN_PUBLIC_TEXT)
})
