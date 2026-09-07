'use client'

import { useState, useCallback, useEffect, useRef } from 'react'
import { v4 as uuidv4 } from 'uuid'

import type { ChatMessage, CollaborationProgressPhase } from '@/types/chat'
import type { Place } from '@/types/place'
import { fetchWithDeadline, recoverExpiredLogin } from '@/lib/request-safety'

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

  useEffect(() => () => abortRef.current?.abort(), [])

  useEffect(() => {
    if (!persistedMessages.length) return
    setMessages((current) => {
      const byId = new Map(current.map((message) => [message.messageId, message]))
      for (const message of persistedMessages) {
        if (!byId.has(message.messageId)) byId.set(message.messageId, message)
      }
      return [...byId.values()].sort((left, right) => left.createdAt.localeCompare(right.createdAt))
    })
  }, [persistedMessages])

  useEffect(() => {
    const last = messages[messages.length - 1]
    if (!last || last.role !== 'assistant' || last.status !== 'done') return
    persistCompleted?.(messages.slice(-2))
  }, [messages, persistCompleted])

  const sendMessage = useCallback(async (
    text: string,
    selectedPlaceIds: string[] = [],
    tripCity?: string,
  ) => {
    if (isStreaming) return

    const userMsg: ChatMessage = {
      messageId: uuidv4(),
      threadId,
      role: 'user',
      content: text,
      createdAt: new Date().toISOString(),
      status: 'done',
    }

    const assistantMsg: ChatMessage = {
      messageId: uuidv4(),
      threadId,
      role: 'assistant',
      content: '',
      createdAt: new Date().toISOString(),
      status: 'streaming',
      progressPhase: 'UNDERSTANDING',
      placesGenerated: [],
    }

    setMessages((prev) => [...prev, userMsg, assistantMsg])
    setIsStreaming(true)

    const requestController = new AbortController()
    abortRef.current = requestController
    const overallTimer = window.setTimeout(
      () => requestController.abort(new Error('REQUEST_TIMEOUT')),
      45000,
    )

    try {
      const response = await fetchWithDeadline(`${API_BASE}/api/chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(typeof window !== 'undefined' && localStorage.getItem('authToken')
            ? { Authorization: `Bearer ${localStorage.getItem('authToken')}` }
            : {}),
        },
        body: JSON.stringify({
          thread_id: threadId,
          user_id: userId,
          room_id: roomId || null,
          message: text,
          selected_place_ids: selectedPlaceIds,
          trip_city: tripCity || null,
          use_long_term_memory: false,
        }),
        signal: requestController.signal,
      })

      // 非 2xx 响应处理
      if (response.status === 401) {
        recoverExpiredLogin()
        throw new Error('AUTH_REQUIRED')
      }
      if (!response.ok) {
        throw new Error(response.status === 403 ? 'ACCESS_DENIED' : 'SERVICE_UNAVAILABLE')
      }

      if (!response.body) throw new Error('无响应体')

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let terminalReceived = false

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const frames = buffer.split('\n\n')
        buffer = frames.pop() || ''

        for (const frame of frames) {
          if (!frame.startsWith('data: ')) continue
          try {
            const payload = JSON.parse(frame.slice(6))
            const { event, data } = payload
            if (event === 'error' || (event === 'done' && (data?.status === 'READY' || data?.status === 'LIMITED'))) {
              terminalReceived = true
            }

            setMessages((prev) => {
              const last = prev[prev.length - 1]
              if (!last || last.role !== 'assistant') return prev

              if (event === 'progress' && PUBLIC_PHASES.has(data.phase)) {
                return [
                  ...prev.slice(0, -1),
                  { ...last, progressPhase: data.phase as CollaborationProgressPhase },
                ]
              }

              if (event === 'place') {
                const place = parsePublicPlace(data.place)
                if (!place) return prev
                const existing = last.placesGenerated || []
                // 已有同 id 卡片则跳过追加（P1-12 真流式：预览 + synthesizer 二段去重）
                if (existing.some((p) => p.placeId === place.placeId)) return prev
                return [
                  ...prev.slice(0, -1),
                  { ...last, placesGenerated: [...existing, place] },
                ]
              }

              if (event === 'place_update') {
                const pid: string = data.place_id
                const fields = (data.fields || {}) as Record<string, unknown>
                const merged = (last.placesGenerated || []).map((p) => {
                  if (p.placeId !== pid) return p
                  const patched: Partial<Place> = {}
                  if (typeof fields.description === 'string') patched.description = fields.description
                  if (Array.isArray(fields.confirmation_actions)) {
                    patched.confirmationActions = fields.confirmation_actions.filter((item): item is string => typeof item === 'string').slice(0, 5)
                  }
                  if (Array.isArray(fields.tags)) patched.tags = fields.tags.filter((item): item is string => typeof item === 'string').slice(0, 8)
                  if (typeof fields.suggested_visit_minutes === 'number') patched.estimatedDuration = fields.suggested_visit_minutes
                  return { ...p, ...patched }
                })
                return [
                  ...prev.slice(0, -1),
                  { ...last, placesGenerated: merged },
                ]
              }

              if (event === 'place_remove') {
                const pid: string = data.place_id
                const filtered = (last.placesGenerated || []).filter((p) => p.placeId !== pid)
                return [
                  ...prev.slice(0, -1),
                  { ...last, placesGenerated: filtered },
                ]
              }

              if (event === 'text') {
                return [
                  ...prev.slice(0, -1),
                  { ...last, content: last.content + data.delta },
                ]
              }

              if (event === 'text_reset') {
                // Critic 触发重检索时 synthesizer 会再跑一次，清空前一轮文本避免段落重复
                return [
                  ...prev.slice(0, -1),
                  { ...last, content: '' },
                ]
              }

              if (event === 'done' && (data.status === 'READY' || data.status === 'LIMITED')) {
                return [
                  ...prev.slice(0, -1),
                  { ...last, status: 'done', resultStatus: data.status },
                ]
              }

              if (event === 'error') {
                return [
                  ...prev.slice(0, -1),
                  { ...last, status: 'error', content: '暂时无法完成，请稍后重试。' },
                ]
              }

              return prev
            })
          } catch {
            // 忽略解析错误的帧
          }
        }
      }

      if (!terminalReceived) {
        setMessages((prev) => {
          const last = prev[prev.length - 1]
          if (!last || last.role !== 'assistant' || last.status !== 'streaming') return prev
          return [...prev.slice(0, -1), { ...last, status: 'error' as const, content: '连接中断，请重试。' }]
        })
      }

    } catch (err) {
      const aborted = requestController.signal.aborted
      const code = aborted ? 'REQUEST_TIMEOUT' : (err as Error).message
      const errMsg = code === 'AUTH_REQUIRED'
        ? '登录状态已失效，请重新登录。'
        : code === 'ACCESS_DENIED'
          ? '你无权访问这个协同房间。'
          : code === 'REQUEST_TIMEOUT'
            ? '这次整理等待时间较长，已安全停止；可以稍后重试。'
            : '服务暂时不可用，请稍后重试。'
      setMessages((prev) => {
        const last = prev[prev.length - 1]
        if (!last || last.role !== 'assistant') return prev
        return [...prev.slice(0, -1), { ...last, status: 'error', content: errMsg }]
      })
    } finally {
      window.clearTimeout(overallTimer)
      setIsStreaming(false)
      abortRef.current = null
    }
  }, [threadId, userId, roomId, isStreaming, persistCompleted])

  const clearMessages = useCallback(() => setMessages([]), [])

  return { messages, isStreaming, sendMessage, clearMessages }
}
