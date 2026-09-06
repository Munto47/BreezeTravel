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
  const [city,setCity]=useState<''|'北京'|'上海'|'杭州'>(card.city==='北京'||card.city==='上海'||card.city==='杭州'?card.city:'')
  const [items,setItems]=useState<PlaceCandidateView[]>([])
  const [selected,setSelected]=useState<PlaceCandidateView|null>(null)
  const [message,setMessage]=useState('')
  const [searching,setSearching]=useState(false)
  const [saving,setSaving]=useState(false)
  const saveLock=useRef(false)
  const request=useRef<AbortController|null>(null)
  const currentCandidates=useRef<PlaceCandidateView[]>([])
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
  function clearSearch() {
    const previous=request.current
    request.current=null
    previous?.abort()
    currentCandidates.current=[]
    setSearching(false);setItems([]);setSelected(null);setMessage('')
  }
  async function search() {
    if(searching||locked||!query.trim())return
    clearSearch()
    const controller=new AbortController(); request.current=controller
    setSearching(true);setItems([]);setSelected(null);setMessage('')
    const timeout=window.setTimeout(()=>controller.abort(),15000)
    try {
      const result=await queryTripPlaceCandidates(resource,card.activity_token,query.trim(),controller.signal,city||undefined)
      if(controller.signal.aborted||request.current!==controller)return
      const candidates=result.status==='AVAILABLE'&&Array.isArray(result.candidates)?result.candidates.filter(item=>item&&typeof item.candidate_token==='string'&&typeof item.name==='string'&&item.name.trim()):[]
      currentCandidates.current=candidates
      setItems(candidates)
      if(!candidates.length)setMessage(result.status==='EMPTY'?'没找到合适的地点，试试完整名称。':'暂时无法查询，请重试。')
    } catch { if(request.current===controller)setMessage('查询暂不可用，请重试。') }
    finally {clearTimeout(timeout);if(request.current===controller)setSearching(false)}
  }
  async function confirm(candidate:PlaceCandidateView) {
    if(locked||saveLock.current||selected?.candidate_token!==candidate.candidate_token||!currentCandidates.current.includes(candidate))return
    saveLock.current=true
    setSaving(true);setMessage('')
    try {
      const outcome=await onCommand({command_type:'PLACE_CONFIRM',activity_token:card.activity_token,candidate_token:candidate.candidate_token})
      if(outcome.status==='APPLIED')close.current()
      else setMessage(outcome.status==='SYNCED'?'行程已变化，请重新核对地点。':'正在确认保存结果，请稍候。')
    } catch {setMessage('未能确认保存，请稍后重试。')}
    finally {saveLock.current=false;setSaving(false)}
  }
  return <div ref={root} className="pending-place-dropdown" data-testid="pending-place-dropdown" role="region" aria-label={`修改地点 ${card.name}`} onKeyDown={event=>{if(event.key==='Escape'&&!locked){event.stopPropagation();close.current()}}}>
    <div className="pending-place-head"><strong>地点</strong><button type="button" aria-label="收起地点确认" disabled={locked} onClick={onClose}>×</button></div>
    <label style={{display:'grid',gap:4,marginBottom:8,fontSize:13}}>查询城市
      <select aria-label="查询城市" value={city} disabled={locked}
        style={{width:'100%',minHeight:44,padding:'8px',border:'1px solid #d4e9f0',borderRadius:12,background:'#fff',color:'inherit',fontSize:13}}
        onChange={event=>{clearSearch();setCity(event.target.value as typeof city)}}>
        <option value="">跟随行程</option>
        <option value="北京">北京</option><option value="上海">上海</option><option value="杭州">杭州</option>
      </select>
    </label>
    <form onSubmit={event=>{event.preventDefault();void search()}}>
      <input aria-label="搜索地点名称" value={query} maxLength={40} placeholder="地点名称或地址" disabled={locked} onChange={event=>{clearSearch();setQuery(event.target.value)}} />
      <button type="submit" disabled={locked||searching||!query.trim()}>{searching?'查询中…':'搜索'}</button>
    </form>
    <div className="pending-place-options">{items.map(item=>{
      const chosen=selected?.candidate_token===item.candidate_token
      return <div key={item.candidate_token} className="pending-place-row" data-selected={chosen}>
        <button className="pending-place-option" type="button" disabled={locked} aria-pressed={chosen}
          onClick={()=>{if(chosen)void confirm(item);else setSelected(item)}}>
          <strong>{item.name}</strong><small>{item.area_or_address||'地址暂缺'} · {item.category}</small>
        </button>
        {chosen&&<button type="button" className="pending-place-confirm" aria-label="使用这个地点" disabled={locked} onClick={()=>void confirm(item)}>{saving?'保存中…':'确认'}</button>}
      </div>
    })}</div>
    {message&&<p role="status">{message}</p>}
  </div>
}
