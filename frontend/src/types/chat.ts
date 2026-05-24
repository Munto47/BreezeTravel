import type { Place } from './place'

export type MessageRole = 'user' | 'assistant' | 'system'
export type MessageStatus = 'sending' | 'streaming' | 'done' | 'error'

/** LangGraph 各节点的执行状态（用于 ThinkingSteps 组件展示）*/
export interface ThinkingStep {
  node: 'router' | 'rag_retrieval' | 'amap_search' | 'synthesizer' | 'optimizer'
  summary: string       // 简短说明，如"检索到 5 篇相关游记"
  durationMs: number    // 节点耗时（毫秒）
}

export interface ChatMessage {
  messageId: string
  threadId: string
  role: MessageRole
  content: string         // 完整文本（流式时逐字追加）
  createdAt: string       // ISO 8601

  status: MessageStatus

  // AI 回复附加字段
  agentNode?: string                // 触发回复的最终节点
  placesGenerated?: Place[]         // 本轮 AI 推荐的地点列表
  thinkingSteps?: ThinkingStep[]    // Agent 思考链（实时展示推理过程）
}
