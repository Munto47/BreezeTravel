'use client'

import { Clock3, MapPin, ShieldAlert, Sparkles } from 'lucide-react'

import type { RevisionStop, SuggestionCandidateV1, SuggestionIntent, SuggestionSetV1 } from '@/types/workspace'


const INTENTS: Array<{ value: SuggestionIntent; label: string }> = [
  { value: 'NEARBY', label: '附近' },
  { value: 'POPULAR', label: '热门' },
  { value: 'FUN', label: '好玩' },
  { value: 'FOOD', label: '美食' },
]

const CLASSIFICATION: Record<SuggestionCandidateV1['classification'], string> = {
  ON_ROUTE: '顺路',
  ACCEPTABLE_DETOUR: '可接受绕行',
  DEFER_TO_OTHER_DAY: '更适合其他天',
  INFEASIBLE: '不可行',
}

const FRESHNESS: Record<SuggestionCandidateV1['evidence_freshness']['status'], string> = {
  FRESH: '证据新鲜',
  STALE: '证据已过期',
  UNKNOWN: '证据未知',
}

interface Props {
  anchor: RevisionStop | null
  suggestionSet: SuggestionSetV1 | null
  intents: SuggestionIntent[]
  pending: 'CREATE' | 'ACCEPT' | null
  message: string | null
  onToggleIntent: (intent: SuggestionIntent) => void
  onCreate: () => void
  onClose: () => void
  onAccept: (candidate: SuggestionCandidateV1) => void
}


export default function SuggestionSetPanel({
  anchor,
  suggestionSet,
  intents,
  pending,
  message,
  onToggleIntent,
  onCreate,
  onClose,
  onAccept,
}: Props) {
  return (
    <section data-testid="suggestion-set-panel" className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center gap-2">
        <Sparkles className="h-4 w-4 text-violet-600" />
        <h2 className="text-sm font-semibold text-slate-900">选择下一站</h2>
      </div>
      <p className="mt-1 text-xs text-slate-500">服务端冻结真实地点、路线证据与排序；接受时客户端只发送候选标识。</p>

      <div className="mt-3 rounded-xl bg-slate-50 p-3 text-xs">
        <p className="text-slate-500">当前 Anchor</p>
        <p data-testid="suggestion-anchor" className="mt-1 font-medium text-slate-900">
          {anchor ? `${anchor.raw_name || anchor.place_id} · 第 ${anchor.day_index + 1} 天` : '先在时间轴或地图选择一个地点'}
        </p>
      </div>

      <div className="mt-3 flex flex-wrap gap-1.5" aria-label="候选意图">
        {INTENTS.map(item => {
          const selected = intents.includes(item.value)
          return (
            <button
              key={item.value}
              type="button"
              aria-pressed={selected}
              onClick={() => onToggleIntent(item.value)}
              className={`rounded-full border px-2.5 py-1 text-xs ${selected ? 'border-violet-500 bg-violet-50 text-violet-800' : 'border-slate-200 text-slate-500'}`}
            >
              {item.label}
            </button>
          )
        })}
      </div>
      <button
        data-testid="create-suggestion-set"
        disabled={!anchor || intents.length === 0 || pending !== null}
        onClick={onCreate}
        className="mt-3 w-full rounded-xl bg-violet-700 px-3 py-2 text-xs font-medium text-white disabled:opacity-40"
      >
        {pending === 'CREATE' ? '正在冻结候选…' : suggestionSet ? '换一批候选' : '基于 Anchor 获取 4–6 个候选'}
      </button>

      {message && (
        <p role="alert" data-testid="suggestion-error" className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-2 text-xs text-amber-950">
          {message}
        </p>
      )}

      {suggestionSet && (
        <div data-testid="suggestion-set" className="mt-4">
          <div className="flex items-start justify-between gap-2">
            <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-slate-500">
              <span>冻结 {suggestionSet.candidates.length} 个</span>
              <span>· policy {suggestionSet.policy_version}</span>
              <span>· snapshot {suggestionSet.provider_snapshot_id}</span>
            </div>
            <button
              type="button"
              data-testid="close-suggestion-set"
              disabled={pending !== null}
              onClick={onClose}
              className="shrink-0 rounded-lg border border-slate-200 px-2 py-1 text-[11px] text-slate-600 disabled:opacity-40"
            >
              关闭候选
            </button>
          </div>
          {(suggestionSet.candidates.length < 4 || suggestionSet.candidates.length > 6) && (
            <p className="mt-2 rounded-lg bg-amber-50 p-2 text-[11px] text-amber-900">Provider 返回数量不在 4–6 范围内；仅展示实际冻结结果，不补造候选。</p>
          )}
          <div className="mt-2 space-y-2">
            {suggestionSet.candidates.map(candidate => (
              <CandidateCard
                key={candidate.candidate_id}
                candidate={candidate}
                disabled={pending !== null}
                onAccept={() => onAccept(candidate)}
              />
            ))}
          </div>
        </div>
      )}
    </section>
  )
}


function CandidateCard({ candidate, disabled, onAccept }: {
  candidate: SuggestionCandidateV1
  disabled: boolean
  onAccept: () => void
}) {
  const route = candidate.route_delta
  const freshness = candidate.evidence_freshness
  const blocked = !candidate.hard_gate.passed
  return (
    <article data-testid={`suggestion-candidate-${candidate.candidate_id}`} className={`rounded-xl border p-3 text-xs ${blocked ? 'border-rose-200 bg-rose-50/40' : 'border-slate-200'}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="font-medium text-slate-900">#{candidate.rank_position} {candidate.canonical_place.name}</p>
          <p className="mt-0.5 flex items-center gap-1 text-slate-500"><MapPin className="h-3 w-3" />{candidate.canonical_place.district || candidate.canonical_place.city} · {candidate.canonical_place.category}</p>
        </div>
        <span className="shrink-0 rounded-full bg-slate-100 px-2 py-1 text-[10px] text-slate-700">{CLASSIFICATION[candidate.classification]}</span>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-1.5 text-[11px]">
        <p className="rounded-lg bg-slate-50 p-2"><Clock3 className="mr-1 inline h-3 w-3" />{route.status === 'AVAILABLE' ? `路线增量 ${route.delta_route_minutes! >= 0 ? '+' : ''}${route.delta_route_minutes} 分钟` : `路线 ${route.status}: ${route.reason_code}`}</p>
        <p className={`rounded-lg p-2 ${freshness.status === 'FRESH' ? 'bg-emerald-50 text-emerald-800' : 'bg-amber-50 text-amber-900'}`}>{FRESHNESS[freshness.status]}{freshness.observed_at ? ` · ${new Date(freshness.observed_at).toLocaleString('zh-CN')}` : ''}</p>
      </div>
      <p className="mt-2 text-[11px] text-slate-600">解释：{candidate.explanation_codes.join(' · ')}</p>
      <p className="mt-1 text-[11px] text-slate-500">来源先验：{candidate.source_prior_refs.length ? candidate.source_prior_refs.join(' · ') : '无'}</p>
      {blocked && <p className="mt-2 flex items-center gap-1 text-[11px] font-medium text-rose-800"><ShieldAlert className="h-3 w-3" />HARD：{candidate.hard_gate.reason_codes.join(' · ')}</p>}
      {!blocked && <p className="mt-2 text-[11px] text-emerald-700">HARD 通过 · 综合分 {candidate.total_score.toFixed(3)}</p>}
      <button
        data-testid={`accept-suggestion-${candidate.candidate_id}`}
        disabled={disabled || blocked || freshness.status !== 'FRESH' || route.status !== 'AVAILABLE'}
        onClick={onAccept}
        className="mt-2 w-full rounded-lg bg-slate-900 px-2 py-1.5 text-white disabled:opacity-40"
      >
        接受并设为下一轮 Anchor
      </button>
    </article>
  )
}
