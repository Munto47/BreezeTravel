'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiRequestError } from '@/lib/api'
import { parseCurrentRoomRoute, type CurrentRoomRoute } from '@/lib/current-room-route'
import { recoverExpiredLogin, runWithDeadline } from '@/lib/request-safety'
import type { Place } from '@/types/place'

const API_BASE = process.env.NEXT_PUBLIC_API_URL || ''

export function useCurrentRoomRoute(roomId: string, userId: string, token: string | null,
  enabled: boolean, notifiedVersion: number, announceVersion: (version: number) => void) {
  const [route, setRoute] = useState<CurrentRoomRoute | null>(null)
  const [loading, setLoading] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [message, setMessage] = useState('')
  const [needsReadback, setNeedsReadback] = useState(false)
  const current = useRef<CurrentRoomRoute | null>(null)
  const generation = useRef(0)
  const readSequence = useRef(0)
  const busy = useRef(false)
  const controllers = useRef(new Set<AbortController>())
  const identity = `${roomId}:${userId}:${token || ''}`
  const identityRef = useRef(identity)
  identityRef.current = identity
  const active = useRef(false)

  const request = useCallback(async (path: string, body?: unknown) => {
    const controller = new AbortController()
    controllers.current.add(controller)
    try {
      return await runWithDeadline(async signal => {
        const response = await fetch(`${API_BASE}${path}`, {
          method: body === undefined ? 'GET' : 'POST', signal,
          cache: 'no-store',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        })
        const data = await response.json()
        if (response.status === 401 && identityRef.current === identity) recoverExpiredLogin()
        if (!response.ok) throw new ApiRequestError('共同路线请求失败', {
          status: response.status, context: typeof data.detail === 'object' ? data.detail : {},
        })
        return data
      }, body === undefined ? 15000 : 45000, controller.signal)
    } finally { controllers.current.delete(controller) }
  }, [identity, token])

  const refresh = useCallback(async (): Promise<CurrentRoomRoute | null> => {
    if (!enabled || !token || !userId || !active.current) return null
    const run = ++readSequence.current, epoch = generation.current
    const valid = () => active.current && epoch === generation.current && identityRef.current === identity && run === readSequence.current
    setLoading(true)
    try {
      const next = parseCurrentRoomRoute(await request(`/api/room/${encodeURIComponent(roomId)}/current-itinerary`), roomId)
      if (!valid()) return null
      // A notification is untrusted. Neither it nor an older GET may lower the authoritative version.
      if (current.current && next.version < current.current.version) return current.current
      if (current.current && next.version === current.current.version) {
        setMessage('')
        setNeedsReadback(false)
        return current.current
      }
      current.current = next
      setRoute(next)
      setMessage('')
      setNeedsReadback(false)
      return next
    } catch {
      if (valid()) {
        setNeedsReadback(true)
        setMessage('共同路线暂时无法读取，原路线保留。请重新读取后再排线。')
      }
      return null
    } finally { if (valid()) setLoading(false) }
  }, [enabled, identity, request, roomId, token, userId])

  useEffect(() => {
    generation.current++
    active.current = enabled && Boolean(token && userId)
    current.current = null
    setRoute(null)
    setMessage('')
    setNeedsReadback(false)
    setPublishing(false)
    busy.current = false
    void refresh()
    return () => {
      active.current = false
      generation.current++
      readSequence.current++
      for (const controller of controllers.current) controller.abort()
      controllers.current.clear()
    }
  }, [identity, enabled, refresh, token, userId])

  useEffect(() => {
    if (!busy.current && notifiedVersion > (current.current?.version ?? 0)) void refresh()
  }, [notifiedVersion, publishing, refresh])

  useEffect(() => {
    const onFocus = () => { if (!busy.current && document.visibilityState === 'visible') void refresh() }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onFocus)
    return () => {
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onFocus)
    }
  }, [refresh])

  const publish = useCallback(async (places: Place[], tripDays: number, threadId: string): Promise<boolean> => {
    if (!active.current || busy.current || loading || needsReadback || !current.current) return false
    const baseVersion = current.current.version, epoch = generation.current
    const valid = () => active.current && epoch === generation.current && identityRef.current === identity
    busy.current = true
    setPublishing(true)
    setMessage('')
    let acknowledged = false, conflict = false
    try {
      const response = await request('/api/optimize', {
        thread_id: threadId, room_id: roomId, trip_days: tripDays, relative_only: true,
        persist_workspace: false, base_room_route_version: baseVersion,
        room_route_request_id: crypto.randomUUID(),
        places: places.map(p => ({ place_id: p.placeId, name: p.name, category: p.category,
          address: p.address, coords: p.coords, city: p.city, source: p.source,
          amap_rating: p.amapRating, amap_price: p.amapPrice, amap_photos: p.amapPhotos,
          description: p.description, tags: p.tags })),
      })
      if (!valid()) return false
      if (!Number.isSafeInteger(response.room_route_version) || response.room_route_version <= baseVersion)
        throw new Error('INVALID_PUBLISHED_ROUTE')
      acknowledged = true
      announceVersion(response.room_route_version)
    } catch (failure) {
      if (!valid()) return false
      conflict = failure instanceof ApiRequestError && failure.code === 'ROOM_ROUTE_VERSION_CONFLICT'
    }
    try {
      // A lost response must read PostgreSQL, never silently repeat optimization.
      const latest = await refresh()
      if (!valid()) return false
      if (latest && latest.version > baseVersion) {
        announceVersion(latest.version)
        if (conflict || !acknowledged) setMessage('已读取最新共同路线，请核对同行者的方案；没有自动重新排线。')
        return true
      }
      setNeedsReadback(true)
      setMessage(acknowledged
        ? '排线已提交，共同路线尚未读回。请重新读取，不要重复排线。'
        : conflict ? '共同路线已变化，请重新读取后核对；没有覆盖同行者的方案。'
          : '尚未确认排线是否保存。原路线保留，请先重新读取共同路线。')
      return false
    } finally {
      if (valid()) { busy.current = false; setPublishing(false) }
    }
  }, [announceVersion, identity, loading, needsReadback, refresh, request, roomId])

  return { route, loading, publishing, message, needsReadback, refresh, publish }
}
