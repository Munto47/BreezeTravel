const { test, expect } = require('@playwright/test');

const roomId = 'local-verification-room';

const itinerary = {
  itineraryId: 'itin-local',
  threadId: 'thread-local',
  city: '杭州',
  generatedAt: '2026-08-09T00:00:00Z',
  version: 2,
  days: [{
    dayIndex: 0,
    clusterId: 0,
    slots: [{
      placeId: 'p1', startTime: '09:00', endTime: '11:00', tips: [], transport: null,
      place: {
        placeId: 'p1', name: '西湖博物馆', category: 'attraction', address: '杭州', city: '杭州',
        coords: { lng: 120.1, lat: 30.2 }, source: 'amap_poi', tags: ['室内'], amapPhotos: [],
      },
    }],
    weatherSummary: null,
  }],
};

const report = {
  report_id: 'report-local', task_id: 'task-local', task_revision: 1,
  itinerary_id: 'itin-local', itinerary_version: 2, planning_input_hash: 'sha256:local',
  overall_status: 'VIOLATED', verified_at: '2026-08-09T00:00:00Z', repair_rounds: 2,
  unresolved_reasons: ['weather:no-data'],
  checks: [
    { constraint_id: 'must', status: 'SATISFIED', reason_code: 'OK', message: '博物馆已安排', repairable: false },
    { constraint_id: 'meal', status: 'VIOLATED', reason_code: 'EMPTY', message: '晚餐时段缺少餐饮', repairable: true },
    { constraint_id: 'weather', status: 'UNKNOWN', reason_code: 'MISSING', message: '天气数据缺失', repairable: false },
  ],
};

test.beforeEach(async ({ page }) => {
  await page.addInitScript(({ room, itineraryValue, reportValue }) => {
    localStorage.setItem(`itinerary_${room}`, JSON.stringify(itineraryValue));
    localStorage.setItem(`verification_${room}`, JSON.stringify(reportValue));
    if (localStorage.getItem(`verification_stale_${room}`) === null) {
      localStorage.setItem(`verification_stale_${room}`, 'false');
    }
  }, { room: roomId, itineraryValue: itinerary, reportValue: report });
});

test('renders three-state verification and hides stale green evidence after a collaborative change', async ({ page }) => {
  await page.goto(`/room/${roomId}/itinerary`);
  const panel = page.getByTestId('constraint-panel');
  await expect(panel).toBeVisible();
  await expect(panel.getByText('满足 · 博物馆已安排')).toBeVisible();
  await expect(panel.getByText('违反 · 晚餐时段缺少餐饮')).toBeVisible();
  await expect(panel.getByText('未知 · 天气数据缺失')).toBeVisible();
  await expect(panel.getByText('缺少证据，不会自动当作通过或触发修复。')).toBeVisible();

  await page.evaluate(room => localStorage.setItem(`verification_stale_${room}`, 'true'), roomId);
  await page.reload();
  await expect(page.getByTestId('verification-stale')).toContainText('验证结果已过期');
  await expect(page.getByTestId('constraint-panel')).toHaveCount(0);
});
