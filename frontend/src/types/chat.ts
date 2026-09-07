import type { Place } from './place'

export type MessageRole = 'user' | 'assistant' | 'system'
export type MessageStatus = 'sending' | 'streaming' | 'done' | 'error'

export type CollaborationProgressPhase = 'UNDERSTANDING' | 'FINDING_PLACES' | 'ORGANIZING'
export type CollaborationResultStatus = 'READY' | 'LIMITED'

export interface ChatMessage {
  messageId: string
  threadId: string
  role: MessageRole
  content: string         // 完整文本（流式时逐字追加）
  createdAt: string       // ISO 8601

  status: MessageStatus

  // AI 回复附加字段
  placesGenerated?: Place[]         // 本轮 AI 推荐的地点列表
  progressPhase?: CollaborationProgressPhase
  resultStatus?: CollaborationResultStatus
}
