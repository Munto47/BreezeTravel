'use client'
import {useRef,useState} from 'react'
import {queryTripPlaceCandidates,readTripUnderstandingResult,type TripSourceView,type UserFacingTripResult,type TripUnderstandingCommand} from '@/lib/trip-understanding-v3'
import type {WorkspaceCommandResult} from './itinerary-workspace'

export default function SourceRestoreEditor({source,day,result,resource,onCommand,onSaved,disabled}:{
  source:TripSourceView;day:number;result:UserFacingTripResult;resource:string;disabled:boolean
  onCommand:(command:TripUnderstandingCommand)=>Promise<WorkspaceCommandResult>;onSaved:()=>void
}) {
  const [fragmentId,setFragmentId]=useState('')
  const [kind,setKind]=useState<'NOTE'|'PLACE'>('NOTE')
  const [name,setName]=useState('')
  const [position,setPosition]=useState(result.days[day]?.activities.length || 0)
  const [busy,setBusy]=useState(false)
  const [message,setMessage]=useState('')
  const lock=useRef(false), command=useRef(onCommand);command.current=onCommand
  const fragments=(source.fragments || []).filter(fragment=>fragment.day_index==null||fragment.day_index===day+1)
  const fragment=fragments.find(fragment=>fragment.fragment_id===fragmentId)
  async function save(){
    if(!fragment||fragment.restored||disabled||lock.current)return
    lock.current=true;setBusy(true);setMessage('')
    try{
      const outcome=await command.current({command_type:'SOURCE_RESTORE',source_token:fragment.source_token,kind,day_index:day+1,position,...(kind==='PLACE'?{name:name.trim()}:{})})
      if(outcome.status!=='APPLIED'){setMessage('保存结果尚未确认，请先核对当前行程。');return}
      setMessage('原文已恢复，可撤销。')
      if(kind==='PLACE'){
        const current=await readTripUnderstandingResult(resource)
        const card=current.status===200?(current.body as UserFacingTripResult).days.flatMap(day=>day.activities).find(card=>card.source_fragment_id===fragment.fragment_id):null
        if(card){
          const candidates=await queryTripPlaceCandidates(resource,card.activity_token,name.trim())
          const first=candidates.candidates?.[0]
          if(first){await command.current({command_type:'PLACE_CONFIRM',activity_token:card.activity_token,candidate_token:first.candidate_token})}
          else setMessage(candidates.status==='EMPTY'?'安排已保存；未找到有效地点，可稍后继续确认。':'安排已保存；地点服务暂不可用，可稍后重试。')
        }
      }
      onSaved()
    }catch{setMessage('请核对保存结果；已保存的安排会保留，地点查询可以重试。')}
    finally{lock.current=false;setBusy(false)}
  }
  return <section className="e-editor">
    <label className="e-field">要恢复的原文<select aria-label="要恢复的原文" value={fragmentId} onChange={event=>setFragmentId(event.target.value)} disabled={busy||disabled}>
      <option value="">选择原文片段</option>{fragments.map(fragment=><option key={fragment.fragment_id} value={fragment.fragment_id} disabled={fragment.restored}>{fragment.restored?'已恢复 · ':''}{fragment.text.slice(0,55)}</option>)}
    </select></label>
    {fragment&&<><blockquote className="inspector-source">{fragment.text}</blockquote>
      <label className="e-field">恢复方式<select value={kind} onChange={event=>setKind(event.target.value as typeof kind)} disabled={busy||disabled}><option value="NOTE">保留为备注</option><option value="PLACE">加入地点安排</option></select></label>
      {kind==='PLACE'&&<label className="e-field">核对地点名称<input aria-label="恢复地点名称" value={name} maxLength={40} disabled={busy||disabled} onChange={event=>setName(event.target.value)}/></label>}
      <label className="e-field">插入位置<select value={position} disabled={busy||disabled} onChange={event=>setPosition(Number(event.target.value))}>{Array.from({length:(result.days[day]?.activities.length || 0)+1},(_,index)=><option key={index} value={index}>{index===0?'当天开头':`第 ${index} 站之后`}</option>)}</select></label>
      <p className="e-small e-muted">将{kind==='NOTE'?'作为原文备注保留，不更改路线':'加入当天行程并自动匹配地点，更新相邻路线'}。</p>
      <button className="e-button e-button-primary" disabled={busy||disabled||fragment.restored||(kind==='PLACE'&&!name.trim())} onClick={()=>void save()}>{busy?'正在保存…':kind==='NOTE'?'保留为备注':'加入行程'}</button>
    </>}{message&&<p role="status">{message}</p>}
  </section>
}
