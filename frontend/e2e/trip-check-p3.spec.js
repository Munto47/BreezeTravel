const { test, expect } = require('@playwright/test');


const NOW = '2026-08-23T08:00:00Z';
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
  'base64',
);


function provenance(confidence = 0.62) {
  return {
    source_spans: [], confidence, origin: 'PARSER', confirmation: 'UNCONFIRMED', hardness: 'SOFT',
  };
}


function scenario() {
  const workspace = {
    workspace_id: 'p3-screenshot-workspace', room_id: 'p3-screenshot-room', city: '北京',
    trip_date_range: { start: '2026-09-01', end: '2026-09-02' },
    current_itinerary_revision: null, current_import_id: 'p3-screenshot-import',
    current_brief_id: 'p3-screenshot-brief', current_trip_brief_revision: 1,
    current_trip_check_run_id: null, current_report_id: null,
    current_member_constraint_revision: null, status: 'NEEDS_CONFIRMATION',
  };
  const itineraryImport = {
    import_id: 'p3-screenshot-import', workspace_id: workspace.workspace_id,
    source_type: 'AI_TEXT', raw_text: '北京2人\n第1天 09:00-12:00 颐和园',
    parse_version: 'deterministic-cn-v1', status: 'NEEDS_RESOLUTION',
    raw_stops: [{
      raw_stop_id: 'p3-raw-stop', day_index: 0, raw_name: '颐和园', raw_time: '09:00-12:00',
      source_span: { start: 17, end: 20 }, source_sentence: '第1天 09:00-12:00 颐和园', fixed_commitment: false,
    }],
    resolutions: [{
      raw_stop_id: 'p3-raw-stop', canonical_place_id: null, confidence: 0.62,
      resolution_status: 'AMBIGUOUS', resolution_version: 1,
      candidates: [{
        place_id: 'summer-palace', name: '颐和园', city: '北京', district: '海淀区', address: '受控地址',
        category: 'attraction', retrieval_provider: 'controlled_p3_fixture', execution_mode: 'fixture',
        score: 0.97, reasons: ['NAME_EXACT'],
      }],
    }],
    member_summary: [], parse_errors: [], state_version: 1,
  };
  const brief = {
    brief_id: 'p3-screenshot-brief', workspace_id: workspace.workspace_id, revision: 1,
    parent_revision: null, content_hash: '1'.repeat(64), city: '北京',
    date_range: workspace.trip_date_range, traveler_count: 2,
    arrival: { location: null, at: null, notes: null },
    departure: { location: null, at: null, notes: null },
    accommodation: { hotel_name: null, area: null }, transport_modes: ['WALKING', 'TRANSIT'],
    transport_restrictions: 'NO_PREFERENCE', budget: 'NO_PREFERENCE', dining_style: 'NO_PREFERENCE',
    lodging_style: 'NO_PREFERENCE', dietary_restrictions: 'NO_PREFERENCE', daily_pace: 'NO_PREFERENCE',
    activity_intensity: 'NO_PREFERENCE', field_provenance: { city: provenance(), traveler_count: provenance() },
    status: 'NEEDS_CONFIRMATION', confirmed_by: null, confirmed_at: null,
  };
  const result = {
    itinerary_import: itineraryImport,
    ocr_receipts: [{
      asset_id: 'p3-asset', asset_hash: 'a'.repeat(64), media_type: 'image/png', byte_size: PNG.length,
      engine: 'controlled_ocr_fixture', engine_version: 'p3', observed_at: NOW,
      lines: [{
        text: itineraryImport.raw_text, confidence: 0.62,
        box: { x_min: 1, y_min: 1, x_max: 300, y_max: 60 }, requires_confirmation: true,
      }],
    }],
    cleanup_receipts: [{
      receipt_id: 'p3-cleanup', asset_id: 'p3-asset', terminal_reason: 'SUCCEEDED',
      cleanup_status: 'DELETED', asset_hash: 'a'.repeat(64), cleanup_attempted_at: NOW,
      cleanup_error_category: null,
    }],
  };
  return { workspace, itineraryImport, brief, result };
}


function resumePayload(data) {
  return {
    schema_version: '1.0', workspace: data.workspace, current_revision: null,
    current_import: data.itineraryImport, current_brief: data.brief,
    current_trip_check_run: null, current_advice: null, current_report: null, current_evidence: null,
    proposed_repairs: [], applied_repair: null, current_tips: null, tips_state: 'NOT_APPLICABLE',
    write_etags: { itinerary: null, import: '"1"' },
  };
}


async function authenticate(page) {
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'p3-browser-fixture-token');
    localStorage.setItem('authUser', JSON.stringify({
      userId: 'p3-browser-user', nickname: 'P3 Browser Fixture',
    }));
  });
}


test('截图 OCR 显示低置信度和原图删除回执，刷新恢复导入草稿', async ({ page }) => {
  const data = scenario();
  const observed = { multipart: false, idempotencyKey: null, resumeCount: 0 };
  await authenticate(page);
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST' && path === '/api/trip-workspaces') {
      return route.fulfill({ status: 200, contentType: 'application/json', json: data.workspace });
    }
    if (request.method() === 'POST' && path.endsWith('/imports/screenshots')) {
      observed.multipart = (request.headers()['content-type'] || '').startsWith('multipart/form-data; boundary=');
      observed.idempotencyKey = request.headers()['idempotency-key'];
      return route.fulfill({ status: 201, contentType: 'application/json', json: data.result });
    }
    if (request.method() === 'GET' && path.endsWith('/resume')) {
      observed.resumeCount += 1;
      return route.fulfill({ status: 200, contentType: 'application/json', json: resumePayload(data) });
    }
    return route.fulfill({ status: 500, contentType: 'application/json', json: { detail: `${request.method()} ${path}` } });
  });

  await page.goto('/import?roomId=p3-screenshot-room&city=%E5%8C%97%E4%BA%AC&days=2');
  await page.getByRole('button', { name: '上传截图' }).click();
  await page.getByLabel('选择行程截图').setInputFiles({ name: 'trip.png', mimeType: 'image/png', buffer: PNG });
  await page.getByRole('button', { name: 'OCR 识别并生成待确认草稿' }).click();

  await expect(page.getByRole('heading', { name: 'OCR 与隐私回执' })).toBeVisible();
  await expect(page.getByText('原图清理 1/1')).toBeVisible();
  await expect(page.getByText('含低置信度字段，必须人工确认')).toBeVisible();
  await expect(page.getByRole('heading', { name: '确认 TripBrief' })).toBeVisible();
  await expect(page.getByText('请选择候选地点（本地 fixture）')).toBeVisible();
  expect(observed.multipart).toBe(true);
  expect(observed.idempotencyKey).toBeTruthy();

  await page.reload();
  await expect(page.locator('textarea')).toHaveValue(data.itineraryImport.raw_text);
  await expect(page.getByRole('heading', { name: '确认 TripBrief' })).toBeVisible();
  expect(observed.resumeCount).toBeGreaterThanOrEqual(2);
});


test('原图删除失败时页面显示 PRIVACY_BLOCKED 且不创建导入草稿', async ({ page }) => {
  const data = scenario();
  await authenticate(page);
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST' && path === '/api/trip-workspaces') {
      return route.fulfill({ status: 200, contentType: 'application/json', json: data.workspace });
    }
    if (request.method() === 'POST' && path.endsWith('/imports/screenshots')) {
      return route.fulfill({
        status: 500, contentType: 'application/json',
        json: { detail: { code: 'PRIVACY_BLOCKED', message: '原图清理失败，流程已阻断' } },
      });
    }
    return route.fulfill({ status: 500, contentType: 'application/json', json: { detail: `${request.method()} ${path}` } });
  });

  await page.goto('/import?roomId=p3-screenshot-room&city=%E5%8C%97%E4%BA%AC&days=2');
  await page.getByRole('button', { name: '上传截图' }).click();
  await page.getByLabel('选择行程截图').setInputFiles({ name: 'trip.png', mimeType: 'image/png', buffer: PNG });
  await page.getByRole('button', { name: 'OCR 识别并生成待确认草稿' }).click();

  await expect(page.getByText('原图清理失败，流程已阻断')).toBeVisible();
  await expect(page.getByRole('heading', { name: '确认 TripBrief' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'OCR 与隐私回执' })).toHaveCount(0);
});
