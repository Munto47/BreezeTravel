'use client'

import {useEffect, useRef, useState} from 'react'
import {cancelTripSupplement, readTripSupplement, requestTripSupplement, type TripSupplementState} from '@/lib/trip-understanding-v3'

export default function SourceSupplement(props:{resource:string;etag:string;open:boolean;disabled:boolean;onSaved:()=>void}) {
  const [state,setState]=useState<TripSupplementState|null>(null)
  const [busy,setBusy]=useState(false)
  const [message,setMessage]=useState('')
  const [refresh,setRefresh]=useState(0)
  const request=useRef<{etag:string;retry:boolean;key:string}|null>(null)
  const latest=useRef(props);latest.current=props
  const seen=useRef(new Set<string>())
  useEffect(()=>{setState(null);request.current=null;seen.current.clear()},[props.resource])
  useEffect(()=>{
    if(!props.open)return
    const controller=new AbortController()
    let timer:ReturnType<typeof setTimeout>|undefined
    const read=async()=>{
      try {
        const value=await readTripSupplement(props.resource,controller.signal)
        if(controller.signal.aborted)return
        setState(value)
        if(value.status==='QUEUED'||value.status==='RUNNING')timer=setTimeout(()=>void read(),1500)
      } catch {
        if(!controller.signal.aborted){setMessage('补全状态暂时无法读取，请稍后刷新');timer=setTimeout(()=>void read(),4000)}
      }
    }
    void read()
    return()=>{controller.abort();if(timer)clearTimeout(timer)}
  },[props.resource,props.etag,props.open,refresh])
  useEffect(()=>{
    if(state?.status!=='APPLIED'||!state.job_id||seen.current.has(state.job_id)||props.disabled)return
    seen.current.add(state.job_id)
    if(state.result_etag!==props.etag.replace(/^"|"$/g,''))latest.current.onSaved()
  },[state,props.disabled,props.etag])
  async function start(retry:boolean){
    if(busy||props.disabled)return
    setBusy(true);setMessage('')
    if(request.current?.etag!==props.etag||request.current?.retry!==retry)
      request.current={etag:props.etag,retry,key:crypto.randomUUID()}
    try {
      setState(await requestTripSupplement(props.resource,props.etag,retry,request.current.key))
      request.current=null
    } catch(error){setMessage(error instanceof Error?error.message:'补全请求尚未确认，请稍后查看')}
    finally{setBusy(false);setRefresh(value=>value+1)}
  }
  async function stop(){
    if(!state?.job_id||busy)return
    setBusy(true);setMessage('')
    try{setState(await cancelTripSupplement(props.resource,state.job_id))}
    catch(error){setMessage(error instanceof Error?error.message:'停止结果尚未确认，请稍后查看')}
    finally{setBusy(false);setRefresh(value=>value+1)}
  }
  return <section className="inspector-item" aria-label="对照原文补全">
    <div><strong>对照原文补全</strong>
      <p className="e-small e-muted" role="status">{message||state?.message||'正在读取补全状态…'}</p>
      {!!state?.rejected.length&&<p className="e-small e-muted">{state.rejected.length} 项未通过核对，未写入行程；请对照原文检查。</p>}
      {state?.available_actions.includes('REQUEST')&&<button className="e-button" disabled={busy||props.disabled} onClick={()=>void start(false)}>补全遗漏安排</button>}
      {state?.available_actions.includes('RETRY')&&<button className="e-button" disabled={busy||props.disabled} onClick={()=>void start(true)}>基于当前行程再试一次</button>}
      {state?.available_actions.includes('CANCEL')&&<button className="e-button" disabled={busy} onClick={()=>void stop()}>停止本次补全</button>}
    </div>
  </section>
}
