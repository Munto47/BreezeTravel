'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const http = require('node:http')
const { spawn } = require('node:child_process')
const jwt = require('jsonwebtoken')
const WebSocket = require('ws')
const Y = require('yjs')
const { WebsocketProvider } = require('y-websocket')

const secret = 'restart-test-secret-at-least-32-characters'
const cleanupSecret = 'restart-cleanup-secret-at-least-32-chars'

function freePort() {
  return 19000 + Math.floor(Math.random() * 1000)
}

async function waitForHealth(port) {
  for (let i = 0; i < 50; i += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}`)
      if (response.ok) return response.json()
    } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 50))
  }
  throw new Error('y-websocket child did not become healthy')
}

async function startServer(port, persistence) {
  const child = spawn(process.execPath, ['server.js'], {
    cwd: __dirname,
    env: {
      ...process.env,
      HOST: '127.0.0.1',
      PORT: String(port),
      JWT_SECRET_KEY: secret,
      YPERSISTENCE: persistence,
      YJS_E2E_CLEANUP_SECRET: cleanupSecret,
      YJS_RESTART_GATE_MODE: 'true',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const health = await waitForHealth(port)
  if (health.service !== 'breezetravel-yjs' || !health.boot_generation?.instance_id) {
    await stopServer(child)
    throw new Error('occupied port or invalid Yjs boot witness')
  }
  return { child, health }
}

async function stopServer(child) {
  if (child.exitCode !== null) return
  child.kill('SIGTERM')
  await Promise.race([
    new Promise(resolve => child.once('exit', resolve)),
    new Promise(resolve => setTimeout(resolve, 1000)),
  ])
  if (child.exitCode === null) child.kill('SIGKILL')
}

async function waitForUnavailable(port) {
  for (let i = 0; i < 50; i += 1) {
    try {
      await fetch(`http://127.0.0.1:${port}`)
    } catch (_) {
      return
    }
    await new Promise(resolve => setTimeout(resolve, 20))
  }
  throw new Error('stopped Yjs child remained reachable')
}

function roomToken(roomId) {
  return jwt.sign(
    { sub: 'restart-user', room_id: roomId, token_type: 'room_ws', scope: ['yjs:connect'] },
    secret,
    { audience: 'breezetravel-yjs', expiresIn: 60, algorithm: 'HS256' },
  )
}

function syncedProvider(port, roomId, doc) {
  return new Promise((resolve, reject) => {
    const provider = new WebsocketProvider(`ws://127.0.0.1:${port}`, roomId, doc, {
      WebSocketPolyfill: WebSocket,
      params: { token: roomToken(roomId) },
    })
    const timer = setTimeout(() => reject(new Error('Yjs sync timeout')), 3000)
    provider.once('sync', isSynced => {
      if (!isSynced) return
      clearTimeout(timer)
      resolve(provider)
    })
  })
}

test('restores persisted Yjs room state after a real server restart', async () => {
  const persistence = fs.mkdtempSync(path.join(os.tmpdir(), 'breezetravel-yjs-'))
  const roomId = `e2e-dual-restart-room-${Date.now()}-abcdef12`
  let server
  try {
    const firstPort = freePort()
    server = await startServer(firstPort, persistence)
    const firstDoc = new Y.Doc()
    const firstProvider = await syncedProvider(firstPort, roomId, firstDoc)
    firstDoc.getMap('room').set('tripCity', '杭州')
    firstDoc.getMap('places').set('west-lake', { name: '西湖', note: 'edited-by-client-b' })
    firstDoc.getArray('builderEvents').push([{ event_id: 'accept-1', event_type: 'candidate_accepted' }])
    await new Promise(resolve => setTimeout(resolve, 300))
    firstProvider.destroy()
    firstDoc.destroy()
    const firstWitness = server.health.boot_generation
    const firstPid = server.child.pid
    await stopServer(server.child)
    await waitForUnavailable(firstPort)

    const secondPort = freePort()
    server = await startServer(secondPort, persistence)
    const secondDoc = new Y.Doc()
    const secondProvider = await syncedProvider(secondPort, roomId, secondDoc)
    await new Promise(resolve => setTimeout(resolve, 200))
    assert.equal(secondDoc.getMap('room').get('tripCity'), '杭州')
    assert.deepEqual(secondDoc.getMap('places').get('west-lake'), { name: '西湖', note: 'edited-by-client-b' })
    assert.deepEqual(secondDoc.getArray('builderEvents').toArray(), [{ event_id: 'accept-1', event_type: 'candidate_accepted' }])
    assert.notEqual(server.health.boot_generation.instance_id, firstWitness.instance_id)
    assert.notEqual(server.health.boot_generation.started_at, firstWitness.started_at)
    assert.notEqual(server.child.pid, firstPid)
    secondProvider.destroy()
    secondDoc.destroy()
    await new Promise(resolve => setTimeout(resolve, 100))
    const wrongCleanup = await fetch(`http://127.0.0.1:${secondPort}/__e2e/doc/${roomId}`, {
      method: 'DELETE',
      headers: { 'X-E2E-Cleanup-Secret': `${cleanupSecret}-wrong` },
    })
    assert.equal(wrongCleanup.status, 404)
    const cleanup = await fetch(`http://127.0.0.1:${secondPort}/__e2e/doc/${roomId}`, {
      method: 'DELETE',
      headers: { 'X-E2E-Cleanup-Secret': cleanupSecret },
    })
    assert.equal(cleanup.status, 200)
    const replay = await fetch(`http://127.0.0.1:${secondPort}/__e2e/doc/${roomId}`, {
      method: 'DELETE',
      headers: { 'X-E2E-Cleanup-Secret': cleanupSecret },
    })
    assert.equal(replay.status, 404)
    await stopServer(server.child)
    await waitForUnavailable(secondPort)

    const thirdPort = freePort()
    server = await startServer(thirdPort, persistence)
    const thirdDoc = new Y.Doc()
    const thirdProvider = await syncedProvider(thirdPort, roomId, thirdDoc)
    await new Promise(resolve => setTimeout(resolve, 100))
    assert.equal(thirdDoc.getMap('places').size, 0)
    assert.equal(thirdDoc.getArray('builderEvents').length, 0)
    thirdProvider.destroy()
    thirdDoc.destroy()
  } finally {
    if (server?.child) await stopServer(server.child)
    fs.rmSync(persistence, { recursive: true, force: true })
  }
})

test('fails closed when the requested Yjs port belongs to another process', async () => {
  const occupied = http.createServer((_, response) => {
    response.writeHead(200, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify({ status: 'ok', service: 'not-breezetravel-yjs' }))
  })
  await new Promise((resolve, reject) => {
    occupied.once('error', reject)
    occupied.listen(0, '127.0.0.1', resolve)
  })
  const port = occupied.address().port
  const persistence = fs.mkdtempSync(path.join(os.tmpdir(), 'breezetravel-yjs-port-'))
  try {
    await assert.rejects(startServer(port, persistence), /occupied port|invalid Yjs boot witness/)
  } finally {
    await new Promise(resolve => occupied.close(resolve))
    fs.rmSync(persistence, { recursive: true, force: true })
  }
})
