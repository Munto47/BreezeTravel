import type {
  ActivityCardView,
  MapRenderView,
  PublicTripCheckItem,
} from '@/lib/trip-understanding-v3'

export function activityTime(card: ActivityCardView) {
  if (!card.start_time)
    return card.time_hint ? `${card.time_hint} · 时间待定` : '时间待定'
  const range = `${card.start_time}${card.end_time ? `–${card.end_time}` : ''}`
  return card.timing_source === 'SUGGESTED' ? `约 ${range}` : range
}
export function needsRecheck(
  item: PublicTripCheckItem,
  map: MapRenderView | null,
) {
  return (
    item.basis_status === 'NEEDS_RECHECK' ||
    (map?.status === 'NEEDS_UPDATE' && item.depends_on_routes !== false)
  )
}
export function findingLabel(
  item: PublicTripCheckItem,
  map: MapRenderView | null,
) {
  return needsRecheck(item, map) ? '待复检' : item.label
}
export function formatExpiry(value?: string | null) {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? null
    : date.toLocaleString('zh-CN', {
        month: 'long',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
}
