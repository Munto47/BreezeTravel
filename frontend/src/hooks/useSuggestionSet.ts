'use client'

import { useRef, useState } from 'react'

import { api, ApiRequestError } from '@/lib/api'
import { randomUuid } from '@/lib/randomUuid'
import type {
  AcceptSuggestionResult,
  CreateSuggestionSetRequest,
  RecommendationEventCommandResult,
  SuggestionCandidateV1,
  SuggestionSetV1,
} from '@/types/workspace'


export function suggestionErrorMessage(reason: unknown): string {
  if (!(reason instanceof ApiRequestError)) {
    return reason instanceof Error ? reason.message : '下一站候选请求失败，未修改行程。'
  }
  const messages: Record<string, string> = {
    SUGGESTION_SET_EXPIRED: '这组冻结候选已经过期，没有加入地点。请基于当前版本重新获取。',
    SUGGESTION_SET_STALE: '行程版本已变化，这组候选已经失效，没有加入地点。请重新获取。',
    ITINERARY_REVISION_CONFLICT: '行程已被其他成员更新，候选没有加入。刷新工作台后再获取下一站。',
    SUGGESTION_PROVIDER_UNAVAILABLE: '实时地点或路线来源暂不可用，没有生成候选，也没有修改行程。',
    SUGGESTION_CANDIDATE_EVIDENCE_UNAVAILABLE: '候选的地点或路线证据不可用，没有加入行程。',
    SUGGESTION_CANDIDATE_HARD_BLOCKED: '该候选未通过硬约束，不能加入行程。',
    SUGGESTION_INSERT_EDGE_CONFLICT: '候选绑定的插入位置已变化，没有加入地点。请重新选择 Anchor。',
  }
  if (reason.code && messages[reason.code]) return messages[reason.code]
  if (reason.status === 409) return '候选与当前行程状态冲突，没有加入地点。请刷新后重新获取。'
  return reason.message || '下一站候选请求失败，未修改行程。'
}


export function useSuggestionSet(workspaceId: string) {
  const [suggestionSet, setSuggestionSet] = useState<SuggestionSetV1 | null>(null)
  const [pending, setPending] = useState<'CREATE' | 'ACCEPT' | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const sessionId = useRef<string | null>(null)
  const generation = useRef(0)
  const eventIdempotencyKeys = useRef(new Map<string, string>())
  const acceptedSuggestionCount = useRef(0)

  const getSessionId = () => {
    if (!sessionId.current) sessionId.current = randomUuid()
    return sessionId.current
  }

  const eventKey = (action: string, setId: string, candidateId = '') => {
    const identity = `${action}:${setId}:${candidateId}`
    const existing = eventIdempotencyKeys.current.get(identity)
    if (existing) return existing
    const created = randomUuid()
    eventIdempotencyKeys.current.set(identity, created)
    return created
  }

  const recordPreview = async (set: SuggestionSetV1, candidate: SuggestionCandidateV1) => {
    await api.postWithHeaders<RecommendationEventCommandResult>(
      `/api/trip-workspaces/${workspaceId}/suggestion-sets/${encodeURIComponent(set.suggestion_set_id)}/candidates/${encodeURIComponent(candidate.candidate_id)}:preview`,
      {},
      { 'Idempotency-Key': eventKey('preview', set.suggestion_set_id, candidate.candidate_id) },
    )
  }

  const recordDismissals = async (set: SuggestionSetV1, reasonCode: 'USER_CLOSED' | 'BATCH_REPLACED') => {
    await Promise.allSettled(set.candidates.map(candidate => api.postWithHeaders<RecommendationEventCommandResult>(
      `/api/trip-workspaces/${workspaceId}/suggestion-sets/${encodeURIComponent(set.suggestion_set_id)}/candidates/${encodeURIComponent(candidate.candidate_id)}:dismiss`,
      { reason_code: reasonCode },
      { 'Idempotency-Key': eventKey(`dismiss:${reasonCode}`, set.suggestion_set_id, candidate.candidate_id) },
    )))
  }

  const recordLineCompleted = async (set: SuggestionSetV1) => {
    await api.postWithHeaders<RecommendationEventCommandResult>(
      `/api/trip-workspaces/${workspaceId}/suggestion-sets/${encodeURIComponent(set.suggestion_set_id)}:line-completed`,
      {},
      { 'Idempotency-Key': eventKey('line-completed', set.suggestion_set_id) },
    )
  }

  const create = async (request: Omit<CreateSuggestionSetRequest, 'session_id'>) => {
    const replaced = suggestionSet
    const requestGeneration = ++generation.current
    setPending('CREATE')
    setMessage(null)
    // A failed refresh must not leave an old set looking current.
    setSuggestionSet(null)
    if (replaced) void recordDismissals(replaced, 'BATCH_REPLACED')
    try {
      const result = await api.post<SuggestionSetV1>(
        `/api/trip-workspaces/${workspaceId}/suggestion-sets`,
        { ...request, session_id: getSessionId() },
      )
      // A drag/edit/reload can invalidate the insertion edge while provider
      // ranking is still in flight. Never resurrect that late, stale set.
      if (generation.current !== requestGeneration) return null
      setSuggestionSet(result)
      // Cards are all rendered in this compact panel. Event delivery is
      // best-effort telemetry and must never turn a valid frozen set into a
      // failed itinerary operation.
      void Promise.allSettled(result.candidates.map(candidate => recordPreview(result, candidate)))
      return result
    } catch (reason) {
      if (generation.current !== requestGeneration) return null
      setSuggestionSet(null)
      setMessage(suggestionErrorMessage(reason))
      return null
    } finally {
      if (generation.current === requestGeneration) setPending(null)
    }
  }

  const accept = async (candidate: SuggestionCandidateV1) => {
    const current = suggestionSet
    if (!current || current.suggestion_set_id !== candidate.suggestion_set_id) return null
    setPending('ACCEPT')
    setMessage(null)
    try {
      const idempotencyKey = randomUuid()
      // The empty body is intentional. Canonical POI, coordinates, scores and
      // evidence remain server-frozen and are never echoed as edit authority.
      const result = await api.postWithHeaders<AcceptSuggestionResult>(
        `/api/trip-workspaces/${workspaceId}/suggestion-sets/${encodeURIComponent(current.suggestion_set_id)}/candidates/${encodeURIComponent(candidate.candidate_id)}:accept`,
        {},
        { 'If-Match': `"${current.base_revision}"`, 'Idempotency-Key': idempotencyKey },
      )
      setSuggestionSet(null)
      acceptedSuggestionCount.current += 1
      if (acceptedSuggestionCount.current === 3) {
        // A seed plus three accepted suggestions is the four-stop continuous
        // line used by the Builder gate. This event is observational only.
        void recordLineCompleted(current).catch(() => undefined)
      }
      return result
    } catch (reason) {
      // Any failed accept invalidates this client view of the frozen set. The
      // next attempt must obtain a set bound to the latest server revision.
      acceptedSuggestionCount.current = 0
      setSuggestionSet(null)
      setMessage(suggestionErrorMessage(reason))
      return null
    } finally {
      setPending(null)
    }
  }

  const close = () => {
    const current = suggestionSet
    generation.current += 1
    setPending(null)
    setSuggestionSet(null)
    setMessage(null)
    acceptedSuggestionCount.current = 0
    if (current) void recordDismissals(current, 'USER_CLOSED')
  }

  const clear = () => {
    generation.current += 1
    setPending(null)
    setSuggestionSet(null)
    setMessage(null)
    acceptedSuggestionCount.current = 0
  }

  return { suggestionSet, pending, message, create, accept, close, clear }
}
