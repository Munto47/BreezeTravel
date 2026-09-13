'use client'

import { useState, useCallback, useEffect, useRef } from 'react'
import { v4 as uuidv4 } from 'uuid'

import type { ChatMessage, CollaborationProgressPhase } from '@/types/chat'
import type { Place } from '@/types/place'
import { recoverExpiredLogin } from '@/lib/request-safety'

const API_BASE = process.env.NEXT_PUBLIC_API_URL || ''
const PUBLIC_PHASES = new Set<CollaborationProgressPhase>([
  'UNDERSTANDING',
  'FINDING_PLACES',
  'ORGANIZING',
])
const PUBLIC_CATEGORIES = new Set<Place['category']>([
  'attraction',
  'food',
  'hotel',
  'transport',
])

function parsePublicPlace(raw: unknown): Place | null {
  if (!raw || typeof raw !== 'object') return null
  const value = raw as Record<string, unknown>
  const coords = value.coords as Record<string, unknown> | undefined
  const placeId = typeof value.place_id === 'string' ? value.place_id : ''
  const name = typeof value.name === 'string' ? value.name.trim() : ''
  const category = value.category as Place['category']
  const lng = Number(coords?.lng)
  const lat = Number(coords?.lat)
  if (
    !placeId.startsWith('place_')
    || !name
    || !PUBLIC_CATEGORIES.has(category)
    || !Number.isFinite(lng)
    || !Number.isFinite(lat)
    || Math.abs(lng) > 180
    || Math.abs(lat) > 90
  ) return null
  return {
    placeId,
    name,
    category,
    address: typeof value.address === 'string' ? value.address : '',
    coords: { lng, lat },
    city: typeof value.city === 'string' ? value.city : '',
    district: typeof value.district === 'string' ? value.district : undefined,
    source: 'synthesized',
    amapRating: typeof value.rating === 'number' ? value.rating : undefined,
    amapPrice: typeof value.average_price === 'number' ? value.average_price : undefined,
    openingHours: typeof value.opening_hours === 'string' ? value.opening_hours : undefined,
    phone: typeof value.phone === 'string' ? value.phone : undefined,
    amapPhotos: [],
    description: typeof value.description === 'string' ? value.description : undefined,
    tags: Array.isArray(value.tags) ? value.tags.filter((item): item is string => typeof item === 'string').slice(0, 8) : [],
    constraintEvidence: [],
    geoEvidence: [],
    confirmationActions: Array.isArray(value.confirmation_actions)
      ? value.confirmation_actions.filter((item): item is string => typeof item === 'string').slice(0, 5)
      : [],
    estimatedDuration: typeof value.suggested_visit_minutes === 'number'
      ? value.suggested_visit_minutes
      : undefined,
  }
}

interface UseAIChatReturn {
  messages: ChatMessage[]
  isStreaming: boolean
  sendMessage: (text: string, selectedPlaceIds?: string[], tripCity?: string) => Promise<void>
  stopMessage: () => void
  clearMessages: () => void
}

export function useAIChat(
  threadId: string,
  userId: string,
  roomId?: string,
  persistedMessages: ChatMessage[] = [],
  persistCompleted?: (messages: ChatMessage[]) => void,
): UseAIChatReturn {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const assistantIdRef = useRef<string | null>(null)
  const scope = `${roomId || ''}:${threadId}:${userId}`
  const scopeRef = useRef(scope)
  scopeRef.current = scope

  useEffect(() => {
    setMessages([])
    setIsStreaming(false)
    return () => {
      const active = abortRef.current
      abortRef.current = null
      active?.abort()
    }
  }, [scope])

  useEffect(() => {
    if (!persistedMessages.length) return
    setMessages((current) => {
      const byId = new Map(current.map((message) => [message.messageId, message]))
      for (const message of persistedMessages) {
        if (message.threadId === threadId && !byId.has(message.messageId)) byId.set(message.messageId, message)
      }
      return [...byId.values()].sort((left, right) => left.createdAt.localeCompare(right.createdAt))
    })
  }, [persistedMessages, threadId])

  useEffect(() => {
    const last = messages[messages.length - 1]
    if (!last || last.role !== 'assistant' || last.status !== 'done') return
    persistCompleted?.(messages.slice(-2))
  }, [messages, persistCompleted])

  const stopMessage = useCallback(() => {
    const active = abortRef.current
    if (!active) return
    const messageId = assistantIdRef.current
    abortRef.current = null
    active.abort()
    setIsStreaming(false)
    setMessages(current => current.map(message => message.messageId === messageId
      ? { ...message, status: 'error', notice: '回答已停止，以上内容尚未完成。' }
      : message))
  }, [])

  const sendMessage = useCallback(async (
    text: string,
    selectedPlaceIds: string[] = [],
    tripCity?: string,
  ) => {
    if (abortRef.current || !text.trim()) return
    const requestController = new AbortController()
    const token = localStorage.getItem('authToken')
    abortRef.current = requestController
    const valid = () => abortRef.current === requestController
      && !requestController.signal.aborted && scopeRef.current === scope
      && localStorage.getItem('authToken') === token
    const userMsg: ChatMessage = {
      messageId: uuidv4(), threadId, role: 'user', content: text,
      createdAt: new Date().toISOString(), status: 'done',
    }
    const assistantMsg: ChatMessage = {
      messageId: uuidv4(), threadId, role: 'assistant', content: '',
      createdAt: new Date().toISOString(), status: 'streaming',
      progressPhase: 'UNDERSTANDING', placesGenerated: [],
    }
    assistantIdRef.current = assistantMsg.messageId
    const update = (apply: (message: ChatMessage) => ChatMessage) => {
      if (!valid()) return
      setMessages(current => scopeRef.current === scope && localStorage.getItem('authToken') === token
        && !requestController.signal.aborted ? current.map(message => message.messageId === assistantMsg.messageId
        ? apply(message) : message) : current)
    }
    setMessages(current => [...current, userMsg, assistantMsg])
    setIsStreaming(true)
    // Keep this signal attached through response-body consumption, so stopping
    // also aborts an answer after its HTTP headers have already arrived.
    const overallTimer = window.setTimeout(() => requestController.abort(new Error('REQUEST_TIMEOUT')), 45000)
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
    try {
      const response = await fetch(`${API_BASE}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify({
          thread_id: threadId, user_id: userId, room_id: roomId || null,
          message: text, selected_place_ids: selectedPlaceIds,
          trip_city: tripCity || null, use_long_term_memory: false,
        }),
        signal: requestController.signal,
      })
      if (!valid()) return
      if (response.status === 401) {
        recoverExpiredLogin()
        throw new Error('AUTH_REQUIRED')
      }
      if (response.status === 409) throw new Error('ROOM_SELECTION_CHANGED')
      if (!response.ok) throw new Error(response.status === 403 ? 'ACCESS_DENIED' : 'SERVICE_UNAVAILABLE')
      if (!response.body) throw new Error('EMPTY_ANSWER')
      reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = '', content = ''
      const placeIds = new Set<string>()
      let terminalReceived = false
      while (valid() && !terminalReceived) {
        const { done, value } = await reader.read()
        if (!valid()) return
        if (done) break
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
        const frames = buffer.split('\n\n')
        buffer = frames.pop() || ''
        for (const frame of frames) {
          if (!frame.startsWith('data: ')) continue
          let payload
          try { payload = JSON.parse(frame.slice(6)) } catch { continue }
          const { event, data } = payload
          if (!data || typeof data !== 'object') continue
          if (event === 'error') throw new Error('SERVICE_UNAVAILABLE')
          if (event === 'progress' && PUBLIC_PHASES.has(data.phase))
            update(last => ({ ...last, progressPhase: data.phase }))
          if (event === 'place') {
            const place = parsePublicPlace(data.place)
            if (place && !placeIds.has(place.placeId)) {
              placeIds.add(place.placeId)
              update(last => ({ ...last, placesGenerated: [...(last.placesGenerated || []), place] }))
            }
          }
          if (event === 'place_update') {
            const fields = data.fields || {}
            const patched: Partial<Place> = {}
            if (typeof fields.description === 'string') patched.description = fields.description
            if (Array.isArray(fields.confirmation_actions)) patched.confirmationActions = fields.confirmation_actions.filter((item: unknown): item is string => typeof item === 'string').slice(0, 5)
            if (Array.isArray(fields.tags)) patched.tags = fields.tags.filter((item: unknown): item is string => typeof item === 'string').slice(0, 8)
            if (typeof fields.suggested_visit_minutes === 'number') patched.estimatedDuration = fields.suggested_visit_minutes
            update(last => ({ ...last, placesGenerated: (last.placesGenerated || []).map(place => place.placeId === data.place_id ? { ...place, ...patched } : place) }))
          }
          if (event === 'place_remove') {
            placeIds.delete(data.place_id)
            update(last => ({ ...last, placesGenerated: (last.placesGenerated || []).filter(place => place.placeId !== data.place_id) }))
          }
          if (event === 'text' && typeof data.delta === 'string') {
            content += data.delta
            const received = content
            update(last => ({ ...last, content: received }))
          }
          if (event === 'text_reset') {
            content = ''
            update(last => ({ ...last, content: '' }))
          }
          if (event === 'done' && (data.status === 'READY' || data.status === 'LIMITED')) {
            if (!content.trim() && !placeIds.size) throw new Error('EMPTY_ANSWER')
            terminalReceived = true
            update(last => ({ ...last, status: 'done', resultStatus: data.status }))
            break
          }
        }
      }
      if (!terminalReceived && valid()) throw new Error('INCOMPLETE_ANSWER')
    } catch (error) {
      // A stopped request, previous room or previous account must not update
      // the new conversation or clear a newer login after a late 401.
      if (abortRef.current !== requestController || scopeRef.current !== scope || localStorage.getItem('authToken') !== token) return
      const code = requestController.signal.aborted ? 'REQUEST_TIMEOUT' : (error as Error).message
      const notice = code === 'ACCESS_DENIED' ? '你无权访问这个协同房间。'
        : code === 'AUTH_REQUIRED' ? '登录状态已失效，请重新登录。'
        : code === 'ROOM_SELECTION_CHANGED' ? '地点选择正在同步或已变化，请稍后重新发送。'
        : code === 'EMPTY_ANSWER' ? '没有收到有效回答，请重试。'
        : code === 'INCOMPLETE_ANSWER' ? '连接中断，以上回答尚未完成；请重试。'
        : code === 'REQUEST_TIMEOUT' ? '回答等待较久，已停止；已收到的内容尚未完成。'
        : '服务暂时不可用；已收到的内容尚未完成，请稍后重试。'
      setMessages(current => current.map(message => message.messageId === assistantMsg.messageId
        ? { ...message, status: 'error', notice } : message))
    } finally {
      window.clearTimeout(overallTimer)
      void reader?.cancel().catch(() => {})
      if (abortRef.current === requestController) {
        abortRef.current = null
        setIsStreaming(false)
      }
    }
  }, [threadId, userId, roomId, scope])

  const clearMessages = useCallback(() => { stopMessage(); setMessages([]) }, [stopMessage])
  return { messages, isStreaming, sendMessage, stopMessage, clearMessages }
}
