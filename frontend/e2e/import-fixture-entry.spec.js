const { test, expect } = require('@playwright/test');

const workspace = {
  workspace_id: 'workspace-fixture-import', room_id: 'room-fixture-import', city: '北京',
  trip_date_range: { start: '2026-10-01', end: '2026-10-03' },
  current_itinerary_revision: null, current_import_id: null, current_report_id: null,
  current_member_constraint_revision: null, status: 'DRAFT',
};

const rawText = 'Day 1 北京\n09:00-11:00 故宫博物院\n12:00-14:00 景山公园';

const imported = {
  import_id: 'import-fixture', workspace_id: workspace.workspace_id, source_type: 'AI_TEXT', raw_text: rawText,
  parse_version: 'deterministic-cn-v1', status: 'READY', member_summary: [], parse_errors: [],
  state_version: 2, applied_revision: null, created_by: 'fixture-user',
  raw_stops: [
    { raw_stop_id: 'stop-gugong', import_id: 'import-fixture', day_index: 0, raw_name: '故宫博物院', raw_time: '09:00-11:00', source_span: { start: 9, end: 26 }, source_sentence: '09:00-11:00 故宫博物院', fixed_commitment: false },
    { raw_stop_id: 'stop-jingshan', import_id: 'import-fixture', day_index: 0, raw_name: '景山公园', raw_time: '12:00-14:00', source_span: { start: 27, end: 43 }, source_sentence: '12:00-14:00 景山公园', fixed_commitment: false },
  ],
  resolutions: [
    { raw_stop_id: 'stop-gugong', canonical_place_id: 'fixture-gugong', confidence: 0.95, resolution_status: 'AUTO_MATCHED', resolution_version: 1, confirmed_by: null, confirmed_at: null, candidates: [{ place_id: 'fixture-gugong', name: '故宫博物院', city: '北京', district: '东城区', address: '景山前街4号', category: 'attraction', retrieval_provider: 'amap_fixture', execution_mode: 'fixture', score: 0.95, reasons: ['NAME_EXACT'] }] },
    { raw_stop_id: 'stop-jingshan', canonical_place_id: 'fixture-jingshan', confidence: 0.95, resolution_status: 'AUTO_MATCHED', resolution_version: 1, confirmed_by: null, confirmed_at: null, candidates: [{ place_id: 'fixture-jingshan', name: '景山公园', city: '北京', district: '东城区', address: '景山西街44号', category: 'attraction', retrieval_provider: 'amap_fixture', execution_mode: 'fixture', score: 0.95, reasons: ['NAME_EXACT'] }] },
  ],
};

test('controlled fixture import labels its candidates and does not render a Day-city header as a POI', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'fixture-token');
    localStorage.setItem('authUser', JSON.stringify({ userId: 'fixture-user', nickname: 'Fixture 测试' }));
  });
  await page.route('**/api/trip-workspaces', route => route.fulfill({ status: 201, json: workspace }));
  await page.route('**/api/trip-workspaces/workspace-fixture-import/imports', route => route.fulfill({ status: 201, json: imported }));

  await page.goto('/import?roomId=room-fixture-import&city=%E5%8C%97%E4%BA%AC&days=3');
  await page.locator('textarea').fill(rawText);
  await page.getByRole('button', { name: '解析并生成 POI 候选' }).click();

  await expect(page.getByText('当前为本地 fixture 候选，仅用于受控开发测试，不是实时 Provider 核验。')).toBeVisible();
  await expect(page.getByText('请选择候选地点（本地 fixture）')).toHaveCount(0);
  await expect(page.getByText('D1 · 故宫博物院')).toBeVisible();
  await expect(page.getByText('D1 · 景山公园')).toBeVisible();
  await expect(page.getByText('D1 · 北京')).toHaveCount(0);
  await expect(page.getByText('本地 fixture', { exact: false })).toHaveCount(3);
});
