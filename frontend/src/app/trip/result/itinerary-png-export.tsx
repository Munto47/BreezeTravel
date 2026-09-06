'use client'

import { useEffect, useRef, useState } from 'react'
import { Download, Image as ImageIcon, X } from 'lucide-react'

import type { MapRenderView, UserFacingTripResult } from '@/lib/trip-understanding-v3'
import AccessibleDialog from './accessible-dialog'
import { serpentineLayout, serpentineEdge } from './serpentine-layout'
import { DAY_COLORS, transportConnectorFor, distanceLabel } from './result-presentation'

function routeSummary(
  mapView: MapRenderView | null,
  day: UserFacingTripResult['days'][number],
  fromIndex: number,
) {
  if (!mapView) return '交通待确认'
  const connector=transportConnectorFor(day,day.activities[fromIndex],day.activities[fromIndex+1],mapView)
  if(connector.status==='NEEDS_UPDATE')return '路线需要更新'
  if(connector.status!=='AVAILABLE')return '交通待确认'
  return `${connector.mode==='walking'?'步行':'公交'}约 ${connector.durationMinutes} 分钟${connector.distanceMeters===null?'':` · ${distanceLabel(connector.distanceMeters)}`}`

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

async function renderItinerary(
  result: UserFacingTripResult,
  mapView: MapRenderView | null,
) {
  if ('fonts' in document) await document.fonts.ready
  const leftWidth = 140
  const padding = 36
  const width = 1440
  const dayLayouts = result.days.map(day => serpentineLayout(width-padding*2-leftWidth-16, day.activities.length))
  const headerHeight = 188
  const height = headerHeight + dayLayouts.reduce((sum, layout) => sum + layout.height + 30, 0) + 64
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const context = canvas.getContext('2d')
  if (!context) throw new Error('CANVAS_UNAVAILABLE')

  const gradient = context.createLinearGradient(0, 0, width, height)
  gradient.addColorStop(0, '#def5ff')
  gradient.addColorStop(0.42, '#e8f0ff')
  gradient.addColorStop(1, '#fafcfd')
  context.fillStyle = gradient
  context.fillRect(0, 0, width, height)
  context.fillStyle = '#0c789d'
  context.font = '700 17px "Microsoft YaHei", sans-serif'
  context.fillText('行程查 · 完整行程', padding, 46)
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

  let dayY = headerHeight - 14
  result.days.forEach((day, dayIndex) => {
    const y = dayY
    const layout = dayLayouts[dayIndex]
    const dayHeight = layout.height + 30
    const baseX = padding + leftWidth
    const baseY = y + 8
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
    context.fillText(fitText(context, day.label, leftWidth - 58), padding + 30, y + 43)
    context.fillStyle = '#607984'
    context.font = '400 12px "Microsoft YaHei", sans-serif'
    context.fillText(`${day.activities.length} 个地点`, padding + 28, y + 78)

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
      const labelWidth=context.measureText(label).width+14
      context.fillStyle='#eef8f5'
      context.beginPath(); context.roundRect(edge.x-labelWidth/2,edge.y-12,labelWidth,24,12);context.fill()
      context.fillStyle='#427c77'; context.textAlign='center'
      context.fillText(label,edge.x,edge.y+4)
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
      context.fillText(fitText(context, card.name, cardWidth - 32), x + 16, cardY + 59)
      context.fillStyle = card.status === 'READY' ? '#e5f5f7' : '#fff4dd'
      context.beginPath()
      context.roundRect(x + 16, cardY + 79, card.status === 'READY' ? 58 : 50, 24, 12)
      context.fill()
      context.fillStyle = card.status === 'READY' ? '#0c789d' : '#855b19'
      context.font = '600 11px "Microsoft YaHei", sans-serif'
      context.fillText(card.status === 'READY' ? '已确认' : '待确认', x + 26, cardY + 95)
    })
    dayY += dayHeight
  })

  context.fillStyle = '#607984'
  context.font = '400 12px "Microsoft YaHei", sans-serif'
  context.fillText('计划内容仅供出行准备；待确认与路线过期状态已如实保留。', padding, height - 30)
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
}: {
  result: UserFacingTripResult
  mapView: MapRenderView | null
  etag: string
  disabled: boolean
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
      const canvas = await renderItinerary(result, mapView)
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
          : '暂时无法生成图片，请稍后重试。',
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
        disabled={disabled || busy}
        onClick={() => void createPreview()}
        className="inline-flex min-h-11 items-center gap-2 rounded-xl border border-[#0c789d]/20 bg-white px-4 text-sm font-semibold text-[#0c789d] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] disabled:opacity-50"
      >
        <ImageIcon className="h-4 w-4" aria-hidden="true" />
        {busy ? '正在生成…' : '导出图片'}
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
              <p className="text-xs font-semibold text-[#0c789d]">完整横链 PNG</p>
              <h2 id="png-preview-title" className="mt-1 text-xl font-semibold text-slate-900">图片预览</h2>
            </div>
            <button type="button" aria-label="关闭图片预览" onClick={() => setPreviewUrl('')} className="flex min-h-11 min-w-11 items-center justify-center rounded-xl hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d]"><X className="h-5 w-5" aria-hidden="true" /></button>
          </div>
          <p id="png-preview-description" className="mt-2 text-sm text-slate-600">完整行程 · 不含地图</p>
          <div className="mt-4 max-h-[55vh] overflow-auto rounded-2xl border border-slate-200 bg-slate-50 p-2">
            {/* Blob URL is created locally from structured result data. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={previewUrl} alt="完整行程横链导出预览" className="max-w-none" />
          </div>
          <button data-testid="download-itinerary-png" type="button" onClick={download} className="mt-5 inline-flex min-h-12 w-full items-center justify-center gap-2 rounded-xl bg-[#0c789d] px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] focus-visible:ring-offset-2"><Download className="h-4 w-4" aria-hidden="true" />下载 PNG</button>
        </AccessibleDialog>
      )}
    </>
  )
}
