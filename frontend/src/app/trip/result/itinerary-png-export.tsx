'use client'

import { useEffect, useRef, useState } from 'react'
import { Download, Image as ImageIcon, X } from 'lucide-react'

import type { MapRenderView, StaySuggestionView, TripSupplementaryView, UserFacingTripResult } from '@/lib/trip-understanding-v3'
import type {SourceLodgingCard} from '@/lib/confirmed-trip-view'
import AccessibleDialog from './accessible-dialog'
import {renderShareImage, type ShareImageStage} from './share-image'
import {exportResultWithStay, exportStatus} from './share-image-content'
import {throwIfExportAborted} from './share-image-photos'

function canvasBlob(canvas: HTMLCanvasElement, signal: AbortSignal) {
  return new Promise<Blob>((resolve, reject) => {
    const abort = () => reject(new DOMException('图片生成已取消', 'AbortError'))
    if (signal.aborted) {abort(); return}
    signal.addEventListener('abort', abort, {once: true})
    canvas.toBlob(
      (blob) => {
        signal.removeEventListener('abort', abort)
        if (signal.aborted) return
        if (blob) resolve(blob)
        else reject(new Error('PNG_RENDER_FAILED'))
      },
      'image/png',
    )
  })
}

export default function ItineraryPngExport({
  resource,
  result,
  mapView,
  stayView,
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
  stayView?: StaySuggestionView | null
  etag: string
  disabled: boolean
  unresolvedDays?: UserFacingTripResult['days']
  sourceMealDescriptions?: string[][]
  supplementary?: TripSupplementaryView | null
  sourceLodgings?: SourceLodgingCard[]
}) {
  const unavailable = disabled || supplementary === null
  const exportResult = exportResultWithStay(result, stayView)
  // Compare actual export data: map and lodging details can refresh while the
  // itinerary ETag and their status labels stay unchanged.
  const basis = JSON.stringify({resource, etag, result: exportResult, mapView, supplementary,
    unresolvedDays, sourceMealDescriptions, sourceLodgings})
  const currentBasis = useRef(basis)
  const currentDisabled = useRef(unavailable)
  const previewBasis = useRef('')
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const [previewUrl, setPreviewUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [stage, setStage] = useState<ShareImageStage>('PHOTOS')
  const generating = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  const [error, setError] = useState('')
  currentBasis.current = basis
  currentDisabled.current = unavailable

  useEffect(() => {
    mounted.current = true
    return () => {mounted.current = false; generating.current?.abort()}
  }, [])

  useEffect(() => () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl)
  }, [previewUrl])

  useEffect(() => {
    generating.current?.abort()
    if (previewUrl && (previewBasis.current !== basis || unavailable)) {
      setPreviewUrl('')
      setError('生成期间行程已经更新，请重新生成。')
    }
  }, [basis, unavailable, previewUrl])

  const createPreview = async () => {
    if (generating.current || currentDisabled.current) return
    const controller = new AbortController()
    generating.current = controller
    const startingBasis = currentBasis.current
    setBusy(true)
    setStage('PHOTOS')
    setError('')
    try {

      const canvas = await renderShareImage(exportResult, sourceLodgings, resource, {
        mapView, supplementary, unresolvedDays, sourceMealDescriptions, signal: controller.signal,
        onStage: next => {if (mounted.current && !controller.signal.aborted) setStage(next)},
      })
      if (startingBasis !== currentBasis.current || currentDisabled.current) throw new Error('ITINERARY_CHANGED')
      throwIfExportAborted(controller.signal)
      setStage('ENCODING')
      const blob = await canvasBlob(canvas, controller.signal)
      if (startingBasis !== currentBasis.current || currentDisabled.current) throw new Error('ITINERARY_CHANGED')
      throwIfExportAborted(controller.signal)
      if (!mounted.current) return
      previewBasis.current = startingBasis
      setPreviewUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous)
        return URL.createObjectURL(blob)
      })
    } catch (reason) {
      if (!mounted.current) return
      setError(
        startingBasis !== currentBasis.current || currentDisabled.current ||
        (reason instanceof Error && reason.message === 'ITINERARY_CHANGED')
          ? '生成期间行程已经更新，请重新生成。'
          : controller.signal.aborted ? '已取消图片生成。'
          : reason instanceof Error && reason.message === 'SUPPLEMENTARY_NOT_READY'
            ? '补充安排尚未读取，请稍后再导出。' : '暂时无法生成图片，请稍后重试。',
      )
    } finally {
      if (generating.current === controller) generating.current = null
      if (mounted.current) setBusy(false)
    }
  }

  const download = () => {
    if (!previewUrl) return
    if (previewBasis.current !== currentBasis.current || currentDisabled.current) {
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
        disabled={unavailable || busy}
        onClick={() => void createPreview()}
        className="inline-flex min-h-11 items-center gap-2 rounded-xl border border-[#0c789d]/20 bg-white px-4 text-sm font-semibold text-[#0c789d] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] disabled:opacity-50"
      >
        <ImageIcon className="h-4 w-4" aria-hidden="true" />
        {busy ? '正在生成…' : '导出图片'}
      </button>
      {busy && <>
        <button type="button" data-testid="cancel-itinerary-png" onClick={() => generating.current?.abort()}
          className="ml-2 inline-flex min-h-11 items-center rounded-xl px-3 text-sm font-semibold text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d]">取消生成</button>
        <p role="status" className="mt-2 text-sm text-slate-600">{stage === 'PHOTOS' ? '正在准备照片；加载较慢的照片会使用配图或类别占位。' : stage === 'DRAWING' ? '正在排版全部日期与补充安排…' : '正在生成 PNG 文件…'}</p>
      </>}
      {supplementary === null && <p role="status" className="mt-2 text-sm text-slate-600">正在读取补充安排，稍后即可导出。</p>}
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
          <p id="png-preview-description" className="mt-2 text-sm text-slate-600">全部 {result.days.length} 天 · 包含行程、原文备选及待确认说明。{exportStatus(exportResult, {unresolvedDays, supplementary, mapView}).join(' ')}</p>
          <div className="mt-4 max-h-[55vh] overflow-auto rounded-2xl border border-slate-200 bg-slate-50 p-2">
            {/* Blob URL is created locally from structured result data. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={previewUrl} alt="行程横链导出预览" className="h-auto w-full max-w-full" />
          </div>
          <p className="mt-3 text-sm text-slate-600">如浏览器未开始下载，可长按预览图片保存，或在 Safari／Chrome 中打开后下载。</p>
          <button data-testid="download-itinerary-png" type="button" onClick={download} className="mt-5 inline-flex min-h-12 w-full items-center justify-center gap-2 rounded-xl bg-[#0c789d] px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] focus-visible:ring-offset-2"><Download className="h-4 w-4" aria-hidden="true" />下载 PNG</button>
        </AccessibleDialog>
      )}
    </>
  )
}
