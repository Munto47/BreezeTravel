'use client'

import {useEffect, useRef, useState} from 'react'
import {readTripSource, type TripSourceView} from '@/lib/trip-understanding-v3'
import {tripWaitClock} from '@/lib/trip-wait-clock'

export default function GenerationSource({resource, hasCards}: {resource: string; hasCards: boolean}) {
  const [source, setSource] = useState<TripSourceView | null>(null)
  const [failed, setFailed] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [fromSubmission, setFromSubmission] = useState(false)
  const [collapsed, setCollapsed] = useState(false)
  const panel = useRef<HTMLElement>(null)
  const interacted = useRef(false)
  useEffect(() => {
    if (!resource) return
    const clock = tripWaitClock(resource)
    setFromSubmission(clock.fromSubmission)
    const update = () => setElapsed(Math.max(0, Math.floor((Date.now() - clock.startedAt) / 1000)))
    update()
    const timer = setInterval(update, 1000)
    const controller = new AbortController()
    setSource(null); setFailed(false)
    void readTripSource(resource, controller.signal).then(setSource).catch(() => {
      if (!controller.signal.aborted) setFailed(true)
    })
    return () => {clearInterval(timer); controller.abort()}
  }, [resource])
  useEffect(() => {
    if (hasCards && !interacted.current && !panel.current?.contains(document.activeElement)) setCollapsed(true)
  }, [hasCards])
  const hint = elapsed >= 60 ? '等待时间较长，可后台继续，稍后从我的行程查看。'
    : elapsed >= 20 ? '首批地点尚未返回，可以先查看原文。' : '正在整理地点和先后，完整地点返回后会立即显示。'
  return <section ref={panel} className={`live-source${hasCards ? ' has-cards' : ''}`} aria-label="提交内容与等待状态"
    onFocusCapture={() => {interacted.current = true}} onPointerDown={() => {interacted.current = true}}>
    <div className="live-source-heading"><button type="button" aria-expanded={!collapsed} onClick={() => setCollapsed(value => !value)}>你提交的旅行安排 <span>{collapsed ? '展开原文' : '收起原文'}</span></button>
      <span className="live-elapsed">{fromSubmission ? '已等待' : '本次查看已等待'} {String(Math.floor(elapsed / 60)).padStart(2, '0')}:{String(elapsed % 60).padStart(2, '0')}</span></div>
    {!hasCards && <><div className="live-wait-line" aria-hidden="true"/><p className="live-wait-hint" role="status">{hint}</p></>}
    <div hidden={collapsed}>
      {source?.status === 'AVAILABLE' && source.text ? <><pre className={`live-source-text${expanded ? ' expanded' : ''}`}>{source.text}</pre><button type="button" className="live-source-expand" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? '收起全文' : '展开全文'}</button></>
        : <p className="live-source-unavailable">{source?.status === 'DELETED' ? '原文已删除，整理状态仍可继续查看。' : failed || source ? '原文暂时无法读取，不影响后台整理。' : '正在读取已提交的原文…'}</p>}
      <p className="live-source-help">原文仅供对照 · 地点出现后可展开阅读，完成或停止后可编辑。</p>
    </div>
  </section>
}
