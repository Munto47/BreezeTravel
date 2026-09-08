'use client'

import {useEffect, useRef, useState} from 'react'
import {UtensilsCrossed} from 'lucide-react'
import {readDailyDining, type ActivityCardView, type DailyDiningView, type DailyMealView, type TripUnderstandingCommand} from '@/lib/trip-understanding-v3'
import type {WorkspaceCommandResult} from './itinerary-workspace'
import PendingPlaceDropdown from './pending-place-dropdown'

export function useDailyDining(resource: string | null, etag: string) {
  const [value, setValue] = useState<DailyDiningView | null>(null)
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
  return {value, refresh, busy}
}

export function DailyMealCard({day, state, disabled, onRefresh, onCommand, resource, existingActivity}: {
  day?: DailyMealView; state: DailyDiningView | null; disabled: boolean
  resource: string; existingActivity?: ActivityCardView
  onRefresh: () => void; onCommand: (command: TripUnderstandingCommand) => Promise<WorkspaceCommandResult>
}) {
  const [writing, setWriting] = useState(false)
  const [error, setError] = useState('')
  const [confirming, setConfirming] = useState(false)
  const confirmButton = useRef<HTMLButtonElement>(null)
  const pendingMeal = day?.status === 'NEEDS_CONFIRMATION' && !!day.existing_activity_token
  useEffect(() => {setConfirming(false)}, [day?.existing_activity_token, day?.status])
  function closeConfirmation() {
    setConfirming(false)
    requestAnimationFrame(() => confirmButton.current?.focus({preventScroll: true}))
  }
  const lock = useRef(false)
  const action = 'min-h-11 rounded-xl px-3 text-sm font-medium text-sky-800 hover:bg-white disabled:opacity-40 focus-visible:ring-2 focus-visible:ring-sky-500'
  async function adopt(token: string) {
    if (disabled || lock.current || !day?.after_activity_token) return
    lock.current = true; setWriting(true); setError('')
    try {
      const result = await onCommand({command_type:'DINING_INSERT',after_activity_token:day.after_activity_token,candidate_token:token,
        ...(day.insert_before ? {insert_before:true} : {}), meal_role:'LUNCH'})
      if (result.status === 'RECONCILING') setError('保存结果尚未确认，请使用页面的确认保存入口。')
    } catch {setError('暂未确认加入结果，请刷新后查看。')}
    finally {lock.current = false; setWriting(false)}
  }
  return <aside className="mt-4 rounded-2xl border border-sky-100 bg-sky-50/60 px-4 py-3" aria-label={`${day?.label || ''}中途用餐建议`} data-testid="daily-meal-card">
    <div className="flex flex-wrap items-center gap-2"><UtensilsCrossed className="h-4 w-4 text-sky-700" aria-hidden="true"/><strong className="text-sm text-sky-900">{day?.meal_role === 'LUNCH' ? '午餐建议' : '中途用餐'}</strong>{day?.area && <span className="text-xs text-slate-600">{day.area_relation === 'NEARBY' ? '附近用餐区：' : '用餐区域：'}{day.area}</span>}</div>
    {day?.area && day.area_relation === 'NEARBY' && day.area_distance_m != null && <p className="mt-1 text-xs text-slate-500">距首选饭店直线约{day.area_distance_m}米，步行路线待确认。</p>}
    <p className="mt-1 text-xs text-slate-600">{day?.message || state?.message || '正在准备中途用餐建议，地点卡片已可使用。'}</p>
    {day?.next_name && <p className="mt-1 text-xs text-slate-500">前往{day.next_name}之前用餐</p>}
    {!pendingMeal && day?.candidates.map((item,index) => {
      const card = <div className="flex flex-wrap items-center justify-between gap-2 py-2"><div className="min-w-0"><p className="text-sm font-medium">{item.name}{index===0 && <span className="ml-2 text-xs text-sky-700">建议</span>}</p><p className="text-xs text-slate-500">{item.area_or_address}</p><p className="text-xs text-slate-500">{item.reason}</p></div><button className={action} disabled={disabled || writing || state?.status !== 'AVAILABLE'} onClick={() => void adopt(item.candidate_token)}>加入行程</button></div>
      return index===0 ? <div key={item.candidate_token}>{card}</div> : <details key={item.candidate_token}><summary className="min-h-11 cursor-pointer py-3 text-xs text-sky-800">备选 · {item.name}</summary>{card}</details>
    })}
    {pendingMeal && existingActivity && <div className="relative">
      <button ref={confirmButton} type="button" className={action} disabled={disabled || writing} aria-expanded={confirming} onClick={() => setConfirming(value => !value)}>确认原文午餐</button>
      {confirming && <PendingPlaceDropdown card={existingActivity} resource={resource} disabled={disabled || writing} onCommand={onCommand} onClose={closeConfirmation}/>}
    </div>}
    {state && state.status !== 'PREPARING' && !pendingMeal && (!day || ['UNAVAILABLE','EMPTY','NEEDS_CONFIRMATION'].includes(day.status)) && <button className={action} disabled={disabled || writing} onClick={onRefresh}>更新用餐建议</button>}
    {error && <p role="status" className="text-xs text-slate-600">{error}</p>}
  </aside>
}
