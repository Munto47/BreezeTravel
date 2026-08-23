const { test, expect } = require('@playwright/test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');


const NOW = '2026-08-23T08:00:00Z';
const COMMIT_SHA = execFileSync('git', ['rev-parse', 'HEAD'], {
  cwd: path.resolve(__dirname, '../..'),
  encoding: 'utf8',
}).trim();


function provenance(origin = 'PARSER', confirmation = 'UNCONFIRMED') {
  return {
    source_spans: [], confidence: 0.9, origin, confirmation,
    hardness: origin === 'DEFAULT_NO_PREFERENCE' ? 'NO_PREFERENCE' : 'SOFT',
  };
}


function buildScenario(city, index, names) {
  const slug = ['bj', 'sh', 'hz'][index];
  const workspaceId = `p1-browser-${slug}`;
  const importId = `${workspaceId}-import`;
  const briefId = `${workspaceId}-brief`;
  const runId = `${workspaceId}-run`;
  const reportId = `${workspaceId}-report-source`;
  const postcheckReportId = `${workspaceId}-report-postcheck`;
  const repairId = `${workspaceId}-repair`;
  const factId = `${workspaceId}-route-fact`;
  const rawText = `${city}2人。第1天 09:00-12:00 ${names[0]}，12:00-13:00 ${names[1]}；第2天 09:00-11:00 ${names[2]}。`;
  const workspace = {
    workspace_id: workspaceId,
    room_id: `${workspaceId}-room`,
    city,
    trip_date_range: { start: '2026-10-01', end: '2026-10-02' },
    current_itinerary_revision: null,
    current_import_id: importId,
    current_report_id: null,
    current_member_constraint_revision: null,
    current_brief_id: briefId,
    current_trip_brief_revision: 1,
    current_trip_check_run_id: null,
    status: 'DRAFT',
  };
  const rawStops = names.map((name, stopIndex) => ({
    raw_stop_id: `${slug}-stop-${stopIndex + 1}`,
    import_id: importId,
    day_index: stopIndex < 2 ? 0 : 1,
    raw_name: name,
    raw_time: stopIndex === 0 ? '09:00-12:00' : stopIndex === 1 ? '12:00-13:00' : '09:00-11:00',
    source_span: { start: stopIndex * 20, end: stopIndex * 20 + name.length },
    source_sentence: name,
    commitment_kind: null,
    fixed_commitment: false,
  }));
  const resolutions = rawStops.map((stop, stopIndex) => ({
    raw_stop_id: stop.raw_stop_id,
    canonical_place_id: `${slug}-place-${stopIndex + 1}`,
    confidence: 0.97,
    resolution_status: 'AUTO_MATCHED',
    resolution_version: 1,
    confirmed_by: null,
    confirmed_at: null,
    candidates: [{
      place_id: `${slug}-place-${stopIndex + 1}`,
      name: stop.raw_name,
      city,
      district: '受控 fixture',
      address: '受控 fixture',
      category: 'attraction',
      retrieval_provider: 'controlled_p1_fixture',
      execution_mode: 'fixture',
      score: 0.97,
      reasons: ['NAME_EXACT'],
    }],
    rejected_candidates: [],
  }));
  const itineraryImport = {
    import_id: importId,
    workspace_id: workspaceId,
    source_type: 'MANUAL_TEXT',
    raw_text: rawText,
    parse_version: 'deterministic-cn-v1',
    status: 'READY',
    member_summary: [],
    parse_errors: [],
    state_version: 2,
    applied_revision: null,
    created_by: 'p1-browser-user',
    raw_stops: rawStops,
    resolutions,
  };
  const briefBase = {
    brief_id: briefId,
    workspace_id: workspaceId,
    revision: 1,
    parent_revision: null,
    content_hash: '1'.repeat(64),
    city,
    date_range: workspace.trip_date_range,
    traveler_count: 2,
    arrival: { location: null, at: null, notes: null },
    departure: { location: null, at: null, notes: null },
    accommodation: { hotel_name: null, area: null },
    transport_modes: ['WALKING', 'TRANSIT'],
    transport_restrictions: 'NO_PREFERENCE',
    budget: 'NO_PREFERENCE',
    dining_style: 'NO_PREFERENCE',
    lodging_style: 'NO_PREFERENCE',
    dietary_restrictions: 'NO_PREFERENCE',
    daily_pace: 'NO_PREFERENCE',
    activity_intensity: 'NO_PREFERENCE',
    field_provenance: {
      city: provenance(),
      date_range: provenance(),
      traveler_count: provenance(),
      daily_pace: provenance('DEFAULT_NO_PREFERENCE', 'CONFIRMED'),
      activity_intensity: provenance('DEFAULT_NO_PREFERENCE', 'CONFIRMED'),
    },
    status: 'NEEDS_CONFIRMATION',
    confirmed_by: null,
    confirmed_at: null,
  };
  const confirmedBrief = {
    ...briefBase,
    revision: 2,
    parent_revision: 1,
    content_hash: '2'.repeat(64),
    status: 'CONFIRMED',
    confirmed_by: 'p1-browser-user',
    confirmed_at: NOW,
  };
  const revision1 = {
    itinerary_id: `${workspaceId}-itinerary`,
    workspace_id: workspaceId,
    revision: 1,
    parent_revision: null,
    source_type: 'IMPORT',
    city,
    date_range: workspace.trip_date_range,
    days: [],
    locked_commitments: [],
    change_summary: {},
    content_hash: '3'.repeat(64),
    created_by: 'p1-browser-user',
    created_at: NOW,
  };
  const revision2 = {
    ...revision1,
    revision: 2,
    parent_revision: 1,
    source_type: 'REPAIR',
    content_hash: '4'.repeat(64),
    change_summary: { repair_id: repairId },
  };
  const runSpec = {
    schema_version: 'trip-check-run-spec-v1',
    commit_sha: COMMIT_SHA,
    prompt_version: 'none-p1',
    model_version: 'none-p1',
    provider_version: 'controlled-fixture-v1',
    rule_set_version: 'audit-v1',
    execution_mode: 'fixture',
    dataset_hash: 'a'.repeat(64),
    snapshot_hash: 'b'.repeat(64),
    fault_profile: 'none',
    random_seed: 7,
    budget: {
      max_tokens: 0, max_provider_queries: 0, max_retries: 1,
      timeout_seconds: 30, max_cost_usd: 0,
    },
  };
  const baseRun = {
    run_id: runId,
    workspace_id: workspaceId,
    itinerary_revision: 1,
    brief_id: briefId,
    brief_revision: 2,
    stage: 'COLLECT_EVIDENCE',
    stage_attempt: 1,
    lease_owner: 'worker:p1-browser',
    lease_until: '2099-01-01T00:00:00Z',
    run_spec: runSpec,
    config_hash: 'c'.repeat(64),
    completed_stages: ['PARSE', 'WAIT_BRIEF_CONFIRMATION', 'RESOLVE_PLACES'],
    partial_failures: [],
    status: 'RUNNING',
    evidence_snapshot_id: null,
    report_id: null,
    advice_bundle_id: null,
    version: 2,
    created_at: NOW,
    updated_at: NOW,
  };
  const waitingRun = {
    ...baseRun,
    stage: 'WAIT_ADOPTION',
    status: 'WAITING',
    lease_owner: null,
    lease_until: null,
    completed_stages: [
      ...baseRun.completed_stages,
      'COLLECT_EVIDENCE', 'AUDIT', 'BUILD_ADVICE',
    ],
    evidence_snapshot_id: `${workspaceId}-snapshot-source`,
    report_id: reportId,
    advice_bundle_id: `${workspaceId}-advice`,
    version: 5,
  };
  const succeededRun = {
    ...waitingRun,
    itinerary_revision: 1,
    stage: 'POSTCHECK',
    status: 'SUCCEEDED',
    completed_stages: [...waitingRun.completed_stages, 'POSTCHECK'],
    evidence_snapshot_id: `${workspaceId}-snapshot-postcheck`,
    report_id: postcheckReportId,
    version: 6,
  };
  const finding = {
    finding_id: `${workspaceId}-finding`,
    rule_id: 'audit.route_gap',
    rule_version: '1.0.0',
    status: 'VIOLATED',
    severity: 'HIGH',
    reason_code: 'ROUTE_GAP_INSUFFICIENT',
    message: `${names[0]} 到 ${names[1]} 的交通空档不足`,
    input_values: { available_minutes: 0, route_duration_minutes: 35 },
    affected_days: [0],
    affected_stop_ids: [rawStops[0].raw_stop_id, rawStops[1].raw_stop_id],
    affected_member_ids: [],
    evidence_fact_ids: [factId],
    repairable: true,
    confirmation_action: '调整前后活动时间',
  };
  const sourceReport = {
    report_id: reportId,
    workspace_id: workspaceId,
    itinerary_id: revision1.itinerary_id,
    itinerary_revision: 1,
    task_id: `${workspaceId}-task`,
    task_revision: 1,
    member_constraint_revision_set: [],
    evidence_snapshot_id: waitingRun.evidence_snapshot_id,
    audit_rule_set_version: 'audit-v1',
    report_input_hash: 'd'.repeat(64),
    overall_status: 'VIOLATED',
    findings: [finding],
    created_at: NOW,
    supersedes_report_id: null,
  };
  const postcheckReport = {
    ...sourceReport,
    report_id: postcheckReportId,
    itinerary_revision: 2,
    evidence_snapshot_id: succeededRun.evidence_snapshot_id,
    report_input_hash: 'e'.repeat(64),
    overall_status: 'SATISFIED',
    findings: [],
    supersedes_report_id: reportId,
  };
  const sourceEvidence = {
    snapshot_id: waitingRun.evidence_snapshot_id,
    workspace_id: workspaceId,
    itinerary_revision: 1,
    policy_version: 'controlled-p1',
    facts: [{
      fact_id: factId,
      snapshot_id: waitingRun.evidence_snapshot_id,
      subject_type: 'ROUTE_EDGE',
      subject_id: `${rawStops[0].raw_stop_id}->${rawStops[1].raw_stop_id}`,
      fact_type: 'ROUTE_TIME',
      value: { duration_minutes: 35, mode: 'driving' },
      provider: 'controlled_route_fixture_v1',
      observed_at: NOW,
      valid_from: null,
      valid_until: '2027-08-23T08:00:00Z',
      confidence: 1,
      freshness_status: 'FRESH',
      source_url: null,
    }],
    provider_set: ['controlled_route_fixture_v1'],
    provider_failures: [],
    created_at: NOW,
    supersedes_snapshot_id: null,
  };
  const postcheckEvidence = {
    ...sourceEvidence,
    snapshot_id: succeededRun.evidence_snapshot_id,
    itinerary_revision: 2,
    facts: sourceEvidence.facts.map(item => ({
      ...item,
      snapshot_id: succeededRun.evidence_snapshot_id,
    })),
    supersedes_snapshot_id: sourceEvidence.snapshot_id,
  };
  const repair = {
    repair_id: repairId,
    source_report_id: reportId,
    base_itinerary_revision: 1,
    operations: [{
      operation: 'ADJUST_TIME',
      payload: { stop_id: rawStops[1].raw_stop_id, start_time: '12:35', end_time: '13:35' },
      rationale: `将 ${names[1]} 顺延 35 分钟，留出交通时间`,
    }],
    targeted_finding_ids: [finding.finding_id],
    edit_cost: 1,
    risk_cost: 0,
    route_cost_delta: 0,
    new_unknown_count: 0,
    tradeoffs: ['保留停留时长，但当天结束时间顺延'],
    affected_member_ids: [],
    result_preview: revision2,
    postcheck_report_id: postcheckReportId,
    status: 'PROPOSED',
    decided_by: null,
    decision_reason: null,
    decided_at: null,
    created_at: NOW,
  };
  const appliedRepair = {
    ...repair,
    status: 'APPLIED',
    decided_by: 'p1-browser-user',
    decided_at: NOW,
  };
  const advice = {
    advice_bundle_id: waitingRun.advice_bundle_id,
    workspace_id: workspaceId,
    run_id: runId,
    report_id: reportId,
    itinerary_revision: 1,
    brief_revision: 2,
    evidence_snapshot_id: sourceEvidence.snapshot_id,
    actions: [{
      advice_id: `${workspaceId}-advice-action`,
      finding_id: finding.finding_id,
      action: `将 ${names[1]} 顺延 35 分钟`,
      expected_impact: '创建新 revision 并完整 postcheck',
      uncertainty: '仅使用受控 fixture Evidence',
      candidate_set_id: null,
      evidence_fact_ids: [factId],
      provider_receipt_ids: [`${workspaceId}-receipt`],
      route_delta: { duration_delta_minutes: 0 },
      repair_id: repairId,
      tradeoffs: repair.tradeoffs,
    }],
    created_at: NOW,
  };
  return {
    city, workspaceId, importId, rawText, workspace, itineraryImport,
    briefBase, confirmedBrief, revision1, revision2, baseRun, waitingRun,
    succeededRun, sourceReport, postcheckReport, sourceEvidence,
    postcheckEvidence, repair, appliedRepair, advice,
  };
}


function resumePayload(scenario, state) {
  const applied = state.repairApplied;
  return {
    schema_version: '1.0',
    workspace: {
      ...scenario.workspace,
      current_itinerary_revision: applied ? 2 : state.runCreated ? 1 : null,
      current_report_id: applied
        ? scenario.postcheckReport.report_id
        : state.runCreated ? scenario.sourceReport.report_id : null,
      current_trip_brief_revision: state.briefConfirmed ? 2 : 1,
      current_trip_check_run_id: state.runCreated ? scenario.waitingRun.run_id : null,
    },
    current_revision: applied ? scenario.revision2 : state.runCreated ? scenario.revision1 : null,
    current_import: {
      ...scenario.itineraryImport,
      status: state.runCreated ? 'APPLIED' : scenario.itineraryImport.status,
      applied_revision: state.runCreated ? 1 : null,
    },
    current_brief: state.briefConfirmed ? scenario.confirmedBrief : scenario.briefBase,
    current_trip_check_run: state.runCreated
      ? applied ? scenario.succeededRun : scenario.waitingRun
      : null,
    current_advice: state.runCreated ? scenario.advice : null,
    current_report: state.runCreated
      ? applied ? scenario.postcheckReport : scenario.sourceReport
      : null,
    current_evidence: state.runCreated
      ? applied ? scenario.postcheckEvidence : scenario.sourceEvidence
      : null,
    proposed_repairs: state.runCreated && !applied ? [scenario.repair] : [],
    applied_repair: applied ? scenario.appliedRepair : null,
    current_tips: null,
    tips_state: applied ? 'INELIGIBLE' : 'NOT_APPLICABLE',
    write_etags: {
      itinerary: state.runCreated ? `"${applied ? 2 : 1}"` : null,
      import: '"2"',
    },
  };
}


async function installControlledApi(page, scenario) {
  const state = {
    briefConfirmed: false,
    runCreated: false,
    repairApplied: false,
    runCreateCount: 0,
    eventStreamCount: 0,
    reconnectHeaders: [],
  };
  await page.route('**/api/**', async route => {
    const request = route.request();
    const method = request.method();
    const path = new URL(request.url()).pathname;
    const json = value => route.fulfill({ status: 200, contentType: 'application/json', json: value });

    if (method === 'POST' && path === '/api/trip-workspaces') return json(scenario.workspace);
    if (method === 'POST' && path.endsWith('/imports')) return json(scenario.itineraryImport);
    if (method === 'GET' && path.endsWith('/resume')) return json(resumePayload(scenario, state));
    if (method === 'POST' && /\/trip-briefs\/\d+\/confirm$/.test(path)) {
      state.briefConfirmed = true;
      return json(scenario.confirmedBrief);
    }
    if (method === 'POST' && path.endsWith('/apply') && path.includes('/imports/')) {
      return json({
        itinerary_import: { ...scenario.itineraryImport, status: 'APPLIED', applied_revision: 1 },
        revision: scenario.revision1,
      });
    }
    if (method === 'POST' && path.endsWith('/trip-check-runs')) {
      state.runCreated = true;
      state.runCreateCount += 1;
      return json(scenario.baseRun);
    }
    if (method === 'GET' && path.endsWith('/events')) {
      state.reconnectHeaders.push(request.headers()['last-event-id'] || null);
      state.eventStreamCount += 1;
      const event = state.repairApplied
        ? { event_id: 5, run_id: scenario.waitingRun.run_id, event_type: 'run_succeeded', stage: 'POSTCHECK', run_version: 6, payload: { status: 'SUCCEEDED' }, created_at: NOW }
        : state.eventStreamCount === 1
          ? { event_id: 2, run_id: scenario.waitingRun.run_id, event_type: 'stage_started', stage: 'AUDIT', run_version: 3, payload: { status: 'RUNNING' }, created_at: NOW }
          : { event_id: 4, run_id: scenario.waitingRun.run_id, event_type: 'stage_completed', stage: 'WAIT_ADOPTION', run_version: 5, payload: { status: 'WAITING' }, created_at: NOW };
      const duplicate = { ...event };
      const stale = { ...event, event_id: Math.max(1, event.event_id - 1) };
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: [event, duplicate, stale].map(item => (
          `id: ${item.event_id}\nevent: ${item.event_type}\ndata: ${JSON.stringify(item)}\n\n`
        )).join(''),
      });
    }
    if (method === 'GET' && path === `/api/trip-check-runs/${scenario.waitingRun.run_id}`) {
      if (!state.repairApplied && state.eventStreamCount === 1) return json(scenario.baseRun);
      return json(state.repairApplied ? scenario.succeededRun : scenario.waitingRun);
    }
    if (method === 'GET' && path === `/api/audits/${scenario.sourceReport.report_id}`) {
      return json(scenario.sourceReport);
    }
    if (method === 'GET' && path === `/api/audits/${scenario.postcheckReport.report_id}`) {
      return json(scenario.postcheckReport);
    }
    if (method === 'GET' && path === `/api/audits/${scenario.sourceReport.report_id}/evidence`) {
      return json(scenario.sourceEvidence);
    }
    if (method === 'GET' && path === `/api/audits/${scenario.postcheckReport.report_id}/evidence`) {
      return json(scenario.postcheckEvidence);
    }
    if (method === 'GET' && path === `/api/audits/${scenario.sourceReport.report_id}/repairs`) {
      return json([scenario.repair]);
    }
    if (method === 'GET' && path === `/api/audits/${scenario.postcheckReport.report_id}/repairs`) {
      return json([]);
    }
    if (method === 'GET' && path.endsWith(`/reports/${scenario.sourceReport.report_id}/advice`)) {
      return json(scenario.advice);
    }
    if (method === 'POST' && path.endsWith(`/repairs/${scenario.repair.repair_id}/apply`)) {
      state.repairApplied = true;
      return json({
        repair: scenario.appliedRepair,
        new_revision: 2,
        postcheck_report_id: scenario.postcheckReport.report_id,
      });
    }
    if (method === 'POST' && path.endsWith('/tips')) {
      return route.fulfill({
        status: 409,
        contentType: 'application/json',
        json: { detail: { code: 'TIPS_NOT_ELIGIBLE', message: 'controlled browser fixture' } },
      });
    }
    return route.fulfill({
      status: 500,
      contentType: 'application/json',
      json: { detail: { code: 'UNEXPECTED_BROWSER_FIXTURE_REQUEST', message: `${method} ${path}` } },
    });
  });
  return state;
}


async function authenticate(page) {
  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'p1-browser-fixture-token');
    localStorage.setItem('authUser', JSON.stringify({
      userId: 'p1-browser-user', nickname: 'P1 Browser Fixture',
    }));
  });
}


for (const [index, [city, names]] of [
  ['北京', ['故宫博物院', '天坛公园', '颐和园']],
  ['上海', ['外滩', '上海迪士尼乐园', '豫园']],
  ['杭州', ['西湖风景名胜区', '灵隐寺', '雷峰塔']],
].entries()) {
  test(`${city}文本主链完成 Repair、新 Revision、postcheck 与 SSE 断点恢复`, async ({ page }) => {
    const scenario = buildScenario(city, index, names);
    await authenticate(page);
    const state = await installControlledApi(page, scenario);

    await page.goto(`/import?roomId=${scenario.workspace.room_id}&city=${encodeURIComponent(city)}&days=2`);
    await page.locator('textarea').fill(scenario.rawText);
    await page.getByRole('button', { name: '解析并生成 POI 候选' }).click();

    await expect(page.getByRole('heading', { name: '确认 TripBrief' })).toBeVisible();
    await expect(page.getByText(`${city} · 2026-10-01 至 2026-10-02`)).toBeVisible();
    await page.getByRole('button', { name: '确认当前 Brief' }).click();
    await expect(page.getByText('已确认', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '应用为 revision 1 并启动行程核验' }).click();

    await expect(page.getByText('WAITING · WAIT_ADOPTION')).toBeVisible();
    await expect.poll(() => state.reconnectHeaders.includes('2')).toBe(true);
    await expect(page.getByText('#2 stage_started · AUDIT · run v3')).toHaveCount(1);
    await expect(page.getByText(`将 ${names[1]} 顺延 35 分钟`, { exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Repair A/B' })).toBeVisible();
    await page.getByRole('button', { name: '预览确认并应用' }).first().click();

    await expect(page.getByText('权威行程 revision 2', { exact: false })).toBeVisible();
    await expect(page.getByText('SUCCEEDED · POSTCHECK', { exact: true })).toBeVisible();
    await expect(page.getByText('当前证据快照下未发现违反或未知结论。')).toBeVisible();

    await page.reload();
    await expect(page.getByText('权威行程 revision 2', { exact: false })).toBeVisible();
    await expect(page.getByText('SUCCEEDED · POSTCHECK', { exact: true })).toBeVisible();
    await expect(page.getByText('#5 run_succeeded · POSTCHECK · run v6')).toBeVisible();
    await expect.poll(() => state.reconnectHeaders.includes('4')).toBe(true);
    expect(state.runCreateCount).toBe(1);
  });
}


test('BJ-02 歧义地点未经人工选择不能创建权威 revision 或 Run', async ({ page }) => {
  const scenario = buildScenario('北京', 0, ['博物馆', '景山公园', '颐和园']);
  scenario.itineraryImport = {
    ...scenario.itineraryImport,
    status: 'NEEDS_RESOLUTION',
    resolutions: scenario.itineraryImport.resolutions.map((item, index) => index === 0
      ? { ...item, canonical_place_id: null, confidence: 0.55, resolution_status: 'AMBIGUOUS' }
      : item),
  };
  await authenticate(page);
  const state = await installControlledApi(page, scenario);

  await page.goto(`/import?roomId=${scenario.workspace.room_id}&city=${encodeURIComponent('北京')}&days=2`);
  await page.locator('textarea').fill(scenario.rawText);
  await page.getByRole('button', { name: '解析并生成 POI 候选' }).click();
  await page.getByRole('button', { name: '确认当前 Brief' }).click();

  await expect(page.getByText('请选择候选地点（本地 fixture）')).toBeVisible();
  await expect(page.getByRole('button', { name: '确认全部低置信度地点' })).toBeVisible();
  await expect(page.getByRole('button', { name: '应用为 revision 1 并启动行程核验' })).toHaveCount(0);
  expect(state.runCreateCount).toBe(0);
});
