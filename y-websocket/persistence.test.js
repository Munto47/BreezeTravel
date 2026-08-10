'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { spawn } = require('node:child_process')
const jwt = require('jsonwebtoken')
const WebSocket = require('ws')
const Y = require('yjs')
const { WebsocketProvider } = require('y-websocket')

const secret = 'restart-test-secret-at-least-32-characters'

function freePort() {
  return 19000 + Math.floor(Math.random() * 1000)
}

async function waitForHealth(port) {
  for (let i = 0; i < 50; i += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}`)
      if (response.ok) return
    } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 50))
  }
  throw new Error('y-websocket child did not become healthy')
}

async function startServer(port, persistence) {
  const child = spawn(process.execPath, ['server.js'], {
    cwd: __dirname,
    env: { ...process.env, HOST: '127.0.0.1', PORT: String(port), JWT_SECRET_KEY: secret, YPERSISTENCE: persistence },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  await waitForHealth(port)
  return child
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
  const roomId = `restart-${Date.now()}`
  let server
  try {
    const firstPort = freePort()
    server = await startServer(firstPort, persistence)
    const firstDoc = new Y.Doc()
    const firstProvider = await syncedProvider(firstPort, roomId, firstDoc)
    firstDoc.getMap('room').set('tripCity', '杭州')
    await new Promise(resolve => setTimeout(resolve, 300))
    firstProvider.destroy()
    firstDoc.destroy()
    await stopServer(server)

    const secondPort = freePort()
    server = await startServer(secondPort, persistence)
    const secondDoc = new Y.Doc()
    const secondProvider = await syncedProvider(secondPort, roomId, secondDoc)
    await new Promise(resolve => setTimeout(resolve, 200))
    assert.equal(secondDoc.getMap('room').get('tripCity'), '杭州')
    secondProvider.destroy()
    secondDoc.destroy()
  } finally {
    if (server) await stopServer(server)
    fs.rmSync(persistence, { recursive: true, force: true })
  }
})
