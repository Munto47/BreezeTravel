const { test, expect } = require('@playwright/test')

async function account(page) {
  const shares = [
    { share_ref: 'private-share-first', expires_at: '2030-11-01T12:00:00Z', status: 'ACTIVE' },
    { share_ref: 'private-share-second', expires_at: '2030-11-02T12:00:00Z', status: 'ACTIVE' },
  ]
  const state = { shares, deletes: [], failDelete: false, failList: false, loseDeleteResponse: false }
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'fixed-profile-token')
    localStorage.setItem('authUser', JSON.stringify({ userId: 'profile-owner', nickname: '旅行者' }))
  })
  await page.route('**/api/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const reply = (body, status = 200) => route.fulfill({ status, json: body })
    if (path === '/api/user/me') return reply({ user_id: 'profile-owner', nickname: '旅行者', created_at: '2026-01-01T00:00:00Z' })
    if (path === '/api/v3/me/data-consents') return reply({ memory_enabled: false, feedback_enabled: false, training_eval_enabled: false })
    if (path === '/api/v3/me/travel-preferences') return reply(null)
    if (path === '/api/v3/me/shares') return reply(state.failList ? {} : shares, state.failList ? 503 : 200)
    if (path.startsWith('/api/v3/me/shares/') && request.method() === 'DELETE') {
      const ref = path.split('/').at(-1)
      state.deletes.push(ref)
      if (state.failDelete) return reply({}, 503)
      shares.find(item => item.share_ref === ref).status = 'REVOKED'
      if (state.loseDeleteResponse) return route.abort('failed')
      return route.fulfill({ status: 204 })
    }
    throw new Error(`Unexpected profile request: ${request.method()} ${path}`)
  })
  return state
}

test('account can revoke one share without deleting other shares or travel data', async ({ page }) => {
  const state = await account(page)
  await page.goto('/profile#shares')
  const list = page.getByRole('list', { name: '已创建的分享链接' })
  await expect(list.getByRole('listitem')).toHaveCount(2)
  await page.getByRole('button', { name: '撤销分享链接 1', exact: true }).click()
  await expect(list.getByRole('listitem').nth(0)).toContainText('已撤销')
  await expect(list.getByRole('listitem').nth(1)).toContainText('生效中')
  await expect(page.getByRole('button', { name: '撤销分享链接 2', exact: true })).toBeEnabled()
  await expect(page.getByRole('status').filter({ hasText: '该链接已撤销' })).toContainText('已下载的内容无法收回')
  expect(state.deletes).toEqual(['private-share-first'])
  await expect(page.locator('body')).not.toContainText('private-share-first')
  await page.reload()
  await expect(list.getByRole('listitem').nth(0)).toContainText('已撤销')
  await expect(list.getByRole('listitem').nth(1)).toContainText('生效中')
})

test('share failures remain retryable and do not claim revocation', async ({ page }) => {
  const state = await account(page)
  state.failList = true
  await page.goto('/profile#shares')
  await expect(page.locator('#shares').getByRole('alert')).toContainText('分享列表暂时无法读取')
  state.failList = false
  await page.getByRole('button', { name: '重新读取分享列表' }).click()
  const button = page.getByRole('button', { name: '撤销分享链接 1', exact: true })
  await expect(button).toBeEnabled()
  state.failDelete = true
  await button.click()
  await expect(page.locator('#shares').getByRole('alert')).toContainText('尚未确认撤销结果')
  await expect(button).toBeDisabled()
  await expect(page.getByRole('listitem').filter({ hasText: '分享链接 1' })).toContainText('状态待重新读取')
  state.failDelete = false
  await page.getByRole('button', { name: '重新读取分享列表' }).click()
  await expect(button).toBeEnabled()
  await button.click()
  await expect(page.getByRole('listitem').filter({ hasText: '分享链接 1' })).toContainText('已撤销')
})

test('a lost revoke response stays uncertain until the server state is read again', async ({ page }) => {
  const state = await account(page)
  state.loseDeleteResponse = true
  await page.goto('/profile#shares')
  await page.getByRole('button', { name: '撤销分享链接 1', exact: true }).click()
  const first = page.getByRole('listitem').filter({ hasText: '分享链接 1' })
  await expect(first).toContainText('状态待重新读取')
  await expect(first).not.toContainText('生效中')
  state.failList = true
  await page.getByRole('button', { name: '重新读取分享列表' }).click()
  await expect(page.locator('#shares').getByRole('alert')).toContainText('分享列表暂时无法读取')
  await expect(first).toContainText('状态待重新读取')
  state.failList = false
  await page.getByRole('button', { name: '重新读取分享列表' }).click()
  await expect(first).toContainText('已撤销')
  await expect(page.getByRole('listitem').filter({ hasText: '分享链接 2' })).toContainText('生效中')
})
