'use client'

import {useEffect, useId, useRef, useState} from 'react'
import {Check, ChevronDown, ChevronUp, MapPin, RefreshCw, UtensilsCrossed} from 'lucide-react'
import {readDailyDining, type ActivityCardView, type DailyDiningView, type DailyMealView, type TripUnderstandingCommand} from '@/lib/trip-understanding-v3'
import type {WorkspaceCommandResult} from './itinerary-workspace'
import PendingPlaceDropdown from './pending-place-dropdown'
import {diningCandidateReason} from './result-presentation'
import DiningAccessNote, {diningAdoptionBlocked, diningAdoptionLabel} from './dining-access'
import './daily-dining.css'

export function useDailyDining(resource: string | null, etag: string) {
  const [value, setValue] = useState<DailyDiningView | null>(null)
  const [loadedFor, setLoadedFor] = useState('')
  const [generation, setGeneration] = useState(0)
  const [busy, setBusy] = useState(false)
  const locked = useRef(false)
  const pendingRefresh = useRef<{resource:string;etag:string;key:string} | null>(null)
  const refreshController = useRef<AbortController | null>(null)
  const current = useRef({resource, etag}); current.current = {resource, etag}
  useEffect(() => {
    if (!resource || !etag) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    let rounds = 0
    setValue(null)
    setLoadedFor(`${resource}/${etag}`)
    async function poll() {
      try {
        const response = await readDailyDining(resource!, controller.signal)
        if (controller.signal.aborted) return
        if (response.etag !== etag) {setValue({status:'NEEDS_UPDATE', message:'行程已调整，请刷新后更新用餐建议。',days:[]}); return}
        setValue(response.body)
        if (response.body.status === 'PREPARING' && ++rounds < 50) timer = setTimeout(poll, 2000)
        else if (response.body.status === 'PREPARING') setValue({status:'UNAVAILABLE', message:'用餐建议尚未准备好，可稍后更新。', days:[]})
      } catch {if (!controller.signal.aborted) setValue({status:'UNAVAILABLE',message:'用餐建议暂时无法读取。',days:[]})}
    }
    void poll()
    return () => {controller.abort(); clearTimeout(timer); refreshController.current?.abort()}
  }, [resource, etag, generation])
  async function refresh() {
    if (!resource || !etag || locked.current) return
    if (pendingRefresh.current?.resource !== resource || pendingRefresh.current.etag !== etag)
      pendingRefresh.current = {resource,etag,key:crypto.randomUUID()}
    const controller = new AbortController()
    refreshController.current = controller
    locked.current = true; setBusy(true)
    try {
      await readDailyDining(resource, controller.signal, {etag, key:pendingRefresh.current.key})
      pendingRefresh.current = null
      if (current.current.resource === resource && current.current.etag === etag) setGeneration(v => v+1)
    } catch {
      if (current.current.resource === resource && current.current.etag === etag) setValue({status:'UNAVAILABLE',message:'建议更新未确认，可稍后重试。',days:[]})
    } finally {refreshController.current = null; locked.current = false; setBusy(false)}
  }
  // A new public version must not briefly render the previous version's
  // actionable candidates while the effect starts the next read.
  return {value: loadedFor === `${resource}/${etag}` ? value : null, refresh, busy}
}

export function DailyMealCard({day, dayIndex, activities, state, disabled, onRefresh, onCommand, resource, existingActivity}: {
  day?: DailyMealView; state: DailyDiningView | null; disabled: boolean
  dayIndex?: number; activities?: ActivityCardView[]
  resource: string; existingActivity?: ActivityCardView
  onRefresh: () => void; onCommand: (command: TripUnderstandingCommand) => Promise<WorkspaceCommandResult>
}) {
  const [writing, setWriting] = useState(false)
  const [error, setError] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [selectionNeedsReview, setSelectionNeedsReview] = useState(false)
  const contentId = useId()
  const confirmButton = useRef<HTMLButtonElement>(null)
  const current = state?.status === 'AVAILABLE'
  // The server returns current, protected source meals even while other
  // recommendations are preparing or stale. They are not old candidates.
  const pendingMeal = day?.status === 'NEEDS_CONFIRMATION' && !!day.existing_activity_token
  const existingMeal = day?.status === 'EXISTING'
  const recordedMeal = existingActivity || activities?.find(card => card.activity_token === day?.existing_activity_token)
  const recordedMealName = recordedMeal?.meal_role
    ? {BREAKFAST:'早餐', LUNCH:'午餐', DINNER:'晚餐', SNACK:'加餐'}[recordedMeal.meal_role] : null
  const candidates = current && day?.status === 'AVAILABLE' && !selectionNeedsReview ? day.candidates.slice(0, 3) : []
  const canAdopt = current && day?.status === 'AVAILABLE' && !!day.after_activity_token
  const anchor = activities?.find(card => card.activity_token === day?.after_activity_token)
  const position = anchor
    ? `加入「${anchor.name}」${day?.insert_before ? '之前' : '之后'}`
    : day?.next_name ? `前往「${day.next_name}」之前用餐` : ''
  const status = existingMeal ? '已安排' : pendingMeal ? '地点待确认'
    : state?.status === 'NEEDS_UPDATE' || selectionNeedsReview ? '需要更新'
    : !state || state.status === 'PREPARING' ? '准备中'
    : candidates.length ? '待选择' : '暂缺建议'
  useEffect(() => {setConfirming(false)}, [day?.existing_activity_token, day?.status])
  useEffect(() => {setSelectionNeedsReview(false); setError('')}, [day, state])
  function closeConfirmation() {
    setConfirming(false)
    requestAnimationFrame(() => confirmButton.current?.focus({preventScroll: true}))
  }
  const lock = useRef(false)
  const action = 'daily-dining-action'
  async function adopt(token: string) {
    const candidate = candidates.find(item => item.candidate_token === token)
    if (disabled || lock.current || !canAdopt || !day?.after_activity_token || !candidate || diningAdoptionBlocked(candidate, 'LUNCH')) return
    lock.current = true; setWriting(true); setError('')
    try {
      const result = await onCommand({command_type:'DINING_INSERT',after_activity_token:day.after_activity_token,candidate_token:token,
        ...(day.insert_before ? {insert_before:true} : {}), meal_role:'LUNCH'})
      if (result.status === 'RECONCILING') setError('保存结果尚未确认，请使用页面的确认保存入口。')
      if (result.status === 'SYNCED') {
        setSelectionNeedsReview(true)
        setError('已读取最新行程，请核对餐厅是否加入；若未加入，请更新用餐建议后再选。')
      }
    } catch {setError('暂未确认加入结果，请刷新后查看。')}
    finally {lock.current = false; setWriting(false)}
  }
  return <aside className="daily-dining" aria-label={`Day ${dayIndex || day?.day_index || ''}中途用餐建议`} data-testid="daily-meal-card">
    <div className="daily-dining-heading">
      <span className="daily-dining-icon"><UtensilsCrossed aria-hidden="true"/></span>
      <div className="daily-dining-title"><strong>{existingMeal ? recordedMealName ? `${recordedMealName}安排` : '已有用餐' : pendingMeal ? `${recordedMealName || '用餐'}待确认` : day?.meal_role === 'LUNCH' ? '午餐建议' : '中途用餐'}</strong><span>{existingMeal ? recordedMealName ? '当前行程 · 保留已有用餐' : '餐别未指定 · 保留已有用餐' : pendingMeal ? recordedMealName ? '原文地点 · 确认后保留' : '餐别未指定 · 确认后保留' : '系统建议 · 选择后加入'}</span></div>
      <span className={`daily-dining-state${existingMeal ? ' is-selected' : ''}`}>{existingMeal && <Check aria-hidden="true"/>}{status}</span>
      {!!candidates.length && <button type="button" className="daily-dining-toggle" aria-controls={contentId} aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>
        {expanded ? '收起候选' : `查看 ${candidates.length} 家候选`}{expanded ? <ChevronUp aria-hidden="true"/> : <ChevronDown aria-hidden="true"/>}
      </button>}
    </div>
    <p className="daily-dining-message" role="status">{(current || pendingMeal || existingMeal ? day?.message : '') || state?.message || '正在准备中途用餐建议，地点卡片已可使用。'}</p>
    {current && day?.area && <p className="daily-dining-area"><MapPin aria-hidden="true"/>{day.area_relation === 'NEARBY' ? '附近用餐区：' : '用餐区域：'}{day.area}</p>}
    {current && day?.area && day.area_relation === 'NEARBY' && day.area_distance_m != null && <p className="daily-dining-note">距首选饭店直线约{day.area_distance_m}米，步行路线待确认。</p>}
    {!!candidates.length && position && <p className="daily-dining-position">{position}{anchor && !day?.insert_before && day?.next_name ? `，前往「${day.next_name}」之前用餐` : ''}</p>}
    {!!candidates.length && expanded && <div id={contentId} className="daily-dining-candidates">
      {candidates.map(item => <article key={item.candidate_token} className="daily-dining-candidate" data-testid="daily-dining-candidate">
        <div className="daily-dining-candidate-heading"><span className="daily-dining-restaurant-icon"><UtensilsCrossed aria-hidden="true"/></span><div><h3>{item.name}</h3>{item.recommended && item.route_coverage_scope && <span className="daily-dining-recommended">{item.route_coverage_scope==='RETURNED_SEGMENTS'?'局部路段优先比较':'建议优先比较'}</span>}</div></div>
        <p className="daily-dining-address">{item.area_or_address}</p>
        <p className="daily-dining-reason">{diningCandidateReason(item)}</p>
        <DiningAccessNote value={item} showUnknown/>
        <button type="button" className="daily-dining-adopt" disabled={disabled || writing || !canAdopt || diningAdoptionBlocked(item, 'LUNCH')} onClick={() => void adopt(item.candidate_token)}>{writing ? '正在保存' : diningAdoptionLabel(item, '加入行程', 'LUNCH')}</button>
      </article>)}
    </div>}
    {!!candidates.length && !canAdopt && <p className="daily-dining-note">这组建议的加入位置尚未确认，请更新建议后再选择。</p>}
    {pendingMeal && existingActivity && <div className="daily-dining-confirm">
      <button ref={confirmButton} type="button" className={action} disabled={disabled || writing} aria-expanded={confirming} onClick={() => setConfirming(value => !value)}>{recordedMealName ? `确认原文${recordedMealName}` : '确认已有用餐地点'}</button>
      {confirming && <PendingPlaceDropdown card={existingActivity} resource={resource} disabled={disabled || writing} onCommand={onCommand} onClose={closeConfirmation}/>}
    </div>}
    {state && state.status !== 'PREPARING' && !pendingMeal && !existingMeal && <button type="button" className={`${action} daily-dining-refresh`} disabled={disabled || writing} onClick={onRefresh}><RefreshCw aria-hidden="true"/>更新用餐建议</button>}
    {error && <p role="status" className="daily-dining-error">{error}</p>}
  </aside>
}
