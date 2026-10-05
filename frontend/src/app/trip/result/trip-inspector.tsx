'use client'

import {useEffect, useRef, useState, type ReactNode} from 'react'
import {ArrowLeft, ChevronRight, Sparkles, X} from 'lucide-react'
import JourneySuggestions, {type JourneySuggestionProps} from './journey-suggestions'
import PendingPlaceDropdown from './pending-place-dropdown'
import SourceRestoreEditor from './source-restore-editor'
import SourceSupplement from './source-supplement'
import {readTripInspector, type InspectorIssue} from '@/lib/trip-understanding-v3'
import {readTripSource, type TripSourceView, type ActivityCardView, type PublicTripCheckItem, type UserFacingTripResult} from '@/lib/trip-understanding-v3'

type Item = {id: string; title: string; description: string; day?: number; kind: 'place'|'source'|'check'|'meal'|'alternative'|'lodging'; card?: ActivityCardView; check?: PublicTripCheckItem; optional?:boolean}
export default function TripInspector(props: JourneySuggestionProps & {
  unresolvedDays: UserFacingTripResult['days']; recovery: ReactNode; suspended?: boolean; onSourceAdd:(day:number)=>void
  onFocusTarget:(day:number,visit?:string)=>void
  onSupplementSaved:()=>void
}) {
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<'pending'|'optional'|'handled'>('pending')
  const [activeIssueId, setActiveIssueId] = useState<string|null>(null)
  const [sourceDay, setSourceDay] = useState<number|null>(null)
  const [feedback, setFeedback] = useState('')
  const [source, setSource] = useState<TripSourceView|null>(null)
  const [dayFilter, setDayFilter] = useState<number|null>(null)
  const [targetToken, setTargetToken] = useState<string|null>(null)
  const [serverIssues,setServerIssues]=useState<{input_version:string;issues:InspectorIssue[];changes:Array<{change_etag:string;title:string}>}|null>(null)
  useEffect(()=>{
    const controller=new AbortController()
    void readTripInspector(props.resource,controller.signal).then(setServerIssues).catch(()=>{})
    return ()=>controller.abort()
  },[props.resource,props.etag,props.checks,props.map?.status])
  const list = useRef<HTMLDivElement>(null)
  const returnFocus = useRef<HTMLElement|null>(null)
  const scroll = useRef(0)
  let items: Item[] = props.unresolvedDays.flatMap((day, index) => day.activities.map(card => ({
    id: `place:${card.visit_id || card.activity_token}`, title: card.name,
    description: card.semantic_review === 'PENDING' ? '安排待复核 · 对照修改后的原文' : '未找到有效地点 · 补充名称或重新搜索',
    kind: card.semantic_review === 'PENDING' ? 'source' as const : 'place' as const, day:index, card,
  })))
  props.result.days.forEach((day, index) => {
    if (day.unprocessed_count) items.push({id:`source:${day.day_id || index}`, title:`Day ${index+1} · 原文待整理`,
      description:'对照原文，补充尚未加入的安排', kind:'source', day:index})
  })
  const pendingTokens = new Set(items.flatMap(item => item.card ? [item.card.activity_token] : []))
  const checks = [...new Map((props.checks?.all_items || props.checks?.items || []).map(check => [check.issue_id || check.check_token, check])).values()]
  checks.filter(check => check.basis_status !== 'NEEDS_RECHECK' &&
    !(check.depends_on_routes && check.affected_activity_tokens?.some(token => pendingTokens.has(token))))
    .forEach(check => items.push({id:check.issue_id || check.check_token, title:check.title, description:check.message, kind:'check', check}))
  if(serverIssues?.input_version===props.etag.replace(/^"|"$/g,'')) items=serverIssues.issues.map(issue=>({
    id:issue.issue_id,title:issue.title,description:issue.message,day:issue.day_index ?? undefined,
    kind:issue.kind.toLowerCase() as Item['kind'],optional:issue.category==='OPTIONAL',check:issue.check || undefined,
    card:['PLACE','SOURCE'].includes(issue.kind)?props.unresolvedDays.flatMap(day=>day.activities).find(card=>issue.target_visit_ids.includes(card.visit_id || '')):undefined,
  }))
  const dispositions = props.result.issue_dispositions || {}
  const handled = Object.entries(dispositions).filter(([id, value]) => {
    const item = items.find(item=>item.id===id)
    return !item?.check || item.check.input_fingerprint === value.input_fingerprint
  })
  const handledIds = new Set(handled.map(([id])=>id))
  const excluded = props.supplementary?.status === 'AVAILABLE'
    ? props.supplementary.days.flatMap(day => day.items.filter(item => item.role === 'EXCLUDED')
      .map(item => ({name: item.name, day: day.day_index}))) : []
  const pending = items.filter(item => !handledIds.has(item.id) && !item.optional && item.check?.label !== '可以更好')
  const optional = items.filter(item => !handledIds.has(item.id) && (item.optional || item.check?.label === '可以更好'))
  const active = items.find(item => targetToken ? item.card?.activity_token === targetToken || item.card?.visit_id === targetToken : item.id === activeIssueId)
  const updating = props.map?.status === 'PREPARING' || props.checking
  useEffect(()=>{
    if((active?.kind!=='source' && sourceDay==null)||source)return
    const controller=new AbortController()
    void readTripSource(props.resource,controller.signal).then(setSource).catch(()=>{if(!controller.signal.aborted)setFeedback('原文暂时无法读取，请稍后重试。')})
    return ()=>controller.abort()
  },[active?.kind,sourceDay,source,props.resource])
  useEffect(() => {
    const show = (event: Event) => {
      const detail = (event as CustomEvent<{day?:number; token?:string}>).detail
      setSourceDay(null); setActiveIssueId(null); setDayFilter(detail?.day ?? null)
      setTargetToken(detail?.token ?? null); setTab('pending')
      setOpen(true)
    }
    window.addEventListener('trip-inspector-open', show)
    return () => window.removeEventListener('trip-inspector-open', show)
  }, [])
  useEffect(() => {if(props.alternativesRequest){setTab('optional');setOpen(true)}},[props.alternativesRequest])
  function back() {
    setActiveIssueId(null); setTargetToken(null); setSourceDay(null)
    requestAnimationFrame(() => {if(list.current)list.current.scrollTop=scroll.current;returnFocus.current?.focus({preventScroll:true})})
  }
  function select(item: Item, trigger: HTMLElement) {
    returnFocus.current=trigger;scroll.current=list.current?.scrollTop || 0
    setTargetToken(null); setActiveIssueId(item.id)
    if(item.check) props.onLocate(item.check)
    else if(item.day != null) {
      props.onFocusTarget(item.day,item.card?.visit_id || item.card?.activity_token)
    }
  }
  return <>
    <button className="e-button" data-testid="journey-suggestions-toggle" aria-expanded={open}
      onClick={()=>{setDayFilter(null);setOpen(value=>!value)}}><Sparkles size={17}/>检查与建议{pending.length>0 && <span className="inspector-count">{pending.length}</span>}</button>
    <aside id="trip-inspector" className="trip-inspector" hidden={!open || props.suspended} aria-label="检查与建议">
      <header><div><Sparkles size={19}/><h2>检查与建议</h2></div><button aria-label="关闭检查侧栏" onClick={()=>setOpen(false)}><X size={18}/></button></header>
      <p className="inspector-background" role="status">{updating?'正在后台更新 · 已保存的修改可继续查看':'行程修改自动保存'}</p>
      {feedback&&<p className="inspector-background" role="status">{feedback}</p>}
      <nav aria-label="事项分类">{([['pending','待确认',pending.length],['optional','可选建议',optional.length],['handled','已处理',handled.length + excluded.length]] as const).map(([key,label,count])=><button key={key} aria-pressed={tab===key} onClick={()=>{back();setDayFilter(null);setTab(key)}}>{label}{count>0&&<small>{count}</small>}</button>)}</nav>
      <div ref={list} className="inspector-list" hidden={Boolean(active) || sourceDay != null}>
        {tab==='pending'&&<SourceSupplement resource={props.resource} etag={props.etag} open={open} disabled={props.disabled} onSaved={props.onSupplementSaved}/>}
        {tab==='pending'&&(props.result.correction_suggestions||[]).map(suggestion=>{
          const day=props.result.days.findIndex(day=>day.activities.some(card=>card.visit_id===suggestion.target_visit_id))
          const card=props.result.days[day]?.activities.find(card=>card.visit_id===suggestion.target_visit_id)
          return <article className="inspector-item" key={suggestion.suggestion_id}><span><strong>原文名称待核对</strong><small>原文提到“{suggestion.source_name}”；当前“{card?.name||'安排'}”已保留，请核对后自行修改。</small></span>{card&&<button className="e-button" onClick={()=>props.onFocusTarget(day,card.visit_id)}>查看安排</button>}</article>
        })}
        {dayFilter != null && <p className="inspector-background">Day {dayFilter+1} 的事项 <button className="e-button" onClick={()=>setDayFilter(null)}>查看全部</button></p>}
        {(tab==='pending'?pending:tab==='optional'?optional:[]).filter(item=>dayFilter == null || item.day === dayFilter).map(item=><button key={item.id} data-issue-id={item.id} className="inspector-item" onClick={event=>select(item,event.currentTarget)}>
          <span><strong>{item.title}</strong><small>{item.day != null?`Day ${item.day+1} · `:''}{item.description}</small></span><ChevronRight size={16}/>
        </button>)}
        {tab==='pending'&&!pending.length&&<p className="inspector-empty">{props.checking?'正在检查行程…':'暂无需要你决定的事项。'}</p>}
        <div hidden={tab!=='optional'}><JourneySuggestions {...props} embedded mapDock={false}/></div>
        {tab==='handled'&&!handled.length&&!excluded.length&&<p className="inspector-empty">暂无已处理事项。</p>}
        {tab==='handled'&&!!excluded.length&&<section aria-label="原文已取消的安排">
          <h3>原文已取消的安排</h3>
          <p className="e-small e-muted">这些安排已在原文中排除，保留供核对，不计入路线。</p>
          {excluded.map((item,index)=><article className="inspector-item" key={`${item.day}:${index}`}>
            <span><strong>{item.name}</strong><small>{item.day == null ? '未指定日期' : `Day ${item.day}`} · 原文已取消</small></span>
          </article>)}
        </section>}
        {tab==='handled'&&!!serverIssues?.changes?.length&&<details open><summary>修改记录</summary>
          <p className="e-small e-muted">撤销指定修改会保留后续无关内容；同一内容有新修改时会提示冲突。</p>
          {serverIssues.changes.map((change,index)=><article className="inspector-item" key={change.change_etag}><span><strong>{change.title}</strong><small>{index===0?'最近一次修改':`之前第 ${index} 次修改`}</small></span><button className="e-button" disabled={props.disabled} onClick={async()=>{
            const response=await props.onCommand({command_type:'UNDO',change_etag:change.change_etag})
            setFeedback(response.status==='APPLIED'?'已撤销这次修改，其他内容保留。':'这次修改尚未撤销，请核对是否存在后续编辑冲突。')
          }}>撤销此项</button></article>)}</details>}
        {tab==='handled'&&handled.map(([id,value])=><article key={id} className="inspector-item"><span><strong>{value.title}</strong><small>已忽略 · 检查结论未改变</small></span><button className="e-button" disabled={props.disabled} onClick={async()=>{
          const response=await props.onCommand({command_type:'ISSUE_DISPOSITION',issue_id:id,disposition:'OPEN'})
          setFeedback(response.status==='APPLIED'?'已重新打开事项':'保存结果尚未确认，请核对后重试。')
        }}>重新打开</button></article>)}
      </div>
      <div className="inspector-detail" hidden={!active && sourceDay == null}>
        <button className="inspector-back" onClick={back}><ArrowLeft size={16}/>返回列表</button>
        {active && <><h3>{active.title}</h3><p>{active.description}</p></>}
        {active?.card&&active.kind==='place'&&<PendingPlaceDropdown embedded card={active.card} resource={props.resource} disabled={props.disabled} onCommand={props.onCommand} onClose={back}/>}
        {active && ['meal','alternative'].includes(active.kind)&&<JourneySuggestions {...props} embedded initialTab={active.kind==='meal'?'dining':'all'} initialDay={active.day} mapDock={false}/>}
        {active?.kind==='lodging'&&props.recovery}
        {(active?.kind==='source'||sourceDay!=null)&&<>
          <p>原文会一直保留；补充安排后仍可对照查看。</p>
          <details><summary>查看原始行程</summary>{source?.text?<pre className="inspector-source">{source.text}</pre>:<p>{source?.status==='DELETED'?'原文已删除':'正在读取原文…'}</p>}</details>
          {source?.status==='AVAILABLE'&&<SourceRestoreEditor source={source} result={props.result} day={active?.day ?? sourceDay ?? 0} resource={props.resource} onCommand={props.onCommand} disabled={props.disabled} onSaved={()=>setSource(null)}/>}
        </>}
        {active?.check&&<><details><summary>依据与影响范围</summary><p>{active.check.message}</p><p>{active.check.affected_days.join('、')}</p></details>
          {active.check.can_preview&&<button className="e-button e-button-primary" disabled={props.disabled} onClick={()=>props.onPreview(active.check!)}>预览调整</button>}
          {!!active.check.issue_id&&<button className="e-button" disabled={props.disabled} onClick={async()=>{
            const response=await props.onCommand({command_type:'ISSUE_DISPOSITION',issue_id:active.check!.issue_id!,disposition:'IGNORED'})
            setFeedback(response.status==='APPLIED'?'已忽略，检查结论保留。可在已处理或撤销中恢复。':'保存结果尚未确认，请核对后重试。')
            if(response.status==='APPLIED')back()
          }}>保留安排，忽略此项</button>}</>}
      </div>
    </aside>
    <div id="inspector-editor-slot"/>
  </>
}
