const { test, expect } = require('@playwright/test');

const suffix = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
const roomId = `chat-persist-${suffix}`;
const threadId = `thread-${suffix}`;
const email = `persist-${suffix}@example.com`;
const password = 'BreezeTravel-test-2026!';
const message = `刷新恢复验证-${suffix}`;
const answer = `已完成并持久化-${suffix}`;
let auth;

test.beforeAll(async ({ request }) => {
  const register = await request.post('http://127.0.0.1:8000/api/auth/email-register', {
    data: { email, password, nickname: '持久化测试' },
  });
  expect(register.ok()).toBeTruthy();
  auth = await register.json();
  const room = await request.post('http://127.0.0.1:8000/api/room', {
    headers: { Authorization: `Bearer ${auth.token}` },
    data: { room_id: roomId, thread_id: threadId, trip_city: '杭州', trip_days: 2 },
  });
  expect(room.ok()).toBeTruthy();
});

test('restores one finalized message pair from persistent Yjs chat after refresh', async ({ page }) => {
  await page.addInitScript(({ token, userId }) => {
    const user = { userId, nickname: '持久化测试' };
    localStorage.setItem('authToken', token);
    localStorage.setItem('authUser', JSON.stringify(user));
    localStorage.setItem('userId', userId);
    localStorage.setItem('nickname', user.nickname);
  }, { token: auth.token, userId: auth.user_id });

  await page.route('**/api/chat', async (route) => {
    const requestBody = route.request().postDataJSON();
    const responseText = requestBody?.message === message ? answer : '初始化完成';
    const frames = [
      { event: 'text', data: { delta: responseText } },
      { event: 'done', data: { total_places: 0, trace_id: `trace-${suffix}` } },
    ].map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join('');
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: frames,
    });
  });

  await page.goto(`/room/${roomId}`);
  await expect(page.getByTestId('chat-input')).toBeVisible();
  await page.getByTestId('chat-input').fill(message);
  await page.getByTestId('chat-send').click();
  await expect(page.getByText(answer)).toBeVisible();
  await page.waitForTimeout(1200);

  await page.reload();
  await expect(page.getByText(message)).toHaveCount(1);
  await expect(page.getByText(answer)).toHaveCount(1);
});
