const { test, expect } = require('@playwright/test');

const workspaceId = 'workspace-suggestions';
const roomId = 'room-suggestions';

function stop(stopId, placeId, name, orderIndex) {
  return {
    stop_id: stopId, place_id: placeId, day_index: 0, order_index: orderIndex,
    start_time: null, end_time: null, visit_duration_minutes: 60,
    transport_to_next: null, raw_name: name, fixed_commitment: false, locked: false,
    category: 'attraction', notes: '',
  };
}

function revision(number, stops) {
  return {
    itinerary_id: 'itinerary-suggestions', workspace_id: workspaceId, revision: number,
    content_hash: `sha256:fixture-revision-${number}`,
    days: [{ day_index: 0, date: '2026-10-01', stops: stops.map((item, index) => ({ ...item, order_index: index })) }],
  };
}

function candidate(setId, round, rank) {
  const id = `candidate-${round}-${rank}`;
  const names = ['断桥残雪', '浙江省博物馆', '曲院风荷', '楼外楼', '苏堤春晓', '花港观鱼'];
  return {
    suggestion_set_id: setId,
    candidate_id: id,
    canonical_place: {
      place_id: `poi-${round}-${rank}`, name: `${names[rank - 1]} ${round}`, city: '杭州',
      district: '西湖区', address: `受控地址 ${round}-${rank}`, category: rank === 4 ? 'food' : 'attraction',
      coords: { lng: 120.14 + rank / 1000, lat: 30.25 + rank / 1000 },
    },
    provider_receipt_id: `receipt-${round}-${rank}`,
    rank_position: rank,
    classification: rank === 1 ? 'ON_ROUTE' : rank <= 4 ? 'ACCEPTABLE_DETOUR' : 'DEFER_TO_OTHER_DAY',
    source_prior_refs: [`official:hangzhou:route-${round}`, `ugc_snapshot:fixture-${round}`],
    score_components: { route: 0.9, popularity: 0.8 },
    total_score: 0.9 - rank / 100,
    hard_gate: { passed: true, reason_codes: [] },
    route_delta: {
      status: 'AVAILABLE', delta_route_minutes: rank * 2,
      previous_to_candidate_minutes: 10, candidate_to_next_minutes: null,
      previous_to_next_minutes: null, reason_code: null,
    },
    evidence_freshness: {
      status: 'FRESH', observed_at: '2026-08-21T02:00:00Z', max_age_seconds: 86400, reason_code: null,
    },
    explanation_codes: ['ANCHOR_NEARBY', 'OFFICIAL_ROUTE_PRIOR', 'UGC_PRIOR'],
  };
}

async function installFailureFixture(page, failure) {
  const seed = stop('stop-seed', 'poi-west-lake', '西湖', 0);
  const initial = revision(1, [seed]);
  const setId = `set-${failure.code.toLowerCase()}`;
  let acceptBody = null;

  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'suggestion-fixture-token');
    localStorage.setItem('authUser', JSON.stringify({ userId: 'suggestion-user', nickname: '候选测试' }));
  });
  await page.route(`**/api/room/${roomId}/ws-token`, route => route.fulfill({ json: { token: 'room-token' } }));
  await page.route(`**/api/trip-workspaces/${workspaceId}/members`, route => route.fulfill({ json: [] }));
  await page.route(`**/api/trip-workspaces/${workspaceId}/hotel-areas`, route => route.fulfill({ json: {
    workspace_id: workspaceId, revision: 1, areas: [], route_context_status: 'AVAILABLE',
  } }));
  await page.route(`**/api/trip-workspaces/${workspaceId}/revisions/1/map-projection`, route => route.fulfill({ json: {
    workspace_id: workspaceId, revision: 1, city: '杭州', stops: [], coordinate_links: [],
    missing_stop_ids: ['stop-seed'], status: 'UNAVAILABLE', unavailable_reason: 'CONTROLLED_FIXTURE_NO_MAP',
  } }));
  await page.route(`**/api/trip-workspaces/${workspaceId}/resume`, route => route.fulfill({ json: {
    schema_version: '1.0',
    workspace: {
      workspace_id: workspaceId, room_id: roomId, city: '杭州',
      trip_date_range: { start: '2026-10-01', end: '2026-10-01' },
      current_itinerary_revision: 1, current_import_id: null, current_report_id: null,
      current_member_constraint_revision: null, status: 'DRAFT',
    },
    current_revision: initial, current_import: null, current_report: null, current_evidence: null,
    proposed_repairs: [], applied_repair: null, current_tips: null, tips_state: 'NOT_APPLICABLE',
    write_etags: { itinerary: '"1"', import: null },
  } }));
  await page.route(new RegExp(`/api/trip-workspaces/${workspaceId}/suggestion-sets(?:/.*)?$`), async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === 'POST' && url.pathname.endsWith('/suggestion-sets')) {
      if (failure.phase === 'create') {
        return route.fulfill({ status: failure.status, json: { detail: { code: failure.code, message: 'controlled failure' } } });
      }
      const body = request.postDataJSON();
      return route.fulfill({ status: 201, json: {
        suggestion_set_id: setId, workspace_id: workspaceId, base_revision: 1,
        day_index: 0, insert_after_stop_id: 'stop-seed', insert_before_stop_id: null,
        intents: body.intents, context_hash: 'b'.repeat(64), policy_version: 'fixture-policy-v1',
        provider_snapshot_id: 'controlled-fixture-snapshot-failure', expires_at: '2099-08-21T03:00:00Z',
        session_id: body.session_id, candidates: [candidate(setId, 9, 1), candidate(setId, 9, 2), candidate(setId, 9, 3), candidate(setId, 9, 4)],
        created_by: 'suggestion-user', created_at: '2026-08-21T02:00:00Z',
      } });
    }
    acceptBody = request.postDataJSON();
    return route.fulfill({ status: failure.status, json: { detail: { code: failure.code, message: 'controlled failure' } } });
  });
  return { getAcceptBody: () => acceptBody };
}

test('fixture: seed → frozen suggestions → accept → new anchor → four stops → Undo', async ({ page }) => {
  let currentRevision = 1;
  let currentStops = [stop('stop-seed', 'poi-west-lake', '西湖', 0)];
  let createCount = 0;
  let forbiddenPlannerCalls = 0;
  const revisions = new Map([[1, revision(1, currentStops)]]);
  const sets = new Map();
  const createRequests = [];
  const acceptRequests = [];
  const eventRequests = [];

  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'suggestion-fixture-token');
    localStorage.setItem('authUser', JSON.stringify({ userId: 'suggestion-user', nickname: '候选测试' }));
  });

  await page.route('**/api/chat', route => { forbiddenPlannerCalls += 1; return route.abort(); });
  await page.route('**/api/optimize', route => { forbiddenPlannerCalls += 1; return route.abort(); });
  await page.route(`**/api/room/${roomId}/ws-token`, route => route.fulfill({ json: { token: 'room-token' } }));
  await page.route(`**/api/trip-workspaces/${workspaceId}/members`, route => route.fulfill({ json: [] }));
  await page.route(`**/api/trip-workspaces/${workspaceId}/hotel-areas`, route => route.fulfill({ json: {
    workspace_id: workspaceId, revision: currentRevision, areas: [], route_context_status: 'AVAILABLE',
  } }));
  await page.route(`**/api/trip-workspaces/${workspaceId}/revisions/*/map-projection`, route => route.fulfill({ json: {
    workspace_id: workspaceId, revision: currentRevision, city: '杭州', stops: [], coordinate_links: [],
    missing_stop_ids: currentStops.map(item => item.stop_id), status: 'UNAVAILABLE', unavailable_reason: 'CONTROLLED_FIXTURE_NO_MAP',
  } }));
  await page.route(new RegExp(`/api/trip-workspaces/${workspaceId}/revisions/(\\d+)$`), route => {
    const number = Number(new URL(route.request().url()).pathname.split('/').at(-1));
    return route.fulfill({ json: revisions.get(number) });
  });
  await page.route(`**/api/trip-workspaces/${workspaceId}/resume`, route => route.fulfill({ json: {
    schema_version: '1.0',
    workspace: {
      workspace_id: workspaceId, room_id: roomId, city: '杭州',
      trip_date_range: { start: '2026-10-01', end: '2026-10-01' },
      current_itinerary_revision: currentRevision, current_import_id: null, current_report_id: null,
      current_member_constraint_revision: null, status: 'DRAFT',
    },
    current_revision: revisions.get(currentRevision), current_import: null, current_report: null, current_evidence: null,
    proposed_repairs: [], applied_repair: null, current_tips: null, tips_state: 'NOT_APPLICABLE',
    write_etags: { itinerary: `\"${currentRevision}\"`, import: null },
  } }));

  await page.route(new RegExp(`/api/trip-workspaces/${workspaceId}/suggestion-sets(?:/.*)?$`), async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === 'POST' && url.pathname.endsWith('/suggestion-sets')) {
      createCount += 1;
      const body = request.postDataJSON();
      createRequests.push(body);
      const setId = `set-${createCount}`;
      const frozenCandidates = Array.from({ length: 6 }, (_, index) => candidate(setId, createCount, index + 1));
      const value = {
        suggestion_set_id: setId, workspace_id: workspaceId, base_revision: currentRevision,
        day_index: 0, insert_after_stop_id: body.insert_after_stop_id,
        insert_before_stop_id: body.insert_before_stop_id, intents: body.intents,
        context_hash: 'a'.repeat(64), policy_version: 'fixture-policy-v1',
        provider_snapshot_id: `controlled-fixture-snapshot-${createCount}`,
        expires_at: '2099-08-21T03:00:00Z', session_id: body.session_id,
        candidates: frozenCandidates, created_by: 'suggestion-user', created_at: '2026-08-21T02:00:00Z',
      };
      sets.set(setId, value);
      return route.fulfill({ status: 201, json: value });
    }

    const preview = url.pathname.match(/suggestion-sets\/([^/]+)\/candidates\/([^:]+):preview$/);
    if (request.method() === 'POST' && preview) {
      const [, setId, candidateId] = preview;
      const controlledFailure = setId === 'set-1' && candidateId === 'candidate-1-6';
      eventRequests.push({ action: 'preview', setId, candidateId, body: request.postDataJSON(), headers: request.headers(), controlledFailure });
      if (controlledFailure) return route.fulfill({ status: 503, json: { detail: { code: 'EVENT_STORE_UNAVAILABLE' } } });
      return route.fulfill({ json: {
        event: { event_id: `preview-${setId}-${candidateId}`, event_type: 'candidate_previewed' },
        idempotent_replay: false,
      } });
    }

    const dismiss = url.pathname.match(/suggestion-sets\/([^/]+)\/candidates\/([^:]+):dismiss$/);
    if (request.method() === 'POST' && dismiss) {
      const [, setId, candidateId] = dismiss;
      const controlledFailure = setId === 'set-1' && candidateId === 'candidate-1-6';
      eventRequests.push({ action: 'dismiss', setId, candidateId, body: request.postDataJSON(), headers: request.headers(), controlledFailure });
      if (controlledFailure) return route.fulfill({ status: 503, json: { detail: { code: 'EVENT_STORE_UNAVAILABLE' } } });
      return route.fulfill({ json: {
        event: { event_id: `dismiss-${setId}-${candidateId}`, event_type: 'candidate_dismissed' },
        idempotent_replay: false,
      } });
    }

    const completed = url.pathname.match(/suggestion-sets\/([^/:]+):line-completed$/);
    if (request.method() === 'POST' && completed) {
      const [, setId] = completed;
      eventRequests.push({ action: 'line-completed', setId, candidateId: null, body: request.postDataJSON(), headers: request.headers() });
      return route.fulfill({ json: {
        event: { event_id: `line-${setId}`, event_type: 'line_completed' },
        idempotent_replay: false,
      } });
    }

    const match = url.pathname.match(/suggestion-sets\/([^/]+)\/candidates\/([^:]+):accept$/);
    if (request.method() === 'POST' && match) {
      const [, setId, candidateId] = match;
      const body = request.postDataJSON();
      acceptRequests.push({ setId, candidateId, body, headers: request.headers() });
      const frozen = sets.get(setId).candidates.find(item => item.candidate_id === candidateId);
      const baseRevision = currentRevision;
      currentStops = [...currentStops, stop(`stop-${currentRevision + 1}`, frozen.canonical_place.place_id, frozen.canonical_place.name, currentStops.length)];
      currentRevision += 1;
      const next = revision(currentRevision, currentStops);
      revisions.set(currentRevision, next);
      return route.fulfill({ json: {
        accepted: true, suggestion_set_id: setId, candidate_id: candidateId,
        new_revision: currentRevision, stop_id: `stop-${currentRevision}`, revision: next,
        event: { event_type: 'candidate_accepted' }, idempotent_replay: false,
      } });
    }
    return route.fulfill({ status: 404, json: { detail: { code: 'RESOURCE_NOT_FOUND' } } });
  });

  await page.route(`**/api/trip-workspaces/${workspaceId}/undo`, async route => {
    const body = route.request().postDataJSON();
    expect(route.request().headers()['if-match']).toBe('4');
    expect(body.target_revision).toBe(3);
    currentStops = revisions.get(3).days[0].stops;
    currentRevision = 5;
    const next = revision(currentRevision, currentStops);
    revisions.set(currentRevision, next);
    return route.fulfill({ json: {
      accepted: true, command_id: body.command_id, new_revision: 5, changed_days: [0],
      changed_route_edges: [], route_delta: null, incremental_findings: [], affected_rule_ids: [],
      audit_mode: 'INCREMENTAL_REVISION_ONLY', llm_calls: 0, report_stale: true, idempotent_replay: false,
    } });
  });

  await page.goto(`/workspace/${workspaceId}`);
  await expect(page.getByText('服务端 revision 1')).toBeVisible();
  await page.getByRole('heading', { name: '西湖' }).click();
  await expect(page.getByTestId('suggestion-anchor')).toContainText('西湖');

  // Closing and replacing are explicit dismissal reasons. Neither operation
  // sends any itinerary facts or changes the authoritative revision.
  await page.getByTestId('create-suggestion-set').click();
  await expect(page.getByTestId('suggestion-set')).toContainText('冻结 6 个');
  await page.getByTestId('close-suggestion-set').click();
  await expect(page.getByTestId('suggestion-set')).toHaveCount(0);
  await expect(page.getByText('服务端 revision 1')).toBeVisible();

  await page.getByTestId('create-suggestion-set').click();
  await expect(page.getByTestId('suggestion-set')).toContainText('controlled-fixture-snapshot-2');
  await page.getByTestId('create-suggestion-set').click();
  await expect(page.getByTestId('suggestion-set')).toContainText('controlled-fixture-snapshot-3');

  for (let acceptedIndex = 0; acceptedIndex < 3; acceptedIndex += 1) {
    const round = acceptedIndex + 3;
    if (acceptedIndex > 0) {
      await page.getByTestId('create-suggestion-set').click();
      await expect(page.getByTestId('suggestion-set')).toContainText(`controlled-fixture-snapshot-${round}`);
    }
    await expect(page.getByTestId('suggestion-set')).toContainText('冻结 6 个');
    await expect(page.getByTestId(`suggestion-candidate-candidate-${round}-1`)).toContainText('OFFICIAL_ROUTE_PRIOR');
    await expect(page.getByTestId(`suggestion-candidate-candidate-${round}-1`)).toContainText('UGC_PRIOR');
    await page.getByTestId(`accept-suggestion-candidate-${round}-1`).click();
    await expect(page.getByText(`服务端 revision ${acceptedIndex + 2}`)).toBeVisible();
    await expect(page.getByTestId('suggestion-anchor')).toContainText(`断桥残雪 ${round}`);
  }

  await expect(page.locator('article').filter({ has: page.getByText('第 1 天') }).getByRole('heading', { level: 3 })).toHaveCount(4);
  expect(createRequests).toHaveLength(5);
  expect(createRequests[0]).toMatchObject({
    base_revision: 1, day_index: 0, insert_after_stop_id: 'stop-seed', insert_before_stop_id: null,
    intents: ['NEARBY', 'POPULAR', 'FUN', 'FOOD'],
  });
  expect(createRequests[1].insert_after_stop_id).toBe('stop-seed');
  expect(createRequests[2].insert_after_stop_id).toBe('stop-seed');
  expect(createRequests[3].insert_after_stop_id).toBe('stop-2');
  expect(createRequests[4].insert_after_stop_id).toBe('stop-3');

  expect(acceptRequests).toHaveLength(3);
  for (const [index, request] of acceptRequests.entries()) {
    expect(request.setId).toBe(`set-${index + 3}`);
    expect(request.candidateId).toBe(`candidate-${index + 3}-1`);
    expect(request.body).toEqual({});
    expect(request.headers['if-match']).toBe(`"${index + 1}"`);
    expect(request.headers['idempotency-key']).toBeTruthy();
    const serialized = JSON.stringify(request.body);
    expect(serialized).not.toContain('canonical_place');
    expect(serialized).not.toContain('place_id');
    expect(serialized).not.toContain('coords');
  }
  await expect.poll(() => eventRequests.filter(item => item.action === 'preview').length).toBe(30);
  await expect.poll(() => eventRequests.filter(item => item.action === 'dismiss').length).toBe(12);
  await expect.poll(() => eventRequests.filter(item => item.action === 'line-completed').length).toBe(1);

  const dismissals = eventRequests.filter(item => item.action === 'dismiss');
  expect(dismissals.filter(item => item.body.reason_code === 'USER_CLOSED')).toHaveLength(6);
  expect(dismissals.filter(item => item.body.reason_code === 'BATCH_REPLACED')).toHaveLength(6);
  expect(eventRequests.find(item => item.action === 'line-completed').setId).toBe('set-5');
  expect(eventRequests.filter(item => item.controlledFailure)).toHaveLength(2);
  for (const eventRequest of eventRequests) {
    expect(eventRequest.headers['idempotency-key']).toBeTruthy();
    if (eventRequest.action === 'dismiss') {
      expect(Object.keys(eventRequest.body)).toEqual(['reason_code']);
    } else {
      expect(eventRequest.body).toEqual({});
    }
    const serialized = JSON.stringify(eventRequest.body);
    for (const forbidden of ['session_id', 'workspace_id', 'actor_id', 'revision_before', 'revision_after', 'context_hash', 'policy_version', 'provider_snapshot_id', 'rank_position', 'occurred_at']) {
      expect(serialized).not.toContain(forbidden);
    }
  }
  expect(forbiddenPlannerCalls).toBe(0);

  await page.getByRole('button', { name: '撤销' }).click();
  await expect(page.getByText('服务端 revision 5')).toBeVisible();
  await expect(page.getByRole('heading', { name: '断桥残雪 5' })).toHaveCount(0);
  await expect(page.locator('article').filter({ has: page.getByText('第 1 天') }).getByRole('heading', { level: 3 })).toHaveCount(3);
});

for (const failure of [
  { code: 'SUGGESTION_SET_EXPIRED', status: 409, phase: 'accept', expected: '已经过期' },
  { code: 'SUGGESTION_SET_STALE', status: 409, phase: 'accept', expected: '已经失效' },
  { code: 'CONTROLLED_CONFLICT', status: 409, phase: 'accept', expected: '当前行程状态冲突' },
  { code: 'SUGGESTION_PROVIDER_UNAVAILABLE', status: 503, phase: 'create', expected: '实时地点或路线来源暂不可用' },
]) {
  test(`fixture failure closes safely: ${failure.code}`, async ({ page }) => {
    const fixture = await installFailureFixture(page, failure);
    await page.goto(`/workspace/${workspaceId}`);
    await page.getByRole('heading', { name: '西湖' }).click();
    await page.getByTestId('create-suggestion-set').click();
    if (failure.phase === 'accept') {
      await page.getByTestId('accept-suggestion-candidate-9-1').click();
      expect(fixture.getAcceptBody()).toEqual({});
    }
    await expect(page.getByTestId('suggestion-error')).toContainText(failure.expected);
    await expect(page.getByText('服务端 revision 1')).toBeVisible();
    await expect(page.getByRole('heading', { name: '西湖' })).toHaveCount(1);
    await expect(page.locator('article').filter({ has: page.getByText('第 1 天') }).getByRole('heading', { level: 3 })).toHaveCount(1);
  });
}

test('fixture browser contract: drag and day selector emit the same MOVE_TO_DAY command and roll back on 409', async ({ page }) => {
  const moveWorkspaceId = 'workspace-move-equivalence';
  const moveRoomId = 'room-move-equivalence';
  const seed = stop('stop-move-seed', 'poi-west-lake', '西湖', 0);
  const initial = {
    itinerary_id: 'itinerary-move-equivalence', workspace_id: moveWorkspaceId, revision: 1,
    content_hash: 'a'.repeat(64),
    days: [
      { day_index: 0, date: '2026-10-01', stops: [seed] },
      { day_index: 1, date: '2026-10-02', stops: [] },
    ],
  };
  const commands = [];

  await page.addInitScript(() => {
    localStorage.setItem('authToken', 'suggestion-fixture-token');
    localStorage.setItem('authUser', JSON.stringify({ userId: 'suggestion-user', nickname: '候选测试' }));
  });
  await page.route(`**/api/room/${moveRoomId}/ws-token`, route => route.fulfill({ json: { token: 'room-token' } }));
  await page.route(`**/api/trip-workspaces/${moveWorkspaceId}/members`, route => route.fulfill({ json: [] }));
  await page.route(`**/api/trip-workspaces/${moveWorkspaceId}/hotel-areas`, route => route.fulfill({ json: {
    workspace_id: moveWorkspaceId, revision: 1, areas: [], route_context_status: 'AVAILABLE',
  } }));
  await page.route(`**/api/trip-workspaces/${moveWorkspaceId}/revisions/1/map-projection`, route => route.fulfill({ json: {
    workspace_id: moveWorkspaceId, revision: 1, city: '杭州', stops: [], coordinate_links: [],
    missing_stop_ids: ['stop-move-seed'], status: 'UNAVAILABLE', unavailable_reason: 'CONTROLLED_FIXTURE_NO_MAP',
  } }));
  await page.route(`**/api/trip-workspaces/${moveWorkspaceId}/resume`, route => route.fulfill({ json: {
    schema_version: '1.0',
    workspace: {
      workspace_id: moveWorkspaceId, room_id: moveRoomId, city: '杭州',
      trip_date_range: { start: '2026-10-01', end: '2026-10-02' },
      current_itinerary_revision: 1, current_import_id: null, current_report_id: null,
      current_member_constraint_revision: null, status: 'DRAFT',
    },
    current_revision: initial, current_import: null, current_report: null, current_evidence: null,
    proposed_repairs: [], applied_repair: null, current_tips: null, tips_state: 'NOT_APPLICABLE',
    write_etags: { itinerary: '"1"', import: null },
  } }));
  await page.route(`**/api/trip-workspaces/${moveWorkspaceId}/edits`, async route => {
    commands.push({
      body: route.request().postDataJSON(),
      headers: route.request().headers(),
    });
    return route.fulfill({
      status: 409,
      json: { detail: { code: 'ITINERARY_REVISION_CONFLICT', actual_revision: 1 } },
    });
  });

  await page.goto(`/workspace/${moveWorkspaceId}`);
  const dayOne = page.locator('article').filter({
    has: page.getByRole('heading', { name: '第 1 天', exact: true }),
  });
  const dayTwo = page.locator('article').filter({
    has: page.getByRole('heading', { name: '第 2 天', exact: true }),
  });
  await expect(dayOne.getByRole('heading', { name: '西湖' })).toBeVisible();

  await page.locator('[draggable="true"]').filter({
    has: page.getByRole('heading', { name: '西湖', exact: true }),
  }).dragTo(dayTwo);
  await expect.poll(() => commands.length).toBe(1);
  await expect(dayOne.getByRole('heading', { name: '西湖' })).toBeVisible();

  await page.reload();
  await page.getByLabel('移动到另一天').selectOption('1');
  await expect.poll(() => commands.length).toBe(2);
  await expect(dayOne.getByRole('heading', { name: '西湖' })).toBeVisible();

  const semantic = commands.map(item => ({
    base_revision: item.body.base_revision,
    operation: item.body.operation,
    payload: item.body.payload,
    if_match: item.headers['if-match'],
  }));
  expect(semantic[0]).toEqual(semantic[1]);
  expect(semantic[0]).toEqual({
    base_revision: 1,
    operation: 'MOVE_TO_DAY',
    payload: { stop_id: 'stop-move-seed', target_day_index: 1, target_order_index: 0 },
    if_match: '1',
  });
  expect(commands[0].body.command_id).not.toBe(commands[1].body.command_id);
});
