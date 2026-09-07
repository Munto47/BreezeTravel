'use client'

import {useEffect, useRef, useState} from 'react'
import {Sparkles, X} from 'lucide-react'
import {queryTripDiningCandidates, type DiningCandidatesView, type UserFacingTripResult,
  type TripUnderstandingCommand, type PublicTripChecksView, type PublicTripCheckItem,
  type MapRenderView, type StaySuggestionView} from '@/lib/trip-understanding-v3'
import type {WorkspaceCommandResult} from './itinerary-workspace'
import {boundedTripRequest} from './use-trip-experience'
import {findingLabel, needsRecheck} from './presentation'

type Props = {
  resource: string; etag: string; result: UserFacingTripResult; disabled: boolean
  checks: PublicTripChecksView | null; checking: boolean; checksError: string
  map: MapRenderView | null; stay: StaySuggestionView | null
  onCommand: (command: TripUnderstandingCommand) => Promise<WorkspaceCommandResult>
  onRetry: () => void; onPreview: (item: PublicTripCheckItem) => void
  onLocate: (item: PublicTripCheckItem) => void; onStay: (token: string) => void
  alternativesRequest?: {dayIndex: number; trigger: HTMLButtonElement} | null
}

export default function JourneySuggestions(props: Props) {
  const {result, resource, etag, disabled} = props
  const [open, setOpen] = useState(false)
  const [dayIndex, setDayIndex] = useState(0)
  const [anchorIndex, setAnchorIndex] = useState(0)
  const [searching, setSearching] = useState(false)
  const [dining, setDining] = useState<{view: DiningCandidatesView; anchor: string; etag: string} | null>(null)
  const [error, setError] = useState('')
  const [writing, setWriting] = useState(false)
  const writeLock = useRef(false)
  const generation = useRef(0)
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLElement>(null)
  const alternativesSection = useRef<HTMLDetailsElement>(null)
  const returnFocus = useRef<HTMLElement | null>(null)
  const pendingAlternativesFocus = useRef(false)
  const currentDayIndex = Math.min(dayIndex, Math.max(0, result.days.length - 1))
  const day = result.days[currentDayIndex]
  const anchors = day?.activities.filter(card => card.status === 'READY' && card.category !== '餐饮') || []
  const anchor = anchors[Math.min(anchorIndex, Math.max(0, anchors.length - 1))]
  const stale = Boolean(dining && dining.etag !== etag)
  const busy = disabled || writing
  const alternatives = day?.alternatives || []
  const stay = props.stay || result.stay
  const items = props.checks?.items || []

  function closePanel() {
    setOpen(false)
    pendingAlternativesFocus.current = false
    if (returnFocus.current?.isConnected) {
      returnFocus.current.focus({preventScroll: true})
      return
    }
    const heading = props.alternativesRequest && document.querySelector<HTMLElement>(`[data-day-heading="${props.alternativesRequest.dayIndex + 1}"]`)
    ;(heading || trigger.current)?.focus({preventScroll: true})
  }

  useEffect(() => {
    const request = props.alternativesRequest
    if (!request) return
    returnFocus.current = request.trigger
    pendingAlternativesFocus.current = true
    setDayIndex(request.dayIndex); setAnchorIndex(0); setDining(null); setError(''); setOpen(true)
  }, [props.alternativesRequest])
  useEffect(() => {
    if (!open || !pendingAlternativesFocus.current || !props.alternativesRequest || currentDayIndex !== props.alternativesRequest.dayIndex) return
    const section = alternativesSection.current
    if (!section) return
    pendingAlternativesFocus.current = false
    section.open = true
    section.scrollIntoView({block: 'nearest'})
    section.querySelector('summary')?.focus({preventScroll: true})
  }, [open, props.alternativesRequest, currentDayIndex])

  useEffect(() => {
    generation.current += 1
    setSearching(false)
  }, [resource, etag, currentDayIndex])
  useEffect(() => {
    if (!open) return
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closePanel()
    }
    const outside = (event: PointerEvent) => {
      if (!panel.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('keydown', close)
    document.addEventListener('pointerdown', outside)
    return () => {document.removeEventListener('keydown', close); document.removeEventListener('pointerdown', outside)}
  }, [open, props.alternativesRequest])
  useEffect(() => () => {generation.current += 1}, [])

  async function findDining() {
    if (!anchor || searching || busy) return
    const stamp = ++generation.current
    const token = anchor.activity_token
    setSearching(true); setError(''); setDining(null)
    try {
      const response = await boundedTripRequest(signal => queryTripDiningCandidates(resource, token, signal))
      if (stamp === generation.current) setDining({view: response.body, anchor: token, etag: response.etag})
    } catch {
      if (stamp === generation.current) setError('暂时没能查到附近餐饮，请重试。')
    } finally {
      if (stamp === generation.current) setSearching(false)
    }
  }

  async function apply(command: TripUnderstandingCommand) {
    if (writeLock.current || busy) return
    writeLock.current = true; setWriting(true); setError('')
    try {
      const outcome = await props.onCommand(command)
      if (outcome.status === 'APPLIED') setDining(null)
    } catch {setError('保存结果尚未确认，请使用页面上的确认保存入口。')}
    finally {writeLock.current = false; setWriting(false)}
  }

  const action = 'min-h-11 rounded-xl px-3 text-sm font-medium text-sky-800 hover:bg-sky-50 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600'
  return <div className="relative">
    <button ref={trigger} type="button" className={action} aria-expanded={open} aria-controls="journey-suggestions"
      onClick={() => {returnFocus.current = trigger.current; pendingAlternativesFocus.current = false; setOpen(value => !value)}} data-testid="journey-suggestions-toggle"><Sparkles className="mr-1 inline h-4 w-4" aria-hidden="true"/>检查与建议</button>
    {open && <aside ref={panel} id="journey-suggestions" aria-label="检查与建议" className="fixed right-3 top-24 z-50 max-h-[calc(100dvh-7rem)] w-[min(25rem,calc(100vw-1.5rem))] overflow-y-auto rounded-3xl border border-sky-100 bg-white p-4 shadow-xl">
      <div className="flex items-center justify-between"><strong>检查与建议</strong><button type="button" className={action} aria-label="关闭建议" onClick={closePanel}><X className="h-4 w-4"/></button></div>
      <details open className="border-b border-slate-100 py-2"><summary className="cursor-pointer py-2 text-sm font-semibold">行程检查</summary>
        <div className="grid gap-3" aria-live="polite">
          {items.map(item => {
            const outdated = needsRecheck(item, props.map)
            const label = findingLabel(item, props.map)
            return <article key={item.check_token} className="rounded-2xl bg-slate-50 p-3" data-testid="suggestion-check">
              <span className={`text-xs ${label === '必须调整' && !outdated ? 'text-red-700' : 'text-sky-800'}`}>{outdated ? '需要确认' : label}</span>
              <h3 className="mt-1 text-sm font-semibold">{outdated ? '路线更新后再检查' : item.title}</h3>
              <p className="mt-1 text-sm text-slate-600">{outdated ? '当前行程已调整，请手动更新路线。' : item.message}</p>
              {item.can_preview && !outdated && <button className={action} disabled={busy || props.checking} onClick={() => {setOpen(false); props.onPreview(item)}}>预览调整</button>}
              {!!item.affected_activity_tokens?.length && <button className={action} disabled={busy} onClick={() => {setOpen(false); props.onLocate(item)}}>查看涉及地点</button>}
            </article>
          })}
          {!items.length && <p className="text-sm text-slate-500">{props.checking ? '正在检查…' : props.checksError || props.checks?.message || '检查结果暂未就绪。'}</p>}
          <button className={action} disabled={busy || props.checking} onClick={props.onRetry}>重新检查</button>
          {!!items.length && <p className="text-xs leading-5 text-slate-500">{props.checks?.message}</p>}
          <p className="text-xs leading-5 text-slate-500">按已有地点、路线和明确时间检查；营业、预约与天气尚未核验。</p>
        </div>
      </details>
      <label className="my-3 flex items-center gap-3 text-sm">日期<select aria-label="建议所属日期" className="min-h-11 min-w-0 flex-1 rounded-xl bg-slate-50 px-3" value={currentDayIndex} onChange={event => {setDayIndex(Number(event.target.value)); setAnchorIndex(0); setDining(null); setError('')}}>{result.days.map((item, index) => <option key={index} value={index}>{item.label}</option>)}</select></label>
      <details className="border-b border-slate-100 py-2"><summary className="cursor-pointer py-2 text-sm font-semibold">附近餐饮</summary>
        {anchors.length ? <><label className="mt-2 flex items-center gap-2 text-sm">靠近<select aria-label="餐饮附近地点" className="min-h-11 min-w-0 flex-1 rounded-xl bg-slate-50 px-3" value={Math.min(anchorIndex, anchors.length - 1)} onChange={event => {setAnchorIndex(Number(event.target.value)); generation.current++; setSearching(false); setDining(null)}}>{anchors.map((card, index) => <option key={index} value={index}>{card.name}</option>)}</select></label>
          <button type="button" className={action} disabled={busy || searching} onClick={() => void findDining()}>{searching ? '正在查找…' : '找附近餐饮'}</button></> : <p className="py-2 text-sm text-slate-500">先确认当天的一个地点。</p>}
        {dining && <><p className="py-2 text-sm text-slate-500">{stale ? '行程有调整，请重新查询。' : dining.view.message}</p>{!stale && dining.view.candidates.map(item => <article key={item.candidate_token} className="my-2 rounded-2xl bg-slate-50 p-3"><h3 className="text-sm font-semibold">{item.name}</h3><p className="mt-1 text-xs text-slate-500">{item.area_or_address}</p><p className="mt-1 text-xs text-slate-500">{item.reason}</p><button className={action} disabled={busy} onClick={() => void apply({command_type:'DINING_INSERT', after_activity_token:dining.anchor, candidate_token:item.candidate_token})}>加入行程</button></article>)}</>}
      </details>
      {!!alternatives.length && <details ref={alternativesSection} className="border-b border-slate-100 py-2"><summary className="cursor-pointer py-2 text-sm font-semibold">备选地点 · {alternatives.length}</summary>{alternatives.map((item, index) => <div key={index} className="flex items-center justify-between gap-2 py-1 text-sm"><span>{item.name}{item.city ? ` · ${item.city}` : ''}</span><button className={action} disabled={busy} onClick={() => void apply({command_type:'ACTIVITY_INSERT', day_index:currentDayIndex+1, position:day.activities.length, name:item.name, category:item.category, city:item.city})}>加入待确认</button></div>)}</details>}
      <details className="py-2"><summary className="cursor-pointer py-2 text-sm font-semibold">住宿</summary><p className="py-2 text-sm text-slate-500">{stay.message}</p>{stay.candidates.map(item => <article key={item.candidate_token} className="my-2 rounded-2xl bg-slate-50 p-3"><h3 className="text-sm font-semibold">{item.name}</h3><p className="mt-1 text-xs text-slate-500">{item.area_or_address}</p><p className="mt-1 text-xs text-slate-600">{item.commute_summary}</p><p className="mt-1 text-xs text-slate-500">{item.reason}</p><button className={action} disabled={busy || item.selected || stay.status === 'NEEDS_UPDATE'} onClick={() => props.onStay(item.candidate_token)}>{item.selected ? '已选择' : '选择这家住宿'}</button></article>)}</details>
      {error && <p role="status" className="mt-2 text-sm text-slate-600">{error}</p>}
    </aside>}
  </div>
}
