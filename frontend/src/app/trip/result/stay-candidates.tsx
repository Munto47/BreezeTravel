'use client'

import type {StaySuggestionView} from '@/lib/trip-understanding-v3'

/** Shared presentation keeps each overnight segment visible in both entrypoints. */
export default function StayCandidates({stay, disabled, onSelect}: {
  stay: StaySuggestionView; disabled: boolean; onSelect: (token: string) => void
}) {
  const segments = stay.segments?.length ? stay.segments : [{
    segment_token: 'legacy', city: null, overnight_days: [], status: stay.status,
    message: '', candidates: stay.candidates, preserved_hotels: [],
  }]
  return <div className="mt-3 space-y-4">{segments.map(segment => <section key={segment.segment_token} data-testid="stay-segment" className="space-y-2">
    {(segment.city || segment.overnight_days.length > 0) && <h3 className="text-sm font-semibold text-slate-800">{segment.city || '住宿安排'}{segment.overnight_days.length > 0 && <span className="ml-2 text-xs font-normal text-slate-500">{segment.overnight_days.join('、')}晚</span>}</h3>}
    {segment.message && <p className="text-xs leading-5 text-slate-600">{segment.message}</p>}
    {!!segment.preserved_hotels?.length && <p className="text-xs text-slate-600">保留原安排：{segment.preserved_hotels.join('、')}</p>}
    {segment.candidates.map(item => <article key={item.candidate_token} className="rounded-2xl border border-slate-200 bg-slate-50/60 p-3">
      <h4 className="text-sm font-semibold text-slate-900">{item.name}</h4>
      <p className="mt-1 text-xs leading-5 text-slate-500">{item.area_or_address}</p>
      <p className="mt-1 text-xs leading-5 text-slate-600">{item.commute_summary}</p>
      <p className="mt-1 text-xs leading-5 text-slate-500">{item.reason}</p>
      {item.brand_note && <p className="mt-1 text-xs leading-5 text-slate-500">{item.brand_note}</p>}
      <button type="button" data-testid="choose-stay" disabled={disabled || item.selected || stay.status === 'NEEDS_UPDATE' || segment.status === 'NEEDS_UPDATE'} onClick={() => onSelect(item.candidate_token)}
        className="mt-2 min-h-11 rounded-xl px-3 text-sm font-medium text-sky-800 hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600 disabled:opacity-40">{item.selected ? '已选择' : '选择这家住宿'}</button>
    </article>)}
  </section>)}</div>
}
