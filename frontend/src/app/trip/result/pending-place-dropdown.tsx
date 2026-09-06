'use client'

import { useEffect, useRef, useState } from 'react'
import { queryTripPlaceCandidates, type ActivityCardView, type PlaceCandidateView, type TripUnderstandingCommand } from '@/lib/trip-understanding-v3'
import type { WorkspaceCommandResult } from './itinerary-workspace'

/** An anchored, non-modal search. Opening never searches or confirms a place. */
export default function PendingPlaceDropdown({card,resource,disabled,onCommand,onClose}: {
  card:ActivityCardView; resource:string; disabled:boolean
  onCommand:(command:TripUnderstandingCommand)=>Promise<WorkspaceCommandResult>
  onClose:()=>void
}) {
  const [query,setQuery]=useState(card.name==='地点待确认'?'':card.name)
  const [items,setItems]=useState<PlaceCandidateView[]>([])
  const [selected,setSelected]=useState<PlaceCandidateView|null>(null)
  const [message,setMessage]=useState('')
  const [searching,setSearching]=useState(false)
  const [saving,setSaving]=useState(false)
  const saveLock=useRef(false)
  const request=useRef<AbortController|null>(null)
  const root=useRef<HTMLDivElement>(null)
  const close=useRef(onClose); close.current=onClose
  const locked=disabled||saving
  useEffect(()=>{
    root.current?.querySelector('input')?.focus({preventScroll:true})
    return ()=>request.current?.abort()
  },[])
  useEffect(()=>{
    const outside=(event:PointerEvent)=>{
      if(!locked && event.target instanceof Node && !root.current?.parentElement?.contains(event.target)) close.current()
    }
    const escape=(event:KeyboardEvent)=>{if(event.key==='Escape'&&!locked){event.preventDefault();close.current()}}
    document.addEventListener('keydown',escape)
    document.addEventListener('pointerdown',outside)
    return ()=>{document.removeEventListener('pointerdown',outside);document.removeEventListener('keydown',escape)}
  },[locked])
  async function search() {
    if(searching||locked||!query.trim())return
    request.current?.abort()
    const controller=new AbortController(); request.current=controller
    setSearching(true);setItems([]);setSelected(null);setMessage('')
    const timeout=window.setTimeout(()=>controller.abort(),15000)
    try {
      const result=await queryTripPlaceCandidates(resource,card.activity_token,query.trim(),controller.signal)
      if(controller.signal.aborted)return
      const candidates=result.status==='AVAILABLE'&&Array.isArray(result.candidates)?result.candidates.filter(item=>item&&typeof item.candidate_token==='string'&&typeof item.name==='string'&&item.name.trim()):[]
      setItems(candidates)
      if(!candidates.length)setMessage(result.status==='EMPTY'?'没找到合适的地点，试试完整名称。':'暂时无法查询，请重试。')
    } catch { if(request.current===controller)setMessage('查询暂不可用，请重试。') }
    finally {clearTimeout(timeout);if(request.current===controller)setSearching(false)}
  }
  async function confirm() {
    if(!selected||locked||saveLock.current)return
    saveLock.current=true
    setSaving(true);setMessage('')
    try {
      const outcome=await onCommand({command_type:'PLACE_CONFIRM',activity_token:card.activity_token,candidate_token:selected.candidate_token})
      if(outcome.status==='APPLIED')close.current()
      else setMessage(outcome.status==='SYNCED'?'行程已变化，请重新核对地点。':'正在确认保存结果，请稍候。')
    } catch {setMessage('未能确认保存，请稍后重试。')}
    finally {saveLock.current=false;setSaving(false)}
  }
  return <div ref={root} className="pending-place-dropdown" data-testid="pending-place-dropdown" role="region" aria-label={`修改地点 ${card.name}`} onKeyDown={event=>{if(event.key==='Escape'&&!locked){event.stopPropagation();close.current()}}}>
    <div className="pending-place-head"><strong>地点</strong><button type="button" aria-label="收起地点确认" disabled={locked} onClick={onClose}>×</button></div>
    <form onSubmit={event=>{event.preventDefault();void search()}}>
      <input aria-label="搜索地点名称" value={query} maxLength={200} placeholder="地点名称或地址" disabled={locked} onChange={event=>{request.current?.abort();request.current=null;setSearching(false);setQuery(event.target.value);setItems([]);setSelected(null);setMessage('')}} />
      <button type="submit" disabled={locked||searching||!query.trim()}>{searching?'查询中…':'搜索'}</button>
    </form>
    <div className="pending-place-options">{items.map(item=><button key={item.candidate_token} type="button" disabled={locked} aria-pressed={selected?.candidate_token===item.candidate_token} onClick={()=>setSelected(item)}><strong>{item.name}</strong><small>{item.area_or_address||'地址暂缺'} · {item.category}</small></button>)}</div>
    {selected&&<button type="button" className="pending-place-confirm" disabled={locked} onClick={()=>void confirm()}>{saving?'保存中…':'使用这个地点'}</button>}
    {message&&<p role="status">{message}</p>}
  </div>
}
