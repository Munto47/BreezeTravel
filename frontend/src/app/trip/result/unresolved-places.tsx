'use client'

import {useState} from 'react'
import type {UserFacingTripResult, TripUnderstandingCommand} from '@/lib/trip-understanding-v3'
import type {WorkspaceCommandResult} from './itinerary-workspace'
import PendingPlaceDropdown from './pending-place-dropdown'

export default function UnresolvedPlaces({days, coverage, resource, disabled, onCommand}: {
  days: UserFacingTripResult['days']; coverage?: UserFacingTripResult['coverage']; resource: string; disabled: boolean
  onCommand: (command: TripUnderstandingCommand) => Promise<WorkspaceCommandResult>
}) {
  const [selected, setSelected] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const count = days.reduce((total, day) => total + day.activities.length, 0)
  const unclassified = coverage?.unclassified_mention_count || 0
  const unprocessed = coverage?.unprocessed_count || 0
  if (!count && !unclassified && !unprocessed) return null
  function close(token: string) {
    setSelected(null)
    requestAnimationFrame(() => document.getElementById(`recover-${token}`)?.focus({preventScroll:true}))
  }
  return <details className="my-2 rounded-2xl border border-sky-100 bg-white p-3" data-testid="unresolved-places" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="min-h-11 cursor-pointer text-sm leading-6 text-slate-600" data-testid="unmatched-places-note">{count ? `已展示匹配到的真实地点。另有 ${count} 项待确认，展开补全。` : '部分原文尚未完整整理，展开查看补全方式。'}</summary>
    {!!(unclassified || unprocessed) && <p className="my-2 text-sm text-slate-600">部分原文尚未完整整理，请对照原文补充；已确认地点可以继续使用。</p>}
    {open && days.map(day => day.activities.length > 0 && <section key={day.label} className="mb-3"><h3 className="text-xs font-semibold text-slate-500">{day.label}</h3>{day.activities.map(card => <div key={card.activity_token} className="relative mt-1">
      <button type="button" id={`recover-${card.activity_token}`} disabled={disabled} aria-expanded={selected === card.activity_token} onClick={() => setSelected(selected === card.activity_token ? null : card.activity_token)} className="min-h-11 rounded-xl px-3 text-left text-sm text-sky-800 hover:bg-sky-50 focus-visible:ring-2 focus-visible:ring-sky-600">{card.name}{card.city ? ` · ${card.city}` : ''} · 确认地点</button>
      {selected === card.activity_token && <PendingPlaceDropdown card={card} resource={resource} disabled={disabled} onCommand={onCommand} onClose={() => close(card.activity_token)}/>}
    </div>)}</section>)}
  </details>
}
