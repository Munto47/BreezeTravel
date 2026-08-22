'use client'

import { useEffect, useRef, useState, useCallback } from 'react'
import * as Y from 'yjs'
import { WebsocketProvider } from 'y-websocket'

import type { YjsPlace, YjsRoomMeta, RoomMember, RoomPhase } from '@/types/room'
import type { Place } from '@/types/place'
import type { ChatMessage } from '@/types/chat'

const Y_WEBSOCKET_URL = process.env.NEXT_PUBLIC_Y_WEBSOCKET_URL || 'ws://localhost:1234'

// 用户颜色池（多人协同时区分不同用户）
const MEMBER_COLORS = [
  '#3B82F6', '#EF4444', '#10B981', '#F59E0B',
  '#8B5CF6', '#06B6D4', '#F97316', '#EC4899',
]

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
  nickname: string,
): UseYjsRoomReturn {
  const docRef = useRef<Y.Doc | null>(null)
  const providerRef = useRef<WebsocketProvider | null>(null)

  const [places, setPlaces] = useState<YjsPlace[]>([])
  const [members, setMembers] = useState<RoomMember[]>([])
  const [phase, setPhaseState] = useState<RoomPhase>('exploring')
  const [isConnected, setIsConnected] = useState(false)
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([])

  // 用户颜色（基于 userId hash 确保稳定）
  const userColor = MEMBER_COLORS[
    Math.abs(userId.split('').reduce((a, c) => a + c.charCodeAt(0), 0)) % MEMBER_COLORS.length
  ]

  useEffect(() => {
    if (!roomId) return

    // 初始化 YDoc
    const doc = new Y.Doc()
    docRef.current = doc

    // 初始化 Yjs 共享数据结构
    const placesMap = doc.getMap<YjsPlace>('places')
    const roomMeta = doc.getMap<unknown>('room')
    const chatArray = doc.getArray<ChatMessage>('chat')

    let provider: WebsocketProvider | null = null
    let cancelled = false

    // 连接状态监听
    const updateMembers = () => {
      if (!provider) return
      const states = Array.from(provider.awareness.getStates().entries())
      const seenIds = new Set<string>()
      const onlineMembers: RoomMember[] = states
        .filter(([, state]) => state.user)
        .map(([, state]) => ({
          userId: (state.user as Record<string, string>).userId,
          nickname: (state.user as Record<string, string>).nickname,
          color: (state.user as Record<string, string>).color,
          isOnline: true,
        }))
        .filter((m) => {
          if (seenIds.has(m.userId)) return false
          seenIds.add(m.userId)
          return true
        })
      setMembers(onlineMembers)
    }
    const connect = async () => {
      const apiBase = process.env.NEXT_PUBLIC_API_URL || ''
      const authToken = localStorage.getItem('authToken')
      if (!authToken) return
      const response = await fetch(`${apiBase}/api/room/${encodeURIComponent(roomId)}/ws-token`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${authToken}` },
      })
      if (!response.ok || cancelled) return
      const body = await response.json() as { token: string }
      provider = new WebsocketProvider(Y_WEBSOCKET_URL, roomId, doc, {
        params: { token: body.token },
      })
      providerRef.current = provider
      provider.on('status', ({ status }: { status: string }) => {
        setIsConnected(status === 'connected')
      })
      provider.awareness.setLocalStateField('user', { userId, nickname, color: userColor })
      provider.awareness.on('change', updateMembers)
      updateMembers()
    }
    void connect()

    // 监听地点 Map 变化
    const updatePlaces = () => {
      const allPlaces = Array.from(placesMap.values())
      setPlaces(allPlaces)
    }
    placesMap.observe(updatePlaces)
    updatePlaces()

    // 监听 phase 变化
    const updatePhase = () => {
      const p = roomMeta.get('phase') as RoomPhase | undefined
      if (p) setPhaseState(p)
    }
    roomMeta.observe(updatePhase)
    updatePhase()

    // Only finalized messages are appended to Yjs. SSE deltas remain local,
    // avoiding a CRDT update for every token while still surviving refresh.
    const updateChat = () => {
      const seen = new Set<string>()
      setChatMessages(chatArray.toArray().filter((message) => {
        if (!message?.messageId || seen.has(message.messageId)) return false
        seen.add(message.messageId)
        return true
      }))
    }
    chatArray.observe(updateChat)
    updateChat()

    return () => {
      cancelled = true
      provider?.awareness.off('change', updateMembers)
      placesMap.unobserve(updatePlaces)
      roomMeta.unobserve(updatePhase)
      chatArray.unobserve(updateChat)
      provider?.destroy()
      doc.destroy()
      docRef.current = null
      providerRef.current = null
    }
  }, [roomId, userId, nickname, userColor])

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
      addedBy: userId,
      addedAt: new Date().toISOString(),
      note: '',
      isPinned: false,
    }
    doc.transact(() => {
      placesMap.set(place.placeId, yjsPlace)
    })
  }, [userId])

  /** 从协同工作台移除地点 */
  const removePlace = useCallback((placeId: string) => {
    const doc = docRef.current
    if (!doc) return
    const placesMap = doc.getMap<YjsPlace>('places')
    doc.transact(() => {
      placesMap.delete(placeId)
    })
  }, [])

  /** 切换当前用户对某地点的勾选状态 */
  const toggleVote = useCallback((placeId: string) => {
    const doc = docRef.current
    if (!doc) return
    const placesMap = doc.getMap<YjsPlace>('places')
    const place = placesMap.get(placeId)
    if (!place) return

    const isVoted = place.votedBy.includes(userId)
    const newVotedBy = isVoted
      ? place.votedBy.filter((id) => id !== userId)
      : [...place.votedBy, userId]

    doc.transact(() => {
      placesMap.set(placeId, { ...place, votedBy: newVotedBy })
    })
  }, [userId])

  /** 更新地点备注（实时协同编辑，调用方应 debounce 500ms）*/
  const updateNote = useCallback((placeId: string, note: string) => {
    const doc = docRef.current
    if (!doc) return
    const placesMap = doc.getMap<YjsPlace>('places')
    const place = placesMap.get(placeId)
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

  const appendChatMessages = useCallback((messages: ChatMessage[]) => {
    const doc = docRef.current
    if (!doc || !messages.length) return
    const chatArray = doc.getArray<ChatMessage>('chat')
    const knownIds = new Set(chatArray.toArray().map((message) => message.messageId))
    const newMessages = messages.filter(
      (message) => message.status === 'done' && !knownIds.has(message.messageId),
    )
    if (!newMessages.length) return
    doc.transact(() => chatArray.push(newMessages))
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
