'use client'

import { useState, useCallback, useEffect, useRef } from 'react'
import { v4 as uuidv4 } from 'uuid'

import type { ChatMessage, ThinkingStep, Citation } from '@/types/chat'
import type { Place } from '@/types/place'
import { parsePlaceFromAPI } from '@/types/place'

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

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
      thinkingSteps: [],
      placesGenerated: [],
    }

    setMessages((prev) => [...prev, userMsg, assistantMsg])
    setIsStreaming(true)

    abortRef.current = new AbortController()

    try {
      const response = await fetch(`${API_BASE}/api/chat`, {
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
        }),
        signal: abortRef.current.signal,
      })

      // 非 2xx 响应处理
      if (!response.ok) {
        const errText = await response.text().catch(() => '未知错误')
        throw new Error(`服务器错误 ${response.status}: ${errText.slice(0, 200)}`)
      }

      if (!response.body) throw new Error('无响应体')

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

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

            setMessages((prev) => {
              const last = prev[prev.length - 1]
              if (!last || last.role !== 'assistant') return prev

              if (event === 'thinking') {
                const step: ThinkingStep = {
                  node: data.node,
                  summary: data.summary,
                  durationMs: data.ms || 0,
                }
                return [
                  ...prev.slice(0, -1),
                  { ...last, thinkingSteps: [...(last.thinkingSteps || []), step] },
                ]
              }

              if (event === 'place') {
                const place = parsePlaceFromAPI(data.place)
                const existing = last.placesGenerated || []
                // 已有同 id 卡片则跳过追加（P1-12 真流式：预览 + synthesizer 二段去重）
                if (existing.some((p) => p.placeId === place.placeId)) return prev
                return [
                  ...prev.slice(0, -1),
                  { ...last, placesGenerated: [...existing, place] },
                ]
              }

              if (event === 'place_update') {
                // Synthesizer 增强字段增量合并：描述、标签、证据、时长
                const pid: string = data.place_id
                const fields = (data.fields || {}) as Record<string, unknown>
                const merged = (last.placesGenerated || []).map((p) => {
                  if (p.placeId !== pid) return p
                  const patched: Partial<Place> = {}
                  if (typeof fields.description === 'string') patched.description = fields.description
                  if (Array.isArray(fields.tags)) patched.tags = fields.tags as string[]
                  if (Array.isArray(fields.constraint_evidence)) {
                    patched.constraintEvidence = parsePlaceFromAPI({
                      place_id: p.placeId,
                      name: p.name,
                      category: p.category,
                      address: p.address,
                      coords: p.coords,
                      city: p.city,
                      source: p.source,
                      constraint_evidence: fields.constraint_evidence,
                    }).constraintEvidence
                  }
                  if (typeof fields.selection_evidence_status === 'string') {
                    patched.selectionEvidenceStatus = fields.selection_evidence_status as Place['selectionEvidenceStatus']
                  }
                  if (Array.isArray(fields.geo_evidence)) {
                    patched.geoEvidence = parsePlaceFromAPI({
                      place_id: p.placeId,
                      name: p.name,
                      category: p.category,
                      address: p.address,
                      coords: p.coords,
                      city: p.city,
                      source: p.source,
                      geo_evidence: fields.geo_evidence,
                    }).geoEvidence
                  }
                  if (Array.isArray(fields.confirmation_actions)) {
                    patched.confirmationActions = fields.confirmation_actions as string[]
                  }
                  if (fields.rag_meta && typeof fields.rag_meta === 'object') {
                    const rm = fields.rag_meta as Record<string, unknown>
                    patched.ragMeta = {
                      tipSnippets: (rm.tip_snippets as string[]) || [],
                      sentimentScore: (rm.sentiment_score as number) || 0,
                      sourceNoteIds: (rm.source_note_ids as string[]) || [],
                    }
                  }
                  if (typeof fields.estimated_duration === 'number') patched.estimatedDuration = fields.estimated_duration
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

              if (event === 'citations') {
                const citations: Citation[] = (data.citations || []).map((item: Record<string, unknown>) => ({
                  sourceId: String(item.source_id),
                  title: String(item.title),
                  url: typeof item.url === 'string' ? item.url : undefined,
                  excerpt: String(item.excerpt || ''),
                  score: Number(item.score || 0),
                  retrievalSources: Array.isArray(item.retrieval_sources) ? item.retrieval_sources as string[] : [],
                  publishedAt: typeof item.published_at === 'string' ? item.published_at : undefined,
                  retrievedAt: typeof item.retrieved_at === 'string' ? item.retrieved_at : undefined,
                  license: typeof item.license === 'string' ? item.license : undefined,
                  revision: typeof item.revision === 'string' ? item.revision : undefined,
                  attribution: typeof item.attribution === 'string' ? item.attribution : undefined,
                  corpusKind: String(item.corpus_kind || 'synthetic'),
                }))
                const known = new Set((last.citations || []).map((citation) => citation.sourceId))
                return [...prev.slice(0, -1), { ...last, citations: [...(last.citations || []), ...citations.filter((citation) => !known.has(citation.sourceId))] }]
              }

              if (event === 'text_reset') {
                // Critic 触发重检索时 synthesizer 会再跑一次，清空前一轮文本避免段落重复
                return [
                  ...prev.slice(0, -1),
                  { ...last, content: '' },
                ]
              }

              if (event === 'done') {
                return [
                  ...prev.slice(0, -1),
                  { ...last, status: 'done', traceId: data.trace_id },
                ]
              }

              if (event === 'error') {
                return [
                  ...prev.slice(0, -1),
                  { ...last, status: 'error', content: `错误：${data.message}` },
                ]
              }

              return prev
            })
          } catch {
            // 忽略解析错误的帧
          }
        }
      }

      // 流结束后确保状态为 done
      setMessages((prev) => {
        const last = prev[prev.length - 1]
        if (!last || last.role !== 'assistant' || last.status === 'done' || last.status === 'error') return prev
        return [...prev.slice(0, -1), { ...last, status: 'done' as const }]
      })

    } catch (err) {
      if ((err as Error).name === 'AbortError') return
      const errMsg = (err as Error).message || '请求失败，请重试'
      setMessages((prev) => {
        const last = prev[prev.length - 1]
        if (!last || last.role !== 'assistant') return prev
        return [...prev.slice(0, -1), { ...last, status: 'error', content: errMsg }]
      })
    } finally {
      setIsStreaming(false)
      abortRef.current = null
    }
  }, [threadId, userId, roomId, isStreaming, persistCompleted])

  const clearMessages = useCallback(() => setMessages([]), [])

  return { messages, isStreaming, sendMessage, clearMessages }
}
