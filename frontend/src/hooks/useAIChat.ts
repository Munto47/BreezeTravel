'use client'

import { useState, useCallback, useRef } from 'react'
import { v4 as uuidv4 } from 'uuid'

import type { ChatMessage, ThinkingStep } from '@/types/chat'
import type { Place } from '@/types/place'
import { parsePlaceFromAPI } from '@/types/place'

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

interface UseAIChatReturn {
  messages: ChatMessage[]
  isStreaming: boolean
  sendMessage: (text: string, selectedPlaceIds?: string[], tripCity?: string) => Promise<void>
  clearMessages: () => void
}

export function useAIChat(threadId: string, userId: string): UseAIChatReturn {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

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
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          thread_id: threadId,
          user_id: userId,
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
                return [
                  ...prev.slice(0, -1),
                  { ...last, placesGenerated: [...(last.placesGenerated || []), place] },
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

              if (event === 'done') {
                return [
                  ...prev.slice(0, -1),
                  { ...last, status: 'done' },
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
        return [...prev.slice(0, -1), { ...last, status: 'done' }]
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
  }, [threadId, userId, isStreaming])

  const clearMessages = useCallback(() => setMessages([]), [])

  return { messages, isStreaming, sendMessage, clearMessages }
}
