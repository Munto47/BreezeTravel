'use client'

import {useEffect, useRef, useState} from 'react'
import type {UserFacingTripResult, TripSupplementaryView, TripUnderstandingCommand} from '@/lib/trip-understanding-v3'
import type {WorkspaceCommandResult} from './itinerary-workspace'
import {relativeDayLabel} from './result-presentation'
import PendingPlaceDropdown from './pending-place-dropdown'
import PendingLodgingRecovery from './pending-lodging-recovery'

export default function UnresolvedPlaces({days, coverage, resource, disabled, onCommand, visitDays=days, etag='', pendingLodgings=[],
  lodgingDetails=[], lodgingStatus='IDLE', sourceDeleted=false, onLoadLodgings, onClearLodgings, onRefresh}: {
  days: UserFacingTripResult['days']; coverage?: UserFacingTripResult['coverage']; resource: string; disabled: boolean
  onCommand: (command: TripUnderstandingCommand) => Promise<WorkspaceCommandResult>
  visitDays?:UserFacingTripResult['days']; etag?:string; pendingLodgings?:UserFacingTripResult['pending_lodgings']
  lodgingDetails?:TripSupplementaryView['pending_lodgings']; lodgingStatus?:'IDLE'|'LOADING'|'AVAILABLE'|'UNAVAILABLE'|'DELETED'
  sourceDeleted?:boolean; onLoadLodgings?:()=>void; onClearLodgings?:()=>void;onRefresh?:()=>void
}) {
  const [selected, setSelected] = useState<string | null>(null)
  const [selectedHotel, setSelectedHotel] = useState<string|null>(null)
  const [open, setOpen] = useState(false)
  const details=useRef<HTMLDetailsElement>(null)
  useEffect(()=>()=>onClearLodgings?.(),[onClearLodgings])
  const hotelReferences=sourceDeleted?[]:pendingLodgings
  const hotels=(lodgingDetails||[]).filter(hotel=>hotelReferences?.some(reference=>reference.pending_token===hotel.pending_token))
  const count = days.reduce((total, day) => total + day.activities.length, 0)+(hotelReferences?.length||0)
  const unclassified = coverage?.unclassified_mention_count || 0
  const unprocessed = coverage?.unprocessed_count || 0
  if (!count && !unclassified && !unprocessed) return null
  function close(token: string) {
    setSelected(null)
    requestAnimationFrame(() => document.getElementById(`recover-${token}`)?.focus({preventScroll:true}))
  }
  function closeHotel() {
    setSelectedHotel(null);onClearLodgings?.()
    if(details.current)details.current.open=false
    requestAnimationFrame(()=>details.current?.querySelector('summary')?.focus({preventScroll:true}))
  }
  return <details ref={details} className="my-2 rounded-2xl border border-sky-100 bg-white p-3" data-testid="unresolved-places" onToggle={event => {
    const expanded=event.currentTarget.open;setOpen(expanded)
    if(expanded&&hotelReferences?.length)onLoadLodgings?.()
    if(!expanded){setSelected(null);setSelectedHotel(null);onClearLodgings?.()}
  }}>
    <summary className="min-h-11 cursor-pointer text-sm leading-6 text-slate-600" data-testid="unmatched-places-note">{count ? `已展示匹配到的真实地点。另有 ${count} 项待确认，展开补全。` : '部分原文尚未完整整理，展开查看补全方式。'}</summary>
    {!!(unclassified || unprocessed) && <p className="my-2 text-sm text-slate-600">部分原文尚未完整整理，请对照原文补充；已确认地点可以继续使用。</p>}
    {open&&!!hotelReferences?.length&&<section data-testid="pending-lodgings" className="mb-3">
      <h3 className="text-xs font-semibold text-slate-500">住宿用途待确认</h3>
      {lodgingStatus==='LOADING'&&<p role="status" className="my-2 text-sm">正在读取待确认酒店…</p>}
      {lodgingStatus==='UNAVAILABLE'&&<p role="status" className="my-2 text-sm">暂时无法读取酒店名称。<button type="button" className="e-button" disabled={disabled} onClick={onLoadLodgings}>重新读取酒店</button></p>}
      {lodgingStatus==='DELETED'&&<p role="status" className="my-2 text-sm">原文已删除，未确认酒店不能恢复。</p>}
      {hotels.map(hotel=><div key={hotel.pending_token} className="relative mt-1">
        <button type="button" className="min-h-11 rounded-xl px-3 text-left text-sm text-sky-800 focus-visible:ring-2 focus-visible:ring-sky-600"
          disabled={disabled} aria-expanded={selectedHotel===hotel.pending_token} onClick={()=>{setSelected(null);setSelectedHotel(selectedHotel===hotel.pending_token?null:hotel.pending_token)}}>
          {hotel.name} · 住宿用途待确认</button>
        {selectedHotel===hotel.pending_token&&<PendingLodgingRecovery hotel={hotel} days={visitDays} resource={resource} etag={etag}
          disabled={disabled} onCommand={onCommand} onClose={closeHotel} onInvalidated={()=>{closeHotel();onRefresh?.()}}/>}
      </div>)}
    </section>}
    {open && days.map((day, dayIndex) => day.activities.length > 0 && <section key={day.label} className="mb-3"><h3 className="text-xs font-semibold text-slate-500">{relativeDayLabel(dayIndex)}</h3>{day.activities.map(card => <div key={card.activity_token} className="relative mt-1">
      <button type="button" id={`recover-${card.activity_token}`} disabled={disabled} aria-expanded={selected === card.activity_token} onClick={() => setSelected(selected === card.activity_token ? null : card.activity_token)} className="min-h-11 rounded-xl px-3 text-left text-sm text-sky-800 hover:bg-sky-50 focus-visible:ring-2 focus-visible:ring-sky-600">{card.name}{card.city ? ` · ${card.city}` : ''} · 确认地点</button>
      {selected === card.activity_token && <PendingPlaceDropdown card={card} resource={resource} disabled={disabled} onCommand={onCommand} onClose={() => close(card.activity_token)}/>}
    </div>)}</section>)}
  </details>
}
