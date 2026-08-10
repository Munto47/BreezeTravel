'use strict'

const http = require('http')
const jwt = require('jsonwebtoken')
const WebSocket = require('ws')
const { setupWSConnection } = require('y-websocket/bin/utils')

function roomFromRequest(req) {
  const url = new URL(req.url, 'http://localhost')
  return decodeURIComponent(url.pathname.replace(/^\/+/, '').split('/')[0] || '')
}

function verifyHandshake(req, secret) {
  const url = new URL(req.url, 'http://localhost')
  const token = url.searchParams.get('token')
  const roomId = roomFromRequest(req)
  if (!token || !roomId) throw new Error('missing room token')
  const claims = jwt.verify(token, secret, {
    algorithms: ['HS256'],
    audience: 'breezetravel-yjs',
  })
  if (claims.token_type !== 'room_ws' || claims.room_id !== roomId) {
    throw new Error('room token scope mismatch')
  }
  if (!Array.isArray(claims.scope) || !claims.scope.includes('yjs:connect')) {
    throw new Error('room token lacks yjs scope')
  }
  return claims
}

function createServer(options = {}) {
  const host = options.host || process.env.HOST || '0.0.0.0'
  const port = Number(options.port || process.env.PORT || 1234)
  const secret = options.secret || process.env.JWT_SECRET_KEY
  const maxPayload = Number(process.env.YJS_MAX_PAYLOAD_BYTES || 262144)
  const maxPerIp = Number(process.env.YJS_MAX_CONNECTIONS_PER_IP || 20)
  if (!secret) throw new Error('JWT_SECRET_KEY is required')

  const connectionsByIp = new Map()
  const server = http.createServer((req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ status: 'ok', service: 'breezetravel-yjs' }))
  })
  const wss = new WebSocket.Server({ noServer: true, maxPayload })

  server.on('upgrade', (req, socket, head) => {
    const ip = req.socket.remoteAddress || 'unknown'
    if ((connectionsByIp.get(ip) || 0) >= maxPerIp) {
      socket.write('HTTP/1.1 429 Too Many Requests\r\nConnection: close\r\n\r\n')
      socket.destroy()
      return
    }
    let claims
    try {
      claims = verifyHandshake(req, secret)
    } catch (_) {
      socket.write('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n')
      socket.destroy()
      return
    }
    wss.handleUpgrade(req, socket, head, (ws) => {
      ws.roomClaims = claims
      wss.emit('connection', ws, req)
    })
  })

  wss.on('connection', (ws, req) => {
    const ip = req.socket.remoteAddress || 'unknown'
    connectionsByIp.set(ip, (connectionsByIp.get(ip) || 0) + 1)
    const expiresInMs = Math.max(0, Number(ws.roomClaims.exp) * 1000 - Date.now())
    const expiryTimer = setTimeout(() => ws.close(4001, 'room token expired'), expiresInMs)
    ws.on('close', () => {
      clearTimeout(expiryTimer)
      const next = Math.max(0, (connectionsByIp.get(ip) || 1) - 1)
      if (next === 0) connectionsByIp.delete(ip)
      else connectionsByIp.set(ip, next)
    })
    setupWSConnection(ws, req, { docName: roomFromRequest(req), gc: true })
  })

  return { server, wss, listen: () => server.listen(port, host) }
}

if (require.main === module) {
  const instance = createServer()
  instance.listen()
}

module.exports = { createServer, roomFromRequest, verifyHandshake }
