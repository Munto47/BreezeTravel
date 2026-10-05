'use client'

import { useEffect, useRef, useState } from 'react'
import { Download, Image as ImageIcon, X } from 'lucide-react'

import type { MapRenderView, TripSupplementaryView, UserFacingTripResult } from '@/lib/trip-understanding-v3'
import type {SourceLodgingCard} from '@/lib/confirmed-trip-view'
import AccessibleDialog from './accessible-dialog'
import {renderShareImage} from './share-image'

function exportStatus(result: UserFacingTripResult, unresolvedDays: UserFacingTripResult['days'] = []) {
  // Include unresolved historical entries when calculating the export notice.
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

function canvasBlob(canvas: HTMLCanvasElement) {
  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error('PNG_RENDER_FAILED'))),
      'image/png',
    )
  })
}

export default function ItineraryPngExport({
  resource,
  result,
  mapView,
  etag,
  disabled,
  unresolvedDays = [],
  sourceMealDescriptions,
  supplementary,
  sourceLodgings = [],
}: {
  resource?: string
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
  const currentDisabled = useRef(disabled)
  const previewEtag = useRef('')
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const [previewUrl, setPreviewUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  currentEtag.current = etag
  currentDisabled.current = disabled

  useEffect(() => () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl)
  }, [previewUrl])

  useEffect(() => {
    if (previewUrl && previewEtag.current !== etag) {
      setPreviewUrl('')
      setError('生成期间行程已经更新，请重新生成。')
    }
  }, [etag, previewUrl])

  const createPreview = async () => {
    const startingEtag = currentEtag.current
    setBusy(true)
    setError('')
    try {

      const canvas = await renderShareImage(result, sourceLodgings, resource)
      if (startingEtag !== currentEtag.current || currentDisabled.current) throw new Error('ITINERARY_CHANGED')
      const blob = await canvasBlob(canvas)
      if (startingEtag !== currentEtag.current || currentDisabled.current) throw new Error('ITINERARY_CHANGED')
      previewEtag.current = startingEtag
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
    if (previewEtag.current !== currentEtag.current || currentDisabled.current) {
      setPreviewUrl('')
      setError('行程正在更新或已经改变，请重新生成图片。')
      return
    }
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
              <p className="text-xs font-semibold text-[#0c789d]">旅行分享 PNG</p>
              <h2 id="png-preview-title" className="mt-1 text-xl font-semibold text-slate-900">图片预览</h2>
            </div>
            <button type="button" aria-label="关闭图片预览" onClick={() => setPreviewUrl('')} className="flex min-h-11 min-w-11 items-center justify-center rounded-xl hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d]"><X className="h-5 w-5" aria-hidden="true" /></button>
          </div>
          <p id="png-preview-description" className="mt-2 text-sm text-slate-600">全部 {result.days.length} 天 · 仅包含已安排的行程及照片。{exportStatus(result, unresolvedDays).join('；')}</p>
          <div className="mt-4 max-h-[55vh] overflow-auto rounded-2xl border border-slate-200 bg-slate-50 p-2">
            {/* Blob URL is created locally from structured result data. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={previewUrl} alt="行程横链导出预览" className="h-auto w-full max-w-full" />
          </div>
          <button data-testid="download-itinerary-png" type="button" onClick={download} className="mt-5 inline-flex min-h-12 w-full items-center justify-center gap-2 rounded-xl bg-[#0c789d] px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] focus-visible:ring-offset-2"><Download className="h-4 w-4" aria-hidden="true" />下载 PNG</button>
        </AccessibleDialog>
      )}
    </>
  )
}
