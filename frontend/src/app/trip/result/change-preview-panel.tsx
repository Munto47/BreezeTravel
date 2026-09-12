'use client'

import type {PublicChangePreview, UserFacingTripResult} from '@/lib/trip-understanding-v3'

export default function ChangePreviewPanel({preview, loading, notice, onCancel}: {
  preview: PublicChangePreview | null
  loading: boolean
  stale: boolean
  busy: boolean
  checking: boolean
  result: UserFacingTripResult
  notice: string
  onRetry: () => void
  onAdopt: () => void
  onCancel: () => void
}) {
  // This persisted contract contains only clock/duration adjustments. Keep it
  // readable, but it no longer represents an action in the relative-order UI.
  return <div className="e-change-preview" data-testid="change-preview">
    <p role="status">{preview
      ? '这条旧建议不再适用，请重新检查。'
      : loading ? '正在准备调整预览…' : notice || '暂时没有可查看的调整预览。'}</p>
    <button type="button" className="e-button" onClick={onCancel}>返回行程</button>
  </div>
}
