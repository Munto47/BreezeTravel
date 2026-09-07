'use strict'

const http = require('http')
const crypto = require('crypto')
const jwt = require('jsonwebtoken')
const WebSocket = require('ws')
const Y = require('yjs')
const { docs, getPersistence, setupWSConnection } = require('y-websocket/bin/utils')

const bootGeneration = Object.freeze({
  instance_id: crypto.randomUUID(),
  started_at: new Date().toISOString(),
  pid: process.pid,
})

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

function isLoopbackAddress(address) {
  return ['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(address)
}

const cleanupRoomPattern = /^e2e-dual-restart-room-\d+-[0-9a-f]+$/

function validCleanupRoomIds(roomIds) {
  return Array.isArray(roomIds) &&
    roomIds.length >= 1 && roomIds.length <= 12 &&
    new Set(roomIds).size === roomIds.length &&
    roomIds.every(roomId => typeof roomId === 'string' && cleanupRoomPattern.test(roomId))
}

async function readJsonBody(req, maxBytes = 16 * 1024) {
  const chunks = []
  let received = 0
  for await (const chunk of req) {
    received += chunk.length
    if (received > maxBytes) throw new Error('request body too large')
    chunks.push(chunk)
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'))
}

function createServer(options = {}) {
  const host = options.host || process.env.HOST || '0.0.0.0'
  const port = Number(options.port || process.env.PORT || 1234)
  const secret = options.secret || process.env.JWT_SECRET_KEY
  const maxPayload = Number(process.env.YJS_MAX_PAYLOAD_BYTES || 262144)
  const maxPerIp = Number(process.env.YJS_MAX_CONNECTIONS_PER_IP || 20)
  const cleanupSecret = options.cleanupSecret || process.env.YJS_E2E_CLEANUP_SECRET || ''
  const restartGateMode = String(options.restartGateMode ?? process.env.YJS_RESTART_GATE_MODE ?? 'false') === 'true'
  let cleanupSecretConsumed = false
  let closing = false
  let closePromise = null
  if (!secret) throw new Error('JWT_SECRET_KEY is required')

  const connectionsByIp = new Map()
  const server = http.createServer(async (req, res) => {
    if (closing) {
      res.writeHead(503, { 'Content-Type': 'application/json', 'Connection': 'close' })
      res.end(JSON.stringify({ detail: 'service stopping' }))
      return
    }
    const url = new URL(req.url, 'http://localhost')
    const singleCleanup = req.method === 'DELETE' && url.pathname.startsWith('/__e2e/doc/')
    const batchCleanup = req.method === 'DELETE' && url.pathname === '/__e2e/docs'
    if (singleCleanup || batchCleanup) {
      const supplied = req.headers['x-e2e-cleanup-secret'] || ''
      const suppliedDigest = crypto.createHash('sha256').update(String(supplied)).digest()
      const expectedDigest = crypto.createHash('sha256').update(cleanupSecret).digest()
      const authorized = restartGateMode &&
        cleanupSecret.length >= 32 &&
        String(supplied).length >= 32 &&
        crypto.timingSafeEqual(suppliedDigest, expectedDigest) &&
        !cleanupSecretConsumed &&
        req.socket.remoteAddress &&
        isLoopbackAddress(req.socket.remoteAddress)
      if (!authorized) {
        res.writeHead(404, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ detail: 'not found' }))
        return
      }
      let roomIds
      try {
        roomIds = singleCleanup
          ? [decodeURIComponent(url.pathname.slice('/__e2e/doc/'.length))]
          : (await readJsonBody(req)).room_ids
      } catch (_) {
        res.writeHead(400, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ detail: 'invalid cleanup request' }))
        return
      }
      if (!validCleanupRoomIds(roomIds)) {
        res.writeHead(400, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ detail: 'only isolated E2E rooms may be removed' }))
        return
      }
      const activeDocs = roomIds.map(roomId => [roomId, docs.get(roomId)])
      if (activeDocs.some(([, active]) => active && active.conns.size > 0)) {
        res.writeHead(409, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ detail: 'one or more Yjs rooms still have active clients' }))
        return
      }
      const persistence = getPersistence()
      if (!persistence?.provider?.clearDocument) {
        res.writeHead(409, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ detail: 'Yjs persistence is unavailable' }))
        return
      }
      // Consume before the first await so two concurrent cleanup requests
      // cannot both pass authorization. Failure remains fail-closed and does
      // not make this destructive capability reusable.
      cleanupSecretConsumed = true
      try {
        for (const [roomId, active] of activeDocs) {
          if (active) {
            docs.delete(roomId)
            active.destroy()
          }
          await persistence.provider.clearDocument(roomId)
        }
        res.writeHead(200, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({
          ok: true,
          room_ids: roomIds,
          room_count: roomIds.length,
          ...(singleCleanup ? { room_id: roomIds[0] } : {}),
        }))
      } catch (_) {
        res.writeHead(500, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ detail: 'Yjs cleanup failed' }))
      }
      return
    }
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({
      status: 'ok',
      service: 'breezetravel-yjs',
      boot_generation: bootGeneration,
    }))
  })
  const wss = new WebSocket.Server({ noServer: true, maxPayload })

  server.on('upgrade', (req, socket, head) => {
    if (closing) {
      socket.write('HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n')
      socket.destroy()
      return
    }
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

  const close = (timeoutMs = 5000) => {
    if (closePromise) return closePromise
    closing = true
    closePromise = (async () => {
      const persistence = getPersistence()
      if (persistence?.provider?.storeUpdate) {
        const durableSnapshots = Array.from(
          docs.entries(),
          ([name, doc]) => [name, Y.encodeStateAsUpdate(doc)],
        )
        for (const [name, update] of durableSnapshots) {
          await persistence.provider.storeUpdate(name, update)
        }
      }
      const httpClosed = new Promise(resolve => {
        if (!server.listening) {
          resolve()
          return
        }
        server.close(() => resolve())
      })
      const clientsClosed = Promise.all(Array.from(wss.clients, ws => new Promise(resolve => {
        if (ws.readyState === WebSocket.CLOSED) {
          resolve()
          return
        }
        ws.once('close', resolve)
        ws.close(1001, 'service stopping')
      })))
      let shutdownTimer
      const timeout = new Promise((_, reject) => {
        shutdownTimer = setTimeout(() => reject(new Error('Yjs graceful shutdown timed out')), timeoutMs)
      })
      let shutdownError = null
      try {
        await Promise.race([clientsClosed, timeout])
      } catch (error) {
        shutdownError = error
        for (const ws of wss.clients) ws.terminate()
      } finally {
        clearTimeout(shutdownTimer)
      }
      await httpClosed
      if (persistence?.provider?.destroy) await persistence.provider.destroy()
      if (shutdownError) throw shutdownError
    })()
    return closePromise
  }

  return { server, wss, listen: () => server.listen(port, host), close }
}

if (require.main === module) {
  const instance = createServer()
  instance.listen()
  let stopping = false
  const stop = async () => {
    if (stopping) return
    stopping = true
    try {
      await instance.close()
      process.exitCode = 0
    } catch (_) {
      process.exitCode = 1
    } finally {
      if (process.connected) process.disconnect()
    }
  }
  for (const signal of ['SIGTERM', 'SIGINT', 'SIGBREAK']) {
    process.once(signal, () => { void stop() })
  }
  process.on('message', message => {
    if (message === 'shutdown') void stop()
  })
}

module.exports = {
  bootGeneration,
  createServer,
  isLoopbackAddress,
  roomFromRequest,
  validCleanupRoomIds,
  verifyHandshake,
}
