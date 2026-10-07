'use client'

import Link from 'next/link'
import {useLayoutEffect, useRef, useState} from 'react'
import {ArrowDown, ArrowLeft, Check, ChevronDown, MapPin, Square, Wifi} from 'lucide-react'
import type {TripUnderstandingProgressMetrics, TripUnderstandingProgressView, UserFacingTripResult} from '@/lib/trip-understanding-v3'
import './generation-workspace.css'
import GenerationSource from './generation-source'
import PlacePhoto, {PlacePhotoProvider} from './place-photo'
import {useCompactScreen} from './mobile-ui'

export type GenerationReading = {dayIndex: number; token: string | null; offset: number; expanded: string[]; following: boolean}
export const initialGenerationReading: GenerationReading = {dayIndex: 0, token: null, offset: 0, expanded: [], following: true}

export default function GenerationWorkspace({resource, phase, progress, snapshot, streamState, reading, onReadingChange, notice, cancelling, onStop, onResume}: {
  resource: string
  phase: TripUnderstandingProgressView['phase']
  progress: TripUnderstandingProgressMetrics
  snapshot: UserFacingTripResult | null
  streamState: 'SYNCING' | 'STREAMING' | 'POLLING' | 'PAUSED'
  reading: GenerationReading
  onReadingChange: (change: Partial<GenerationReading>) => void
  notice: string
  cancelling: boolean
  onStop: () => void
  onResume: () => void
}) {
  const feed = useRef<HTMLDivElement>(null)
  const mobile = useCompactScreen()
  const previous = useRef(new Set<string>())
  const readingRef = useRef(reading)
  readingRef.current = reading
  const [unseen, setUnseen] = useState(0)
  const days = snapshot?.days || []
  const cards = days.flatMap(day => day.activities)
  const confirmed = cards.filter(card => card.status === 'READY').length
  const verifying = cards.filter(card => card.status !== 'READY' && card.verification_pending).length
  const unresolved = cards.length - confirmed - verifying
  const understanding = progress.semantic_complete === false || (progress.semantic_complete == null && phase === 'RECEIVED')
  const heading = !cards.length ? '正在阅读你的旅行安排' : understanding ? '行程正在逐步形成' : '正在完成地点核验'
  const connection = {SYNCING: '正在连接进度', STREAMING: snapshot ? '已收到新内容' : '进度连接已建立', POLLING: '正在重连，已生成内容保留', PAUSED: '连接暂时中断，已生成内容保留'}[streamState]

  useLayoutEffect(() => {
    const container = feed.current
    if (!container) return
    const tokens = new Set((snapshot?.days || []).flatMap(day => day.activities.map(card => card.visit_id || card.activity_token)))
    const added = [...tokens].filter(token => !previous.current.has(token)).length
    previous.current = tokens
    if (readingRef.current.following) {
      container.scrollTop = container.scrollHeight
      setUnseen(0)
    } else {
      if (added) setUnseen(value => value + added)
      const anchor = [...container.querySelectorAll<HTMLElement>('[data-generation-token]')].find(el => el.dataset.generationToken === readingRef.current.token)
      if (anchor) container.scrollTop += anchor.getBoundingClientRect().top - container.getBoundingClientRect().top - readingRef.current.offset
    }
  }, [snapshot])

  function rememberPosition() {
    const container = feed.current
    if (!container) return
    const top = container.getBoundingClientRect().top
    const focused = document.activeElement?.closest<HTMLElement>('[data-generation-token]')
    const focusedVisible = focused && container.contains(focused) && focused.getBoundingClientRect().bottom > top && focused.getBoundingClientRect().top < container.getBoundingClientRect().bottom
    const anchor = focusedVisible ? focused : [...container.querySelectorAll<HTMLElement>('[data-generation-token]')].find(el => el.getBoundingClientRect().bottom > top + 12)
    const following = container.scrollHeight - container.clientHeight - container.scrollTop < 64
    if (following) setUnseen(0)
    onReadingChange({following, ...(anchor ? {token: anchor.dataset.generationToken!, dayIndex: Number(anchor.dataset.dayIndex), offset: anchor.getBoundingClientRect().top - top} : {})})
  }
  function chooseDay(index: number) {
    const container = feed.current
    const day = container?.querySelector<HTMLElement>(`[data-live-day="${index}"]`)
    if (!day || !container) return
    onReadingChange({dayIndex: index, token: null, following: false})
    container.scrollTop += day.getBoundingClientRect().top - container.getBoundingClientRect().top
    day.querySelector<HTMLElement>('h2')?.focus({preventScroll: true})
  }
  function follow() {
    onReadingChange({following: true})
    setUnseen(0)
    if (feed.current) feed.current.scrollTop = feed.current.scrollHeight
  }
  return <PlacePhotoProvider days={days}><section className="live-generation" data-testid="generation-workspace">
    <div className="live-topline"><Link href="/" className="live-back"><ArrowLeft size={16}/>返回首页</Link><span className="live-connection" role="status"><Wifi size={14}/>{connection}</span></div>
    <header className="live-heading"><div><span className="live-eyebrow">你的行程 · 实时整理</span><h1>{heading}</h1><p>地点一出现就可以展开阅读，确认结果会更新在同一张卡片上。</p></div><div className="live-actions"><Link href="/my-trips">后台继续</Link><button type="button" disabled={cancelling} onClick={onStop}><Square size={13}/>{cancelling ? '正在停止…' : '停止整理'}</button></div></header>
    {!!cards.length && <div className="live-statusbar" role="status">
      <span><b>{cards.length}</b> 已识别主线地点</span><span className="is-confirmed"><b>{confirmed}</b> 已确认</span><span><b>{verifying}</b> 核验中</span><span><b>{unresolved}</b> 待确认</span>
      <span className="live-stage">{understanding ? '正在理解全文' : '全文理解已结束'} · {verifying ? '同时核验地点' : '继续整理安排'}</span>
    </div>}
    {notice && <p className="live-notice" role="alert">{notice}</p>}
    {streamState === 'PAUSED' && <button type="button" className="four-secondary" onClick={onResume}>重新连接进度</button>}
    <details className="live-mobile-details" open={mobile ? undefined : true}>
      <summary>查看整理详情与原文</summary>
      <GenerationSource key={resource} resource={resource} hasCards={!!cards.length}/>
    </details>
    <div className={`live-layout${cards.length || days.some(day => day.alternatives?.length || day.source_notes?.length) ? '' : ' is-empty'}`}>
      {!!cards.length && <nav className="live-days" aria-label="浏览逐日预览"><span>行程目录</span>{days.map((day, index) => <button key={day.label} type="button" aria-current={reading.dayIndex === index ? 'true' : undefined} onClick={() => chooseDay(index)}>Day {index + 1}<small>{day.activities.length} 个地点</small></button>)}<p>可展开阅读；完成或停止后开放编辑。</p></nav>}
      <div className="live-feed-wrap">
        <div className="live-feed" ref={feed} onScroll={rememberPosition} role="region" aria-label="逐日整理预览" tabIndex={0}>
          {days.map((day, index) => <section className="live-day" data-live-day={index} key={day.label}>
            <header><h2 tabIndex={-1}>Day {index + 1}</h2><span>{[...new Set([...day.activities, ...(day.alternatives || [])].map(card => card.city).filter(Boolean))].join(' · ')}</span><small>{day.activities.length} 个主线地点</small></header>
            <div className="live-card-grid">{day.activities.map((card, position) => {
              const token = card.visit_id || card.activity_token
              const ready = card.status === 'READY'
              const state = card.semantic_review === 'PENDING' ? '安排待复核' : ready ? '已确认' : card.verification_pending ? '核验中' : '需要确认'
              const expanded = reading.expanded.includes(token)
              return <article className="live-place" key={token} data-generation-token={token} data-day-index={index} data-state={ready ? 'ready' : card.verification_pending ? 'checking' : 'unresolved'}>
                <div className="live-place-cover"><MapPin size={28} aria-hidden="true"/><PlacePhoto card={card}/><span className="live-place-number">{position + 1}</span><span className="live-place-state">{ready && <Check size={12}/>}<span key={state}>{state}</span></span></div>
                <button type="button" className="live-place-toggle" aria-expanded={expanded} onClick={event => {
                  const article = event.currentTarget.closest<HTMLElement>('[data-generation-token]')!
                  onReadingChange({dayIndex: index, token, offset: article.getBoundingClientRect().top - feed.current!.getBoundingClientRect().top, following: false, expanded: expanded ? reading.expanded.filter(id => id !== token) : [...reading.expanded, token]})
                }}><span><strong>{card.name}</strong><small>{card.category || '地点'}{card.source_details?.length ? ` · 内部安排 ${card.source_details.length} 项` : ''}</small></span><ChevronDown size={16}/></button>
                {expanded && <div className="live-place-detail"><p>{ready ? card.area_or_address : card.semantic_review === 'PENDING' ? '根据修改后的原文重新整理中，日序与安排仍待复核。' : '地点身份尚未确认，确认后会在这里更新。'}</p>{!!card.source_details?.length && <><strong>原文内部安排</strong><ol>{card.source_details.map((detail, n) => <li key={`${detail.name}-${n}`}>{detail.name}{detail.optional ? '（备选）' : ''}</li>)}</ol><small>内部安排未单独核验。</small></>}</div>}
              </article>
            })}</div>
            {!day.activities.length && <p className="live-day-empty">这一天的主线地点仍在整理。</p>}
            {!!day.alternatives?.length && <details className="live-alternatives"><summary>备选安排 · {day.alternatives.length} <span>未加入主线</span></summary><ul>{day.alternatives.map((item, n) => <li key={item.activity_token || `${item.name}-${n}`}><strong>{item.name}</strong>{item.semantic_review === 'PENDING' && <span> · 安排待复核</span>}{item.replaces_name && <span> · {item.replacement_condition || "条件满足时"}替换{item.replaces_name}</span>}{!!item.source_details?.length && <span> · {item.source_details.map(detail => detail.name).join('、')}</span>}</li>)}</ul></details>}
            {!!day.source_notes?.length && <ul className="live-notes" aria-label="原文说明">{day.source_notes.map(note => <li key={note.note_id}>{note.text}</li>)}</ul>}
            {!!day.unprocessed_count && <p className="live-unresolved">还有 {day.unprocessed_count} 项原文尚未整理，完成后可继续补全。</p>}
          </section>)}
          {!!days.length && <p className="live-feed-end">{understanding ? '继续整理后续安排…' : '正在保存与核对结果…'} · 交通路线尚未完成检查</p>}
        </div>
        {!reading.following && <button type="button" className="live-follow" onClick={follow}><ArrowDown size={15}/>{unseen ? `新增 ${unseen} 个地点，查看新增内容` : '跟随最新内容'}</button>}
      </div>
    </div>
  </section></PlacePhotoProvider>
}
