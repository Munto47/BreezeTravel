'use client'

import {useEffect, useRef, useState} from 'react'
import type {LodgingRecoveryIntent, TripSupplementaryView, TripUnderstandingCommand, UserFacingTripResult} from '@/lib/trip-understanding-v3'
import type {WorkspaceCommandResult} from './itinerary-workspace'
import PendingPlaceDropdown from './pending-place-dropdown'

type PendingHotel = NonNullable<TripSupplementaryView['pending_lodgings']>[number]

/** The user supplies missing scope; opening never chooses a scope or searches. */
export default function PendingLodgingRecovery({hotel, days, resource, etag, disabled, onCommand, onClose, onInvalidated}: {
  hotel:PendingHotel; days:UserFacingTripResult['days']; resource:string; etag:string; disabled:boolean
  onCommand:(command:TripUnderstandingCommand)=>Promise<WorkspaceCommandResult>; onClose:()=>void;onInvalidated:()=>void
}) {
  const [kind,setKind]=useState<LodgingRecoveryIntent['kind']|''>('')
  const [nights,setNights]=useState<number[]>([])
  const [day,setDay]=useState<number|null>(null)
  const [before,setBefore]=useState<string|null>(null)
  const [positionChosen,setPositionChosen]=useState(false)
  const root=useRef<HTMLDivElement>(null)
  useEffect(()=>{root.current?.querySelector<HTMLSelectElement>('select')?.focus({preventScroll:true})},[])
  let intent:LodgingRecoveryIntent|null=null
  if(kind==='WHOLE_TRIP')intent={kind}
  if(kind==='NIGHTS'&&nights.length)intent={kind,overnight_days:nights}
  if(kind==='VISIT_ONLY'&&day!==null&&positionChosen)intent={kind,day_index:day,before_activity_token:before}
  const selectClass='min-h-11 w-full rounded-xl border border-sky-100 bg-white px-3 text-sm'
  return <div ref={root} data-testid="pending-lodging-recovery" role="region" aria-label={`补全住宿用途 ${hotel.name}`}
    className="relative my-2 rounded-xl border border-sky-100 bg-sky-50 p-3 text-sm"
    onKeyDown={event=>{if(event.key==='Escape'&&!disabled){event.preventDefault();event.stopPropagation();onClose()}}}>
    <div className="flex items-center justify-between gap-2"><strong>{hotel.name}</strong>
      <button type="button" className="e-button e-button-quiet" disabled={disabled} onClick={onClose}>取消补全</button></div>
    <p className="my-2 leading-6 text-slate-600">请补充酒店用途，再搜索具体门店。确认前不会改变行程。</p>
    <label className="grid gap-1">酒店用途
      <select aria-label="酒店用途" className={selectClass} value={kind} disabled={disabled} onChange={event=>{
        setKind(event.target.value as typeof kind);setNights([]);setDay(null);setBefore(null);setPositionChosen(false)
      }}><option value="">请选择用途</option><option value="WHOLE_TRIP" disabled={days.length<2}>全程住宿</option>
        <option value="NIGHTS" disabled={days.length<2}>指定夜晚</option><option value="VISIT_ONLY">仅到访</option></select>
    </label>
    {kind==='WHOLE_TRIP'&&<p className="mt-2 leading-6">用于行程内全部 {Math.max(0,days.length-1)} 晚，独立显示住宿安排。</p>}
    {kind==='NIGHTS'&&<fieldset className="mt-3"><legend>选择住宿夜晚</legend><div className="flex flex-wrap gap-2">
      {days.slice(0,-1).map((item,index)=><label key={index} className="flex min-h-11 items-center gap-2 rounded-lg bg-white px-3">
        <input type="checkbox" disabled={disabled} checked={nights.includes(index+1)} onChange={event=>setNights(current=>event.target.checked
          ? [...current,index+1].sort((a,b)=>a-b):current.filter(n=>n!==index+1))}/>{item.label} 后一晚</label>)}
    </div></fieldset>}
    {kind==='VISIT_ONLY'&&<div className="mt-3 grid gap-3">
      <p className="text-slate-600">仅保留当天到访，不作为住宿，也不推断前一晚入住。</p>
      <label className="grid gap-1">到访日期<select aria-label="到访日期" className={selectClass} value={day??''} disabled={disabled}
        onChange={event=>{setDay(event.target.value?Number(event.target.value):null);setBefore(null);setPositionChosen(false)}}>
        <option value="">请选择日期</option>{days.map((item,index)=><option key={index} value={index+1}>{item.label}</option>)}</select></label>
      {day!==null&&<label className="grid gap-1">到访位置<select aria-label="到访位置" className={selectClass}
        value={positionChosen?(before??'__END__'):''} disabled={disabled} onChange={event=>{
          setPositionChosen(Boolean(event.target.value));setBefore(event.target.value==='__END__'?null:event.target.value||null)
        }}><option value="">请选择位置</option>{days[day-1]?.activities.map(card=><option key={card.activity_token} value={card.activity_token}>在{card.name}之前</option>)}
          <option value="__END__">当天最后</option></select></label>}
    </div>}
    {intent&&<PendingPlaceDropdown key={`${etag}:${JSON.stringify(intent)}`} recovery={{hotel,intent,etag,onInvalidated}} resource={resource}
      disabled={disabled} onCommand={onCommand} onClose={onClose}/>}
  </div>
}
