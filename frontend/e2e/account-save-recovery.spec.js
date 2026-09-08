// Real local API/DB and browser recovery. Only token expiry is controlled by
// replacing the browser's token after an edited anonymous result is visible.
// The fixed example is not evidence of live text recognition quality.
const { test, expect } = require('@playwright/test')
const { randomUUID } = require('node:crypto')
const { openDemo, moveFirst, expectFirst } = require('./support/current-experience')

async function register(page, nickname) {
  const email = `save-recovery-${randomUUID()}@example.test`
  const password = `Trip${randomUUID()}7`
  const response = await page.request.post('/api/auth/email-register', {
    data: { email, password, nickname },
  })
  expect(response.ok()).toBeTruthy()
  return { ...(await response.json()), email, password }
}

for (const anotherAccount of [false, true]) {
  test(`expired account save resumes edited anonymous trip in ${anotherAccount ? 'the newly chosen account only' : 'the original account'}`, async ({ page }) => {
    await page.goto('/')
    const original = await register(page, '原账号')
    const chosen = anotherAccount ? await register(page, '本次保存账号') : original
    await page.evaluate(account => {
      localStorage.setItem('authToken', account.token)
      localStorage.setItem('authUser', JSON.stringify({ userId: account.user_id, nickname: account.nickname }))
    }, original)
    await openDemo(page)
    await moveFirst(page)
    await expectFirst(page, '景山公园')
    const anonymousReference = await page.evaluate(() => sessionStorage.getItem('bt_active_trip_ref'))
    const attempts = []
    page.on('response', response => {
      if (new URL(response.url()).pathname.endsWith('/claim')) {
        attempts.push({ status: response.status(), key: response.request().headers()['idempotency-key'] })
      }
    })
    await page.evaluate(() => localStorage.setItem('authToken', 'expired-session-for-recovery-test'))
    await page.getByRole('button', { name: '保存到账号', exact: true }).click()
    await expect(page).toHaveURL(/\/login$/)
    expect(await page.evaluate(() => localStorage.getItem('authToken'))).toBeNull()
    expect(await page.evaluate(() => sessionStorage.getItem('bt_login_return'))).toBe(`/trip/result#trip=${anonymousReference}`)
    expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem('bt_pending_operation')).resource)).toBe(anonymousReference)

    await page.getByLabel('邮箱', { exact: true }).fill(chosen.email)
    await page.getByLabel('密码', { exact: true }).fill(chosen.password)
    await page.getByRole('button', { name: '登录并继续', exact: true }).click()
    await expect(page.getByRole('button', { name: '已保存到账号', exact: true })).toBeVisible({ timeout: 30000 })
    await expectFirst(page, '景山公园')
    expect(attempts.map(attempt => attempt.status)).toEqual([401, 200])
    expect(new Set(attempts.map(attempt => attempt.key)).size).toBe(1)
    expect(await page.evaluate(() => sessionStorage.getItem('bt_pending_operation'))).toBeNull()
    const savedReference = await page.evaluate(() => sessionStorage.getItem('bt_active_trip_ref'))
    expect(savedReference).not.toBe(anonymousReference)

    const owned = await page.request.get(`/api/v3/trip-understandings/${savedReference}/result`, {
      headers: { Authorization: `Bearer ${chosen.token}` },
    })
    expect(owned.status()).toBe(200)
    expect((await owned.json()).ownership).toBe('ACCOUNT')
    if (anotherAccount) {
      const denied = await page.request.get(`/api/v3/trip-understandings/${savedReference}/result`, {
        headers: { Authorization: `Bearer ${original.token}` },
      })
      // The public contract hides resource existence from other accounts.
      expect(denied.status()).toBe(404)
      expect((await denied.json()).detail.code).toBe('RESOURCE_NOT_FOUND')
      const originalList = await page.request.get('/api/v3/me/trips', {
        headers: { Authorization: `Bearer ${original.token}` },
      })
      expect(originalList.status()).toBe(200)
      expect((await originalList.json()).items).toHaveLength(0)
    }
    await page.reload()
    await expectFirst(page, '景山公园')
    await page.getByRole('link', { name: '我的行程', exact: true }).click()
    await expect(page.locator('.e-trip-list-row')).toHaveCount(1)
    await page.getByRole('button', { name: /^继续编辑/ }).click()
    await expectFirst(page, '景山公园')
    await page.getByTestId('undo-trip-command').click()
    await expectFirst(page, '故宫博物院')
    await page.reload()
    await expectFirst(page, '故宫博物院')
  })
}
