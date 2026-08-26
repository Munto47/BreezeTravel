const { test, expect } = require('@playwright/test');

const baseURL = process.env.E2E_BASE_URL;
const apiURL = process.env.E2E_API_BASE_URL || baseURL;
const cleanupSecret = process.env.E2E_CLEANUP_SECRET;

test.describe('public authenticated collaboration smoke', () => {
  test.skip(!baseURL || !cleanupSecret, 'requires E2E_BASE_URL and E2E_CLEANUP_SECRET');

  test('registers, joins an isolated room, receives cited answer, and cleans up', async ({ browser, request }) => {
    const run = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const roomId = `e2e-${run}`;
    const emailA = `e2e+${run}-a@example.test`;
    const emailB = `e2e+${run}-b@example.test`;
    const password = 'Breeze12345';
    const first = await browser.newPage();
    const second = await browser.newPage();
    try {
      await first.goto(`${baseURL}/login`);
      await first.getByTestId('auth-email-tab').click();
      await first.getByText('注册', { exact: true }).click();
      await first.getByTestId('auth-email').fill(emailA);
      await first.getByTestId('auth-password').fill(password);
      await first.getByTestId('auth-nickname').fill('E2E甲');
      await first.getByTestId('auth-email-submit').click();
      await expect(first).toHaveURL(`${baseURL}/`);
      const firstAuth = await first.evaluate(() => localStorage.getItem('authToken'));
      const create = await request.post(`${apiURL}/api/room`, {
        headers: { Authorization: `Bearer ${firstAuth}` },
        data: { room_id: roomId, thread_id: roomId, trip_city: '杭州', trip_days: 2, user_id: await first.evaluate(() => JSON.parse(localStorage.getItem('authUser')).userId), nickname: 'E2E甲' },
      });
      expect(create.ok()).toBeTruthy();

      await second.goto(`${baseURL}/login`);
      await second.getByTestId('auth-email-tab').click();
      await second.getByText('注册', { exact: true }).click();
      await second.getByTestId('auth-email').fill(emailB);
      await second.getByTestId('auth-password').fill(password);
      await second.getByTestId('auth-nickname').fill('E2E乙');
      await second.getByTestId('auth-email-submit').click();
      await expect(second).toHaveURL(`${baseURL}/`);

      const roomUrl = `${baseURL}/room/${roomId}?threadId=${roomId}&city=${encodeURIComponent('杭州')}&days=2`;
      await first.goto(roomUrl);
      await second.goto(roomUrl);
      await expect(first.getByText('AI 旅行顾问')).toBeVisible();
      await expect(second.getByText('AI 旅行顾问')).toBeVisible();

      await first.getByTestId('chat-input').fill('杭州西湖附近适合亲子的餐厅，并给出预约或避坑依据');
      await first.getByTestId('chat-send').click();
      await expect(first.getByText(/回答依据/)).toBeVisible({ timeout: 60000 });
      await expect(first.getByText(/公开资料/)).toBeVisible();
    } finally {
      await request.post(`${apiURL}/api/e2e/cleanup`, {
        headers: { 'X-E2E-Cleanup-Secret': cleanupSecret },
        data: { room_id: roomId, emails: [emailA, emailB] },
      });
      await first.close();
      await second.close();
    }
  });
});
