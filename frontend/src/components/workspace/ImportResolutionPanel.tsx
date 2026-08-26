'use client'

import { useState } from 'react'
import { AlertTriangle, CheckCircle2, Loader2, Lock, Search } from 'lucide-react'

import type { ItineraryImport } from '@/types/workspace'


interface Props {
  itineraryImport: ItineraryImport
  selections: Record<string, string>
  onSelect: (rawStopId: string, placeId: string) => void
  onSearchCandidates: (rawStopId: string, query: string) => void
  searchingRawStopId: string | null
}


export default function ImportResolutionPanel({
  itineraryImport,
  selections,
  onSelect,
  onSearchCandidates,
  searchingRawStopId,
}: Props) {
  const resolutionByStop = new Map(itineraryImport.resolutions.map(item => [item.raw_stop_id, item]))
  const [queries, setQueries] = useState<Record<string, string>>({})
  const usesFixtureCandidates = itineraryImport.resolutions.some(resolution => (
    resolution.candidates.some(candidate => candidate.execution_mode === 'fixture')
  ))

  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold text-slate-900">解析结果与地点确认</h2>
          <p className="mt-1 text-xs text-slate-500">低置信度地点不会自动接受；原文位置和固定承诺会随草稿保留。</p>
          {usesFixtureCandidates && (
            <p className="mt-2 text-xs text-amber-700">当前为本地 fixture 候选，仅用于受控开发测试，不是实时 Provider 核验。</p>
          )}
        </div>
        <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs text-slate-600">{itineraryImport.status}</span>
      </div>

      {itineraryImport.parse_errors.length > 0 && (
        <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
          {itineraryImport.parse_errors.map(item => <p key={item}>• {item}</p>)}
        </div>
      )}

      <div className="mt-4 space-y-3">
        {itineraryImport.raw_stops.map((rawStop) => {
          const resolution = resolutionByStop.get(rawStop.raw_stop_id)
          const resolved = resolution?.resolution_status === 'AUTO_MATCHED' || resolution?.resolution_status === 'USER_CONFIRMED'
          const canonicalCandidate = resolution?.candidates.find(
            candidate => candidate.place_id === resolution.canonical_place_id,
          )
          return (
            <article key={rawStop.raw_stop_id} className="rounded-xl border border-slate-200 p-4">
              <div className="flex items-start gap-3">
                <div className={`mt-0.5 rounded-lg p-2 ${resolved ? 'bg-emerald-50 text-emerald-600' : 'bg-amber-50 text-amber-700'}`}>
                  {resolved ? <CheckCircle2 className="h-4 w-4" /> : <AlertTriangle className="h-4 w-4" />}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="font-medium text-slate-900">D{(rawStop.day_index ?? 0) + 1} · {rawStop.raw_name}</p>
                    {rawStop.raw_time && <span className="text-xs text-slate-500">{rawStop.raw_time}</span>}
                    {rawStop.fixed_commitment && (
                      <span className="inline-flex items-center gap-1 rounded-full bg-rose-50 px-2 py-0.5 text-[11px] text-rose-700">
                        <Lock className="h-3 w-3" /> 固定承诺
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-xs text-slate-400">
                    原文 {rawStop.source_span.start}–{rawStop.source_span.end}：{rawStop.source_sentence}
                  </p>

                  {resolution && !resolved && resolution.candidates.length > 0 && (
                    <div className="mt-3">
                      <label className="mb-1 block text-xs font-medium text-slate-600">
                        {usesFixtureCandidates ? '请选择候选地点（本地 fixture）' : '请选择候选地点'}
                      </label>
                      <select
                        value={selections[rawStop.raw_stop_id] ?? ''}
                        onChange={event => onSelect(rawStop.raw_stop_id, event.target.value)}
                        className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-coral-400"
                      >
                        <option value="">尚未确认</option>
                        {resolution.candidates.map(candidate => (
                          <option key={candidate.place_id} value={candidate.place_id}>
                            {candidate.name} · {candidate.district ?? candidate.city} · {(candidate.score * 100).toFixed(0)}%
                          </option>
                        ))}
                      </select>
                    </div>
                  )}

                  {resolution && !resolved && (
                    <div className="mt-3 rounded-xl border border-slate-200 bg-slate-50 p-3">
                      {resolution.candidates.length === 0 && (
                        <p className="mb-2 text-xs text-rose-600">未找到可靠候选。请修改关键词重新检索，不能手填或静默生成 POI。</p>
                      )}
                      <div className="flex flex-col gap-2 sm:flex-row">
                        <input
                          value={queries[rawStop.raw_stop_id] ?? rawStop.raw_name}
                          onChange={event => setQueries(current => ({
                            ...current,
                            [rawStop.raw_stop_id]: event.target.value,
                          }))}
                          maxLength={160}
                          aria-label={`${rawStop.raw_name} 的候选检索词`}
                          className="min-w-0 flex-1 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-coral-400"
                        />
                        <button
                          type="button"
                          onClick={() => onSearchCandidates(
                            rawStop.raw_stop_id,
                            queries[rawStop.raw_stop_id] ?? rawStop.raw_name,
                          )}
                          disabled={searchingRawStopId !== null || !(queries[rawStop.raw_stop_id] ?? rawStop.raw_name).trim()}
                          className="inline-flex items-center justify-center gap-1 rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-medium text-slate-700 disabled:opacity-40"
                        >
                          {searchingRawStopId === rawStop.raw_stop_id
                            ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            : <Search className="h-3.5 w-3.5" />}
                          重新搜索候选
                        </button>
                      </div>
                    </div>
                  )}

                  {resolved && resolution?.canonical_place_id && (
                    <p className="mt-2 text-xs text-emerald-700">
                      已绑定 POI：{canonicalCandidate?.name ?? resolution.canonical_place_id}
                      {canonicalCandidate && ` · ${resolution.canonical_place_id}`}
                      {' '}· 置信度 {(resolution.confidence * 100).toFixed(0)}%
                      {canonicalCandidate?.execution_mode === 'fixture' && ' · 本地 fixture'}
                    </p>
                  )}
                </div>
              </div>
            </article>
          )
        })}
      </div>
    </section>
  )
}
