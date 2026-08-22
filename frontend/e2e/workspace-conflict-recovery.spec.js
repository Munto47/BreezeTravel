const { test, expect } = require('@playwright/test');

function resume(revision) {
  return {
    schema_version: '1.0',
    workspace: {
      workspace_id: 'workspace-conflict', room_id: 'room-conflict', city: '杭州',
      trip_date_range: { start: '2026-10-01', end: '2026-10-01' },
      current_itinerary_revision: revision, current_import_id: null, current_report_id: null,
      current_member_constraint_revision: null, status: 'DRAFT',
    },
    current_revision: {
      itinerary_id: 'itinerary-conflict', workspace_id: 'workspace-conflict', revision,
      content_hash: `sha256:revision-${revision}`,
      days: [{
        day_index: 0, date: '2026-10-01', stops: [{
          stop_id: 'stop-west-lake', place_id: 'poi-west-lake', day_index: 0, order_index: 0,
          start_time: '09:00', end_time: '10:00', visit_duration_minutes: 60,
          transport_to_next: null, raw_name: '西湖', fixed_commitment: false, locked: false,
          category: '景点', notes: '',
        }],
      }],
    },
    current_import: null, current_report: null, current_evidence: null,
    proposed_repairs: [], applied_repair: null, current_tips: null, tips_state: 'NOT_APPLICABLE',
    write_etags: { itinerary: `\"${revision}\"`, import: null },
  };
}

test('revision conflict requires explicit authoritative reload and never replays the optimistic command', async ({ page }) => {
  let resumeReads = 0;
  let editWrites = 0;
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'test-token');
    localStorage.setItem('authUser', JSON.stringify({ userId: 'user-conflict', nickname: '冲突测试' }));
  });
  await page.route('**/api/trip-workspaces/workspace-conflict/resume', async route => {
    resumeReads += 1;
    await route.fulfill({ json: resume(resumeReads === 1 ? 2 : 3) });
  });
  await page.route('**/api/trip-workspaces/workspace-conflict/candidates?**', route => route.fulfill({
    json: { workspace_id: 'workspace-conflict', revision: 2, day: 0, candidates: [], route_context_status: 'UNAVAILABLE' },
  }));
  await page.route('**/api/trip-workspaces/workspace-conflict/members', route => route.fulfill({ json: [] }));
  await page.route('**/api/room/room-conflict/ws-token', route => route.fulfill({ json: { token: 'room-token' } }));
  await page.route('**/api/trip-workspaces/workspace-conflict/edits', async route => {
    editWrites += 1;
    await route.fulfill({
      status: 409,
      json: { detail: {
        code: 'ITINERARY_REVISION_CONFLICT', message: 'base revision is stale',
        expected_revision: 2, actual_revision: 3,
      } },
    });
  });

  await page.goto('/workspace/workspace-conflict');
  await expect(page.getByText('服务端 revision 2')).toBeVisible();
  await page.getByRole('button', { name: '下移' }).click();

  const panel = page.getByTestId('workspace-conflict-recovery');
  await expect(panel).toContainText('服务端期望');
  await expect(panel).toContainText('2');
  await expect(panel).toContainText('服务端当前');
  await expect(panel).toContainText('3');
  await expect(panel).toContainText('本地乐观预览已回滚');
  await expect(page.getByRole('heading', { name: '西湖' })).toBeVisible();
  expect(editWrites).toBe(1);

  await page.getByTestId('reload-authoritative-workspace').click();
  await expect(page.getByText('服务端 revision 3')).toBeVisible();
  await expect(panel).toContainText('本次本地预览已丢弃且不会重放');
  expect(editWrites).toBe(1);
  expect(resumeReads).toBe(2);
});

test('stale audit confirmation is recovered by an explicit reload, never by reusing the old report', async ({ page }) => {
  let resumeReads = 0;
  let confirmationWrites = 0;
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'test-token');
    localStorage.setItem('authUser', JSON.stringify({ userId: 'user-conflict', nickname: '冲突测试' }));
  });
  await page.route('**/api/trip-workspaces/workspace-conflict/resume', async route => {
    resumeReads += 1;
    await route.fulfill({ json: resume(resumeReads === 1 ? 2 : 3) });
  });
  await page.route('**/api/trip-workspaces/workspace-conflict/candidates?**', route => route.fulfill({
    json: { workspace_id: 'workspace-conflict', revision: 2, day: 0, candidates: [], route_context_status: 'UNAVAILABLE' },
  }));
  await page.route('**/api/trip-workspaces/workspace-conflict/members', route => route.fulfill({ json: [] }));
  await page.route('**/api/room/room-conflict/ws-token', route => route.fulfill({ json: { token: 'room-token' } }));
  await page.route('**/api/trip-workspaces/workspace-conflict/audits', route => route.fulfill({
    json: { report_id: 'report-new-but-stale', itinerary_revision: 2, overall_status: 'SATISFIED' },
  }));
  await page.route('**/api/audits/report-new-but-stale/evidence', route => route.fulfill({ json: { provider_failures: [] } }));
  await page.route('**/api/trip-workspaces/workspace-conflict/confirm', async route => {
    confirmationWrites += 1;
    await route.fulfill({
      status: 409,
      json: { detail: {
        code: 'CURRENT_AUDIT_REQUIRED', message: 'final confirmation requires a current full audit report',
        reason: 'REPORT_REVISION_STALE', current_revision: 3, report_revision: 2,
      } },
    });
  });

  await page.goto('/workspace/workspace-conflict');
  await page.getByRole('button', { name: '审计后确认' }).click();

  const panel = page.getByTestId('workspace-conflict-recovery');
  await expect(panel).toContainText('完整审计已不再适用于当前状态');
  await expect(panel).toContainText('REPORT_REVISION_STALE');
  await expect(panel).toContainText('不会替你复用或覆盖现有审计结果');
  expect(confirmationWrites).toBe(1);

  await page.getByTestId('reload-authoritative-workspace').click();
  await expect(page.getByText('服务端 revision 3')).toBeVisible();
  await expect(panel).toContainText('需要重新执行“最终完整审计”');
  expect(confirmationWrites).toBe(1);
  expect(resumeReads).toBe(2);
});
