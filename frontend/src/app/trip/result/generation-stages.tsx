'use client'

import { useEffect, useState } from 'react'
import { Check, LoaderCircle, Circle, MapPin, Sparkles, Navigation } from 'lucide-react'
import type { TripUnderstandingProgressMetrics } from '@/lib/trip-understanding-v3'

const TIPS = [
  ['先排顺序，再慢慢完善', '不用填几点出发。先确定想去的地方，之后可以随时调整先后。'],
  ['看见“待确认”也没关系', '同名地点可能不止一个，展开卡片核对地址，再选你想去的那个。'],
  ['把想去的地方放在一起', '卡片可以拖到其他日期；松手前的空位就是它的新位置。'],
  ['路线由你决定何时更新', '改完行程后点击“更新步行路线”，就能查看新的路段与耗时。'],
]

/** Only authoritative progress advances a step; animation never fabricates progress. */
export default function GenerationStages({ phase, progress, complete = false }: {
  phase: 'RECEIVED' | 'CARDS_AVAILABLE' | 'CHECKING_PLACES'
  progress: TripUnderstandingProgressMetrics
  complete?: boolean
}) {
  const [tip, setTip] = useState(0)
  const [reading, setReading] = useState(false)
  useEffect(() => {
    if (reading || complete || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    const timer=window.setInterval(()=>setTip(index=>(index+1)%TIPS.length),9000)
    return ()=>window.clearInterval(timer)
  }, [reading, complete])
  const current = complete ? 4 : phase === 'RECEIVED' ? 1 : phase === 'CARDS_AVAILABLE' ? 2
    : progress.places_total > 0 && progress.places_checked >= progress.places_total ? 3 : 2
  const labels = ['收到攻略', '整理每天安排', '核对地点', '生成完毕']
  return (
    <div className="fluid-generation" data-phase={current}>
      <div className="fluid-generation-scene" aria-hidden="true">
        <div className="fluid-orbit fluid-orbit-one" /><div className="fluid-orbit fluid-orbit-two" />
        {[0,1,2].map(index => <div key={index} className={`fluid-paper fluid-paper-${index}`}>
          <div className="fluid-paper-picture"><MapPin /></div><i /><i /><i />
          <span className="fluid-paper-seal"><Check /></span>
        </div>)}
        <div className="fluid-scan-beam" />
        <span className="fluid-travel-spark"><Sparkles /></span><span className="fluid-travel-plane"><Navigation /></span>
        {[0,1,2,3,4].map(index => <span key={index} className={`fluid-particle fluid-particle-${index}`} />)}
      </div>
    <ol className="soft-generation-stages" aria-label="整理进度" aria-live="polite" data-testid="generation-stages">
      {labels.map((label, index) => {
        const done = index < current
        const active = index === current
        const Icon = done ? Check : active ? LoaderCircle : Circle
        return <li key={label} data-state={done ? 'done' : active ? 'active' : 'pending'} aria-current={active ? 'step' : undefined}>
          <span className="soft-stage-dot"><Icon aria-hidden="true" className={active ? 'animate-spin motion-reduce:animate-none' : ''} /></span>
          <span>{index === 3 && active ? '收尾中' : label}</span>
          {index === 2 && active && progress.places_total > 0 && <small>{progress.places_checked}/{progress.places_total}</small>}
        </li>
      })}
    </ol>
    <aside className="generation-reading" data-testid="generation-reading" aria-label="旅途小提示" onMouseEnter={()=>setReading(true)} onMouseLeave={event=>setReading(event.currentTarget.contains(document.activeElement))} onFocus={()=>setReading(true)} onBlur={(event)=>{if(!event.currentTarget.contains(event.relatedTarget))setReading(event.currentTarget.matches(':hover'))}}>
      <div key={tip} className="generation-reading-copy"><small>旅途小提示</small><h2>{TIPS[tip][0]}</h2><p>{TIPS[tip][1]}</p></div>
      <div className="generation-tip-pages" aria-label="选择提示">{TIPS.map((item,index)=><button key={item[0]} type="button" aria-label={`提示 ${index+1}：${item[0]}`} aria-pressed={tip===index} onClick={()=>setTip(index)}><span /></button>)}</div>
    </aside>
    </div>
  )
}
