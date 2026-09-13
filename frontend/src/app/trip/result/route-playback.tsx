'use client'

import { useEffect, useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight, Pause, Play } from 'lucide-react'

import type { MapRenderView, UserFacingTripResult } from '@/lib/trip-understanding-v3'
import {routeModeSummary, routeGeometryParts} from './result-presentation'

type GeometryPoint = { longitude: number; latitude: number }
type PlaybackSegment = {
  key: string
  fromToken?: string
  toToken?: string
  from: string
  to: string
  mode: 'walking' | 'transit'
  duration: number | null
  summary: string
  parts: GeometryPoint[][]
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
      if (selected.status !== 'AVAILABLE') return []
      return [{
        key: `${route.from_activity_token || route.from_name}-${route.to_activity_token || route.to_name}-${index}:${selected.geometry_break_indices?.join(',') ?? 'unknown'}`,
        fromToken: route.from_activity_token,
        toToken: route.to_activity_token,
        from: route.from_name,
        to: route.to_name,
        mode: selectedMode,
        duration: selected.duration_minutes,
        summary: routeModeSummary(selectedMode, selected),
        parts: (routeGeometryParts(selected) || []).filter(part => part.length > 0),
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
  const playbackKey = `${view?.status || 'EMPTY'}:${stationSegments.map((item) => item?.key || '-').join('|')}`
  const [stationIndex, setStationIndex] = useState(0)
  const [pointIndex, setPointIndex] = useState(0)
  const [partIndex, setPartIndex] = useState(0)
  const [nextPartPending, setNextPartPending] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [started, setStarted] = useState(false)
  const currentStation = stations[Math.min(stationIndex, Math.max(stations.length - 1, 0))]
  const nextStation = stations[stationIndex + 1]
  const segment = stationSegments[stationIndex] || null
  const currentPart = segment?.parts[partIndex] || []

  const stationPosition = (index: number): GeometryPoint | null => {
    const station = stations[index]
    if (!station) return null
    const point = view?.points?.find((item) => item.activity_token === station.activity_token)?.position
    if (validPoint(point)) return point
    const outgoing = stationSegments[index]
    if (outgoing?.parts[0]?.length) return outgoing.parts[0][0]
    const incoming = stationSegments[index - 1]
    const finalPart = incoming?.parts[incoming.parts.length - 1]
    if (finalPart?.length) return finalPart[finalPart.length - 1]
    return null
  }

  useEffect(() => {
    setPlaying(false)
    setStarted(false)
    setStationIndex(0)
    setPointIndex(0)
    setPartIndex(0)
    setNextPartPending(false)
    onPosition(null)
  }, [active, day?.label, mode, onPosition, playbackKey])

  useEffect(() => {
    if (!active || !started) {
      onPosition(null)
      return
    }
    if ((playing || nextPartPending) && currentPart.length) {
      onPosition(currentPart[Math.min(pointIndex, currentPart.length - 1)] || null)
      return
    }
    onPosition(stationPosition(stationIndex))
  }, [active, onPosition, playing, pointIndex, currentPart, nextPartPending, started, stationIndex])

  useEffect(() => {
    if (!active || !playing || !segment || !currentPart.length) return
    const timer = window.setTimeout(() => {
      if (pointIndex < currentPart.length - 1) {
        setPointIndex(pointIndex + 1)
        return
      }
      if (partIndex < segment.parts.length - 1) {
        setPlaying(false)
        setNextPartPending(true)
        return
      }
      const followingStation = Math.min(stationIndex + 1, stations.length - 1)
      setStationIndex(followingStation)
      setPointIndex(0)
      setPartIndex(0)
      if (followingStation >= stations.length - 1 || !stationSegments[followingStation]?.parts.length) setPlaying(false)
    }, 650)
    return () => window.clearTimeout(timer)
  }, [active, playing, pointIndex, partIndex, currentPart, segment, stationIndex, stationSegments, stations.length])

  const chooseStation = (next: number) => {
    setPlaying(false)
    setStarted(true)
    setStationIndex(Math.max(0, Math.min(next, stations.length - 1)))
    setPointIndex(0)
    setPartIndex(0)
    setNextPartPending(false)
  }

  const togglePlayback = () => {
    if (playing) {
      setPlaying(false)
      return
    }
    if (nextPartPending) {
      setPartIndex(partIndex + 1)
      setPointIndex(0)
      setNextPartPending(false)
      setPlaying(true)
      return
    }
    let startIndex = stationIndex
    if (stationIndex >= stations.length - 1) startIndex = 0
    if (!stationSegments[startIndex]?.parts.length) return
    setStationIndex(startIndex)
    setPointIndex(0)
    setPartIndex(0)
    setStarted(true)
    setPlaying(true)
  }

  if (!stations.length) {
    return (
      <section data-testid="route-playback" className="rounded-2xl border border-sky-900/10 bg-white/90 p-4" aria-label="路线预演">
        <p className="text-xs font-semibold tracking-[0.12em] text-[#0c789d]">计划路线模拟</p>
        <p className="mt-2 text-sm leading-6 text-slate-600">暂无地点</p>
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
            {segment ? ` · ${segment.summary}` : ''}
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
            disabled={!segment?.parts.length}
            onClick={togglePlayback}
            className="inline-flex min-h-11 items-center gap-2 rounded-xl bg-[#0c789d] px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {playing ? <Pause className="h-4 w-4" aria-hidden="true" /> : <Play className="h-4 w-4" aria-hidden="true" />}
            {playing ? '暂停' : nextPartPending ? '播放下一片段' : '播放'}
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
      {nextPartPending && <p className="mt-3 rounded-xl bg-sky-50 p-3 text-sm text-slate-600">本片段已结束，下一片段与此处尚未衔接。选择播放下一片段后继续。</p>}
      {!segment?.parts.length && stationIndex < stations.length - 1 && (
        <p className="mt-3 rounded-xl bg-sky-50 p-3 text-sm text-slate-600">
          地图动画不可用
        </p>
      )}

    </section>
  )
}
