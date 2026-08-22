const { test, expect } = require('@playwright/test');
const { spawnSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const Y = require('yjs');
const { WebsocketProvider } = require('y-websocket');
const WebSocket = require('ws');

const API_URL = 'http://127.0.0.1:8000';
const YJS_HTTP_URL = 'http://127.0.0.1:1234';
const YJS_WS_URL = 'ws://127.0.0.1:1234';
const REPOSITORY_ROOT = path.resolve(__dirname, '..', '..');
const EVIDENCE_PATH = path.join(
  REPOSITORY_ROOT,
  'backend',
  'evidence',
  'full_stack',
  'dual_user_backend_yjs_restart_2026-08-20.json',
);
const runNumber = Date.now();
const runHex = crypto.randomBytes(4).toString('hex');
const runSuffix = `${runNumber}-${runHex}`;
const emailA = `e2e+g5-a-${runSuffix}@example.com`;
const emailB = `e2e+g5-b-${runSuffix}@example.com`;
const password = 'BreezeTravel-e2e-2026!';

const cityAnchors = {
  北京: [
    ['故宫博物院', 116.397026, 39.918058],
    ['景山公园', 116.3967, 39.9250],
    ['什刹海', 116.3852, 39.9419],
  ],
  上海: [
    ['上海博物馆', 121.4751, 31.2283],
    ['外滩', 121.4903, 31.2417],
    ['豫园', 121.4921, 31.2270],
  ],
  杭州: [
    ['西湖', 120.1489, 30.2425],
    ['灵隐寺', 120.1014, 30.2402],
    ['西溪湿地', 120.0624, 30.2680],
  ],
};

const operationProfiles = [
  { key: 'reorder-map', operation: 'REORDER_STOP', payload: stopIds => ({ stop_id: stopIds[0], target_order_index: 2 }) },
  { key: 'move-day-ledger', operation: 'MOVE_TO_DAY', payload: stopIds => ({ stop_id: stopIds[0], target_day_index: 1, target_order_index: 0 }) },
  { key: 'adjust-time-audit', operation: 'ADJUST_TIME', payload: stopIds => ({ stop_id: stopIds[1], start_time: '14:00', end_time: '15:30' }) },
];

const cityCodes = { 北京: 'bj', 上海: 'sh', 杭州: 'hz' };
const cases = Object.keys(cityAnchors).flatMap((city, cityIndex) => operationProfiles.map((profile, profileIndex) => {
  const index = cityIndex * operationProfiles.length + profileIndex + 1;
  return {
    index,
    case_id: `g5.${cityCodes[city]}.restart.${profile.key}`,
    seed_id: `g5-restart-seed-${String(index).padStart(2, '0')}-${runSuffix}`,
    city,
    profile,
    room_id: `e2e-dual-restart-room-${runNumber + index}-${runHex}`,
    thread_id: `e2e-dual-restart-thread-${index}-${runSuffix}`,
    workspace_id: `e2e-dual-restart-workspace-${index}-${runSuffix}`,
    itinerary_id: `e2e-dual-restart-itinerary-${index}-${runSuffix}`,
  };
}));

let api;
let authA;
let authB;
const openContexts = new Set();
let cleanup = {
  postgres: 'NOT_RUN',
  postgres_room_count: 0,
  yjs_documents: 'NOT_RUN',
  yjs_room_count: 0,
  yjs_service_restored: 'NOT_RUN',
};
let evidence = {
  schema_version: '3.0',
  scenario: 'nine isolated G5 recovery cases survive one real Backend and Yjs process replacement',
  status: 'RUNNING',
  started_at: new Date().toISOString(),
  case_catalog: cases.map(item => ({
    case_id: item.case_id,
    seed_id: item.seed_id,
    city: item.city,
    operation: item.profile.operation,
    room_id: item.room_id,
    workspace_id: item.workspace_id,
  })),
  safety_contract: {
    required_case_count: 9,
    service_restart_opt_in_required: true,
    restarted_services: ['backend', 'y-websocket'],
    untouched_services: ['postgres', 'redis'],
    backend_access: 'public HTTP only',
    yjs_access: 'authenticated public WebSocket plus loopback-only cleanup HTTP',
    direct_domain_calls: 0,
    direct_sql_calls: 0,
    direct_leveldb_calls: 0,
    repository_rebuild_substitute: false,
    provider_scope: 'local fixture; no live/provider/human claim',
    llm_endpoints_called: false,
  },
  assertions: {},
  cases: [],
};

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function sha256(value) {
  return crypto.createHash('sha256').update(stableJson(value)).digest('hex');
}

function docker(args, timeout = 180_000) {
  const result = spawnSync('docker', args, {
    cwd: REPOSITORY_ROOT,
    encoding: 'utf8',
    timeout,
    windowsHide: true,
    maxBuffer: 10 * 1024 * 1024,
  });
  if (result.error || result.status !== 0) {
    throw new Error([
      `docker ${args.join(' ')} failed`,
      result.error?.message,
      result.stdout,
      result.stderr,
    ].filter(Boolean).join('\n'));
  }
  return result.stdout.trim();
}

function inspectContainer(service) {
  const id = docker(['compose', 'ps', '-q', service]);
  if (!id) throw new Error(`docker compose service ${service} has no container`);
  const inspected = JSON.parse(docker(['inspect', id]))[0];
  return {
    id,
    host_pid: inspected.State.Pid,
    started_at: inspected.State.StartedAt,
    running: inspected.State.Running,
    mounts: inspected.Mounts.map(mount => ({
      type: mount.Type,
      name: mount.Name || null,
      source: mount.Source,
      destination: mount.Destination,
    })),
  };
}

function persistEvidence() {
  fs.mkdirSync(path.dirname(EVIDENCE_PATH), { recursive: true });
  fs.writeFileSync(EVIDENCE_PATH, `${JSON.stringify({ ...evidence, cleanup }, null, 2)}\n`, 'utf8');
}

async function waitForHttp(url, timeoutMs = 90_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(2_000) });
      if (response.ok) return;
      lastError = new Error(`${url} returned ${response.status}`);
    } catch (reason) {
      lastError = reason;
    }
    await new Promise(resolve => setTimeout(resolve, 300));
  }
  throw new Error(`timed out waiting for ${url}: ${lastError?.message || 'unknown error'}`);
}

async function waitForHttpUnavailable(url, timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(500) });
      if (!response.ok) return;
    } catch (_) {
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`${url} remained available after its named service was stopped`);
}

async function healthJson(url) {
  const response = await fetch(url, { signal: AbortSignal.timeout(2_000) });
  if (!response.ok) throw new Error(`${url} returned ${response.status}`);
  return response.json();
}

function validateBootWitness(body, service) {
  expect(body.service).toBe(service);
  expect(body.boot_generation).toEqual(expect.objectContaining({
    instance_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
    started_at: expect.any(String),
    pid: expect.any(Number),
  }));
  expect(Number.isNaN(Date.parse(body.boot_generation.started_at))).toBe(false);
  expect(body.boot_generation.pid).toBeGreaterThan(0);
  return body.boot_generation;
}

async function register(email, nickname) {
  const response = await api.post('/api/auth/email-register', {
    data: { email, password, nickname },
  });
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

async function requestJson(auth, method, resourcePath, options = {}) {
  const response = await api.fetch(resourcePath, {
    method,
    headers: {
      Authorization: `Bearer ${auth.token}`,
      ...(options.headers || {}),
    },
    data: options.data,
  });
  const body = await response.json().catch(async () => ({ raw: await response.text() }));
  expect(response.status(), JSON.stringify(body)).toBe(options.expectedStatus || 200);
  return body;
}

function initialItinerary(item) {
  const slots = cityAnchors[item.city].map(([name, lng, lat], index) => ({
    place_id: `e2e-g5-${cityCodes[item.city]}-${item.index}-${index + 1}-${runHex}`,
    place: {
      name,
      category: index === 2 ? 'food' : 'attraction',
      coords: { lng, lat },
    },
    start_time: ['09:00', '11:00', '14:00'][index],
    end_time: ['10:30', '12:30', '15:30'][index],
    tips: [],
  }));
  return {
    itinerary_id: item.itinerary_id,
    thread_id: item.thread_id,
    city: item.city,
    generated_at: '2026-08-20T00:00:00Z',
    version: 1,
    days: [
      { day_index: 0, date: '2026-10-01', cluster_id: 0, slots },
      { day_index: 1, date: '2026-10-02', cluster_id: 1, slots: [] },
    ],
  };
}

function canonicalResume(value) {
  return {
    revision: value.current_revision.revision,
    content_hash: value.current_revision.content_hash,
    report_id: value.current_report.report_id,
    report_revision: value.current_report.itinerary_revision,
    report_status: value.current_report.overall_status,
    member_constraint_revision: value.workspace.current_member_constraint_revision,
  };
}

function canonicalEvents(value) {
  return value.map(event => ({
    event_id: event.event_id,
    event_type: event.event_type,
    workspace_id: event.workspace_id,
    itinerary_revision: event.itinerary_revision,
    suggestion_set_id: event.suggestion_set_id,
    candidate_id: event.candidate_id,
    session_id: event.session_id,
    context_hash: event.context_hash,
    policy_version: event.policy_version,
    provider_snapshot_id: event.provider_snapshot_id,
    rank_position: event.rank_position,
    reason_code: event.reason_code,
  }));
}

async function publicSnapshot(auth, item) {
  const resume = await requestJson(auth, 'GET', `/api/trip-workspaces/${item.workspace_id}/resume`);
  const revision = resume.current_revision.revision;
  const [members, mapProjection, events] = await Promise.all([
    requestJson(auth, 'GET', `/api/trip-workspaces/${item.workspace_id}/members`),
    requestJson(auth, 'GET', `/api/trip-workspaces/${item.workspace_id}/revisions/${revision}/map-projection`),
    requestJson(auth, 'GET', `/api/trip-workspaces/${item.workspace_id}/recommendation-events`),
  ]);
  return {
    resume: canonicalResume(resume),
    members,
    map_projection: mapProjection,
    map_projection_sha256: sha256(mapProjection),
    recommendation_events: canonicalEvents(events),
  };
}

async function issueRoomToken(auth, item) {
  const result = await requestJson(auth, 'POST', `/api/room/${encodeURIComponent(item.room_id)}/ws-token`);
  return result.token;
}

function refsFromDoc(doc) {
  const itineraryRef = doc.getMap('itineraryRef');
  const auditRef = doc.getMap('auditRef');
  const memberRef = doc.getMap('memberConstraintsRef');
  const mapRef = doc.getMap('mapRef');
  return {
    itinerary_revision: Number.isInteger(itineraryRef.get('revision')) ? Number(itineraryRef.get('revision')) : null,
    itinerary_content_hash: typeof itineraryRef.get('contentHash') === 'string' ? itineraryRef.get('contentHash') : null,
    audit_report_id: typeof auditRef.get('reportId') === 'string' ? auditRef.get('reportId') : null,
    audit_revision: Number.isInteger(auditRef.get('revision')) ? Number(auditRef.get('revision')) : null,
    member_constraint_revision: Number.isInteger(memberRef.get('revision')) ? Number(memberRef.get('revision')) : null,
    map_revision: Number.isInteger(mapRef.get('revision')) ? Number(mapRef.get('revision')) : null,
    map_projection_sha256: typeof mapRef.get('projectionSha256') === 'string' ? mapRef.get('projectionSha256') : null,
    places: Array.from(doc.getMap('places').entries())
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([placeId, value]) => ({ place_id: placeId, value })),
    builder_events: doc.getArray('builderEvents').toArray(),
  };
}

async function connectYjs(auth, item, timeoutMs = 20_000) {
  const token = await issueRoomToken(auth, item);
  const doc = new Y.Doc();
  const provider = new WebsocketProvider(YJS_WS_URL, item.room_id, doc, {
    params: { token },
    WebSocketPolyfill: WebSocket,
  });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timed out waiting for Yjs sync: ${item.case_id}`)), timeoutMs);
    provider.on('sync', synced => {
      if (!synced) return;
      clearTimeout(timer);
      resolve();
    });
    provider.on('connection-error', reason => {
      clearTimeout(timer);
      reject(reason instanceof Error ? reason : new Error(`Yjs connection failed: ${item.case_id}`));
    });
  });
  return { doc, provider, destroy: () => { provider.destroy(); doc.destroy(); } };
}

async function seedYjsCase(item, authoritative) {
  const clientA = await connectYjs(authA, item);
  const clientB = await connectYjs(authB, item);
  try {
    const placeId = `yjs-${item.case_id}-${runHex}`;
    const eventIds = authoritative.recommendation_events.map(event => event.event_id);
    expect(eventIds.length).toBeGreaterThan(0);
    clientA.doc.transact(() => {
      clientA.doc.getMap('itineraryRef').set('revision', authoritative.resume.revision);
      clientA.doc.getMap('itineraryRef').set('contentHash', authoritative.resume.content_hash);
      clientA.doc.getMap('auditRef').set('reportId', authoritative.resume.report_id);
      clientA.doc.getMap('auditRef').set('revision', authoritative.resume.report_revision);
      clientA.doc.getMap('memberConstraintsRef').set('revision', authoritative.resume.member_constraint_revision);
      clientA.doc.getMap('mapRef').set('revision', authoritative.resume.revision);
      clientA.doc.getMap('mapRef').set('projectionSha256', authoritative.map_projection_sha256);
      clientA.doc.getMap('places').set(placeId, {
        placeId,
        name: cityAnchors[item.city][0][0],
        city: item.city,
        note: `created-by-a:${item.case_id}`,
        votedBy: [authA.user_id],
      });
    });
    await expect.poll(() => clientB.doc.getMap('places').has(placeId)).toBe(true);
    const place = clientB.doc.getMap('places').get(placeId);
    clientB.doc.transact(() => {
      clientB.doc.getMap('places').set(placeId, {
        ...place,
        note: `edited-by-b:${item.case_id}`,
        votedBy: [authA.user_id, authB.user_id],
      });
      clientB.doc.getArray('builderEvents').push([{
        event_id: `restart-ref-${item.index}-${runSuffix}`,
        event_type: 'suggestions_shown_ref',
        case_id: item.case_id,
        actor_id: authB.user_id,
        backend_event_ids: eventIds,
      }]);
    });
    await expect.poll(() => refsFromDoc(clientA.doc).places[0]?.value?.note)
      .toBe(`edited-by-b:${item.case_id}`);
    await expect.poll(() => refsFromDoc(clientA.doc).builder_events.length).toBe(1);
    return refsFromDoc(clientA.doc);
  } finally {
    clientA.destroy();
    clientB.destroy();
  }
}

async function readYjsCase(auth, item) {
  const client = await connectYjs(auth, item);
  try {
    await new Promise(resolve => setTimeout(resolve, 200));
    return refsFromDoc(client.doc);
  } finally {
    client.destroy();
  }
}

async function waitForYjsCase(auth, item, expected, timeoutMs = 25_000) {
  const deadline = Date.now() + timeoutMs;
  let latest = null;
  while (Date.now() < deadline) {
    latest = await readYjsCase(auth, item);
    if (stableJson(latest) === stableJson(expected)) return latest;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error(`Yjs refs did not converge for ${item.case_id}: ${stableJson(latest)}`);
}

async function newUserContext(browser, auth, nickname) {
  const context = await browser.newContext();
  openContexts.add(context);
  await context.addInitScript(({ token, userId, displayName }) => {
    localStorage.setItem('authToken', token);
    localStorage.setItem('authUser', JSON.stringify({ userId, nickname: displayName }));
    localStorage.setItem('userId', userId);
    localStorage.setItem('nickname', displayName);
  }, { token: auth.token, userId: auth.user_id, displayName: nickname });
  return context;
}

async function closeContext(context) {
  openContexts.delete(context);
  await context.close();
}

async function browserSnapshot(page, item) {
  return page.evaluate(async ({ apiUrl, workspaceId }) => {
    const token = localStorage.getItem('authToken');
    const get = async resourcePath => {
      const response = await fetch(`${apiUrl}${resourcePath}`, { headers: { Authorization: `Bearer ${token}` } });
      if (!response.ok) throw new Error(`${resourcePath} failed: ${response.status}`);
      return response.json();
    };
    const resume = await get(`/api/trip-workspaces/${workspaceId}/resume`);
    const revision = resume.current_revision.revision;
    const [members, mapProjection, events] = await Promise.all([
      get(`/api/trip-workspaces/${workspaceId}/members`),
      get(`/api/trip-workspaces/${workspaceId}/revisions/${revision}/map-projection`),
      get(`/api/trip-workspaces/${workspaceId}/recommendation-events`),
    ]);
    return { resume, members, map_projection: mapProjection, recommendation_events: events };
  }, { apiUrl: API_URL, workspaceId: item.workspace_id });
}

function canonicalBrowserSnapshot(value) {
  return {
    resume: canonicalResume(value.resume),
    members: value.members,
    map_projection: value.map_projection,
    map_projection_sha256: sha256(value.map_projection),
    recommendation_events: canonicalEvents(value.recommendation_events),
  };
}

async function browserReadPair(browser, item, authoritative) {
  const contextA = await newUserContext(browser, authA, 'G5 Alpha');
  const contextB = await newUserContext(browser, authB, 'G5 Beta');
  try {
    const pageA = await contextA.newPage();
    const pageB = await contextB.newPage();
    await Promise.all([
      pageA.goto(`/workspace/${item.workspace_id}`),
      pageB.goto(`/workspace/${item.workspace_id}`),
    ]);
    for (const page of [pageA, pageB]) {
      await expect(page.getByText(`服务端 revision ${authoritative.resume.revision}`)).toBeVisible();
      await expect(page.getByText(/协同引用 已连接/)).toBeVisible();
      await expect(page.getByText('完整审计已完成')).toBeVisible();
      await expect(page.getByTestId('member-confirmation-panel')).toContainText(authA.user_id);
      await expect(page.getByTestId('member-confirmation-panel')).toContainText(authB.user_id);
      await expect(page.getByTestId('member-confirmation-panel')).toContainText('latest_return_time EQ 20:30');
    }
    const [valueA, valueB] = await Promise.all([browserSnapshot(pageA, item), browserSnapshot(pageB, item)]);
    const canonicalA = canonicalBrowserSnapshot(valueA);
    const canonicalB = canonicalBrowserSnapshot(valueB);
    expect(canonicalA).toEqual(authoritative);
    expect(canonicalB).toEqual(authoritative);
    return { browser_a: canonicalA, browser_b: canonicalB };
  } finally {
    await Promise.all([closeContext(contextA), closeContext(contextB)]);
  }
}

async function seedCase(item) {
  let response = await api.post('/api/room', {
    headers: { Authorization: `Bearer ${authA.token}` },
    data: { room_id: item.room_id, thread_id: item.thread_id, trip_city: item.city, trip_days: 2 },
  });
  expect(response.status(), await response.text()).toBe(200);
  response = await api.post(`/api/room/${encodeURIComponent(item.room_id)}/join`, {
    headers: { Authorization: `Bearer ${authB.token}` },
    data: { nickname: 'G5 Beta' },
  });
  expect(response.status(), await response.text()).toBe(200);
  await requestJson(authA, 'POST', '/api/trip-workspaces', {
    expectedStatus: 201,
    data: {
      workspace_id: item.workspace_id,
      room_id: item.room_id,
      city: item.city,
      trip_date_range: { start: '2026-10-01', end: '2026-10-02' },
      initial_itinerary: initialItinerary(item),
    },
  });
  await requestJson(authB, 'PUT', `/api/trip-workspaces/${item.workspace_id}/members/${authB.user_id}/constraints`, {
    data: {
      expected_base_revision: 0,
      constraint: {
        constraint_id: `constraint-${item.index}-${runSuffix}`,
        owner_member_id: authB.user_id,
        type: 'latest_return_time',
        operator: 'EQ',
        value: '20:30',
        hardness: 'HARD',
        priority: 100,
        source: 'MEMBER_EXPLICIT',
        confirmation_status: 'CONFIRMED',
        waivable_by: [],
      },
    },
  });
  const revisionOne = await requestJson(authA, 'GET', `/api/trip-workspaces/${item.workspace_id}/revisions/1`);
  const stopIds = revisionOne.days.flatMap(day => day.stops).map(stop => stop.stop_id);
  await requestJson(authA, 'POST', `/api/trip-workspaces/${item.workspace_id}/edits`, {
    headers: { 'If-Match': '"1"', 'Idempotency-Key': `edit-${item.seed_id}` },
    data: {
      command_id: `edit-${item.seed_id}`,
      base_revision: 1,
      operation: item.profile.operation,
      payload: item.profile.payload(stopIds),
    },
  });
  const report = await requestJson(authA, 'POST', `/api/trip-workspaces/${item.workspace_id}/audits`, {
    headers: { 'Idempotency-Key': `audit-${item.seed_id}` },
    data: { task_id: item.case_id },
  });
  expect(report.itinerary_revision).toBe(2);
  const revisionTwo = await requestJson(authA, 'GET', `/api/trip-workspaces/${item.workspace_id}/revisions/2`);
  const anchor = revisionTwo.days.flatMap(day => day.stops)[0];
  const suggestionSet = await requestJson(authA, 'POST', `/api/trip-workspaces/${item.workspace_id}/suggestion-sets`, {
    expectedStatus: 201,
    data: {
      base_revision: 2,
      day_index: anchor.day_index,
      insert_after_stop_id: anchor.stop_id,
      intents: ['NEARBY', 'POPULAR'],
      session_id: `session-${item.seed_id}`,
    },
  });
  expect(suggestionSet.candidates.length).toBeGreaterThan(0);
  await requestJson(authB, 'POST', `/api/trip-workspaces/${item.workspace_id}/suggestion-sets/${suggestionSet.suggestion_set_id}/candidates/${suggestionSet.candidates[0].candidate_id}:preview`, {
    headers: { 'Idempotency-Key': `preview-${item.seed_id}` },
  });
  const authoritative = await publicSnapshot(authA, item);
  expect(authoritative.resume).toEqual(expect.objectContaining({
    revision: 2,
    report_revision: 2,
    member_constraint_revision: 1,
  }));
  expect(authoritative.resume.content_hash).toMatch(/^[0-9a-f]{64}$/);
  expect(authoritative.resume.report_id).toBeTruthy();
  expect(authoritative.map_projection.status).toBe('AVAILABLE');
  expect(authoritative.map_projection.stops).toHaveLength(3);
  expect(authoritative.recommendation_events.length).toBeGreaterThanOrEqual(2);
  expect(authoritative.members.find(member => member.member_id === authB.user_id).constraints)
    .toEqual(expect.arrayContaining([expect.objectContaining({
      type: 'latest_return_time', value: '20:30', hardness: 'HARD', revision: 1,
    })]));
  return authoritative;
}

async function cleanupPostgres() {
  const roomIds = cases.map(item => item.room_id);
  if (!roomIds.every(value => /^e2e-dual-restart-room-\d+-[0-9a-f]+$/.test(value))) {
    throw new Error('refusing to clean unexpected PostgreSQL room ids');
  }
  const emails = [emailA, emailB];
  if (!emails.every(value => /^e2e\+g5-[ab]-\d+-[0-9a-f]+@example\.com$/.test(value))) {
    throw new Error('refusing to clean unexpected PostgreSQL emails');
  }
  const secret = process.env.E2E_CLEANUP_SECRET;
  if (!secret) throw new Error('E2E_CLEANUP_SECRET is required for public cleanup');
  const output = docker([
    'compose', 'exec', '-T',
    '-e', `TEST_E2E_SECRET=${secret}`,
    '-e', `TEST_E2E_ROOMS_JSON=${JSON.stringify(roomIds)}`,
    '-e', `TEST_E2E_EMAILS_JSON=${JSON.stringify(emails)}`,
    'backend', 'python', '-c',
    "import json,os,urllib.request; body=json.dumps({'room_ids':json.loads(os.environ['TEST_E2E_ROOMS_JSON']),'emails':json.loads(os.environ['TEST_E2E_EMAILS_JSON'])}).encode(); req=urllib.request.Request('http://127.0.0.1:8000/api/e2e/cleanup',data=body,method='POST',headers={'Content-Type':'application/json','X-E2E-Cleanup-Secret':os.environ['TEST_E2E_SECRET']}); print(urllib.request.urlopen(req,timeout=20).read().decode())",
  ]);
  const body = JSON.parse(output.split(/\r?\n/).at(-1));
  expect(body).toEqual({ ok: true, room_count: 9, email_count: 2 });
  cleanup.postgres = 'CLEARED';
  cleanup.postgres_room_count = body.room_count;
}

async function cleanupYjsDocuments() {
  const roomIds = cases.map(item => item.room_id);
  if (!roomIds.every(value => /^e2e-dual-restart-room-\d+-[0-9a-f]+$/.test(value))) {
    throw new Error('refusing to clean unexpected Yjs room ids');
  }
  const secret = process.env.E2E_CLEANUP_SECRET;
  if (!secret) throw new Error('E2E_CLEANUP_SECRET is required for public Yjs cleanup');
  const output = docker([
    'compose', 'exec', '-T',
    '-e', `TEST_E2E_SECRET=${secret}`,
    '-e', `TEST_E2E_ROOMS_JSON=${JSON.stringify(roomIds)}`,
    'y-websocket', 'node', '-e',
    "fetch('http://127.0.0.1:1234/__e2e/docs',{method:'DELETE',headers:{'Content-Type':'application/json','X-E2E-Cleanup-Secret':process.env.TEST_E2E_SECRET},body:JSON.stringify({room_ids:JSON.parse(process.env.TEST_E2E_ROOMS_JSON)})}).then(async r=>{if(!r.ok)throw new Error(r.status+' '+await r.text());console.log(await r.text())}).catch(e=>{console.error(e);process.exit(1)})",
  ]);
  const body = JSON.parse(output.split(/\r?\n/).at(-1));
  expect(body.ok).toBe(true);
  expect(body.room_count).toBe(9);
  expect(body.room_ids).toEqual(roomIds);
  cleanup.yjs_documents = 'CLEARED';
  cleanup.yjs_room_count = body.room_count;
  cleanup.yjs_service_restored = 'RUNNING';
}

test.beforeAll(async ({ playwright }) => {
  test.setTimeout(600_000);
  if (process.env.BREEZE_E2E_ALLOW_SERVICE_RESTART !== '1') {
    throw new Error('set BREEZE_E2E_ALLOW_SERVICE_RESTART=1 to authorize controlled local Backend/Yjs restart');
  }
  expect(cases).toHaveLength(9);
  expect(new Set(cases.map(item => item.case_id)).size).toBe(9);
  expect(new Set(cases.map(item => item.seed_id)).size).toBe(9);
  expect(new Set(cases.map(item => item.room_id)).size).toBe(9);
  expect(new Set(cases.map(item => item.workspace_id)).size).toBe(9);
  api = await playwright.request.newContext({ baseURL: API_URL });
});

test.afterAll(async () => {
  try {
    await Promise.all([...openContexts].map(context => closeContext(context)));
    if (authA || authB) await cleanupPostgres();
    await cleanupYjsDocuments();
  } catch (reason) {
    evidence.status = 'FAILED';
    cleanup.error = reason instanceof Error ? reason.stack || reason.message : String(reason);
    throw reason;
  } finally {
    await api?.dispose();
    evidence.finished_at = new Date().toISOString();
    persistEvidence();
  }
});

test('nine independent G5 cases recover exact HTTP, browser, map, member, event and Yjs refs after one real restart', async ({ browser }) => {
  test.setTimeout(600_000);
  try {
    expect(docker(['compose', 'exec', '-T', 'backend', 'printenv', 'AMAP_MOCK'])).toBe('true');
    expect(docker(['compose', 'exec', '-T', 'backend', 'printenv', 'FT_ROUTER_ENABLED'])).toBe('false');
    authA = await register(emailA, 'G5 Alpha');
    authB = await register(emailB, 'G5 Beta');
    expect(authA.user_id).not.toBe(authB.user_id);

    const caseEvidence = [];
    for (const item of cases) {
      const authoritativeBefore = await seedCase(item);
      const browserBefore = await browserReadPair(browser, item, authoritativeBefore);
      const expectedYjs = await seedYjsCase(item, authoritativeBefore);
      const yjsBefore = await waitForYjsCase(authA, item, expectedYjs);
      caseEvidence.push({
        case_id: item.case_id,
        seed_id: item.seed_id,
        city: item.city,
        operation: item.profile.operation,
        room_id: item.room_id,
        workspace_id: item.workspace_id,
        expected: { authoritative: authoritativeBefore, yjs: expectedYjs },
        before_restart: {
          authoritative_http: authoritativeBefore,
          browser: browserBefore,
          yjs_fresh_client: yjsBefore,
        },
        assertions: {},
      });
    }
    await new Promise(resolve => setTimeout(resolve, 1_000));
    expect(openContexts.size).toBe(0);

    const servicesBefore = {
      backend: inspectContainer('backend'),
      y_websocket: inspectContainer('y-websocket'),
      postgres: inspectContainer('postgres'),
    };
    const bootBefore = {
      backend: validateBootWitness(await healthJson(`${API_URL}/health`), 'breezetravel-backend'),
      y_websocket: validateBootWitness(await healthJson(YJS_HTTP_URL), 'breezetravel-yjs'),
    };
    docker(['compose', 'stop', 'backend', 'y-websocket']);
    await Promise.all([
      waitForHttpUnavailable(`${API_URL}/health`),
      waitForHttpUnavailable(YJS_HTTP_URL),
    ]);
    docker(['compose', 'start', 'backend', 'y-websocket']);
    await Promise.all([waitForHttp(`${API_URL}/health`), waitForHttp(YJS_HTTP_URL)]);
    const servicesAfter = {
      backend: inspectContainer('backend'),
      y_websocket: inspectContainer('y-websocket'),
      postgres: inspectContainer('postgres'),
    };
    const bootAfter = {
      backend: validateBootWitness(await healthJson(`${API_URL}/health`), 'breezetravel-backend'),
      y_websocket: validateBootWitness(await healthJson(YJS_HTTP_URL), 'breezetravel-yjs'),
    };
    expect(servicesAfter.backend.id).toBe(servicesBefore.backend.id);
    expect(servicesAfter.y_websocket.id).toBe(servicesBefore.y_websocket.id);
    expect(servicesAfter.postgres.id).toBe(servicesBefore.postgres.id);
    expect(servicesAfter.backend.started_at).not.toBe(servicesBefore.backend.started_at);
    expect(servicesAfter.y_websocket.started_at).not.toBe(servicesBefore.y_websocket.started_at);
    expect(servicesAfter.postgres.started_at).toBe(servicesBefore.postgres.started_at);
    expect(servicesAfter.backend.host_pid).not.toBe(servicesBefore.backend.host_pid);
    expect(servicesAfter.y_websocket.host_pid).not.toBe(servicesBefore.y_websocket.host_pid);
    for (const service of ['backend', 'y_websocket']) {
      expect(bootAfter[service].instance_id).not.toBe(bootBefore[service].instance_id);
      expect(bootAfter[service].started_at).not.toBe(bootBefore[service].started_at);
    }
    const yjsVolumeBefore = servicesBefore.y_websocket.mounts.find(item => item.destination === '/data');
    const yjsVolumeAfter = servicesAfter.y_websocket.mounts.find(item => item.destination === '/data');
    expect(yjsVolumeBefore?.name).toBeTruthy();
    expect(yjsVolumeAfter?.name).toBe(yjsVolumeBefore.name);

    // Every browser is closed. Read all nine persisted docs using new Yjs
    // clients before creating any post-restart browser context.
    for (const item of cases) {
      const receipt = caseEvidence.find(value => value.case_id === item.case_id);
      receipt.after_restart = {
        yjs_fresh_client_before_browser: await waitForYjsCase(authA, item, receipt.expected.yjs),
      };
    }
    for (const item of cases) {
      const receipt = caseEvidence.find(value => value.case_id === item.case_id);
      const authoritativeAfter = await publicSnapshot(authB, item);
      expect(authoritativeAfter).toEqual(receipt.expected.authoritative);
      const browserAfter = await browserReadPair(browser, item, receipt.expected.authoritative);
      receipt.after_restart.authoritative_http = authoritativeAfter;
      receipt.after_restart.browser = browserAfter;
      receipt.assertions = {
        independent_seed_and_storage_keys: true,
        exact_revision_and_content_hash: true,
        exact_audit_report_and_revision: true,
        exact_member_constraints_and_revision: true,
        exact_available_map_projection_and_hash: true,
        exact_nonempty_recommendation_event_ledger: true,
        exact_yjs_places_and_builder_events: true,
        fresh_yjs_read_preceded_browser_reconnect: true,
        two_fresh_browser_contexts_match_authority: true,
      };
      receipt.status = 'PASS';
    }

    evidence = {
      ...evidence,
      status: 'PASSED',
      users: [
        { role: 'A', user_id: authA.user_id, email_pattern: 'e2e+g5-a-*' },
        { role: 'B', user_id: authB.user_id, email_pattern: 'e2e+g5-b-*' },
      ],
      cases: caseEvidence,
      services: {
        before_restart: servicesBefore,
        after_restart: servicesAfter,
        boot_before: bootBefore,
        boot_after: bootAfter,
        stopped_ports_observed_unavailable: true,
        yjs_named_volume_preserved: yjsVolumeAfter.name,
      },
      assertions: {
        exactly_nine_independent_cases: true,
        all_pre_restart_browser_contexts_closed: true,
        backend_process_restarted: true,
        yjs_process_restarted_with_same_named_volume: true,
        backend_and_yjs_boot_generation_changed: true,
        stopped_ports_were_unavailable_before_start: true,
        postgres_container_not_restarted: true,
        all_fresh_yjs_reads_preceded_browser_reconnect: true,
        all_case_http_yjs_browser_refs_recovered_exactly: true,
        public_http_and_authenticated_yjs_only: true,
      },
    };
  } catch (reason) {
    evidence.status = 'FAILED';
    evidence.error = reason instanceof Error ? reason.stack || reason.message : String(reason);
    throw reason;
  } finally {
    persistEvidence();
  }
});
