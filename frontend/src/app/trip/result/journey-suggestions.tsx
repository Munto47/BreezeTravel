'use client'

import {useEffect, useRef, useState} from 'react'
import {createPortal} from 'react-dom'
import SourceDetails from './source-details'
import {Sparkles, X} from 'lucide-react'
import {queryTripDiningCandidates, type DiningCandidatesView, type UserFacingTripResult,
  type TripUnderstandingCommand, type PublicTripChecksView, type PublicTripCheckItem,
  type MapRenderView, type StaySuggestionView, type TripSupplementaryView} from '@/lib/trip-understanding-v3'
import type {WorkspaceCommandResult} from './itinerary-workspace'
import {boundedTripRequest} from './use-trip-experience'
import {findingLabel, needsRecheck} from './presentation'
import StayCandidates from './stay-candidates'
import ItineraryChoices from './itinerary-choices'
import {relativeDayLabel} from './result-presentation'

type Props = {
  resource: string; etag: string; result: UserFacingTripResult; disabled: boolean
  checks: PublicTripChecksView | null; checking: boolean; checksError: string
  map: MapRenderView | null; stay: StaySuggestionView | null
  supplementary?: TripSupplementaryView | null
  onCommand: (command: TripUnderstandingCommand) => Promise<WorkspaceCommandResult>
  onRetry: () => void; onPreview: (item: PublicTripCheckItem) => void
  onLocate: (item: PublicTripCheckItem) => void; onStay: (token: string) => void
  onRefreshStay: () => void
  mapDock?: boolean; selectedToken?: string | null; selectedDayIndex?: number
  alternativesRequest?: {dayIndex: number; trigger: HTMLButtonElement} | null
}

export default function JourneySuggestions(props: Props) {
  const {result, resource, etag, disabled} = props
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<'all'|'dining'|'route'|'stay'>('all')
  const [dock, setDock] = useState<HTMLElement | null>(null)
  useEffect(() => {
    setDock(props.mapDock ? document.getElementById('map-suggestions-slot') : null)
    if (props.mapDock) setOpen(window.matchMedia('(min-width:1024px)').matches)
  }, [props.mapDock])
  useEffect(() => {if (props.mapDock && props.selectedDayIndex != null) {setDayIndex(props.selectedDayIndex); setAnchorIndex(0)}}, [props.mapDock, props.selectedDayIndex])
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
  const unassigned = props.supplementary?.status === 'AVAILABLE'
    ? props.supplementary.days.filter(item => item.day_index === null)
      .flatMap(item => item.items.filter(choice => choice.role === 'OPTIONAL')) : []
  const stay = props.stay || result.stay
  const items = props.checks?.items || []
  const shownItems = tab === 'route' ? items.filter(item => item.depends_on_routes) : items
  const selectedCard = result.days.flatMap(day => day.activities).find(card => card.activity_token === props.selectedToken)

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
    setDayIndex(request.dayIndex); setAnchorIndex(0); setDining(null); setError(''); setTab('all'); setOpen(true)
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
      if (!props.mapDock && !panel.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('keydown', close)
    document.addEventListener('pointerdown', outside)
    return () => {document.removeEventListener('keydown', close); document.removeEventListener('pointerdown', outside)}
  }, [open, props.alternativesRequest, props.mapDock])
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
  const content = open && <aside ref={panel} id="journey-suggestions" aria-label="检查与建议" className={dock ? 'journey-suggestions-docked' : 'fixed right-3 top-24 z-50 max-h-[calc(100dvh-7rem)] w-[min(25rem,calc(100vw-1.5rem))] overflow-y-auto rounded-3xl border border-sky-100 bg-white p-4 shadow-xl'}>
      <div className="flex items-center justify-between"><strong>检查与建议</strong><button type="button" className={action} aria-label="关闭建议" onClick={closePanel}><X className="h-4 w-4"/></button></div>
      <div className="journey-suggestion-tabs" aria-label="建议分类">{([['all','总体检查'],['dining','用餐'],['route','顺路优化'],['stay','住宿']] as const).map(([key,label]) => <button type="button" key={key} aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</button>)}</div>
      {selectedCard && props.mapDock && <section className="journey-selected-place" aria-label="当前选中地点"><p>当前选中 · {relativeDayLabel(props.selectedDayIndex || 0)}</p><strong>{selectedCard.name}</strong><p>{selectedCard.category} · 已确认</p><SourceDetails card={selectedCard}/></section>}
      {(tab === 'all' || tab === 'route') && <details open className="border-b border-slate-100 py-2"><summary className="cursor-pointer py-2 text-sm font-semibold">{tab === 'route' ? '顺路检查' : '行程检查'}</summary>
        <div className="grid gap-3" aria-live="polite">
          {shownItems.map(item => {
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
          {!shownItems.length && <p className="text-sm text-slate-500">{props.checking ? '正在检查…' : props.checksError || (tab === 'route' ? '暂无已核验的顺路调整方案。' : props.checks?.message) || '检查结果暂未就绪。'}</p>}
          <button className={action} disabled={busy || props.checking} onClick={props.onRetry}>重新检查</button>
          {!!items.length && <p className="text-xs leading-5 text-slate-500">{props.checks?.message}</p>}
          <p className="text-xs leading-5 text-slate-500">按已有地点和路线检查；营业与天气尚未核验。</p>
        </div>
      </details>}
      <label className="my-3 flex items-center gap-3 text-sm">哪一天<select aria-label="建议所属日期" className="min-h-11 min-w-0 flex-1 rounded-xl bg-slate-50 px-3" value={currentDayIndex} onChange={event => {setDayIndex(Number(event.target.value)); setAnchorIndex(0); setDining(null); setError('')}}>{result.days.map((item, index) => <option key={index} value={index}>{relativeDayLabel(index)}</option>)}</select></label>
      {tab === 'all' && !!unassigned.length && <details className="border-b border-slate-100 py-2" data-testid="unassigned-alternatives">
        <summary className="cursor-pointer py-2 text-sm font-semibold">未指定日期 · 备选地点 · {unassigned.length}</summary>
        <p className="py-2 text-sm text-slate-500">这些地点尚未排入行程。可在想去的那一天新增地点，确认后再更新路线。</p>
        <ul>{unassigned.map((item, index) => <li key={index} className="py-2 text-sm">{item.name}</li>)}</ul>
      </details>}
      {(tab === 'all' || tab === 'dining') && <details open={tab === 'dining' || undefined} className="border-b border-slate-100 py-2"><summary className="cursor-pointer py-2 text-sm font-semibold">附近餐饮</summary>
        {anchors.length ? <><label className="mt-2 flex items-center gap-2 text-sm">靠近<select aria-label="餐饮附近地点" className="min-h-11 min-w-0 flex-1 rounded-xl bg-slate-50 px-3" value={Math.min(anchorIndex, anchors.length - 1)} onChange={event => {setAnchorIndex(Number(event.target.value)); generation.current++; setSearching(false); setDining(null)}}>{anchors.map((card, index) => <option key={index} value={index}>{card.name}</option>)}</select></label>
          <button type="button" className={action} disabled={busy || searching} onClick={() => void findDining()}>{searching ? '正在查找…' : '找附近餐饮'}</button></> : <p className="py-2 text-sm text-slate-500">先确认当天的一个地点。</p>}
        {dining && <><p className="py-2 text-sm text-slate-500">{stale ? '行程有调整，请重新查询。' : dining.view.message}</p>{!stale && dining.view.candidates.map(item => <article key={item.candidate_token} className="my-2 rounded-2xl bg-slate-50 p-3"><h3 className="text-sm font-semibold">{item.name}</h3><p className="mt-1 text-xs text-slate-500">{item.area_or_address}</p><p className="mt-1 text-xs text-slate-500">{item.reason}</p><button className={action} disabled={busy} onClick={() => void apply({command_type:'DINING_INSERT', after_activity_token:dining.anchor, candidate_token:item.candidate_token})}>加入行程</button></article>)}</>}
      </details>}
      {tab === 'all' && !!alternatives.length && <details ref={alternativesSection} className="border-b border-slate-100 py-2"><summary className="cursor-pointer py-2 text-sm font-semibold">备选地点 · {alternatives.length}</summary>
        <ItineraryChoices key={`${etag}:${currentDayIndex}`} day={day} dayIndex={currentDayIndex} disabled={busy} onApply={apply}/>
      </details>}
      {(tab === 'all' || tab === 'stay') && <details open={tab === 'stay' || undefined} className="py-2"><summary className="cursor-pointer py-2 text-sm font-semibold">住宿</summary><p className="py-2 text-sm text-slate-500">{stay.message}</p><button className={action} disabled={busy || stay.status === 'PREPARING'} onClick={props.onRefreshStay}>{stay.status === 'PREPARING' ? '正在准备住宿…' : '更新住宿建议'}</button><StayCandidates stay={stay} days={result.days} disabled={busy} onSelect={props.onStay}/></details>}
      {error && <p role="status" className="mt-2 text-sm text-slate-600">{error}</p>}
    </aside>
  return <div className="relative">
    <button ref={trigger} type="button" className={action} aria-expanded={open} aria-controls="journey-suggestions"
      onClick={() => {returnFocus.current = trigger.current; pendingAlternativesFocus.current = false; setOpen(value => !value)}} data-testid="journey-suggestions-toggle"><Sparkles className="mr-1 inline h-4 w-4" aria-hidden="true"/>检查与建议</button>
    {dock ? createPortal(content, dock) : content}
  </div>
}
