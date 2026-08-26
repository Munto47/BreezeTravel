const { test, expect } = require('@playwright/test');
const { spawnSync } = require('child_process');
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
const suffix = `${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
const roomId = `e2e-dual-restart-room-${suffix}`;
const threadId = `e2e-dual-restart-thread-${suffix}`;
const workspaceId = `e2e-dual-restart-workspace-${suffix}`;
const emailA = `e2e+dual-a-${suffix}@example.com`;
const emailB = `e2e+dual-b-${suffix}@example.com`;

let api;
let authA;
let authB;
let contextA;
let contextB;
let cleanup = { postgres: 'NOT_RUN', yjs_document: 'NOT_RUN', yjs_service_restored: 'NOT_RUN' };
let evidence = {
  schema_version: '2.0',
  scenario: 'two independent browser users collaborate, race one revision, then recover after Backend and Yjs restart',
  status: 'RUNNING',
  started_at: new Date().toISOString(),
  isolation: { room_id: roomId, workspace_id: workspaceId },
  safety_contract: {
    service_restart_opt_in_required: true,
    restarted_services: ['backend', 'y-websocket'],
    untouched_services: ['postgres', 'redis'],
    external_provider_mode: 'AMAP_MOCK=true in docker-compose backend environment',
    llm_endpoints_called: false,
    application_candidate_endpoints_loaded: true,
    external_provider_network_authorized: false,
  },
  assertions: {},
};

function docker(args, timeout = 120_000) {
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

function serviceContainer(service) {
  const id = docker(['compose', 'ps', '-q', service]);
  if (!id) throw new Error(`docker compose service ${service} has no container`);
  return id;
}

function inspectContainer(service) {
  const id = serviceContainer(service);
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

async function waitForHttp(url, timeoutMs = 60_000) {
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

async function healthJson(url) {
  const response = await fetch(url, { signal: AbortSignal.timeout(2_000) });
  if (!response.ok) throw new Error(`${url} returned ${response.status}`);
  return response.json();
}

async function waitForHttpUnavailable(url, timeoutMs = 15_000) {
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
    data: { email, password: 'BreezeTravel-e2e-2026!', nickname },
  });
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

async function authorizedGet(auth, resourcePath) {
  const response = await api.get(resourcePath, {
    headers: { Authorization: `Bearer ${auth.token}` },
  });
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

async function issueRoomToken(auth) {
  const response = await api.post(`/api/room/${encodeURIComponent(roomId)}/ws-token`, {
    headers: { Authorization: `Bearer ${auth.token}` },
  });
  expect(response.status(), await response.text()).toBe(200);
  return (await response.json()).token;
}

function refsFromDoc(doc) {
  const itineraryRef = doc.getMap('itineraryRef');
  const auditRef = doc.getMap('auditRef');
  const memberRef = doc.getMap('memberConstraintsRef');
  const places = Array.from(doc.getMap('places').entries())
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([placeId, value]) => ({ place_id: placeId, value }));
  const builderEvents = doc.getArray('builderEvents').toArray();
  return {
    itinerary_revision: Number.isInteger(itineraryRef.get('revision')) ? Number(itineraryRef.get('revision')) : null,
    itinerary_content_hash: typeof itineraryRef.get('contentHash') === 'string' ? itineraryRef.get('contentHash') : null,
    audit_report_id: typeof auditRef.get('reportId') === 'string' ? auditRef.get('reportId') : null,
    audit_revision: Number.isInteger(auditRef.get('revision')) ? Number(auditRef.get('revision')) : null,
    member_constraint_revision: Number.isInteger(memberRef.get('revision')) ? Number(memberRef.get('revision')) : null,
    places,
    builder_events: builderEvents,
  };
}

async function connectYjs(auth, timeoutMs = 15_000) {
  const token = await issueRoomToken(auth);
  const doc = new Y.Doc();
  const provider = new WebsocketProvider(YJS_WS_URL, roomId, doc, {
    params: { token },
    WebSocketPolyfill: WebSocket,
  });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timed out waiting for Yjs sync')), timeoutMs);
    provider.on('sync', synced => {
      if (!synced) return;
      clearTimeout(timer);
      resolve();
    });
    provider.on('connection-error', reason => {
      clearTimeout(timer);
      reject(reason instanceof Error ? reason : new Error('Yjs connection failed'));
    });
  });
  return { doc, provider, destroy: () => { provider.destroy(); doc.destroy(); } };
}

async function writeTwoClientYjsState() {
  const clientA = await connectYjs(authA);
  const clientB = await connectYjs(authB);
  try {
    clientA.doc.getMap('places').set(`e2e-west-lake-${suffix}`, {
      placeId: `e2e-west-lake-${suffix}`,
      name: '西湖',
      city: '杭州',
      note: 'created-by-a',
      votedBy: [authA.user_id],
    });
    await expect.poll(() => clientB.doc.getMap('places').has(`e2e-west-lake-${suffix}`)).toBe(true);
    const place = clientB.doc.getMap('places').get(`e2e-west-lake-${suffix}`);
    clientB.doc.getMap('places').set(`e2e-west-lake-${suffix}`, {
      ...place,
      note: 'edited-by-b',
      votedBy: [authA.user_id, authB.user_id],
    });
    clientB.doc.getArray('builderEvents').push([{
      event_id: `accepted-${suffix}`,
      event_type: 'candidate_accepted',
      actor_id: authB.user_id,
    }]);
    await expect.poll(() => refsFromDoc(clientA.doc).places[0]?.value?.note).toBe('edited-by-b');
    await expect.poll(() => refsFromDoc(clientA.doc).builder_events.length).toBe(1);
    return refsFromDoc(clientA.doc);
  } finally {
    clientA.destroy();
    clientB.destroy();
  }
}

async function readYjsReferences(auth, timeoutMs = 15_000) {
  const token = await issueRoomToken(auth);
  const doc = new Y.Doc();
  const provider = new WebsocketProvider(YJS_WS_URL, roomId, doc, {
    params: { token },
    WebSocketPolyfill: WebSocket,
  });
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('timed out waiting for fresh Yjs client sync')), timeoutMs);
      provider.on('sync', synced => {
        if (!synced) return;
        clearTimeout(timer);
        resolve();
      });
      provider.on('connection-error', reason => {
        clearTimeout(timer);
        reject(reason instanceof Error ? reason : new Error('Yjs connection failed'));
      });
    });
    await new Promise(resolve => setTimeout(resolve, 150));
    return refsFromDoc(doc);
  } finally {
    provider.destroy();
    doc.destroy();
  }
}

async function waitForYjsReferences(auth, expected, timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs;
  let latest = null;
  while (Date.now() < deadline) {
    latest = await readYjsReferences(auth);
    if (JSON.stringify(latest) === JSON.stringify(expected)) return latest;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error(`Yjs refs did not converge. expected=${JSON.stringify(expected)}, actual=${JSON.stringify(latest)}`);
}

async function newUserContext(browser, auth, nickname, editRequests) {
  const context = await browser.newContext();
  context.on('request', request => {
    if (request.method() === 'POST' && request.url().includes(`/api/trip-workspaces/${workspaceId}/edits`)) {
      editRequests.push({ user_id: auth.user_id, body: request.postDataJSON() });
    }
  });
  await context.addInitScript(({ token, userId, displayName }) => {
    localStorage.setItem('authToken', token);
    localStorage.setItem('authUser', JSON.stringify({ userId, nickname: displayName }));
    localStorage.setItem('userId', userId);
    localStorage.setItem('nickname', displayName);
  }, { token: auth.token, userId: auth.user_id, displayName: nickname });
  return context;
}

async function browserResume(page) {
  return page.evaluate(async ({ apiUrl, id }) => {
    const response = await fetch(`${apiUrl}/api/trip-workspaces/${encodeURIComponent(id)}/resume`, {
      headers: { Authorization: `Bearer ${localStorage.getItem('authToken')}` },
    });
    if (!response.ok) throw new Error(`browser resume failed: ${response.status}`);
    return response.json();
  }, { apiUrl: API_URL, id: workspaceId });
}

async function browserMembers(page) {
  return page.evaluate(async ({ apiUrl, id }) => {
    const response = await fetch(`${apiUrl}/api/trip-workspaces/${encodeURIComponent(id)}/members`, {
      headers: { Authorization: `Bearer ${localStorage.getItem('authToken')}` },
    });
    if (!response.ok) throw new Error(`browser member read failed: ${response.status}`);
    return response.json();
  }, { apiUrl: API_URL, id: workspaceId });
}

async function cleanupPostgres() {
  if (!/^e2e-dual-restart-[a-z-]+-\d+-[0-9a-f]+$/.test(roomId)) {
    throw new Error(`refusing to clean unexpected room id ${roomId}`);
  }
  const secret = process.env.E2E_CLEANUP_SECRET;
  if (!secret) throw new Error('E2E_CLEANUP_SECRET is required for public cleanup');
  for (const email of [emailA, emailB]) {
    if (!/^e2e\+dual-[ab]-\d+-[0-9a-f]+@example\.com$/.test(email)) {
      throw new Error(`refusing to clean unexpected email ${email}`);
    }
  }
  docker([
    'compose', 'exec', '-T',
    '-e', `TEST_E2E_SECRET=${secret}`,
    '-e', `TEST_E2E_ROOM=${roomId}`,
    '-e', `TEST_E2E_EMAIL_A=${emailA}`,
    '-e', `TEST_E2E_EMAIL_B=${emailB}`,
    'backend', 'python', '-c',
    "import json,os,urllib.request; body=json.dumps({'room_id':os.environ['TEST_E2E_ROOM'],'emails':[os.environ['TEST_E2E_EMAIL_A'],os.environ['TEST_E2E_EMAIL_B']]}).encode(); req=urllib.request.Request('http://127.0.0.1:8000/api/e2e/cleanup',data=body,method='POST',headers={'Content-Type':'application/json','X-E2E-Cleanup-Secret':os.environ['TEST_E2E_SECRET']}); print(urllib.request.urlopen(req,timeout=10).read().decode())",
  ]);
}

async function cleanupYjsDocument() {
  if (!/^e2e-dual-restart-room-\d+-[0-9a-f]+$/.test(roomId)) {
    throw new Error(`refusing to clean unexpected Yjs room ${roomId}`);
  }
  const secret = process.env.E2E_CLEANUP_SECRET;
  if (!secret) throw new Error('E2E_CLEANUP_SECRET is required for public Yjs cleanup');
  docker([
    'compose', 'exec', '-T',
    '-e', `TEST_E2E_SECRET=${secret}`,
    '-e', `TEST_E2E_ROOM=${roomId}`,
    'y-websocket', 'node', '-e',
    "fetch('http://127.0.0.1:1234/__e2e/doc/'+encodeURIComponent(process.env.TEST_E2E_ROOM),{method:'DELETE',headers:{'X-E2E-Cleanup-Secret':process.env.TEST_E2E_SECRET}}).then(async r=>{if(!r.ok)throw new Error(r.status+' '+await r.text());console.log(await r.text())}).catch(e=>{console.error(e);process.exit(1)})",
  ]);
  cleanup.yjs_document = 'CLEARED';
  cleanup.yjs_service_restored = 'RUNNING';
}

test.beforeAll(async ({ playwright }) => {
  test.setTimeout(240_000);
  if (process.env.BREEZE_E2E_ALLOW_SERVICE_RESTART !== '1') {
    throw new Error('set BREEZE_E2E_ALLOW_SERVICE_RESTART=1 to authorize the controlled local Backend/Yjs restart');
  }
  api = await playwright.request.newContext({ baseURL: API_URL });
});

test.afterAll(async () => {
  try {
    await contextA?.close();
    await contextB?.close();
    contextA = null;
    contextB = null;
    if (authA || authB) {
      await cleanupPostgres();
      cleanup.postgres = 'CLEARED';
    }
    await cleanupYjsDocument();
  } catch (reason) {
    cleanup.error = reason instanceof Error ? reason.message : String(reason);
    throw reason;
  } finally {
    await api?.dispose();
    evidence.finished_at = new Date().toISOString();
    persistEvidence();
  }
});

test('two real browser users recover the same authoritative state after Backend and Yjs restart', async ({ browser }) => {
  test.setTimeout(240_000);
  const editRequests = [];
  try {
    expect(docker(['compose', 'exec', '-T', 'backend', 'printenv', 'AMAP_MOCK'])).toBe('true');
    expect(docker(['compose', 'exec', '-T', 'backend', 'printenv', 'FT_ROUTER_ENABLED'])).toBe('false');

    authA = await register(emailA, 'E2E Alpha');
    authB = await register(emailB, 'E2E Beta');
    expect(authA.user_id).not.toBe(authB.user_id);

    let response = await api.post('/api/room', {
      headers: { Authorization: `Bearer ${authA.token}` },
      data: { room_id: roomId, thread_id: threadId, trip_city: '杭州', trip_days: 2 },
    });
    expect(response.status(), await response.text()).toBe(200);
    response = await api.post(`/api/room/${encodeURIComponent(roomId)}/join`, {
      headers: { Authorization: `Bearer ${authB.token}` },
      data: { nickname: 'E2E Beta' },
    });
    expect(response.status(), await response.text()).toBe(200);

    response = await api.post('/api/trip-workspaces', {
      headers: { Authorization: `Bearer ${authA.token}` },
      data: {
        workspace_id: workspaceId,
        room_id: roomId,
        city: '杭州',
        trip_date_range: { start: '2026-10-01', end: '2026-10-02' },
        initial_itinerary: {
          itinerary_id: `e2e-itinerary-${suffix}`,
          thread_id: threadId,
          city: '杭州',
          generated_at: '2026-08-20T00:00:00Z',
          version: 1,
          days: [
            {
              day_index: 0,
              date: '2026-10-01',
              cluster_id: 0,
              slots: [
                { place_id: `e2e-west-lake-${suffix}`, place: { name: '西湖', category: 'attraction' }, start_time: '09:00', end_time: '10:30', tips: [] },
                { place_id: `e2e-lingyin-${suffix}`, place: { name: '灵隐寺', category: 'attraction' }, start_time: '11:30', end_time: '13:00', tips: [] },
              ],
            },
            { day_index: 1, date: '2026-10-02', cluster_id: 1, slots: [] },
          ],
        },
      },
    });
    expect(response.status(), await response.text()).toBe(201);

    contextA = await newUserContext(browser, authA, 'E2E Alpha', editRequests);
    contextB = await newUserContext(browser, authB, 'E2E Beta', editRequests);
    const pageA = await contextA.newPage();
    const pageB = await contextB.newPage();
    await Promise.all([pageA.goto(`/workspace/${workspaceId}`), pageB.goto(`/workspace/${workspaceId}`)]);
    for (const page of [pageA, pageB]) {
      await expect(page.getByText('服务端 revision 1')).toBeVisible();
      await expect(page.getByText(/协同引用 已连接/)).toBeVisible();
      await expect(page.getByTestId('member-confirmation-panel')).toContainText(authA.user_id);
      await expect(page.getByTestId('member-confirmation-panel')).toContainText(authB.user_id);
    }
    expect(await pageA.evaluate(() => JSON.parse(localStorage.getItem('authUser')).userId)).toBe(authA.user_id);
    expect(await pageB.evaluate(() => JSON.parse(localStorage.getItem('authUser')).userId)).toBe(authB.user_id);

    const memberPanelB = pageB.getByTestId('member-confirmation-panel');
    await memberPanelB.getByLabel('约束类型').fill('latest_return_time');
    await memberPanelB.getByLabel('约束值').fill('20:30');
    await memberPanelB.getByRole('button', { name: '以本人确认写入 HARD 约束' }).click();
    await expect(memberPanelB).toContainText('latest_return_time EQ 20:30');
    await pageA.getByRole('button', { name: '刷新成员状态' }).click();
    await expect(pageA.getByTestId('member-confirmation-panel')).toContainText('latest_return_time EQ 20:30');

    const waitA = pageA.waitForResponse(item => item.request().method() === 'POST' && item.url().includes(`/api/trip-workspaces/${workspaceId}/edits`));
    const waitB = pageB.waitForResponse(item => item.request().method() === 'POST' && item.url().includes(`/api/trip-workspaces/${workspaceId}/edits`));
    await Promise.all([
      pageA.getByRole('button', { name: '下移' }).first().click(),
      pageB.getByRole('button', { name: '下移' }).first().click(),
    ]);
    const [raceA, raceB] = await Promise.all([waitA, waitB]);
    const race = [
      { label: 'A', page: pageA, status: raceA.status(), body: await raceA.json() },
      { label: 'B', page: pageB, status: raceB.status(), body: await raceB.json() },
    ];
    expect(race.map(item => item.status).sort()).toEqual([200, 409]);
    const winner = race.find(item => item.status === 200);
    const loser = race.find(item => item.status === 409);
    expect(winner.body.new_revision).toBe(2);
    expect(winner.body.llm_calls).toBe(0);
    expect(loser.body.detail.code).toBe('ITINERARY_REVISION_CONFLICT');
    await expect(winner.page.getByText('服务端 revision 2')).toBeVisible();
    const recovery = loser.page.getByTestId('workspace-conflict-recovery');
    await expect(recovery).toContainText('服务端当前');
    await expect(recovery).toContainText('2');
    await expect(recovery).toContainText('本地乐观预览已回滚');
    expect(editRequests).toHaveLength(2);
    expect(new Set(editRequests.map(item => item.body.command_id)).size).toBe(2);

    await Promise.all([pageA.reload(), pageB.reload()]);
    for (const page of [pageA, pageB]) {
      await expect(page.getByText('服务端 revision 2')).toBeVisible();
      await expect(page.getByText(/协同引用 已连接/)).toBeVisible();
      await expect(page.getByTestId('member-confirmation-panel')).toContainText('latest_return_time EQ 20:30');
    }
    await new Promise(resolve => setTimeout(resolve, 1_200));
    expect(editRequests).toHaveLength(2);
    const revisions = await authorizedGet(authA, `/api/trip-workspaces/${workspaceId}/revisions`);
    expect(revisions.map(item => item.revision)).toEqual([1, 2]);

    await winner.page.getByRole('button', { name: '最终完整审计' }).click();
    await expect(winner.page.getByText('完整审计已完成')).toBeVisible();
    const preRestartResume = await authorizedGet(authA, `/api/trip-workspaces/${workspaceId}/resume`);
    expect(preRestartResume.current_revision.revision).toBe(2);
    expect(preRestartResume.workspace.current_member_constraint_revision).toBe(1);
    expect(preRestartResume.current_report.report_id).toBeTruthy();
    const expectedRefs = {
      itinerary_revision: 2,
      itinerary_content_hash: preRestartResume.current_revision.content_hash,
      audit_report_id: preRestartResume.current_report.report_id,
      audit_revision: 2,
      member_constraint_revision: 1,
      places: [],
      builder_events: [],
    };
    const collaborativeYjs = await writeTwoClientYjsState();
    expectedRefs.places = collaborativeYjs.places;
    expectedRefs.builder_events = collaborativeYjs.builder_events;
    const preRestartYjsRefs = await waitForYjsReferences(authA, expectedRefs);
    await new Promise(resolve => setTimeout(resolve, 800));

    await contextA.close();
    await contextB.close();
    contextA = null;
    contextB = null;

    const servicesBefore = {
      backend: inspectContainer('backend'),
      y_websocket: inspectContainer('y-websocket'),
      postgres: inspectContainer('postgres'),
    };
    const healthBefore = {
      backend: await healthJson(`${API_URL}/health`),
      y_websocket: await healthJson(YJS_HTTP_URL),
    };
    const bootBefore = {
      backend: validateBootWitness(healthBefore.backend, 'breezetravel-backend'),
      y_websocket: validateBootWitness(healthBefore.y_websocket, 'breezetravel-yjs'),
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
    const healthAfter = {
      backend: await healthJson(`${API_URL}/health`),
      y_websocket: await healthJson(YJS_HTTP_URL),
    };
    const bootAfter = {
      backend: validateBootWitness(healthAfter.backend, 'breezetravel-backend'),
      y_websocket: validateBootWitness(healthAfter.y_websocket, 'breezetravel-yjs'),
    };
    expect(servicesAfter.backend.id).toBe(servicesBefore.backend.id);
    expect(servicesAfter.y_websocket.id).toBe(servicesBefore.y_websocket.id);
    expect(servicesAfter.postgres.id).toBe(servicesBefore.postgres.id);
    expect(servicesAfter.backend.started_at).not.toBe(servicesBefore.backend.started_at);
    expect(servicesAfter.y_websocket.started_at).not.toBe(servicesBefore.y_websocket.started_at);
    expect(servicesAfter.postgres.started_at).toBe(servicesBefore.postgres.started_at);
    for (const service of ['backend', 'y_websocket']) {
      expect(bootAfter[service].instance_id).not.toBe(bootBefore[service].instance_id);
      expect(bootAfter[service].started_at).not.toBe(bootBefore[service].started_at);
    }
    expect(servicesAfter.backend.host_pid).not.toBe(servicesBefore.backend.host_pid);
    expect(servicesAfter.y_websocket.host_pid).not.toBe(servicesBefore.y_websocket.host_pid);
    const yjsVolumeBefore = servicesBefore.y_websocket.mounts.find(item => item.destination === '/data');
    const yjsVolumeAfter = servicesAfter.y_websocket.mounts.find(item => item.destination === '/data');
    expect(yjsVolumeAfter.name).toBe(yjsVolumeBefore.name);

    // Both browser contexts were closed before restart. A new Node Yjs client
    // therefore proves the refs came back from the named LevelDB volume, not
    // from an in-memory browser document that re-seeded an empty server.
    const postRestartYjsRefs = await waitForYjsReferences(authA, expectedRefs);

    contextA = await newUserContext(browser, authA, 'E2E Alpha', editRequests);
    contextB = await newUserContext(browser, authB, 'E2E Beta', editRequests);
    const recoveredA = await contextA.newPage();
    const recoveredB = await contextB.newPage();
    await Promise.all([recoveredA.goto(`/workspace/${workspaceId}`), recoveredB.goto(`/workspace/${workspaceId}`)]);
    for (const page of [recoveredA, recoveredB]) {
      await expect(page.getByText('服务端 revision 2')).toBeVisible();
      await expect(page.getByText(/协同引用 已连接/)).toBeVisible();
      await expect(page.getByText('完整审计已完成')).toBeVisible();
      await expect(page.getByTestId('member-confirmation-panel')).toContainText('latest_return_time EQ 20:30');
      await expect(page.getByTestId('member-confirmation-panel')).toContainText(authA.user_id);
      await expect(page.getByTestId('member-confirmation-panel')).toContainText(authB.user_id);
    }
    const [resumeA, resumeB, membersA, membersB] = await Promise.all([
      browserResume(recoveredA), browserResume(recoveredB), browserMembers(recoveredA), browserMembers(recoveredB),
    ]);
    const canonicalResume = value => ({
      revision: value.current_revision.revision,
      content_hash: value.current_revision.content_hash,
      report_id: value.current_report.report_id,
      report_revision: value.current_report.itinerary_revision,
      member_constraint_revision: value.workspace.current_member_constraint_revision,
    });
    expect(canonicalResume(resumeA)).toEqual(canonicalResume(resumeB));
    expect(canonicalResume(resumeA)).toEqual({
      revision: 2,
      content_hash: expectedRefs.itinerary_content_hash,
      report_id: expectedRefs.audit_report_id,
      report_revision: 2,
      member_constraint_revision: 1,
    });
    expect(membersA).toEqual(membersB);
    expect(membersA.find(item => item.member_id === authB.user_id).constraints).toEqual(
      expect.arrayContaining([expect.objectContaining({ type: 'latest_return_time', value: '20:30', hardness: 'HARD' })]),
    );
    await new Promise(resolve => setTimeout(resolve, 1_000));
    expect(editRequests).toHaveLength(2);
    expect((await authorizedGet(authB, `/api/trip-workspaces/${workspaceId}/revisions`)).map(item => item.revision)).toEqual([1, 2]);

    evidence = {
      ...evidence,
      status: 'PASSED',
      users: [
        { role: 'A', user_id: authA.user_id, browser_context: 'isolated-A' },
        { role: 'B', user_id: authB.user_id, browser_context: 'isolated-B' },
      ],
      race: {
        statuses: race.map(item => ({ user: item.label, http_status: item.status })),
        winner: winner.label,
        loser: loser.label,
        accepted_revision: 2,
        server_revisions_after_reload: [1, 2],
        edit_post_count_after_restart: editRequests.length,
        winning_edit_llm_calls: winner.body.llm_calls,
      },
      references: {
        expected: expectedRefs,
        yjs_before_restart: preRestartYjsRefs,
        yjs_after_restart_fresh_client: postRestartYjsRefs,
        browser_a_after_restart: canonicalResume(resumeA),
        browser_b_after_restart: canonicalResume(resumeB),
      },
      services: {
        before_restart: servicesBefore,
        after_restart: servicesAfter,
        boot_before: bootBefore,
        boot_after: bootAfter,
        stopped_ports_observed_unavailable: true,
      },
      assertions: {
        independent_browser_contexts_and_accounts: true,
        member_hard_constraint_visible_to_both: true,
        concurrent_revision_race_exactly_one_success_one_conflict: true,
        stale_optimistic_operation_not_replayed: true,
        backend_process_restarted: true,
        yjs_process_restarted_with_same_named_volume: true,
        backend_and_yjs_boot_generation_changed: true,
        stopped_ports_were_unavailable_before_start: true,
        postgres_container_not_restarted: true,
        two_yjs_clients_created_edited_and_recorded_accept_event: true,
        yjs_places_and_builder_event_refs_recovered_exactly: true,
        yjs_refs_recovered_by_fresh_client_before_browser_reconnect: true,
        both_fresh_browser_contexts_read_same_revision_report_and_member_refs: true,
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
