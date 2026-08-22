'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const jwt = require('jsonwebtoken')
const {
  bootGeneration,
  createServer,
  isLoopbackAddress,
  roomFromRequest,
  validCleanupRoomIds,
  verifyHandshake,
} = require('./server')

const secret = 'test-secret-at-least-32-characters'

function request(roomId, token) {
  return { url: `/${roomId}?token=${encodeURIComponent(token)}` }
}

test('accepts a valid room-bound token', () => {
  const token = jwt.sign(
    { sub: 'user-1', room_id: 'room-1', token_type: 'room_ws', scope: ['yjs:connect'] },
    secret,
    { audience: 'breezetravel-yjs', expiresIn: 60, algorithm: 'HS256' },
  )
  assert.equal(roomFromRequest(request('room-1', token)), 'room-1')
  assert.equal(verifyHandshake(request('room-1', token), secret).sub, 'user-1')
})

test('rejects token reuse in a different room', () => {
  const token = jwt.sign(
    { sub: 'user-1', room_id: 'room-1', token_type: 'room_ws', scope: ['yjs:connect'] },
    secret,
    { audience: 'breezetravel-yjs', expiresIn: 60, algorithm: 'HS256' },
  )
  assert.throws(() => verifyHandshake(request('room-2', token), secret), /scope mismatch/)
})

test('rejects expired and missing tokens', () => {
  const expired = jwt.sign(
    { sub: 'user-1', room_id: 'room-1', token_type: 'room_ws', scope: ['yjs:connect'] },
    secret,
    { audience: 'breezetravel-yjs', expiresIn: -1, algorithm: 'HS256' },
  )
  assert.throws(() => verifyHandshake(request('room-1', expired), secret), /expired/)
  assert.throws(() => verifyHandshake({ url: '/room-1' }, secret), /missing room token/)
})

test('health exposes one immutable process boot generation witness', async t => {
  const instance = createServer({ host: '127.0.0.1', port: 0, secret })
  await new Promise((resolve, reject) => {
    instance.server.once('error', reject)
    instance.server.listen(0, '127.0.0.1', resolve)
  })
  t.after(() => new Promise(resolve => instance.server.close(resolve)))
  const address = instance.server.address()
  const response = await fetch(`http://127.0.0.1:${address.port}/health`)
  const body = await response.json()
  assert.equal(response.status, 200)
  assert.equal(body.service, 'breezetravel-yjs')
  assert.deepEqual(body.boot_generation, bootGeneration)
  assert.match(body.boot_generation.instance_id, /^[0-9a-f-]{36}$/)
  assert.ok(Number.isInteger(body.boot_generation.pid) && body.boot_generation.pid > 0)
  assert.ok(Number.isFinite(Date.parse(body.boot_generation.started_at)))
})

test('cleanup source contract accepts loopback only', () => {
  assert.equal(isLoopbackAddress('127.0.0.1'), true)
  assert.equal(isLoopbackAddress('::1'), true)
  assert.equal(isLoopbackAddress('::ffff:127.0.0.1'), true)
  assert.equal(isLoopbackAddress('172.20.0.1'), false)
  assert.equal(isLoopbackAddress('203.0.113.9'), false)
})

test('batch cleanup accepts exactly bounded unique isolated restart rooms', () => {
  const nine = Array.from({ length: 9 }, (_, index) => `e2e-dual-restart-room-${index + 1}-abcdef12`)
  assert.equal(validCleanupRoomIds(nine), true)
  assert.equal(validCleanupRoomIds([]), false)
  assert.equal(validCleanupRoomIds([...nine, nine[0]]), false)
  assert.equal(validCleanupRoomIds(['production-room']), false)
  assert.equal(validCleanupRoomIds(Array.from({ length: 13 }, (_, index) => `e2e-dual-restart-room-${index + 1}-abcdef12`)), false)
})

test('cleanup route is hidden when restart-gate mode is disabled', async t => {
  const instance = createServer({
    host: '127.0.0.1',
    port: 0,
    secret,
    cleanupSecret: 'cleanup-secret-at-least-32-characters',
    restartGateMode: false,
  })
  await new Promise((resolve, reject) => {
    instance.server.once('error', reject)
    instance.server.listen(0, '127.0.0.1', resolve)
  })
  t.after(() => new Promise(resolve => instance.server.close(resolve)))
  const address = instance.server.address()
  const response = await fetch(`http://127.0.0.1:${address.port}/__e2e/doc/e2e-dual-restart-room-1-abcdef12`, {
    method: 'DELETE',
    headers: { 'X-E2E-Cleanup-Secret': 'cleanup-secret-at-least-32-characters' },
  })
  assert.equal(response.status, 404)

  const batch = await fetch(`http://127.0.0.1:${address.port}/__e2e/docs`, {
    method: 'DELETE',
    headers: {
      'Content-Type': 'application/json',
      'X-E2E-Cleanup-Secret': 'cleanup-secret-at-least-32-characters',
    },
    body: JSON.stringify({ room_ids: ['e2e-dual-restart-room-1-abcdef12'] }),
  })
  assert.equal(batch.status, 404)
})
