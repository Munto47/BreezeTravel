'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const jwt = require('jsonwebtoken')
const { roomFromRequest, verifyHandshake } = require('./server')

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
