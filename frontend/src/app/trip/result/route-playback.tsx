'use client'

import { useEffect, useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight, Pause, Play } from 'lucide-react'

import type { MapRenderView, UserFacingTripResult } from '@/lib/trip-understanding-v3'

type GeometryPoint = { longitude: number; latitude: number }
type PlaybackSegment = {
  key: string
  fromToken?: string
  toToken?: string
  from: string
  to: string
  mode: 'walking' | 'transit'
  duration: number | null
  points: GeometryPoint[]
}

function validGeometry(points: GeometryPoint[]) {
  return points.length >= 2 && points.every(
    (point) => Number.isFinite(point.longitude)
      && Number.isFinite(point.latitude)
      && Math.abs(point.longitude) <= 180
      && Math.abs(point.latitude) <= 90,
  )
}

function validPoint(point: GeometryPoint | null | undefined): point is GeometryPoint {
  return Boolean(
    point
      && Number.isFinite(point.longitude)
      && Number.isFinite(point.latitude)
      && Math.abs(point.longitude) <= 180
      && Math.abs(point.latitude) <= 90,
  )
}

export default function RoutePlayback({
  active,
  view,
  day,
  mode,
  onPosition,
}: {
  active: boolean
  view: MapRenderView | null
  day: UserFacingTripResult['days'][number] | undefined
  mode: 'recommended' | 'walking' | 'transit'
  onPosition: (point: GeometryPoint | null) => void
}) {
  const segments = useMemo<PlaybackSegment[]>(() => {
    if (!view || !day || !['AVAILABLE', 'LIMITED'].includes(view.status)) return []
    const routeDay = view.days.find((candidate) => candidate.label === day.label)
    return (routeDay?.routes || []).flatMap((route, index) => {
      const selectedMode = mode === 'recommended' ? route.selected_mode : mode
      if (!selectedMode) return []
      const selected = route[selectedMode]
      if (selected.status !== 'AVAILABLE' || !validGeometry(selected.geometry)) return []
      return [{
        key: `${route.from_activity_token || route.from_name}-${route.to_activity_token || route.to_name}-${index}`,
        fromToken: route.from_activity_token,
        toToken: route.to_activity_token,
        from: route.from_name,
        to: route.to_name,
        mode: selectedMode,
        duration: selected.duration_minutes,
        points: selected.geometry,
      }]
    })
  }, [day, mode, view])
  const stations = useMemo(() => day?.activities || [], [day?.activities])
  const stationSegments = useMemo(
    () => stations.slice(0, -1).map((station, index) => {
      const next = stations[index + 1]
      const byToken = segments.filter(
        (segment) => segment.fromToken === station.activity_token && segment.toToken === next.activity_token,
      )
      if (byToken.length === 1) return byToken[0]
      const fromNameCount = stations.filter((item) => item.name === station.name).length
      const toNameCount = stations.filter((item) => item.name === next.name).length
      if (fromNameCount !== 1 || toNameCount !== 1) return null
      const byName = segments.filter(
        (segment) => !segment.fromToken && !segment.toToken && segment.from === station.name && segment.to === next.name,
      )
      return byName.length === 1 ? byName[0] : null
    }),
    [segments, stations],
  )
  const hasVerifiedPlayback = stationSegments.some((item) => item !== null)
  const playbackKey = `${view?.status || 'EMPTY'}:${stationSegments.map((item) => item?.key || '-').join('|')}`
  const [stationIndex, setStationIndex] = useState(0)
  const [pointIndex, setPointIndex] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [started, setStarted] = useState(false)
  const currentStation = stations[Math.min(stationIndex, Math.max(stations.length - 1, 0))]
  const nextStation = stations[stationIndex + 1]
  const segment = stationSegments[stationIndex] || null

  const stationPosition = (index: number): GeometryPoint | null => {
    const station = stations[index]
    if (!station) return null
    const point = view?.points?.find((item) => item.activity_token === station.activity_token)?.position
    if (validPoint(point)) return point
    const outgoing = stationSegments[index]
    if (outgoing?.points.length) return outgoing.points[0]
    const incoming = stationSegments[index - 1]
    if (incoming?.points.length) return incoming.points[incoming.points.length - 1]
    return null
  }

  useEffect(() => {
    setPlaying(false)
    setStarted(false)
    setStationIndex(0)
    setPointIndex(0)
    onPosition(null)
  }, [active, day?.label, mode, onPosition, playbackKey])

  useEffect(() => {
    if (!active || !started) {
      onPosition(null)
      return
    }
    if (playing && segment) {
      onPosition(segment.points[Math.min(pointIndex, segment.points.length - 1)] || null)
      return
    }
    onPosition(stationPosition(stationIndex))
  }, [active, onPosition, playing, pointIndex, segment, started, stationIndex])

  useEffect(() => {
    if (!active || !playing || !segment) return
    const timer = window.setTimeout(() => {
      if (pointIndex < segment.points.length - 1) {
        setPointIndex(pointIndex + 1)
        return
      }
      const followingStation = Math.min(stationIndex + 1, stations.length - 1)
      setStationIndex(followingStation)
      setPointIndex(0)
      if (followingStation >= stations.length - 1 || !stationSegments[followingStation]) setPlaying(false)
    }, 650)
    return () => window.clearTimeout(timer)
  }, [active, playing, pointIndex, segment, stationIndex, stationSegments, stations.length])

  const chooseStation = (next: number) => {
    setPlaying(false)
    setStarted(true)
    setStationIndex(Math.max(0, Math.min(next, stations.length - 1)))
    setPointIndex(0)
  }

  const togglePlayback = () => {
    if (playing) {
      setPlaying(false)
      return
    }
    let startIndex = stationIndex
    if (stationIndex >= stations.length - 1) startIndex = 0
    if (!stationSegments[startIndex]) return
    setStationIndex(startIndex)
    setPointIndex(0)
    setStarted(true)
    setPlaying(true)
  }

  if (!stations.length) {
    return (
      <section data-testid="route-playback" className="rounded-2xl border border-sky-900/10 bg-white/90 p-4" aria-label="路线预演">
        <p className="text-xs font-semibold tracking-[0.12em] text-[#0c789d]">计划路线模拟</p>
        <p className="mt-2 text-sm leading-6 text-slate-600">当天还没有可预演的地点。</p>
      </section>
    )
  }

  return (
    <section data-testid="route-playback" className="rounded-2xl border border-sky-900/10 bg-white/90 p-4" aria-label="路线预演">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs font-semibold tracking-[0.12em] text-[#0c789d]">计划路线模拟</p>
          <p className="mt-1 text-sm font-semibold text-slate-800">
            {currentStation?.name}{nextStation ? ` → ${nextStation.name}` : ' · 行程终点'}
          </p>
          <p className="text-xs text-slate-500">
            第 {stationIndex + 1}/{stations.length} 站
            {segment ? ` · ${segment.mode === 'walking' ? '步行' : '公交'}${segment.duration == null ? '' : `约 ${segment.duration} 分钟`}` : ''}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            aria-label="上一站"
            disabled={stationIndex === 0}
            onClick={() => chooseStation(stationIndex - 1)}
            className="flex min-h-11 min-w-11 items-center justify-center rounded-xl border border-slate-200 bg-white text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] disabled:opacity-40"
          >
            <ChevronLeft className="h-4 w-4" aria-hidden="true" />
          </button>
          <button
            type="button"
            aria-pressed={playing}
            disabled={!segment}
            onClick={togglePlayback}
            className="inline-flex min-h-11 items-center gap-2 rounded-xl bg-[#0c789d] px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {playing ? <Pause className="h-4 w-4" aria-hidden="true" /> : <Play className="h-4 w-4" aria-hidden="true" />}
            {playing ? '暂停' : '播放'}
          </button>
          <button
            type="button"
            aria-label="下一站"
            disabled={stationIndex === stations.length - 1}
            onClick={() => chooseStation(stationIndex + 1)}
            className="flex min-h-11 min-w-11 items-center justify-center rounded-xl border border-slate-200 bg-white text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] disabled:opacity-40"
          >
            <ChevronRight className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      </div>
      {!segment && stationIndex < stations.length - 1 && (
        <p className="mt-3 rounded-xl bg-sky-50 p-3 text-sm text-slate-600">
          当前路段没有可用的真实路线几何，因此地图动画不可用；仍可用上一站、下一站逐站查看。
        </p>
      )}
      <p className="mt-3 text-xs leading-5 text-slate-500">
        {hasVerifiedPlayback
          ? '这是已核对计划路线的视觉预演，不代表实时位置；默认暂停，播放不会重新请求路线。'
          : '这里只按行程卡片逐站预览；路线尚未核对，地图动画不可用。'}
      </p>
    </section>
  )
}
