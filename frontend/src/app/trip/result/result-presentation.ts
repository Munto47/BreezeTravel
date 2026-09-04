import {
  type MapRenderView,
  type PublicTripChecksView,
  type UserFacingTripResult,
} from '@/lib/trip-understanding-v3'


export type ResultViewId = 'ITINERARY' | 'MAP_STAY' | 'CHECKS'

export const DAY_COLORS = ['#047857', '#2563eb', '#7c3aed', '#d97706', '#0f766e', '#be185d'] as const

export const DAY_ACCENTS = [
  ['from-amber-50', 'to-emerald-50', 'text-emerald-800'],
  ['from-sky-50', 'to-teal-50', 'text-teal-800'],
  ['from-violet-50', 'to-amber-50', 'text-violet-800'],
  ['from-rose-50', 'to-orange-50', 'text-orange-800'],
] as const

export type TransportConnector =
  | { status: 'AVAILABLE'; mode: 'walking' | 'transit'; durationMinutes: number }
  | { status: 'NEEDS_UPDATE' | 'PENDING' }

type DayView = UserFacingTripResult['days'][number]
type RouteView = MapRenderView['days'][number]['routes'][number]
type RouteMode = 'walking' | 'transit'


function isPositiveDuration(value: number | null): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
}


export function transportConnectorFor(
  day: DayView,
  fromName: string,
  toName: string,
  mapView: MapRenderView,
  locallyPending = false,
): TransportConnector {
  if (locallyPending || mapView.status === 'NEEDS_UPDATE') return { status: 'NEEDS_UPDATE' }
  if (mapView.status !== 'AVAILABLE') return { status: 'PENDING' }

  const matchingDays = mapView.days.filter((candidate) => candidate.label === day.label)
  if (matchingDays.length !== 1) return { status: 'PENDING' }
  if (day.activities.filter((activity) => activity.name === fromName).length !== 1) return { status: 'PENDING' }
  if (day.activities.filter((activity) => activity.name === toName).length !== 1) return { status: 'PENDING' }

  const routes = matchingDays[0].routes.filter(
    (route) => route.from_name === fromName && route.to_name === toName,
  )
  if (routes.length !== 1) return { status: 'PENDING' }
  const route = routes[0]
  const mode = route.selected_mode
  if (mode !== 'walking' && mode !== 'transit') return { status: 'PENDING' }
  const selected = route[mode]
  if (selected.status !== 'AVAILABLE' || !isPositiveDuration(selected.duration_minutes)) {
    return { status: 'PENDING' }
  }
  return { status: 'AVAILABLE', mode, durationMinutes: selected.duration_minutes }
}


export type RouteGeometrySegment = {
  dayLabel: string
  dayIndex: number
  routeIndex: number
  route: RouteView
  points: RouteView[RouteMode]['geometry']
}


function hasValidPoint(point: unknown): point is { longitude: number; latitude: number } {
  if (!point || typeof point !== 'object') return false
  const candidate = point as { longitude?: unknown; latitude?: unknown }
  return typeof candidate.longitude === 'number'
    && typeof candidate.latitude === 'number'
    && Number.isFinite(candidate.longitude)
    && Number.isFinite(candidate.latitude)
    && candidate.longitude >= -180
    && candidate.longitude <= 180
    && candidate.latitude >= -90
    && candidate.latitude <= 90
}


export function routeGeometrySegments(
  view: MapRenderView,
  mode: RouteMode,
): RouteGeometrySegment[] {
  if (view.status !== 'AVAILABLE' && view.status !== 'LIMITED') return []
  return view.days.flatMap((day, dayIndex) => day.routes.flatMap((route, routeIndex) => {
    const selected = route[mode]
    const geometry = Array.isArray(selected.geometry) ? selected.geometry : []
    if (
      selected.status !== 'AVAILABLE'
      || geometry.length < 2
      || geometry.some((point) => !hasValidPoint(point))
    ) return []
    return [{ dayLabel: day.label, dayIndex, routeIndex, route, points: geometry }]
  }))
}


const PUBLIC_CHECK_LABELS = new Set(['必须调整', '可以更好', '需要确认'])


export function topPublicChecks(view: PublicTripChecksView | null) {
  return (view?.items || [])
    .filter((item) => PUBLIC_CHECK_LABELS.has(item.label))
    .slice(0, 3)
}
