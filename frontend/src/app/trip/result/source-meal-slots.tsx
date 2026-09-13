'use client'

import {useEffect, useRef, useState} from 'react'
import {ChevronDown, ChevronUp, UtensilsCrossed} from 'lucide-react'
import {querySourceMealCandidates, type ActivityCardView, type SourceMealCandidatesView, type UserFacingTripResult} from '@/lib/trip-understanding-v3'
import type {WorkspaceCommandResult} from './itinerary-workspace'
import type {TripUnderstandingCommand} from '@/lib/trip-understanding-v3'
import PendingPlaceDropdown from './pending-place-dropdown'
import {sourceMeals} from './source-meals'
import './source-meal-slots.css'

type Day = UserFacingTripResult['days'][number]
type Props = {day:Day;dayIndex:number;unresolvedActivities?:ActivityCardView[];descriptions?:string[];resource:string;etag?:string;disabled:boolean;onCommand:(command:TripUnderstandingCommand)=>Promise<WorkspaceCommandResult>}
const labels = {BREAKFAST:'早餐',LUNCH:'午餐',DINNER:'晚餐',SNACK:'加餐',UNSPECIFIED:'用餐'}

export default function SourceMealSlots(props:Props) {
  const descriptions = props.descriptions ?? sourceMeals(props.day)
  if (!props.day.meal_slots?.length) return null
  return <section className="source-meal-slots" data-testid={`source-meals-${props.dayIndex}`} aria-label={`Day ${props.dayIndex}原文用餐安排`}>
    <h3>原文用餐安排</h3>
    {props.day.meal_slots.map((slot,index)=><SourceMeal key={index} {...props} slot={slot} index={index} description={descriptions[index]}/>) }
  </section>
}

function SourceMeal({day,dayIndex,unresolvedActivities=[],resource,etag,disabled,onCommand,slot,index,description}:Props & {slot:NonNullable<Day['meal_slots']>[number];index:number;description?:string}) {
  const [expanded,setExpanded]=useState(false), [query,setQuery]=useState(''), [position,setPosition]=useState('')
  const [view,setView]=useState<SourceMealCandidatesView|null>(null), [notice,setNotice]=useState('')
  const [searching,setSearching]=useState(false), [writing,setWriting]=useState(false), [confirming,setConfirming]=useState<ActivityCardView|null>(null)
  const controller=useRef<AbortController|null>(null), writeLock=useRef(false), previous=useRef(etag)
  const current=useRef({resource,etag}); current.current={resource,etag}
  const all=[...day.activities,...unresolvedActivities]
  const anchor=all.find(card=>card.activity_token===(slot.after_activity_token||slot.before_activity_token))
  const selected=all.find(card=>card.activity_token===slot.selected_activity_token)
  const pending=all.find(card=>view?.pending_activity_tokens.includes(card.activity_token))
    || (selected?.status!=='READY'?selected:undefined) || (slot.selection_status!=='SELECTED'?all.find(card=>[slot.after_activity_token,slot.before_activity_token].includes(card.activity_token)&&card.status!=='READY'):undefined)
  const noSourcePosition=!slot.after_activity_token&&!slot.before_activity_token
  const locked=disabled||writing
  useEffect(()=>{
    const previousRequest=controller.current;controller.current=null;previousRequest?.abort();setSearching(false);setView(null);setConfirming(null);setPosition('')
    if(previous.current&&previous.current!==etag)setNotice('行程已更新；原文安排已保留，请重新查找本餐候选。')
    previous.current=etag
    return ()=>controller.current?.abort()
  },[etag,resource])
  function clearQuery(value:string) {const previousRequest=controller.current;controller.current=null;previousRequest?.abort();setSearching(false);setQuery(value);setView(null);setNotice('')}
  async function search() {
    if(locked||searching||!etag||!query.trim()||pending)return
    const request=new AbortController();controller.current?.abort();controller.current=request
    setSearching(true);setView(null);setNotice('')
    const timeout=setTimeout(()=>request.abort(),15000)
    try {
      const [token,side]=position.split('|')
      const result=await querySourceMealCandidates(resource,etag,{meal_slot:{day_index:dayIndex,slot_index:index},query:query.trim(),
        ...(noSourcePosition&&token?{position:{activity_token:token,insert_before:side==='before'}}:{})},request.signal)
      if(!request.signal.aborted&&current.current.resource===resource&&current.current.etag===etag)setView(result)
    } catch(error) {
      if(controller.current===request) setNotice(error instanceof Error&&error.message==='SOURCE_MEAL_VERSION_CHANGED'
        ?'行程已有变化，请刷新行程后重新查找。':error instanceof Error&&error.message==='TRIP_GONE'
          ?'这份行程已不可访问，请从我的行程重新打开。':'餐厅暂时无法查询，原文安排已保留，请稍后重试。')
    } finally {clearTimeout(timeout);if(controller.current===request){controller.current=null;setSearching(false)}}
  }
  async function adopt(token:string) {
    if(locked||writeLock.current||view?.status!=='AVAILABLE'||!view.after_activity_token||!view.candidates.some(c=>c.candidate_token===token))return
    writeLock.current=true;setWriting(true);setNotice('')
    try {
      const result=await onCommand({command_type:'DINING_INSERT',meal_slot:{day_index:dayIndex,slot_index:index},
        after_activity_token:view.after_activity_token,insert_before:view.insert_before,candidate_token:token,
        ...(view.meal_role?{meal_role:view.meal_role}:{})})
      setView(null)
      if(result.status==='RECONCILING')setNotice('保存结果尚未确认，请使用页面的确认保存入口。')
      if(result.status==='SYNCED')setNotice('已读取最新行程，请核对餐厅是否加入；未加入时请重新查找，旧候选不再使用。')
    } catch {setView(null);setNotice('加入结果尚未确认，请先刷新行程核对，不要重复加入。')}
    finally {writeLock.current=false;setWriting(false)}
  }
  return <div className="source-meal-row" data-testid={`source-meal-${dayIndex}-${index}`}>
    <div className="source-meal-top"><p>{description}</p><button type="button" aria-expanded={expanded} className="source-meal-toggle" onClick={()=>setExpanded(v=>!v)}>
      {expanded?'收起':slot.selection_status==='SELECTED'?'查看用餐':`为${labels[slot.meal_role]}选餐厅`}{expanded?<ChevronUp aria-hidden="true"/>:<ChevronDown aria-hidden="true"/>}</button></div>
    {expanded&&<div className="source-meal-editor">
      {slot.selection_status==='SELECTED'&&!pending ? <p>这餐已安排，未重复生成新餐位。可在餐厅卡片调整，撤销可恢复上一安排。</p>:<>
        {pending?<div className="source-meal-confirm"><p>先确认「{pending.name}」，再继续这顿用餐。</p><button type="button" disabled={locked} onClick={()=>setConfirming(pending)}>确认用餐位置：{pending.name}</button>
          {confirming&&<PendingPlaceDropdown card={confirming} resource={resource} disabled={locked} onCommand={onCommand} onClose={()=>setConfirming(null)}/>}</div>:<>
          {noSourcePosition&&<label>原文未指定位置，请明确选择<select value={position} disabled={locked||searching} onChange={event=>{setPosition(event.target.value);setView(null)}}>
            <option value="">请选择加入位置</option>{day.activities.filter(c=>c.status==='READY').flatMap(card=>['before','after'].map(side=><option key={`${card.activity_token}|${side}`} value={`${card.activity_token}|${side}`}>{card.name} {side==='before'?'之前':'之后'}</option>))}
          </select></label>}
          {anchor&&<p className="source-meal-position">按原文位置：在「{anchor.name}」{slot.after_activity_token?'之后':'之前'}</p>}
          <form onSubmit={event=>{event.preventDefault();void search()}}><label>手动搜索词<input value={query} maxLength={40} disabled={locked} placeholder="输入一个店名、菜系或菜品" onChange={event=>clearQuery(event.target.value)}/></label>
            <button type="submit" disabled={locked||searching||!query.trim()||!etag||(noSourcePosition&&!position)}>{searching?'正在查找':'查找本餐门店'}</button></form>
          <p className="source-meal-hint">搜索使用你输入的词，不表示整句偏好、忌口或菜品供应已经核验。</p>
        </>}
      </>}
      {view&&<p role="status">{view.message}</p>}
      {view?.status==='AVAILABLE'&&<div className="source-meal-candidates">{view.candidates.map(candidate=><article key={candidate.candidate_token} data-testid="source-meal-candidate">
        {candidate.dining_info?.photo_url&&<img src={candidate.dining_info.photo_url} alt={`${candidate.name}的供应商照片`} referrerPolicy="no-referrer" onError={event=>{event.currentTarget.hidden=true}}/>}
        <h4><UtensilsCrossed aria-hidden="true"/>{candidate.name}</h4><p>{candidate.area_or_address}</p>
        {candidate.dining_info&&<><p>{[candidate.dining_info.cuisine,candidate.dining_info.rating!=null?`评分 ${candidate.dining_info.rating}/5`:null,candidate.dining_info.cost!=null?`参考人均 ¥${candidate.dining_info.cost}`:null].filter(Boolean).join(' · ')}</p>
          {!!candidate.dining_info.tags.length&&<p>供应商菜品标签：{candidate.dining_info.tags.join('、')}</p>}<small>高德地点资料 · 菜品、营业与价格以门店为准</small></>}
        <p>{candidate.reason}</p><button type="button" disabled={locked} onClick={()=>void adopt(candidate.candidate_token)}>{writing?'正在保存':`选择这家${labels[slot.meal_role]}餐厅`}</button>
      </article>)}</div>}
      {notice&&<p role="status" className="source-meal-notice">{notice}</p>}
    </div>}
  </div>
}
