'use client'

import { useEffect, useRef, useState, useCallback } from 'react'
import * as Y from 'yjs'
import { WebsocketProvider } from 'y-websocket'

import type { YjsPlace, YjsRoomMeta, RoomMember, RoomPhase } from '@/types/room'
import type { Place } from '@/types/place'
import type { ChatMessage } from '@/types/chat'
import { recoverExpiredLogin, runWithDeadline } from '@/lib/request-safety'

const Y_WEBSOCKET_URL = process.env.NEXT_PUBLIC_Y_WEBSOCKET_URL || 'ws://localhost:1234'

const ROOM_SELECTION = 'room-selection'
const ROOM_PHASES = new Set<RoomPhase>([
  'exploring',
  'selecting',
  'optimizing',
  'planned',
])
const PLACE_CATEGORIES = new Set<Place['category']>([
  'attraction',
  'food',
  'hotel',
  'transport',
])

function safeText(value: unknown, maxLength: number): string {
  return typeof value === 'string'
    ? value.replace(/[\u0000-\u001f]/g, ' ').trim().slice(0, maxLength)
    : ''
}

/** Yjs is shared transport, not an identity authority. */
export function parseSharedPlace(key: string, raw: unknown): YjsPlace | null {
  if (!raw || typeof raw !== 'object') return null
  const value = raw as Record<string, unknown>
  const coords = value.coords as Record<string, unknown> | undefined
  const placeId = safeText(key, 200)
  const name = safeText(value.name, 120)
  const category = value.category as Place['category']
  const lng = Number(coords?.lng)
  const lat = Number(coords?.lat)
  if (
    !placeId ||
    !name ||
    !PLACE_CATEGORIES.has(category) ||
    !Number.isFinite(lng) ||
    !Number.isFinite(lat) ||
    Math.abs(lng) > 180 ||
    Math.abs(lat) > 90
  )
    return null
  const source = ['amap_poi', 'rag', 'synthesized'].includes(String(value.source))
    ? (value.source as Place['source'])
    : 'synthesized'
  const finiteNumber = (candidate: unknown) => {
    const number = Number(candidate)
    return Number.isFinite(number) ? number : undefined
  }
  const selected = Array.isArray(value.votedBy) && value.votedBy.length > 0
  return {
    placeId,
    name,
    category,
    address: safeText(value.address, 240),
    coords: { lng, lat },
    city: safeText(value.city, 80),
    district: safeText(value.district, 80) || undefined,
    source,
    amapRating: finiteNumber(value.amapRating),
    amapPrice: finiteNumber(value.amapPrice),
    openingHours: safeText(value.openingHours, 160) || undefined,
    phone: safeText(value.phone, 80) || undefined,
    amapPhotos: Array.isArray(value.amapPhotos)
      ? value.amapPhotos
          .map((item) => safeText(item, 2048))
          .filter((item) => /^https?:\/\//.test(item))
          .slice(0, 5)
      : [],
    description: safeText(value.description, 1000) || undefined,
    tags: Array.isArray(value.tags)
      ? value.tags.map((item) => safeText(item, 40)).filter(Boolean).slice(0, 12)
      : [],
    constraintEvidence: [],
    geoEvidence: [],
    confirmationActions: [],
    clusterId: finiteNumber(value.clusterId),
    visitOrder: finiteNumber(value.visitOrder),
    estimatedDuration: finiteNumber(value.estimatedDuration),
    votedBy: selected ? [ROOM_SELECTION] : [],
    addedBy: 'room',
    addedAt: safeText(value.addedAt, 40),
    note: safeText(value.note, 500),
    isPinned: value.isPinned === true,
  }
}

interface UseYjsRoomReturn {
  // 响应式数据
  places: YjsPlace[]
  members: RoomMember[]
  phase: RoomPhase
  isConnected: boolean
  chatMessages: ChatMessage[]

  // 操作方法
  addPlace: (place: Place) => void
  removePlace: (placeId: string) => void
  toggleVote: (placeId: string) => void
  updateNote: (placeId: string, note: string) => void
  setPhase: (phase: RoomPhase) => void
  initRoom: (meta: Partial<YjsRoomMeta>) => void
  appendChatMessages: (messages: ChatMessage[]) => void
}

export function useYjsRoom(
  roomId: string,
  userId: string,
  _nickname: string,
  enabled = true,
): UseYjsRoomReturn {
  const docRef = useRef<Y.Doc | null>(null)
  const providerRef = useRef<WebsocketProvider | null>(null)

  const [places, setPlaces] = useState<YjsPlace[]>([])
  const [members, setMembers] = useState<RoomMember[]>([])
  const [phase, setPhaseState] = useState<RoomPhase>('exploring')
  const [isConnected, setIsConnected] = useState(false)
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([])

  useEffect(() => {
    if (!roomId || !userId || !enabled) return

    // 初始化 YDoc
    const doc = new Y.Doc()
    docRef.current = doc

    // 初始化 Yjs 共享数据结构
    const placesMap = doc.getMap<YjsPlace>('places')
    const roomMeta = doc.getMap<unknown>('room')
    const chatArray = doc.getArray<ChatMessage>('chat')

    let provider: WebsocketProvider | null = null
    let awareness: WebsocketProvider['awareness'] | null = null
    let cancelled = false
    let refreshTimer: ReturnType<typeof setTimeout> | null = null
    let connectionTimer: ReturnType<typeof setTimeout> | null = null
    let tokenExpiresAt = 0
    let connected = false

    const destroyProvider = () => {
      if (connectionTimer) clearTimeout(connectionTimer)
      connectionTimer = null
      connected = false
      if (!provider) return
      provider.destroy()
      provider = null
      providerRef.current = null
      setIsConnected(false)
      setMembers([])
    }
    const scheduleRefresh = (delayMs: number) => {
      if (refreshTimer) clearTimeout(refreshTimer)
      refreshTimer = setTimeout(() => { void connect() }, delayMs)
    }
    const connect = async () => {
      if (cancelled) return
      const apiBase = process.env.NEXT_PUBLIC_API_URL || ''
      const authToken = localStorage.getItem('authToken')
      if (!authToken) {
        recoverExpiredLogin()
        return
      }
      try {
        const tokenResponse = await runWithDeadline(async (signal) => {
          const response = await fetch(`${apiBase}/api/room/${encodeURIComponent(roomId)}/ws-token`, {
            method: 'POST',
            signal,
            headers: { Authorization: `Bearer ${authToken}` },
          })
          return {
            status: response.status,
            ok: response.ok,
            body: response.ok
              ? await response.json() as {
                  token?: string
                  expires_in_seconds?: number
                }
              : null,
          }
        }, 10000)
        if (tokenResponse.status === 401) {
          destroyProvider()
          recoverExpiredLogin()
          return
        }
        if (tokenResponse.status === 403) {
          destroyProvider()
          return
        }
        if (!tokenResponse.ok || !tokenResponse.body)
          throw new Error('TOKEN_UNAVAILABLE')
        const body = tokenResponse.body
        const expiresIn = Number(body.expires_in_seconds || 300)
        if (!body.token || !Number.isFinite(expiresIn) || expiresIn <= 0) {
          throw new Error('INVALID_TOKEN_RESPONSE')
        }
        if (cancelled) return

        destroyProvider()
        tokenExpiresAt = Date.now() + expiresIn * 1000
        provider = new WebsocketProvider(Y_WEBSOCKET_URL, roomId, doc, {
          params: { token: body.token },
          ...(awareness ? { awareness } : {}),
        })
        awareness ??= provider.awareness
        providerRef.current = provider
        provider.on('status', ({ status }: { status: string }) => {
          connected = status === 'connected'
          setIsConnected(connected)
          if (connected && connectionTimer) {
            clearTimeout(connectionTimer)
            connectionTimer = null
          }
        })
        // Awareness is intentionally connection-only; it cannot assert account identity.
        provider.awareness.setLocalStateField('connection', { active: true })
        setMembers([])
        connectionTimer = setTimeout(() => {
          if (cancelled || connected) return
          destroyProvider()
          scheduleRefresh(5000)
        }, 10000)
        scheduleRefresh(Math.max(5000, (expiresIn - 60) * 1000))
      } catch {
        if (cancelled) return
        if (provider && Date.now() >= tokenExpiresAt) destroyProvider()
        scheduleRefresh(10000)
      }
    }
    void connect()

    // 监听地点 Map 变化
    const updatePlaces = () => {
      const allPlaces = Array.from(placesMap.entries())
        .map(([key, value]) => parseSharedPlace(key, value))
        .filter((place): place is YjsPlace => place !== null)
      setPlaces(allPlaces)
    }
    placesMap.observe(updatePlaces)
    updatePlaces()

    // 监听 phase 变化
    const updatePhase = () => {
      const p = roomMeta.get('phase') as RoomPhase | undefined
      if (p && ROOM_PHASES.has(p)) setPhaseState(p)
    }
    roomMeta.observe(updatePhase)
    updatePhase()

    // Only finalized messages are appended to Yjs. SSE deltas remain local,
    // avoiding a CRDT update for every token while still surviving refresh.
    const updateChat = () => setChatMessages([])
    chatArray.observe(updateChat)
    updateChat()

    return () => {
      cancelled = true
      if (refreshTimer) clearTimeout(refreshTimer)
      placesMap.unobserve(updatePlaces)
      roomMeta.unobserve(updatePhase)
      chatArray.unobserve(updateChat)
      destroyProvider()
      awareness?.destroy()
      awareness = null
      doc.destroy()
      docRef.current = null
      providerRef.current = null
    }
  }, [roomId, userId, enabled])

  /** 初始化房间元数据（加入房间时调用，不覆盖已有的 phase）*/
  const initRoom = useCallback((meta: Partial<YjsRoomMeta>) => {
    const doc = docRef.current
    if (!doc) return
    const roomMeta = doc.getMap('room')
    doc.transact(() => {
      Object.entries(meta).forEach(([k, v]) => {
        // phase 只在尚未设置时才写入，避免覆盖协同中已更新的阶段
        if (k === 'phase' && roomMeta.get('phase')) return
        roomMeta.set(k, v)
      })
    })
  }, [])

  /** 添加地点到协同工作台 */
  const addPlace = useCallback((place: Place) => {
    const doc = docRef.current
    if (!doc) return
    const placesMap = doc.getMap<YjsPlace>('places')
    const yjsPlace: YjsPlace = {
      ...place,
      votedBy: [],      // AI 推荐进候选池，用户主动点心形才算"想去"
      addedBy: 'room',
      addedAt: new Date().toISOString(),
      note: '',
      isPinned: false,
    }
    doc.transact(() => {
      placesMap.set(place.placeId, yjsPlace)
    })
  }, [])

  /** 从协同工作台移除地点 */
  const removePlace = useCallback((placeId: string) => {
    const doc = docRef.current
    if (!doc) return
    const placesMap = doc.getMap<YjsPlace>('places')
    doc.transact(() => {
      placesMap.delete(placeId)
    })
  }, [])

  /** 切换房间共享选择；Yjs 不承载成员身份或个人投票归属。 */
  const toggleVote = useCallback((placeId: string) => {
    const doc = docRef.current
    if (!doc) return
    const placesMap = doc.getMap<YjsPlace>('places')
    const place = parseSharedPlace(placeId, placesMap.get(placeId))
    if (!place) return

    const newVotedBy = place.votedBy.length > 0 ? [] : [ROOM_SELECTION]

    doc.transact(() => {
      placesMap.set(placeId, { ...place, votedBy: newVotedBy })
    })
  }, [])

  /** 更新地点备注（实时协同编辑，调用方应 debounce 500ms）*/
  const updateNote = useCallback((placeId: string, note: string) => {
    const doc = docRef.current
    if (!doc) return
    const placesMap = doc.getMap<YjsPlace>('places')
    const place = parseSharedPlace(placeId, placesMap.get(placeId))
    if (!place) return
    doc.transact(() => {
      placesMap.set(placeId, { ...place, note })
    })
  }, [])

  /** 更新房间阶段（由有权限的成员调用）*/
  const setPhase = useCallback((newPhase: RoomPhase) => {
    const doc = docRef.current
    if (!doc) return
    const roomMeta = doc.getMap('room')
    doc.transact(() => {
      roomMeta.set('phase', newPhase)
    })
  }, [])

  const appendChatMessages = useCallback((_messages: ChatMessage[]) => {
    // Chat remains a local device session until server-authored message identity exists.
  }, [])

  return {
    places,
    members,
    phase,
    isConnected,
    chatMessages,
    addPlace,
    removePlace,
    toggleVote,
    updateNote,
    setPhase,
    initRoom,
    appendChatMessages,
  }
}
