'use client'

import Link from 'next/link'
import { ArrowLeft, Check, Clock3, MapPin, Search, Square } from 'lucide-react'
import type { TripUnderstandingProgressMetrics, TripUnderstandingProgressView, UserFacingTripResult } from '@/lib/trip-understanding-v3'
import GenerationStages from './generation-stages'
import './generation-workspace.css'

export default function GenerationWorkspace({ phase, progress, snapshot, pendingDays, notice, cancelling, onStop }: {
  phase: TripUnderstandingProgressView['phase']
  progress: TripUnderstandingProgressMetrics
  snapshot: UserFacingTripResult | null
  pendingDays: UserFacingTripResult['days']
  notice: string
  cancelling: boolean
  onStop: () => void
}) {
  const checking = phase !== 'RECEIVED'
  const understanding = progress.semantic_complete === false
  const knownTotal = progress.places_total_final !== false
  const finishing = !understanding && knownTotal && checking && progress.places_total > 0 && progress.places_checked >= progress.places_total
  const heading = finishing ? '正在保存逐日行程' : understanding && checking ? '正在整理后续安排，同时确认地点' : checking ? '正在核验每一个地点' : '正在整理每天的地点与顺序'
  const confirmed = snapshot?.days.reduce((count, day) => count + day.activities.filter(card => card.status === 'READY').length, 0) || 0
  return <section className="four-generation" aria-busy="true" data-testid="generation-workspace">
    <Link href="/" className="four-back-link"><ArrowLeft size={16} aria-hidden="true"/>返回首页</Link>
    <div className="four-generation-heading">
      <h1>正在为你整理专属行程</h1>
      <p>保留每天想去的地方，核对地点身份，生成清晰的行程卡片。</p>
    </div>
    <GenerationStages phase={phase} progress={progress} />
    <div className="four-generation-columns">
      <section className="four-generation-detail" aria-label="当前处理状态">
        <div className="four-generation-current">
          <div className="four-generation-art" aria-hidden="true"><span/><MapPin/><Search/></div>
          <div><span className="four-processing-label">正在处理…</span><h2>{heading}</h2>
            <p>{checking ? '地点核验结果会逐步显示在右侧。尚未确认的地点会保留补全入口。' : '区分主线、备选和原文安排；保留相对日序，不自动改变你的计划。'}</p>
          </div>
        </div>
        <div className="four-progress-count" role="status">
          {checking && progress.places_total > 0 ? <>{knownTotal && <progress max={progress.places_total} value={progress.places_checked} aria-label="地点核验进度"/>}<span>已核验 {progress.places_checked}{knownTotal ? ` / ${progress.places_total}` : ''} 项{!knownTotal && '，后续内容仍在整理'}</span></> : <span>正在整理原文，收到核验结果后显示实际进度。</span>}
        </div>
        <ol className="four-processing-log">
          <li data-state="done"><Check aria-hidden="true"/><div><strong>已接收你的攻略文字</strong><p>离开页面后，任务仍会继续。</p></div></li>
          <li data-state={checking && !understanding ? 'done' : 'active'}><Check aria-hidden="true"/><div><strong>{checking && !understanding ? '已整理逐日安排' : '正在整理逐日安排'}</strong><p>{progress.day_count > 0 ? `目前已有 ${progress.day_count} 天的安排；地点和备选会按原文分别保留。` : '正在读取地点、日序和原文安排。'}</p></div></li>
          <li data-state={finishing ? 'done' : checking ? 'active' : 'waiting'}><MapPin aria-hidden="true"/><div><strong>核验地点身份</strong><p>{checking && progress.places_total > 0 ? `已核验 ${progress.places_checked} 项，确认 ${confirmed} 个主线地点。` : '收到逐日安排后开始核验。'}</p></div></li>
          <li data-state={finishing ? 'active' : 'waiting'}><Check aria-hidden="true"/><div><strong>生成行程卡片</strong><p>卡片生成后，地图与餐宿建议分别准备。</p></div></li>
        </ol>
        <div className="four-generation-actions"><button type="button" className="four-secondary" disabled={cancelling} onClick={onStop}><Square size={14} aria-hidden="true"/>{cancelling ? '正在停止…' : '停止整理'}</button><Link href="/my-trips" className="four-secondary"><Clock3 size={16} aria-hidden="true"/>后台继续</Link></div>
        {notice && <p className="four-generation-notice" role="alert">{notice}</p>}
        <p className="four-background-note">稍后可在“我的行程”重新打开；匿名行程仅在当前浏览器凭据有效时可找回。</p>
      </section>
      <aside className="four-generation-preview" aria-label="逐日整理预览">
        <header><h2><MapPin size={21} aria-hidden="true"/>逐日预览</h2><span>实际结果</span></header>
        <p className="four-preview-status">{confirmed ? `已确认 ${confirmed} 个主线地点` : '核验结果尚未返回'}{progress.day_count > 0 ? ` · ${progress.day_count} 天` : ''}</p>
        {snapshot?.days.length ? snapshot.days.map((day, index) => {
          const pending = pendingDays[index]?.activities || []
          const cities = [...new Set([...day.activities, ...pending, ...(day.alternatives || [])].map(card => card.city).filter(Boolean))]
          return <section className="four-preview-trip-day" key={day.label}>
            <header><h3>Day {index + 1}</h3>{cities.length > 0 && <span>{cities.join(' · ')}</span>}<small>{day.activities.length} 个已确认地点</small></header>
            {day.activities.length ? <ol aria-label={`Day ${index + 1} 已确认地点`}>{day.activities.map((card, cardIndex) => <li key={card.activity_token}><b>{cardIndex + 1}</b><details><summary>{card.name} · 已确认</summary><p>{card.area_or_address}</p>{!!card.source_details?.length && <ul aria-label="内部安排">{card.source_details.map((detail, n) => <li key={`${detail.name}-${n}`}>{detail.name}{detail.optional ? '（可选）' : ''}</li>)}</ul>}</details></li>)}</ol> : <p>当天尚无已确认的主线地点。</p>}
            {pending.length > 0 && <div className="four-preview-pending-places" aria-label={`Day ${index + 1} 待确认地点`}><strong>待确认 · {pending.length}</strong><ul>{pending.map(card => <li key={card.activity_token}>{card.name} · {card.verification_pending ? '正在确认地点' : '需要确认'}</li>)}</ul></div>}
            {!!day.alternatives?.length && <div className="four-preview-alternatives"><strong>备选</strong>{day.alternatives.map((item, n) => <span key={`${item.name}-${n}`}>{item.name}</span>)}</div>}
            {!!day.unprocessed_count && <p className="four-preview-pending">还有 {day.unprocessed_count} 项尚未整理。</p>}
            {!!day.source_notes?.length && <ul aria-label="原文说明">{day.source_notes.map(note => <li key={note.note_id}>{note.text}</li>)}</ul>}
          </section>
        }) : <div className="four-preview-awaiting"><MapPin size={40} aria-hidden="true"/><h3>每天的安排会逐步出现在这里</h3><p>保留主线、备选和未完成内容；不展示猜测的地点。</p></div>}
        <p className="four-background-note">这是处理中预览，部分内容仍在核验；交通路线尚未完成检查。</p>
      </aside>
    </div>
  </section>
}
