import {
  type ActivityCardView,
  type MapRenderView,
  type PublicTripChecksView,
  type PublicRouteModeView,
  type UserFacingTripResult,
} from '@/lib/trip-understanding-v3'


export type ResultViewId = 'ITINERARY' | 'MAP_STAY'

// Display only. Stored day labels still bind routes and other versioned data.
export function relativeDayLabel(zeroBasedIndex: number) {
  return `Day ${zeroBasedIndex + 1}`
}

export const DAY_COLORS = ['#047857', '#2563eb', '#7c3aed', '#d97706', '#0f766e', '#be185d'] as const

export const DAY_ACCENTS = [
  ['from-amber-50', 'to-emerald-50', 'text-emerald-800'],
  ['from-sky-50', 'to-teal-50', 'text-teal-800'],
  ['from-violet-50', 'to-amber-50', 'text-violet-800'],
  ['from-rose-50', 'to-orange-50', 'text-orange-800'],
] as const

const LODGING_PURPOSE_LABELS: Record<NonNullable<ActivityCardView['lodging_event']>, string> = {
  OVERNIGHT: '入住',
  CHECK_OUT: '退房',
  DEPARTURE: '酒店出发',
  LUGGAGE_PICKUP: '取行李',
  VISIT_ONLY: '仅到访',
}

export function activityCategoryLabel(card: ActivityCardView): string {
  if (card.category !== '住宿' || card.status !== 'READY' || card.lodging_role_uncertain || !card.lodging_event) {
    return card.category
  }
  return LODGING_PURPOSE_LABELS[card.lodging_event] || card.category
}

export type TransportConnector =
  | { status: 'AVAILABLE'; mode: 'walking' | 'transit'; durationMinutes: number; distanceMeters: number | null; connectionStatus: 'VERIFIED' | 'UNVERIFIED' }
  | { status: 'NEEDS_UPDATE' | 'PENDING' | 'UNAVAILABLE' }

type DayView = UserFacingTripResult['days'][number]
type RouteView = MapRenderView['days'][number]['routes'][number]
type RouteMode = 'walking' | 'transit'

function hasVerifiedConnection(data: PublicRouteModeView) {
  return data.connection_status === 'VERIFIED' && Array.isArray(data.geometry_break_indices)
    && data.geometry_break_indices.length === 0
}


function isPositiveDuration(value: number | null): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
}


export function transportConnectorFor(
  day: DayView,
  from: ActivityCardView,
  to: ActivityCardView,
  mapView: MapRenderView,
  locallyPending = false,
): TransportConnector {
  if (from.status !== 'READY' || to.status !== 'READY') return { status: 'UNAVAILABLE' }
  if (locallyPending || mapView.status === 'NEEDS_UPDATE') return { status: 'NEEDS_UPDATE' }
  if (mapView.status === 'UNAVAILABLE') return { status: 'UNAVAILABLE' }
  if (!['AVAILABLE', 'LIMITED'].includes(mapView.status)) return { status: 'PENDING' }

  const matchingDays = mapView.days.filter((candidate) => candidate.label === day.label)
  if (matchingDays.length !== 1) return { status: 'PENDING' }
  const routes = matchingDays[0].routes.filter(
    (route) =>
      route.from_activity_token === from.activity_token &&
      route.to_activity_token === to.activity_token,
  )
  const namesAreUnique = day.activities.filter((card) => card.name === from.name).length === 1
    && day.activities.filter((card) => card.name === to.name).length === 1
  const fallbackRoutes = namesAreUnique
    ? matchingDays[0].routes.filter(
        (route) =>
          !route.from_activity_token &&
          !route.to_activity_token &&
          route.from_name === from.name &&
          route.to_name === to.name,
      )
    : []
  const candidates = routes.length ? routes : fallbackRoutes
  if (candidates.length !== 1) return { status: 'PENDING' }
  const route = candidates[0]
  const mode = route.walking.status === 'AVAILABLE' && isPositiveDuration(route.walking.duration_minutes)
    && route.walking.duration_minutes <= 30
    ? 'walking' : route.transit.status === 'AVAILABLE' ? 'transit' : null
  if (mode !== 'walking' && mode !== 'transit') return { status: 'UNAVAILABLE' }
  const selected = route[mode]
  if (selected.status !== 'AVAILABLE' || !isPositiveDuration(selected.duration_minutes)) {
    return { status: 'UNAVAILABLE' }
  }
  return { status: 'AVAILABLE', mode, durationMinutes: selected.duration_minutes, distanceMeters: typeof selected.distance_meters === 'number' && Number.isFinite(selected.distance_meters) && selected.distance_meters >= 0 ? selected.distance_meters : null,
    connectionStatus: hasVerifiedConnection(selected) ? 'VERIFIED' : 'UNVERIFIED' }
}


export const ROUTE_CONNECTION_UNVERIFIED = '起终点衔接未核实'
export type RouteCoverageScope = 'REQUESTED_POINTS' | 'RETURNED_SEGMENTS' | null | undefined

/** Preserve returned measurements; availability alone does not prove POI-to-POI coverage. */
export function routeModePresentation(mode: RouteMode, data: PublicRouteModeView | undefined | null) {
  if (!data || data.status !== 'AVAILABLE') return {label: '路线暂不可用', warning: null}
  const time = typeof data.duration_minutes === 'number' && Number.isFinite(data.duration_minutes) && data.duration_minutes >= 0
    ? `${data.duration_minutes} 分钟` : '时长未提供'
  const distance = typeof data.distance_meters === 'number' && Number.isFinite(data.distance_meters) && data.distance_meters >= 0
    ? distanceLabel(data.distance_meters) : ''
  return {label: `${mode === 'walking' ? '步行' : '公交'} · ${time}${distance ? ` · ${distance}` : ''}`,
    warning: hasVerifiedConnection(data) ? null : ROUTE_CONNECTION_UNVERIFIED}
}

export function routeModeSummary(mode: RouteMode, data: PublicRouteModeView | undefined | null) {
  const {label, warning} = routeModePresentation(mode, data)
  const geometryNotice = routeGeometryNotice(data)
  return `${label}${warning ? ` · ${warning}` : ''}${geometryNotice ? ` · ${geometryNotice}` : ''}`
}

export function routeGeometryNotice(data: PublicRouteModeView | undefined | null) {
  if (!data || data.status !== 'AVAILABLE') return null
  const parts = routeGeometryParts(data)
  return parts === null ? '原路线分段尚未核实，请更新路线'
    : (data.geometry_break_indices?.length || 0) > 0 ? '路线分段展示，片段之间尚未衔接' : null
}

/** A break index starts a new returned segment; never join across that boundary. */
export function routeGeometryParts(data: PublicRouteModeView): PublicRouteModeView['geometry'][] | null {
  const breaks = data.geometry_break_indices, points = data.geometry
  if (data.status !== 'AVAILABLE' || !Array.isArray(breaks) || !Array.isArray(points)
    || !points.every(hasValidPoint) || breaks.some((index, i) => !Number.isInteger(index) || index <= 0
      || index >= points.length || (i > 0 && index <= breaks[i - 1]))) return null
  const edges = [0, ...breaks, points.length]
  return edges.slice(0, -1).map((start, i) => points.slice(start, edges[i + 1]))
}

export function connectorPresentation(connector: TransportConnector) {
  if (connector.status !== 'AVAILABLE') return {label: connector.status === 'NEEDS_UPDATE' ? '路线需要更新'
    : connector.status === 'UNAVAILABLE' ? '路线暂不可用' : '路线准备中', warning: null}
  return routeModePresentation(connector.mode, {status: 'AVAILABLE', duration_minutes: connector.durationMinutes,
    distance_meters: connector.distanceMeters, connection_status: connector.connectionStatus,
    geometry_break_indices: connector.connectionStatus === 'VERIFIED' ? [] : null, transfer_count: null, geometry: []})
}

export function dayRouteSummary(day: DayView, mapView: MapRenderView, pending = false): string {
  if (day.activities.length < 2) return ''
  const routes = day.activities.slice(0, -1).map((card, i) => transportConnectorFor(day, card, day.activities[i + 1], mapView, pending))
  if (routes.some(route => route.status === 'NEEDS_UPDATE')) return '路线需要更新'
  const ready = routes.filter(route => route.status === 'AVAILABLE')
  const connected = ready.filter(route => route.connectionStatus === 'VERIFIED')
  if (connected.length !== routes.length) return `已核实衔接 ${connected.length}/${routes.length} 段${ready.some(route => route.connectionStatus !== 'VERIFIED') ? ` · ${ROUTE_CONNECTION_UNVERIFIED}` : ''} · 暂无完整日合计`
  return (['walking', 'transit'] as const).flatMap(mode => {
    const matching = connected.filter(route => route.mode === mode)
    if (!matching.length) return []
    const distance = matching.every(route => route.distanceMeters !== null)
      ? `${distanceLabel(matching.reduce((total, route) => total + route.distanceMeters!, 0))} · ` : ''
    return `${mode === 'walking' ? '步行' : '公交'} ${distance}${matching.reduce((total, route) => total + route.durationMinutes, 0)} 分钟`
  }).join(' / ')
}

/** Strip only old generated route arithmetic; keep the restaurant's other description. */
export function diningCandidateReason(candidate: {reason: string; extra_minutes?: number | null; route_coverage_scope?: RouteCoverageScope}) {
  if (candidate.route_coverage_scope === 'REQUESTED_POINTS') return candidate.reason
  const rest = candidate.reason
    .replace(/(^|[。；;\n])\s*(?:已返回路段比较，)?经此店(?:前往下一站)?约多\s*\d+(?:\.\d+)?\s*分钟[；;，,。]?/g, '$1')
    .replace(/(^|[。；;\n])\s*未含未核实衔接[，,；;。]?/g, '$1').trim()
  if (candidate.route_coverage_scope === 'RETURNED_SEGMENTS' && typeof candidate.extra_minutes === 'number' && Number.isFinite(candidate.extra_minutes)) {
    return `已返回路段比较约多 ${candidate.extra_minutes} 分钟，未含未核实衔接。${rest}`
  }
  if (/绕路时间(?:及营业情况)?尚未确认/.test(rest)) return rest
  return `${rest}${rest && !/[。；;]$/.test(rest) ? '。' : ''}绕路时间尚未确认。`
}

export function routeComparisonPresentation(scope: RouteCoverageScope, minutesSaved: number) {
  if (scope !== 'REQUESTED_POINTS' && scope !== 'RETURNED_SEGMENTS') return {
    comparable: false, heading: '路段比较范围尚未核实', note: '暂不能判断节省，请重新比较；行程没有改动。',
  }
  return {comparable: true,
    heading: scope === 'RETURNED_SEGMENTS' ? `已返回路段比较少 ${minutesSaved} 分钟` : `变化路段可节省 ${minutesSaved} 分钟`,
    note: scope === 'RETURNED_SEGMENTS' ? '未含未核实衔接。只比较发生变化的已返回路段，不是全天交通总时长。'
      : '只合计发生变化的路段，不是全天交通总时长。'}
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
    return (routeGeometryParts(selected) || []).filter(points => points.length >= 2)
      .map(points => ({ dayLabel: day.label, dayIndex, routeIndex, route, points }))
  }))
}


const PUBLIC_CHECK_LABELS = new Set(['必须调整', '可以更好', '需要确认'])


export function topPublicChecks(view: PublicTripChecksView | null) {
  return (view?.items || [])
    .filter((item) => PUBLIC_CHECK_LABELS.has(item.label))
    .slice(0, 3)
}

export function distanceLabel(meters: number | null) {
  if(meters === null) return ''
  return meters < 1000 ? `${Math.round(meters)} 米` : `${(meters/1000).toFixed(1)} 公里`
}
