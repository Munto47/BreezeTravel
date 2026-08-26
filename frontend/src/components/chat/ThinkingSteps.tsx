'use client'

import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ChevronDown, ChevronRight, Loader2 } from 'lucide-react'

import type { ThinkingStep } from '@/types/chat'

const NODE_CONFIG: Record<string, { label: string; icon: string; color: string; bg: string }> = {
  router:        { label: '意图分析', icon: '🔀', color: 'text-purple-600', bg: 'bg-purple-50' },
  tool_executor: { label: '工具执行', icon: '🛠️', color: 'text-blue-600', bg: 'bg-blue-50' },
  synthesizer:   { label: '数据合成', icon: '⚡', color: 'text-orange-600', bg: 'bg-orange-50' },
  optimizer:     { label: '路线优化', icon: '🗺️', color: 'text-coral-600', bg: 'bg-coral-50' },
}

interface ThinkingStepsProps {
  steps: ThinkingStep[]
  isStreaming: boolean
}

export default function ThinkingSteps({ steps, isStreaming }: ThinkingStepsProps) {
  const [isExpanded, setIsExpanded] = useState(false)

  useEffect(() => {
    if (isStreaming) setIsExpanded(true)
  }, [isStreaming])

  if (steps.length === 0 && !isStreaming) return null

  return (
    <div className="mb-3">
      <button
        onClick={() => setIsExpanded((v) => !v)}
        className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-gray-600 transition-colors group"
      >
        {isExpanded
          ? <ChevronDown className="w-3 h-3 text-gray-300 group-hover:text-gray-500 transition-colors" />
          : <ChevronRight className="w-3 h-3 text-gray-300 group-hover:text-gray-500 transition-colors" />
        }
        <span className="font-medium">Agent 思考链</span>
        <span className="text-gray-300">({steps.length} 步)</span>
        {isStreaming && (
          <Loader2 className="w-3 h-3 text-coral-400 animate-spin ml-0.5" />
        )}
      </button>

      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="mt-2 ml-1.5 border-l-2 border-gray-200/60 pl-3 space-y-2">
              {steps.map((step, i) => {
                const config = NODE_CONFIG[step.node] || { label: step.node, icon: '•', color: 'text-gray-600', bg: 'bg-gray-50' }
                const isLast = i === steps.length - 1

                return (
                  <motion.div
                    key={i}
                    initial={{ opacity: 0, x: -6 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: i * 0.05 }}
                    className="flex items-start gap-2"
                  >
                    <div className={`flex-shrink-0 w-6 h-6 rounded-md ${config.bg} flex items-center justify-center`}>
                      {isLast && isStreaming ? (
                        <Loader2 className={`w-3.5 h-3.5 ${config.color} animate-spin`} />
                      ) : (
                        <span className="text-xs">{config.icon}</span>
                      )}
                    </div>
                    <div className="pt-0.5">
                      <span className={`text-xs font-semibold ${config.color}`}>{config.label}</span>
                      <span className="text-xs text-gray-500 ml-1.5">{step.summary}</span>
                      {step.durationMs > 0 && !isStreaming && (
                        <span className="text-[10px] text-gray-300 ml-1.5">{step.durationMs}ms</span>
                      )}
                    </div>
                  </motion.div>
                )
              })}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
