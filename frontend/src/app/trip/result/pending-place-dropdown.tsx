'use client'

import {createPortal} from 'react-dom'
import { useEffect, useRef, useState } from 'react'
import { PROVINCES } from '@/data/cities'
import { queryTripPlaceCandidates, queryPendingLodgingCandidates, type ActivityCardView, type LodgingRecoveryIntent, type PlaceCandidateView, type TripSupplementaryView, type TripUnderstandingCommand } from '@/lib/trip-understanding-v3'
import type { WorkspaceCommandResult } from './itinerary-workspace'
import SourceDetails from './source-details'
import DiningAccessNote, {diningAdoptionBlocked, diningAdoptionLabel} from './dining-access'

/** An anchored, non-modal search. Opening never searches or confirms a place. */
type PendingHotel = NonNullable<TripSupplementaryView['pending_lodgings']>[number]
const searchDrafts = new Map<string, {query:string; city:string}>()
type Props = {
  embedded?:boolean; resource:string; disabled:boolean
  onCommand:(command:TripUnderstandingCommand)=>Promise<WorkspaceCommandResult>
  onClose:()=>void
} & ({card:ActivityCardView; recovery?:never} | {card?:never; recovery:{hotel:PendingHotel;intent:LodgingRecoveryIntent;etag:string;onInvalidated:()=>void}})

export default function PendingPlaceDropdown({card,recovery,resource,disabled,onCommand,onClose,embedded=false}: Props) {
  const [slot,setSlot]=useState<HTMLElement|null>(null)
  useEffect(()=>{if(!embedded){setSlot(document.getElementById('inspector-editor-slot'));window.dispatchEvent(new CustomEvent('trip-inspector-open'))}},[embedded])
  const target = recovery?.hotel || card!
  const draftKey = `${resource}:${card?.visit_id || card?.activity_token || recovery?.hotel.pending_token}`
  const [query,setQuery]=useState(searchDrafts.get(draftKey)?.query ?? (target.name==='地点待确认'?'':target.name))
  const [city,setCity]=useState(searchDrafts.get(draftKey)?.city ?? target.city ?? '')
  useEffect(()=>{searchDrafts.set(draftKey,{query,city})},[draftKey,query,city])
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
    if (!recovery) {
      root.current?.querySelector('input')?.focus({preventScroll:true})
      // Keep the focused search above the mobile result navigation and keyboard.
      const element = root.current
      if (element && element.getBoundingClientRect().bottom > (window.visualViewport?.height || window.innerHeight) - 96)
        element.scrollIntoView({block:'center', behavior:'instant'})
    }
    return ()=>request.current?.abort()
  },[])
  useEffect(()=>{
    const outside=(event:PointerEvent)=>{
      // Clicking the itinerary does not discard an in-progress search.
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
      const result=recovery
        ? await queryPendingLodgingCandidates(resource,recovery.hotel.pending_token,query.trim(),recovery.intent,recovery.etag,controller.signal,city||undefined)
        : await queryTripPlaceCandidates(resource,card!.activity_token,query.trim(),controller.signal,city||undefined)
      if(controller.signal.aborted||request.current!==controller)return
      const candidates=result.status==='AVAILABLE'&&Array.isArray(result.candidates)?result.candidates.filter(item=>item&&typeof item.candidate_token==='string'&&typeof item.name==='string'&&item.name.trim()):[]
      currentCandidates.current=candidates
      setItems(candidates)
      if(!candidates.length)setMessage(result.status==='EMPTY'?'没找到合适的地点，试试完整名称。':'暂时无法查询，请重试。')
    } catch (error) {
      if(request.current===controller) {
        if(recovery&&error instanceof Error&&['REVISION_CONFLICT','TRIP_GONE','LOGIN_REQUIRED'].includes(error.message))recovery.onInvalidated()
        else if(recovery&&error instanceof Error&&error.message==='CITY_REQUIRED'){
          setMessage('请先填写这家酒店所在的城市，再搜索。')
          root.current?.querySelector<HTMLSelectElement>('select[aria-label="查询城市"]')?.focus({preventScroll:true})
        }
        else setMessage('查询暂不可用，请重试。')
      }
    }
    finally {clearTimeout(timeout);if(request.current===controller)setSearching(false)}
  }
  async function confirm(candidate:PlaceCandidateView) {
    if(locked||saveLock.current||selected?.candidate_token!==candidate.candidate_token||!currentCandidates.current.includes(candidate))return
    if(card && diningAdoptionBlocked(candidate, card.meal_role))return
    saveLock.current=true
    setSaving(true);setMessage('')
    try {
      const outcome=await onCommand(recovery
        ? {command_type:'LODGING_RECOVER',pending_token:recovery.hotel.pending_token,candidate_token:candidate.candidate_token,intent:recovery.intent}
        : {command_type:'PLACE_CONFIRM',activity_token:card!.activity_token,candidate_token:candidate.candidate_token})
      if(outcome.status==='APPLIED'){searchDrafts.delete(draftKey);close.current()}
      else {clearSearch();setMessage(outcome.status==='SYNCED'?'行程已变化，请重新核对地点。':'正在确认保存结果，请稍候。')}
    } catch {setMessage('未能确认保存，请稍后重试。')}
    finally {saveLock.current=false;setSaving(false)}
  }
  const content = <div ref={root} className="pending-place-dropdown" data-testid="pending-place-dropdown" role="region" aria-label={`修改地点 ${target.name}`} onKeyDown={event=>{if(event.key==='Escape'&&!locked){event.stopPropagation();close.current()}}}>
    <div className="pending-place-head"><strong>地点</strong><button type="button" aria-label="收起地点确认" disabled={locked} onClick={onClose}>×</button></div>
    <p className="inspector-background">收起后保留搜索草稿；确认才会修改行程。</p>
    {card && <SourceDetails card={card} />}
    {card && <DiningAccessNote value={card} showUnknown={card.category === '餐饮'}/>}
    <label style={{display:'grid',gap:4,marginBottom:8,fontSize:13}}>查询城市
      <select aria-label="查询城市" value={city} disabled={locked}
        style={{width:'100%',minHeight:44,padding:'8px',border:'1px solid #d4e9f0',borderRadius:12,background:'#fff',color:'inherit',fontSize:13}}
        onChange={event=>{clearSearch();setCity(event.target.value as typeof city)}}>
        <option value="">跟随行程</option>
        {Array.from(new Set([...(target.city?[target.city]:[]),...PROVINCES.flatMap(province=>province.cities)])).map(name=><option key={name} value={name}>{name}</option>)}
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
          onClick={()=>{if(chosen&&!recovery)void confirm(item);else setSelected(item)}}>
          <strong>{item.name}</strong><small>{item.area_or_address||'地址暂缺'} · {item.category}</small>
        </button>
        <DiningAccessNote value={item} showUnknown={item.category === '餐饮'}/>
        {chosen && card && !card.meal_role && item.meal_evidence_status === 'LIGHT_FOOD_ITEMS_ONLY' && <p>保留为用途未指定的餐饮地点，不会替代正餐安排。</p>}
        {chosen&&<button type="button" className="pending-place-confirm" aria-label={recovery?'确认保存酒店和用途':diningAdoptionLabel(item, '使用这个地点', card?.meal_role)} disabled={locked || (!!card && diningAdoptionBlocked(item, card.meal_role))} onClick={()=>void confirm(item)}>{saving?'保存中…':recovery?'确认保存':diningAdoptionLabel(item, '确认', card?.meal_role)}</button>}
      </div>
    })}</div>
    {message&&<p role="status">{message}</p>}
  </div>
  return slot ? createPortal(content,slot) : content
}
