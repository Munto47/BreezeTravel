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
    // The legacy route only accepts cached data as a non-authoritative fallback.
    // A report retrieved from localStorage must never render as current evidence.
    localStorage.setItem(`itinerary_cache_${room}`, JSON.stringify(itineraryValue));
    localStorage.setItem(`verification_cache_${room}`, JSON.stringify(reportValue));
  }, { room: roomId, itineraryValue: itinerary, reportValue: report });
});

test('never renders a cached room itinerary before authenticated server readback', async ({ page }) => {
  await page.goto(`/room/${roomId}/itinerary`);
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByText('杭州 1 日游')).toHaveCount(0);
  await expect(page.getByText('西湖博物馆')).toHaveCount(0);
  await expect(page.getByTestId('constraint-panel')).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('bt_login_return')))
    .toBe(`/room/${roomId}/itinerary`);
});
