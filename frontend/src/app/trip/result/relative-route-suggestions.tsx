'use client'

import {useEffect, useRef, useState} from 'react'
import {ArrowRight, Footprints, BusFront} from 'lucide-react'
import {compareRelativeTripRoutes, createTripRequestKey, type PublicComparedRouteEdge, type PublicRelativeRouteOptions} from '@/lib/trip-understanding-v3'
import './relative-route-suggestions.css'

type Props = {resource:string;etag:string;dayIndex:number;disabled:boolean;onAdopt:(token:string,basisEtag:string)=>Promise<boolean>}
const distance = (meters:number) => meters >= 1000 ? `${Math.round(meters/100)/10} 公里` : `${meters} 米`

function Edges({title, edges}:{title:string;edges:PublicComparedRouteEdge[]}) {
  return <div className="relative-route-edges"><strong>{title}</strong><ul>{edges.map((edge,index)=><li key={index}>
    <span>{edge.from_name} <ArrowRight aria-hidden="true"/> {edge.to_name}</span>
    <small>{edge.mode==='walking'?<Footprints aria-hidden="true"/>:<BusFront aria-hidden="true"/>}{edge.mode==='walking'?'步行':'公共交通'} · {edge.duration_minutes} 分钟 · {distance(edge.distance_meters)}</small>
  </li>)}</ul></div>
}

export default function RelativeRouteSuggestions({resource,etag,dayIndex,disabled,onAdopt}:Props) {
  const [view,setView]=useState<{body:PublicRelativeRouteOptions;etag:string}|null>(null)
  const [comparing,setComparing]=useState(false), [saving,setSaving]=useState(false), [notice,setNotice]=useState(''), [selected,setSelected]=useState<string|null>(null)
  const request=useRef<AbortController|null>(null), key=useRef<string|null>(null), saveLock=useRef(false)
  const current=useRef({resource,etag,dayIndex});current.current={resource,etag,dayIndex}
  useEffect(()=>{
    const previous=request.current;request.current=null;previous?.abort();key.current=null
    setComparing(false);setView(null);setSelected(null);setNotice('')
    return ()=>{const previous=request.current;request.current=null;previous?.abort()}
  },[resource,etag,dayIndex])
  const busy=disabled||saving
  async function compare() {
    if(busy||request.current||!etag)return
    const controller=new AbortController();request.current=controller
    const requestKey=key.current||(key.current=createTripRequestKey())
    setComparing(true);setNotice('');setSelected(null);setView(null)
    const timeout=setTimeout(()=>controller.abort(),30000)
    try {
      const body=await compareRelativeTripRoutes(resource,etag,dayIndex,requestKey,controller.signal)
      if(request.current===controller&&!controller.signal.aborted&&current.current.resource===resource&&current.current.etag===etag&&current.current.dayIndex===dayIndex){
        setView({body,etag});key.current=null
      }
    } catch(error) {
      if(request.current===controller)setNotice(error instanceof Error&&error.message==='ROUTE_COMPARISON_STALE'
        ?'行程或路线已有变化，请刷新页面后重新比较。':error instanceof Error&&error.message==='ROUTE_COMPARISON_PENDING'
        ?'同一次比较仍在处理中，请稍后重试；行程没有改动。':error instanceof Error&&error.message==='TRIP_GONE'
        ?'这份行程已不可访问，请从我的行程重新打开。':error instanceof Error&&error.message==='LOGIN_REQUIRED'
        ?'登录已过期，请重新登录后再比较。':'暂时无法完成比较，行程没有改动。可以稍后重试。')
    } finally {clearTimeout(timeout);if(request.current===controller){request.current=null;setComparing(false)}}
  }
  async function adopt() {
    if(busy||saveLock.current||!selected||!view||view.etag!==etag||view.body.status!=='AVAILABLE'||!view.body.options.some(option=>option.change_token===selected))return
    saveLock.current=true;setSaving(true);setNotice('')
    try {
      const applied=await onAdopt(selected,view.etag)
      setView(null);setSelected(null)
      setNotice(applied?'调整已保存；地图仍需手动更新。':'本次调整尚未确认，请查看页面的保存提示。旧方案不再使用。')
    } catch {setView(null);setSelected(null);setNotice('保存结果尚未确认，请使用页面上的确认保存入口。')}
    finally {saveLock.current=false;setSaving(false)}
  }
  const options=view?.etag===etag&&view.body.status==='AVAILABLE'?view.body.options:[]
  return <section className="relative-route-suggestions" data-testid="relative-route-suggestions" aria-label={`Day ${dayIndex}顺路方案`}>
    <h3>Day {dayIndex} · 比较顺路方案</h3>
    <p>按已有地点和已核验路线比较顺序。比较不会改动行程，确认后才保存。</p>
    <button type="button" className="relative-route-compare" disabled={busy||comparing||!etag} onClick={()=>void compare()}>{comparing?'正在比较路线…':'比较当天顺路方案'}</button>
    {view&&<p role="status">{view.body.message}</p>}
    {options.map((option,index)=><article key={option.change_token} data-testid="relative-route-option">
      <h4>方案 {index+1} · {option.title}</h4>
      <p>{option.summary}</p>
      <div className="relative-route-savings"><strong>变化路段可节省 {option.minutes_saved} 分钟</strong>
        <span>{option.duration_minutes_before} → {option.duration_minutes_after} 分钟</span>
        <span>{distance(option.distance_meters_before)} → {distance(option.distance_meters_after)}</span>
      </div>
      <p className="relative-route-scope">只合计发生变化的路段，不是全天交通总时长。</p>
      <div className="relative-route-order" aria-label="完整日序比较"><div><strong>当前顺序</strong><ol>{option.before.map((name,i)=><li key={i}>{name}</li>)}</ol></div>
        <div><strong>调整后</strong><ol>{option.after.map((name,i)=><li key={i}>{name}</li>)}</ol></div></div>
      <details><summary>查看变化路段</summary><Edges title="当前路段" edges={option.routes_before}/><Edges title="调整后路段" edges={option.routes_after}/></details>
      {selected===option.change_token?<div className="relative-route-confirm" role="group" aria-label="确认顺序调整">
        <p>确认按上方顺序调整 Day {dayIndex}？保存后可撤销；地图需手动更新。</p>
        <div><button type="button" className="relative-route-adopt" disabled={busy} onClick={()=>void adopt()}>{saving?'正在保存…':'确认调整顺序'}</button>
          <button type="button" disabled={busy} onClick={()=>setSelected(null)}>取消</button></div>
      </div>:<button type="button" className="relative-route-adopt" disabled={busy} onClick={()=>setSelected(option.change_token)}>选择这个方案</button>}
    </article>)}
    {notice&&<p role="status" className="relative-route-notice">{notice}</p>}
  </section>
}
