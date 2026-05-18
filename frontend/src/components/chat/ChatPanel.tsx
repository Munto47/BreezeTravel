'use client'

import { useState, useRef, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Send, Sparkles, MessageSquare, ChevronRight } from 'lucide-react'

import type { ChatMessage } from '@/types/chat'
import MessageItem from './MessageItem'
import ThinkingSteps from './ThinkingSteps'

interface DayWeather {
  date: string
  condition: string
  icon: string
  temp_high: number
  temp_low: number
  suggestion: string
}

interface WeatherData {
  city: string
  days: DayWeather[]
}

interface ChatPanelProps {
  messages: ChatMessage[]
  isStreaming: boolean
  weather?: WeatherData | null
  tripCity?: string
  onSend: (text: string) => void
}

function getQuickPrompts(city?: string) {
  const c = city || '目的地'
  return [
    { text: `${c}有哪些适合拍照的景点？`, icon: '📸' },
    { text: `${c}有哪些必吃的美食？`, icon: '🍜' },
    { text: `${c}适合带老人的地方`, icon: '👴' },
    { text: `${c}文艺小众打卡地`, icon: '🎨' },
  ]
}

// 天气条：显示今日 + 明日（紧凑单行）
function WeatherBar({ weather }: { weather: WeatherData }) {
  const today = weather.days[0]
  const tomorrow = weather.days[1]

  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      transition={{ duration: 0.3 }}
      className="px-4 py-2 bg-gradient-to-r from-sky-50/80 to-blue-50/60 border-b border-sky-100/60 flex-shrink-0"
    >
      <div className="flex items-center gap-3 text-[11px]">
        {/* 今日 */}
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-base leading-none">{today.icon}</span>
          <div className="min-w-0">
            <span className="font-semibold text-sky-700">{weather.city}</span>
            <span className="text-sky-500 mx-1">·</span>
            <span className="text-sky-600">{today.condition}</span>
            <span className="text-sky-500 mx-1">
              {today.temp_low}~{today.temp_high}°C
            </span>
          </div>
        </div>

        {/* 分隔 */}
        {tomorrow && (
          <>
            <span className="text-sky-200 flex-shrink-0">|</span>
            {/* 明日 */}
            <div className="flex items-center gap-1 flex-shrink-0 text-sky-400">
              <span className="text-sm leading-none">{tomorrow.icon}</span>
              <span>明日 {tomorrow.condition} {tomorrow.temp_low}~{tomorrow.temp_high}°C</span>
            </div>
          </>
        )}

        {/* 建议 — 右侧弹性截断 */}
        <p className="ml-auto text-sky-400 truncate hidden sm:block flex-shrink" style={{ maxWidth: '140px' }}>
          {today.suggestion}
        </p>
      </div>
    </motion.div>
  )
}

export default function ChatPanel({ messages, isStreaming, weather, tripCity, onSend }: ChatPanelProps) {
  const [input, setInput] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const handleSend = () => {
    const text = input.trim()
    if (!text || isStreaming) return
    onSend(text)
    setInput('')
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="flex flex-col h-full">
      {/* 标题栏 */}
      <div className="px-5 py-4 border-b border-gray-100/60 flex-shrink-0">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg bg-coral-50 flex items-center justify-center">
            <Sparkles className="w-4 h-4 text-coral-500" />
          </div>
          <div>
            <h2 className="text-sm font-bold text-gray-900">AI 旅行顾问</h2>
            <p className="text-[11px] text-gray-400 leading-tight">描述需求，AI 推荐适合的地点</p>
          </div>
        </div>
      </div>

      {/* 天气条（有数据时显示） */}
      {weather && weather.days.length > 0 && <WeatherBar weather={weather} />}

      {/* 消息列表 */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4 scrollbar-thin">
        {/* 空状态：快捷提问 */}
        <AnimatePresence>
          {messages.length === 0 && (
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -10 }}
              className="mt-8"
            >
              <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-coral-50 to-coral-100 flex items-center justify-center mx-auto mb-4">
                <MessageSquare className="w-7 h-7 text-coral-400" />
              </div>
              <p className="text-center text-sm font-medium text-gray-500 mb-1">你好！我是你的旅行顾问</p>
              <p className="text-center text-xs text-gray-400 mb-5">试试下面这些问题开始探索</p>
              <div className="grid grid-cols-2 gap-2">
                {getQuickPrompts(tripCity).map((q) => (
                  <button
                    key={q.text}
                    onClick={() => onSend(q.text)}
                    className="text-left text-xs text-gray-600 hover:text-coral-600
                             px-3 py-2.5 rounded-lg
                             bg-white/60 hover:bg-coral-50/80
                             border border-gray-100/80 hover:border-coral-200
                             transition-all duration-200 group"
                  >
                    <span className="text-base mr-1.5 group-hover:scale-110 inline-block transition-transform">
                      {q.icon}
                    </span>
                    {q.text}
                  </button>
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* 消息流 */}
        {messages.map((msg, i) => (
          <motion.div
            key={msg.messageId}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.2, delay: i === messages.length - 1 ? 0.05 : 0 }}
          >
            {msg.role === 'assistant' && msg.thinkingSteps && msg.thinkingSteps.length > 0 && (
              <ThinkingSteps steps={msg.thinkingSteps} isStreaming={msg.status === 'streaming'} />
            )}
            <MessageItem message={msg} />
          </motion.div>
        ))}
        <div ref={bottomRef} />
      </div>

      {/* 输入区 */}
      <div className="p-3 border-t border-gray-100/60 flex-shrink-0">
        <div className="flex gap-2 items-end">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="描述你的旅行需求..."
            rows={2}
            className="input-glass flex-1 resize-none text-sm"
          />
          <button
            onClick={handleSend}
            disabled={isStreaming || !input.trim()}
            className="btn-coral p-2.5 rounded-lg flex-shrink-0"
          >
            {isStreaming ? (
              <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin block" />
            ) : (
              <Send className="w-4 h-4" />
            )}
          </button>
        </div>
      </div>
    </div>
  )
}
