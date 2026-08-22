const { test, expect } = require('@playwright/test');

const template = {
  template_id: 'seed-杭州-classic-v1', city: '杭州', name: '杭州首次到访经典路线', template_version: 1,
  suitable_days: [2, 3, 4, 5], suitable_groups: ['friends'], budget_level: 'medium', intensity: 'medium',
  route_zones: [{ zone_id: 'west-lake', district: '西湖片区', preferred_transport: 'transit' }],
  anchor_slots: [], status: 'DRAFT', provenance: 'MODEL_GENERATED', last_verified_at: null,
};

test('model-generated draft entry creates a workspace, uses an idempotency key, and opens its workspace', async ({ page }) => {
  let createBody = null;
  let applyHeaders = null;
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'template-token');
    localStorage.setItem('authUser', JSON.stringify({ userId: 'template-user', nickname: '模板测试' }));
  });
  await page.route('**/api/route-templates?**', route => route.fulfill({ json: [template] }));
  await page.route('**/api/trip-workspaces', async route => {
    createBody = route.request().postDataJSON();
    await route.fulfill({ status: 201, json: {
      workspace_id: 'workspace-template', room_id: 'room-template', city: '杭州',
      trip_date_range: { start: '2026-10-01', end: '2026-10-03' },
      current_itinerary_revision: null, current_import_id: null, current_report_id: null,
      current_member_constraint_revision: null, status: 'DRAFT',
    } });
  });
  await page.route('**/api/trip-workspaces/workspace-template/templates/seed-%E6%9D%AD%E5%B7%9E-classic-v1/apply', async route => {
    applyHeaders = route.request().headers();
    await route.fulfill({ status: 201, json: {
      workspace_id: 'workspace-template', template_id: template.template_id, template_version: 1,
      revision: {}, workspace: {}, template_provenance: 'MODEL_GENERATED', human_review_evidence: false,
    } });
  });
  await page.route('**/api/trip-workspaces/workspace-template/resume', route => route.fulfill({ json: {
    workspace: { workspace_id: 'workspace-template', room_id: 'room-template', city: '杭州', trip_date_range: { start: '2026-10-01', end: '2026-10-03' }, current_itinerary_revision: 1, current_import_id: null, current_report_id: null, current_member_constraint_revision: null, status: 'DRAFT' },
    current_revision: { itinerary_id: 'itinerary-template', workspace_id: 'workspace-template', revision: 1, content_hash: 'sha256:template', days: [] },
    current_import: null, current_report: null, current_evidence: null, proposed_repairs: [], applied_repair: null, current_tips: null, tips_state: 'NOT_APPLICABLE', write_etags: { itinerary: '"1"', import: null },
  } }));
  await page.route('**/api/trip-workspaces/workspace-template/candidates?**', route => route.fulfill({ json: { workspace_id: 'workspace-template', revision: 1, day: 0, candidates: [], route_context_status: 'REVISION_STOP_COORDINATES_REQUIRED' } }));
  await page.route('**/api/trip-workspaces/workspace-template/hotel-areas', route => route.fulfill({ json: { workspace_id: 'workspace-template', revision: 1, areas: [{ area_id: 'west-lake', score_minutes: null, all_days_covered: false, evidence_freshness: 'UNAVAILABLE', explanation_codes: ['REVISION_STOP_COORDINATES_REQUIRED'] }], route_context_status: 'REVISION_STOP_COORDINATES_REQUIRED' } }));
  await page.route('**/api/trip-workspaces/workspace-template/revisions/1/map-projection', route => route.fulfill({ json: { workspace_id: 'workspace-template', revision: 1, city: '杭州', stops: [], coordinate_links: [], missing_stop_ids: [], status: 'UNAVAILABLE', unavailable_reason: 'none' } }));
  await page.route('**/api/trip-workspaces/workspace-template/members', route => route.fulfill({ json: [] }));
  await page.route('**/api/room/room-template/ws-token', route => route.fulfill({ json: { token: 'room-token' } }));

  await page.goto('/templates?roomId=room-template&city=%E6%9D%AD%E5%B7%9E&days=3&startDate=2026-10-01');
  await expect(page.getByText('GPT-5.6-sol 生成的合成 DRAFT')).toBeVisible();
  await expect(page.getByText('不是已核验 POI、真实住宿建议或人工审核路线')).toBeVisible();
  await page.getByTestId('apply-template-seed-杭州-classic-v1').click();
  await expect(page).toHaveURL(/\/workspace\/workspace-template/);
  expect(createBody).toEqual({ room_id: 'room-template', city: '杭州', trip_date_range: { start: '2026-10-01', end: '2026-10-03' } });
  expect(applyHeaders['idempotency-key']).toBeTruthy();
  await expect(page.getByTestId('workspace-hotel-areas')).toContainText('证据不可用');
  await expect(page.getByTestId('workspace-hotel-areas')).toContainText('不是已核验酒店或住宿推荐');
});
