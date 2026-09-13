'use client'

import { useEffect, useRef, useState } from 'react'
import { Download, Image as ImageIcon, X } from 'lucide-react'

import type { MapRenderView, TripSupplementaryView, UserFacingTripResult } from '@/lib/trip-understanding-v3'
import type {SourceLodgingCard} from '@/lib/confirmed-trip-view'
import AccessibleDialog from './accessible-dialog'
import { serpentineLayout, serpentineEdge } from './serpentine-layout'
import { DAY_COLORS, transportConnectorFor, connectorPresentation, relativeDayLabel } from './result-presentation'
import {sourceMeals} from './source-meals'
import {diningAccessLines} from './dining-access'

function exportStatus(result: UserFacingTripResult, unresolvedDays: UserFacingTripResult['days'] = []) {
  // The page passes the confirmed-only mainline. Coverage counts named places;
  // older results can also contain anonymous pending cards, retained separately.
  const pending = Math.max(result.coverage?.unresolved_place_count || 0,
    result.days.reduce((sum, day) => sum + day.activities.filter(card => card.status !== 'READY').length, 0),
    unresolvedDays.reduce((sum, day) => sum + day.activities.length, 0))
  const messages: string[] = []
  if ((result.coverage?.unprocessed_count || 0) > 0 ||
    (result.coverage?.unclassified_mention_count || 0) > 0 ||
    (pending === 0 && (result.coverage?.complete === false || result.status === 'PARTIAL_RESULT'))) {
    messages.push('原文尚未完整整理，请返回行程补全')
  }
  if (pending > 0) messages.push(`待确认地点：${pending} 处，请返回行程确认`)
  return messages
}

function dayExportStatus(day: UserFacingTripResult['days'][number], unresolvedDay?: UserFacingTripResult['days'][number]) {
  const messages: string[] = []
  if (day.unprocessed_count) messages.push(`原文未整理：${day.unprocessed_count} 处`)
  const pending = Math.max(day.activities.filter(card => card.status !== 'READY').length, unresolvedDay?.activities.length || 0)
  if (pending) messages.push(`待确认地点：${pending} 处`)
  if (day.alternatives?.length) messages.push(`备选与方案：${day.alternatives.length} 处`)
  if (!day.activities.length) messages.push('尚无主线地点')
  return messages.join(' · ')
}

function routeSummary(
  mapView: MapRenderView | null,
  day: UserFacingTripResult['days'][number],
  fromIndex: number,
) {
  if (!mapView) return '交通待确认'
  const connector=transportConnectorFor(day,day.activities[fromIndex],day.activities[fromIndex+1],mapView)
  const {label,warning}=connectorPresentation(connector)
  return `${label}${warning?`\n${warning}`:''}`

}

function fitText(
  context: CanvasRenderingContext2D,
  text: string,
  maxWidth: number,
) {
  if (context.measureText(text).width <= maxWidth) return text
  let output = text
  while (output.length > 1 && context.measureText(`${output}…`).width > maxWidth)
    output = output.slice(0, -1)
  return `${output}…`
}

function wrapText(context: CanvasRenderingContext2D, text: string, maxWidth: number) {
  const lines: string[] = []
  let line = ''
  for (const character of text) {
    if (line && context.measureText(line + character).width > maxWidth) {
      lines.push(line)
      line = ''
    }
    line += character
  }
  if (line) lines.push(line)
  return lines
}

async function renderItinerary(
  result: UserFacingTripResult,
  mapView: MapRenderView | null,
  unresolvedDays: UserFacingTripResult['days'],
  sourceMealDescriptions?: string[][],
  supplementary?: TripSupplementaryView | null,
  sourceLodgings: SourceLodgingCard[] = [],
) {
  if ('fonts' in document) await document.fonts.ready
  const leftWidth = 140
  const padding = 36
  const width = 1440
  const statusMessages = exportStatus(result, unresolvedDays)
  const dayMessages = result.days.map((day, index) => dayExportStatus(day, unresolvedDays[index]))
  const headerHeight = 188 + statusMessages.length * 24
  const canvas = document.createElement('canvas')
  canvas.width = width
  const context = canvas.getContext('2d')
  if (!context) throw new Error('CANVAS_UNAVAILABLE')
  const nameLineHeight = 21
  const layoutWidth = width - padding * 2 - leftWidth - 16
  context.font = '700 15px "Microsoft YaHei", sans-serif'
  const dayNames = result.days.map(day => {
    const layout = serpentineLayout(layoutWidth, day.activities.length)
    return day.activities.map(card => wrapText(context, card.name, layout.cardWidth - 32))
  })
  const dayLayouts = result.days.map((day, index) => {
    const lines = Math.max(1, ...dayNames[index].map(name => name.length))
    // Name starts at y=59; keep its status badge and bottom padding below
    // every line. The shared layout moves later rows and connectors with it.
    const minimumCardHeight = 119 + (lines - 1) * nameLineHeight
    const base = serpentineLayout(layoutWidth, day.activities.length, minimumCardHeight)
    context.font = '400 11px "Microsoft YaHei", sans-serif'
    const turnHeights = day.activities.slice(0, -1).flatMap((_, fromIndex) => {
      if (!serpentineEdge(base, fromIndex).turn) return []
      const routeLines = routeSummary(mapView, day, fromIndex).split('\n').flatMap(line => wrapText(context, line, 96))
      return routeLines.length * 14 + 8
    })
    return serpentineLayout(layoutWidth, day.activities.length, minimumCardHeight, Math.max(0, ...turnHeights) + 8)
  })
  context.font = '600 14px "Microsoft YaHei", sans-serif'
  const sourceLines = result.days.map((day, dayIndex) => {
    const parents = day.activities.filter(card => card.source_details?.length)
    const lines = parents.length ? [{text: '原文安排 · 门口及内部地点未单独核验', heading: true}] : []
    for (const parent of parents) {
      lines.push(...wrapText(context, `${parent.name}：`, width - padding * 2 - leftWidth - 32).map(text => ({text, heading: true})))
      parent.source_details?.forEach((detail, index) => {
        lines.push(...wrapText(context, `${index + 1}. ${detail.name}${detail.optional ? '（备选）' : ''}`, width - padding * 2 - leftWidth - 32).map(text => ({text, heading: false})))
      })
    }
    const pendingDetails = (unresolvedDays[dayIndex]?.activities || []).filter(card => card.source_details?.length)
    if (pendingDetails.length) lines.push({text: '待确认地点的原文安排 · 不作为已核验主线', heading: true})
    for (const parent of pendingDetails) {
      lines.push(...wrapText(context, `${parent.name}（地点待确认）：`, width - padding * 2 - leftWidth - 32).map(text => ({text, heading: true})))
      parent.source_details?.forEach((detail, index) => lines.push(...wrapText(context,
        `${index + 1}. ${detail.name}${detail.optional ? '（备选）' : ''}`, width - padding * 2 - leftWidth - 32).map(text => ({text, heading: false}))))
    }
    const diningCards = [...day.activities, ...(unresolvedDays[dayIndex]?.activities || [])]
      .map(card => ({card, notes: diningAccessLines(card, card.category === '餐饮')})).filter(item => item.notes.length)
    if (diningCards.length) lines.push({text: '餐饮访问与用途 · 场所资料不代表入内或营业已确认', heading: true})
    for (const {card, notes} of diningCards) {
      lines.push(...wrapText(context, `${card.name}${card.status !== 'READY' ? '（地点待确认）' : ''}：`, width - padding * 2 - leftWidth - 32).map(text => ({text, heading: true})))
      for (const note of notes) lines.push(...wrapText(context, note, width - padding * 2 - leftWidth - 32).map(text => ({text, heading: false})))
    }
    const meals = sourceMealDescriptions?.[dayIndex] ?? sourceMeals(day)
    if (meals.length) {
      lines.push({text: '原文用餐安排', heading: true})
      for (const meal of meals) {
        lines.push(...wrapText(context, meal, width - padding * 2 - leftWidth - 32).map(text => ({text, heading: false})))
      }
    }
    if (day.alternatives?.length) {
      lines.push({text: '原文备选与方案 · 未选择的内容不属于主线', heading: true})
      for (const alternative of day.alternatives) {
        const selection = day.choice_selections?.find(item => item.choice_group_token === alternative.choice_group_token)
        const state = selection?.status === 'MODIFIED' ? '已调整，原方案供对照' :
          selection?.branch_token === alternative.branch_token && alternative.branch_token ? '已选择，地点状态见行程' : '备选'
        const label = `${alternative.branch_label ? `${alternative.branch_label} · ` : ''}${alternative.name}（${state}）`
        lines.push(...wrapText(context, label, width - padding * 2 - leftWidth - 32).map(text => ({text, heading: true})))
        if (alternative.source_details?.length) {
          lines.push({text: '随此地点的原文安排 · 门口及内部地点未单独核验', heading: false})
          for (const [index, detail] of alternative.source_details.entries()) {
            lines.push(...wrapText(context, `${index + 1}. ${detail.name}${detail.optional ? '（备选）' : ''}`, width - padding * 2 - leftWidth - 32).map(text => ({text, heading: false})))
          }
        }
      }
    }
    return lines
  })
  const sourceHeights = sourceLines.map(lines => lines.length ? lines.length * 24 + 32 : 0)
  const appendixLines: Array<{text: string; heading: boolean}> = []
  const append = (value: string, heading = false) => appendixLines.push(...wrapText(context, value, width - padding * 2 - 32).map(text => ({text, heading})))
  const unassigned = supplementary?.status === 'AVAILABLE' ? supplementary.days.filter(day => day.day_index == null).flatMap(day => day.items.filter(item => item.role === 'OPTIONAL')) : []
  if (unassigned.length) {
    append('未指定日期 · 原文备选，尚未排入任何一天', true)
    unassigned.forEach(item => append(item.name))
  }
  if (supplementary && supplementary.status !== 'AVAILABLE') append('原文补充安排已删除或暂不可读取；这里只保留当前结构化结果。')
  // The page's sourceLodgings contains confirmed hotels only. The authoritative
  // constraints also carry unconfirmed hotels and their explicit night scope.
  const exportLodgings = [...new Map([...sourceLodgings, ...(result.lodging_constraints || [])]
    .map(lodging => [lodging.activity_token, lodging])).values()]
  if (exportLodgings.length) {
    append('原文住宿安排', true)
    exportLodgings.forEach(lodging => {
      const nights = lodging.overnight_days?.length ? `第${lodging.overnight_days.join('、')}晚` : '全程住宿安排，具体夜晚未列明'
      append(`${lodging.name} · ${nights} · ${lodging.status === 'READY' ? '已确认' : '地点待确认'}`)
      lodging.source_details?.forEach(detail => append(`${detail.name}${detail.optional ? '（备选）' : ''}`))
    })
  }
  const appendixHeight = appendixLines.length ? appendixLines.length * 24 + 40 : 0
  const height = headerHeight + dayLayouts.reduce((sum, layout, index) => sum + layout.height + 30 + (dayMessages[index] ? 32 : 0) + sourceHeights[index], 0) + appendixHeight + 64
  canvas.height = height

  const gradient = context.createLinearGradient(0, 0, width, height)
  gradient.addColorStop(0, '#def5ff')
  gradient.addColorStop(0.42, '#e8f0ff')
  gradient.addColorStop(1, '#fafcfd')
  context.fillStyle = gradient
  context.fillRect(0, 0, width, height)
  context.fillStyle = '#0c789d'
  context.font = '700 17px "Microsoft YaHei", sans-serif'
  context.fillText('行程查 · 行程概览', padding, 46)
  context.fillStyle = '#142f3a'
  context.font = '700 34px "Microsoft YaHei", sans-serif'
  const destination = result.assumptions.find((item) => item.key === 'destination')?.value || '我的行程'
  context.fillText(fitText(context, destination, width - padding * 2), padding, 92)
  context.fillStyle = '#607984'
  context.font = '400 14px "Microsoft YaHei", sans-serif'
  context.fillText(`共 ${result.days.length} 天 · ${result.days.reduce((sum, day) => sum + day.activities.length, 0)} 个地点 · 生成时地图底图未包含`, padding, 120)
  const routeStatus = mapView?.status || result.map.status
  const routeStatusLabel = routeStatus === 'NEEDS_UPDATE'
    ? '路线状态：行程已调整，需要更新'
    : routeStatus === 'LIMITED'
      ? '路线状态：仅部分路段可用'
      : routeStatus === 'UNAVAILABLE'
        ? '路线状态：暂不可用'
        : routeStatus === 'PREPARING'
          ? '路线状态：正在准备'
          : '路线状态：已准备'
  context.fillStyle = routeStatus === 'NEEDS_UPDATE' ? '#8a5a18' : '#0c789d'
  context.font = '600 13px "Microsoft YaHei", sans-serif'
  context.fillText(routeStatusLabel, padding, 148)
  context.fillStyle = '#855b19'
  statusMessages.forEach((message, index) => context.fillText(message, padding, 174 + index * 24))

  let dayY = headerHeight - 14
  result.days.forEach((day, dayIndex) => {
    const y = dayY
    const layout = dayLayouts[dayIndex]
    const message = dayMessages[dayIndex]
    const noticeHeight = message ? 32 : 0
    const dayHeight = layout.height + 30 + noticeHeight + sourceHeights[dayIndex]
    const baseX = padding + leftWidth
    const baseY = y + 8 + noticeHeight
    context.fillStyle = 'rgba(255,255,255,0.88)'
    context.beginPath()
    context.roundRect(padding, y, width - padding * 2, dayHeight - 16, 22)
    context.fill()
    const color = DAY_COLORS[dayIndex % DAY_COLORS.length]
    context.fillStyle = color
    context.beginPath()
    context.roundRect(padding + 18, y + 20, leftWidth - 36, 34, 17)
    context.fill()
    context.fillStyle = '#ffffff'
    context.font = '700 15px "Microsoft YaHei", sans-serif'
    context.fillText(relativeDayLabel(dayIndex), padding + 30, y + 43)
    context.fillStyle = '#607984'
    context.font = '400 12px "Microsoft YaHei", sans-serif'
    context.fillText(`${day.activities.length} 个地点`, padding + 28, y + 78)
    if (message) {
      context.fillStyle = '#855b19'
      context.font = '600 13px "Microsoft YaHei", sans-serif'
      context.fillText(fitText(context, message, width - padding * 2 - leftWidth - 16), baseX + 16, y + 29)
    }

    day.activities.slice(0,-1).forEach((_, index) => {
      const edge=serpentineEdge(layout,index)
      context.save()
      context.translate(baseX,baseY)
      context.strokeStyle='#7995ad'
      context.lineWidth=1.5
      context.stroke(new Path2D(edge.path))
      const tx=edge.arrowX
      context.beginPath(); context.moveTo(tx-3,edge.arrowY-edge.arrowDirection*7);context.lineTo(tx,edge.arrowY-edge.arrowDirection*2);context.lineTo(tx+3,edge.arrowY-edge.arrowDirection*7);context.stroke()
      context.font='400 11px "Microsoft YaHei", sans-serif'
      const label=routeSummary(mapView,day,index)
      const lines=label.split('\n').flatMap(line=>wrapText(context,line,edge.turn?96:184))
      const labelWidth=Math.max(...lines.map(line=>context.measureText(line).width))+14
      const labelHeight=lines.length*14+8
      context.fillStyle=lines.length>1?'#fff5df':'#eef8f5'
      context.beginPath(); context.roundRect(edge.x-labelWidth/2,edge.y-labelHeight/2,labelWidth,labelHeight,10);context.fill()
      context.fillStyle='#427c77'; context.textAlign='center'
      lines.forEach((line,i)=>context.fillText(line,edge.x,edge.y-labelHeight/2+15+i*14))
      context.restore()
    })
    day.activities.forEach((card, cardIndex) => {
      const point=layout.point(cardIndex)
      const cardWidth=layout.cardWidth
      const x=baseX+point.x
      const cardY=baseY+point.y
      context.fillStyle = '#ffffff'
      context.strokeStyle = card.status === 'READY' ? `${color}55` : '#c38a3266'
      context.lineWidth = 2
      context.beginPath()
      context.roundRect(x, cardY, cardWidth, layout.cardHeight, 16)
      context.fill()
      context.stroke()
      context.fillStyle = color
      context.beginPath()
      context.arc(x + 22, cardY + 23, 12, 0, Math.PI * 2)
      context.fill()
      context.fillStyle = '#ffffff'
      context.font = '700 11px "Microsoft YaHei", sans-serif'
      context.textAlign = 'center'
      context.fillText(String(cardIndex + 1), x + 22, cardY + 27)
      context.textAlign = 'left'
      context.fillStyle = '#647984'
      context.font = '400 11px "Microsoft YaHei", sans-serif'
      context.fillText(fitText(context, card.category, cardWidth - 55), x + 42, cardY + 27)
      context.fillStyle = '#172e38'
      context.font = '700 15px "Microsoft YaHei", sans-serif'
      const nameLines = dayNames[dayIndex][cardIndex]
      nameLines.forEach((line, index) => context.fillText(line, x + 16, cardY + 59 + index * nameLineHeight))
      const statusY = cardY + 79 + (nameLines.length - 1) * nameLineHeight
      context.fillStyle = card.status === 'READY' ? '#e5f5f7' : '#fff4dd'
      context.beginPath()
      context.roundRect(x + 16, statusY, card.status === 'READY' ? 58 : 50, 24, 12)
      context.fill()
      context.fillStyle = card.status === 'READY' ? '#0c789d' : '#855b19'
      context.font = '600 11px "Microsoft YaHei", sans-serif'
      context.fillText(card.status === 'READY' ? '已确认' : '待确认', x + 26, statusY + 16)
    })
    sourceLines[dayIndex].forEach((line, index) => {
      context.fillStyle = line.heading ? '#0c789d' : '#425c66'
      context.font = `${line.heading ? '600' : '400'} 14px "Microsoft YaHei", sans-serif`
      context.fillText(line.text, baseX + 16, y + layout.height + noticeHeight + 24 + index * 24)
    })
    dayY += dayHeight
  })

  if (appendixLines.length) {
    context.fillStyle = 'rgba(255,255,255,0.88)'
    context.beginPath()
    context.roundRect(padding, dayY, width - padding * 2, appendixHeight, 20)
    context.fill()
    appendixLines.forEach((line, index) => {
      context.font = `${line.heading ? '600' : '400'} 14px "Microsoft YaHei", sans-serif`
      context.fillStyle = line.heading ? '#0c789d' : '#425c66'
      context.fillText(line.text, padding + 16, dayY + 28 + index * 24)
    })
  }

  context.fillStyle = '#607984'
  context.font = '400 12px "Microsoft YaHei", sans-serif'
  context.fillText('地点状态见各卡片；原文备选与方案另列。路线时效与参观条件需另行核对。', padding, height - 30)
  return canvas
}

function canvasBlob(canvas: HTMLCanvasElement) {
  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error('PNG_RENDER_FAILED'))),
      'image/png',
    )
  })
}

export default function ItineraryPngExport({
  result,
  mapView,
  etag,
  disabled,
  unresolvedDays = [],
  sourceMealDescriptions,
  supplementary,
  sourceLodgings = [],
}: {
  result: UserFacingTripResult
  mapView: MapRenderView | null
  etag: string
  disabled: boolean
  unresolvedDays?: UserFacingTripResult['days']
  sourceMealDescriptions?: string[][]
  supplementary?: TripSupplementaryView | null
  sourceLodgings?: SourceLodgingCard[]
}) {
  const currentEtag = useRef(etag)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const [previewUrl, setPreviewUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  currentEtag.current = etag

  useEffect(() => () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl)
  }, [previewUrl])

  const createPreview = async () => {
    const startingEtag = currentEtag.current
    setBusy(true)
    setError('')
    try {
      if (supplementary === null) throw new Error('SUPPLEMENTARY_NOT_READY')
      const canvas = await renderItinerary(result, mapView, unresolvedDays, sourceMealDescriptions, supplementary, sourceLodgings)
      if (startingEtag !== currentEtag.current) throw new Error('ITINERARY_CHANGED')
      const blob = await canvasBlob(canvas)
      if (startingEtag !== currentEtag.current) throw new Error('ITINERARY_CHANGED')
      setPreviewUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous)
        return URL.createObjectURL(blob)
      })
    } catch (reason) {
      setError(
        reason instanceof Error && reason.message === 'ITINERARY_CHANGED'
          ? '生成期间行程已经更新，请重新生成。'
          : reason instanceof Error && reason.message === 'SUPPLEMENTARY_NOT_READY'
            ? '补充安排尚未读取，请稍后再导出。' : '暂时无法生成图片，请稍后重试。',
      )
    } finally {
      setBusy(false)
    }
  }

  const download = () => {
    if (!previewUrl) return
    const anchor = document.createElement('a')
    anchor.href = previewUrl
    anchor.download = `行程查-${new Date().toISOString().slice(0, 10)}.png`
    anchor.click()
  }

  return (
    <>
      <button
        data-testid="export-itinerary-png"
        ref={triggerRef}
        type="button"
        disabled={disabled || busy || supplementary === null}
        onClick={() => void createPreview()}
        className="inline-flex min-h-11 items-center gap-2 rounded-xl border border-[#0c789d]/20 bg-white px-4 text-sm font-semibold text-[#0c789d] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] disabled:opacity-50"
      >
        <ImageIcon className="h-4 w-4" aria-hidden="true" />
        {busy ? '正在生成…' : supplementary === null ? '正在读取安排…' : '导出图片'}
      </button>
      {error && <p className="mt-2 text-sm text-amber-800" role="alert">{error}</p>}
      {previewUrl && (
        <AccessibleDialog
          titleId="png-preview-title"
          descriptionId="png-preview-description"
          onClose={() => setPreviewUrl('')}
          returnFocusRef={triggerRef}
        >
          <div data-testid="png-preview" className="flex items-start justify-between gap-4">
            <div>
              <p className="text-xs font-semibold text-[#0c789d]">行程横链 PNG</p>
              <h2 id="png-preview-title" className="mt-1 text-xl font-semibold text-slate-900">图片预览</h2>
            </div>
            <button type="button" aria-label="关闭图片预览" onClick={() => setPreviewUrl('')} className="flex min-h-11 min-w-11 items-center justify-center rounded-xl hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d]"><X className="h-5 w-5" aria-hidden="true" /></button>
          </div>
          <p id="png-preview-description" className="mt-2 text-sm text-slate-600">全部 {result.days.length} 天 · 不含地图；原文备选与方案另列。{exportStatus(result, unresolvedDays).join('；')}</p>
          <div className="mt-4 max-h-[55vh] overflow-auto rounded-2xl border border-slate-200 bg-slate-50 p-2">
            {/* Blob URL is created locally from structured result data. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={previewUrl} alt="行程横链导出预览" className="max-w-none" />
          </div>
          <button data-testid="download-itinerary-png" type="button" onClick={download} className="mt-5 inline-flex min-h-12 w-full items-center justify-center gap-2 rounded-xl bg-[#0c789d] px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] focus-visible:ring-offset-2"><Download className="h-4 w-4" aria-hidden="true" />下载 PNG</button>
        </AccessibleDialog>
      )}
    </>
  )
}
