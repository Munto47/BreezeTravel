'use client'

import { ArrowUpRight, CheckCircle2, RefreshCw } from 'lucide-react'

import type { MapRenderView, PublicTripCheckItem, PublicTripChecksView } from '@/lib/trip-understanding-v3'
import { topPublicChecks } from './result-presentation'
import { findingLabel, needsRecheck } from './presentation'

export default function ChecksWorkspace({
  checks,
  mapView,
  checking,
  error,
  disabled,
  onRetry,
  onPreview,
  onLocate,
}: {
  checks: PublicTripChecksView | null
  mapView: MapRenderView | null
  checking: boolean
  error: string
  disabled: boolean
  onRetry: () => void
  onPreview: (item: PublicTripCheckItem) => void
  onLocate: (item: PublicTripCheckItem) => void
}) {
  const items = topPublicChecks(checks)
  return (
    <section data-testid="trip-checks" id="checks-view" aria-label="优先检查" className="mx-auto max-w-5xl px-4 pb-28 pt-8 lg:pb-12 lg:pl-24">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-xs font-semibold tracking-[0.14em] text-[#0c789d]">优先检查 · 最多三项</p>
          <h2 className="mt-2 text-3xl font-semibold tracking-tight text-slate-900">先处理真正影响出行的事</h2>
          <p className="mt-2 text-sm leading-6 text-slate-600">这里只保留可以理解、可以行动的结论，不展示内部编号或技术依据。</p>
        </div>
        <button
          type="button"
          disabled={disabled || checking}
          onClick={onRetry}
          className="inline-flex min-h-11 items-center gap-2 rounded-xl border border-[#0c789d]/20 bg-white px-4 text-sm font-semibold text-[#0c789d] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] disabled:opacity-50"
        >
          <RefreshCw className="h-4 w-4" aria-hidden="true" />
          {checking ? '正在检查…' : '重新检查'}
        </button>
      </header>

      {error && <p className="mt-5 rounded-2xl bg-amber-50 p-4 text-sm text-amber-900" role="status">{error}</p>}
      {!checking && !items.length ? (
        <div className="mt-8 rounded-[1.75rem] border border-sky-900/10 bg-white/85 p-8 text-center shadow-sm">
          <CheckCircle2 className="mx-auto h-10 w-10 text-[#0c789d]" aria-hidden="true" />
          <h3 className="mt-3 text-xl font-semibold text-slate-900">当前没有需要优先处理的项目</h3>
          <p className="mt-2 text-sm text-slate-600">{checks?.message || '地点或路线仍在准备时，可以稍后再检查。'}</p>
        </div>
      ) : (
        <div className="mt-8 grid gap-4">
          {items.map((item, index) => {
            const stale = needsRecheck(item, mapView)
            const label = findingLabel(item, mapView)
            const hard = label === '必须调整' && !stale
            const labelTone = hard
              ? 'bg-red-50 text-red-700'
              : label === '可以更好' && !stale
                ? 'bg-amber-50 text-amber-800'
                : 'bg-blue-50 text-blue-800'
            return (
              <article data-testid="trip-check-item" key={item.check_token} className={`rounded-[1.75rem] border bg-white/90 p-5 shadow-[0_20px_55px_-38px_rgba(15,23,42,0.45)] ${hard ? 'border-red-300' : 'border-sky-900/10'}`}>
                <div className="flex items-start gap-4">
                  <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-sm font-bold ${hard ? 'bg-red-50 text-red-700' : 'bg-sky-50 text-[#0c789d]'}`}>{index + 1}</div>
                  <div className="min-w-0 flex-1">
                    <p className="text-xs font-semibold"><span className={`inline-flex rounded-full px-2.5 py-1 ${labelTone}`}>{stale ? '需要确认' : label}</span></p>
                    <h3 className="mt-1 text-lg font-semibold text-slate-900">{stale ? '交通待更新，需重新检查' : item.title}</h3>
                    <p className="mt-2 text-sm leading-6 text-slate-600">{stale ? '先手动更新路线，再确认这项安排是否冲突。' : item.message}</p>
                    {!!item.affected_days.length && <p className="mt-2 text-xs text-slate-500">涉及：{item.affected_days.join('、')}</p>}
                    <div className="mt-4 flex flex-wrap gap-3">
                      {item.can_preview && !stale && (
                        <button data-testid="preview-change" type="button" disabled={disabled || checking} onClick={() => onPreview(item)} className="inline-flex min-h-12 items-center gap-2 rounded-xl bg-[#0c789d] px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] focus-visible:ring-offset-2 disabled:opacity-50">预览建议 <ArrowUpRight className="h-4 w-4" aria-hidden="true" /></button>
                      )}
                      {!!item.affected_activity_tokens?.length && (
                        <button type="button" disabled={disabled} onClick={() => onLocate(item)} className="min-h-11 rounded-xl border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] disabled:opacity-50">手动调整</button>
                      )}
                    </div>
                  </div>
                </div>
              </article>
            )
          })}
        </div>
      )}
    </section>
  )
}
