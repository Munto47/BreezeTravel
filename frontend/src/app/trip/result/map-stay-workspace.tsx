'use client'

import { useCallback, useEffect, useState } from 'react'
import { ArrowUpRight, List, RefreshCw } from 'lucide-react'

import type {
  MapRenderView,
  StaySuggestionView,
  UserFacingTripResult,
} from '@/lib/trip-understanding-v3'
import RouteMap from './route-map'
import RoutePlayback from './route-playback'
import { DAY_COLORS } from './result-presentation'

type GeometryPoint = { longitude: number; latitude: number }

export default function MapStayWorkspace({
  active,
  result,
  mapView,
  stay,
  dayIndex,
  selected,
  routeMode,
  disabled,
  onDayChange,
  onSelect,
  onRouteMode,
  onRender,
  onRetryMap,
  onSelectStay,
  onEdit,
}: {
  active: boolean
  result: UserFacingTripResult
  mapView: MapRenderView | null
  stay: StaySuggestionView | null
  dayIndex: number
  selected: string | null
  routeMode: 'recommended' | 'walking' | 'transit'
  disabled: boolean
  onDayChange: (index: number) => void
  onSelect: (token: string) => void
  onRouteMode: (mode: 'recommended' | 'walking' | 'transit') => void
  onRender: () => void
  onRetryMap: () => void
  onSelectStay: (token: string) => void
  onEdit: (card: UserFacingTripResult['days'][number]['activities'][number]) => void
}) {
  const currentDay = result.days[dayIndex]
  const [directoryOpen, setDirectoryOpen] = useState(false)
  const [simulationPosition, setSimulationPosition] = useState<GeometryPoint | null>(null)
  const updateSimulationPosition = useCallback(
    (point: GeometryPoint | null) => setSimulationPosition(point),
    [],
  )
  const currentStay = stay || result.stay
  const mapUnavailable = (mapView?.status || result.map.status) === 'UNAVAILABLE'
  const stayUnavailable = currentStay.status === 'UNAVAILABLE'
  const canRender = mapView?.available_actions.includes('RENDER_MAP') ?? false
  const dayColor = DAY_COLORS[dayIndex % DAY_COLORS.length]
  const currentRoutes = mapView && ['AVAILABLE', 'LIMITED'].includes(mapView.status)
    ? mapView.days.flatMap((day) => {
        const index = result.days.findIndex((item) => item.label === day.label)
        return index < 0 ? [] : day.routes.map((route) => ({...route, color:DAY_COLORS[index % DAY_COLORS.length]}))
      })
    : []

  const selectCard = (token: string) => {
    const index = result.days.findIndex(day => day.activities.some(card => card.activity_token === token))
    if (index >= 0 && index !== dayIndex) onDayChange(index)
    onSelect(token)
  }

  useEffect(() => {
    setDirectoryOpen(window.matchMedia('(min-width: 1024px)').matches)
  }, [])

  return (
    <section data-testid="map-theater" id="map-stay-view" aria-label="地图与住宿" className="mx-auto grid max-w-[1500px] gap-5 px-4 pb-28 pt-6 lg:px-8 lg:pb-10 lg:pl-24">
      <div className="min-w-0 space-y-4">
        <header className="flex flex-wrap items-end justify-between gap-4 rounded-2xl border border-sky-900/10 bg-white/80 p-4 backdrop-blur">
          <div>
<h2 className="text-lg font-semibold text-slate-800">全程地图</h2>
            <span className="text-xs text-slate-500">{({PREPARING:'路线准备中', AVAILABLE:'路线已准备', NEEDS_UPDATE:'路线需要更新', LIMITED:'部分路线可用', UNAVAILABLE:'路线暂不可用'})[mapView?.status || result.map.status]}</span>
          </div>
          {canRender && (
            <button
              data-testid="render-map"
              type="button"
              disabled={disabled || mapView?.status === 'PREPARING'}
              onClick={onRender}
              className="inline-flex min-h-12 items-center gap-2 rounded-xl bg-[#0c789d] px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] focus-visible:ring-offset-2 disabled:opacity-50"
            >
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
              {mapView?.status === 'PREPARING' ? '路线准备中' : '手动更新路线'}
            </button>
          )}
        </header>

        {(mapUnavailable || stayUnavailable) && (
          <div
            data-testid="enhancement-read-recovery"
            className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-sky-900/10 bg-sky-50/80 p-4 text-sm text-slate-700"
          >
            <button
              data-testid="retry-enhancements"
              type="button"
              disabled={disabled}
              onClick={onRetryMap}
              className="inline-flex min-h-11 items-center gap-2 rounded-xl border border-[#0c789d]/20 bg-white px-3 font-semibold text-[#0c789d] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] disabled:opacity-50"
            >
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
              {mapUnavailable && stayUnavailable
                ? '重新读取路线与住宿'
                : mapUnavailable
                  ? '重新读取路线'
                  : '重试住宿'}
            </button>
          </div>
        )}

        <div className="flex gap-2 overflow-x-auto pb-1" aria-label="日期颜色与预演选择">
          {result.days.map((day, index) => (
            <button
              key={`${day.label}-${index}`}
              type="button"
              aria-pressed={index === dayIndex}
              onClick={() => onDayChange(index)}
              className={`min-h-11 shrink-0 rounded-xl px-4 text-sm font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] ${index === dayIndex ? 'bg-white text-slate-900 ring-1 ring-sky-200' : 'border border-sky-900/10 bg-white/80 text-slate-700'}`}
            >
<span className="mr-2 inline-block h-2.5 w-2.5 rounded-full" style={{backgroundColor:DAY_COLORS[index % DAY_COLORS.length]}} />{day.label}
            </button>
          ))}
        </div>

        <div className="relative overflow-hidden rounded-[1.75rem] border border-sky-900/10 bg-white shadow-[0_24px_65px_-38px_rgba(12,120,157,0.55)]">
          <button
            data-testid="map-directory-toggle"
            type="button"
            aria-expanded={directoryOpen}
            aria-controls="map-place-directory"
            onClick={() => setDirectoryOpen((value) => !value)}
            className="absolute left-4 top-4 z-20 inline-flex min-h-11 items-center gap-2 rounded-xl border border-white/70 bg-white/90 px-3 text-sm font-semibold text-[#0c789d] shadow-md backdrop-blur focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d]"
          >
            <List className="h-4 w-4" aria-hidden="true" />
            {directoryOpen ? '收起地点' : '查看地点'}
          </button>
          <div
            id="map-place-directory"
            data-testid="map-place-directory"
            hidden={!directoryOpen}
            className="absolute bottom-4 left-4 top-[4.25rem] z-10 w-[min(14rem,calc(100%-2rem))] overflow-y-auto rounded-2xl border border-white/70 bg-white/90 p-2 shadow-lg backdrop-blur"
          >
            <p className="px-2 py-2 text-xs font-semibold tracking-[0.12em] text-slate-500">全部地点</p>
            {result.days.flatMap((listedDay, listedDayIndex) => listedDay.activities.map((card, index) => (
              <button
                key={card.activity_token}
                data-day-index={listedDayIndex}
                type="button"
                onClick={() => { onDayChange(listedDayIndex); onSelect(card.activity_token) }}
                className={`flex min-h-11 w-full items-center gap-2 rounded-xl px-2 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] ${selected === card.activity_token ? 'bg-sky-100 text-sky-950' : 'hover:bg-slate-50'}`}
              >
                <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-bold text-white" style={{ backgroundColor: DAY_COLORS[listedDayIndex % DAY_COLORS.length] }}>{index + 1}</span>
                <span className="truncate">{card.name}</span>
              </button>
            )))}
          </div>
          <RouteMap
            view={mapView}
            day={currentDay}
            days={result.days}
            selected={selected}
            onSelect={selectCard}
            mode={routeMode}
            visible={active}
            focusSelected
            simulationPosition={simulationPosition}
            dayColor={dayColor}
          />
        </div>

        {!!currentRoutes.length && (
          <details open={mapUnavailable || undefined} className="grid gap-2" aria-label="路线文字摘要"><summary className="min-h-11 cursor-pointer text-sm text-slate-600">路线摘要</summary>
            {currentRoutes.map((route, index) => {
              const selectedMode = routeMode === 'recommended' ? route.selected_mode : routeMode
              const selectedRoute = selectedMode ? route[selectedMode] : null
              const geometryCount = selectedRoute?.status === 'AVAILABLE' ? selectedRoute.geometry.length : 0
              return (
                <article
                  key={`${route.from_activity_token || route.from_name}-${route.to_activity_token || route.to_name}-${index}`}
                  data-testid="map-route-summary"
                  data-verified-geometry-count={geometryCount}
                  className="rounded-2xl border border-sky-900/10 bg-white/85 p-4 text-sm text-slate-700"
                >
                  <div className="flex items-center gap-3">
                    {geometryCount >= 2 && (
                      <svg width="38" height="14" viewBox="0 0 38 14" aria-hidden="true" className="shrink-0">
                        <path data-testid="map-route-line" d="M2 11 Q19 1 36 11" fill="none" stroke={route.color} strokeWidth="3" strokeLinecap="round" />
                      </svg>
                    )}
                    <strong className="text-slate-900">{route.from_name} → {route.to_name}</strong>
                  </div>
                  <p className="mt-1 text-xs text-slate-500">
                    {selectedMode && selectedRoute?.status === 'AVAILABLE'
                      ? `${selectedMode === 'walking' ? '步行' : '公交'}${selectedRoute.duration_minutes == null ? '' : ` ${selectedRoute.duration_minutes} 分钟`}`
                      : '路线暂不可用'}
                  </p>
                </article>
              )
            })}
          </details>
        )}

        <div className="flex flex-wrap gap-2" aria-label="路线方式">
          {(['recommended', 'walking', 'transit'] as const).map((mode) => (
            <button
              data-testid={`map-mode-${mode}`}
              type="button"
              key={mode}
              aria-pressed={routeMode === mode}
              onClick={() => onRouteMode(mode)}
              className={`min-h-11 rounded-xl px-4 text-sm font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] ${routeMode === mode ? 'bg-sky-100 text-sky-950' : 'border border-sky-900/10 bg-white text-slate-600'}`}
            >
              {mode === 'recommended' ? '推荐方式' : mode === 'walking' ? '步行' : '公交'}
            </button>
          ))}
        </div>

        <RoutePlayback active={active} view={mapView} day={currentDay} mode={routeMode} onPosition={updateSimulationPosition} />

        <section className="rounded-[1.75rem] border border-sky-900/10 bg-white/85 p-4 shadow-sm" aria-label="全部日期横链">
          <div className="flex snap-x gap-3 overflow-x-auto pb-2">
            {result.days.flatMap((stripDay, stripDayIndex) => stripDay.activities.map((card, index) => (
              <button
                key={card.activity_token}
                type="button"
                onClick={() => selectCard(card.activity_token)}
                className={`min-h-28 w-[min(72vw,13rem)] shrink-0 snap-start rounded-2xl border p-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] ${selected === card.activity_token ? 'border-[#0c789d] bg-sky-50' : 'border-slate-200 bg-white'}`}
              >
                <span className="text-xs" style={{color:DAY_COLORS[stripDayIndex % DAY_COLORS.length]}}>{stripDay.label} · {index + 1}</span>
                <strong className="mt-2 block text-sm text-slate-900">{card.name}</strong>
                <span className="mt-2 block text-xs text-[#0c789d]">{card.status === 'READY' ? '已确认' : '待确认'}</span>
              </button>
            )))}
          </div>
          {currentDay?.activities.find((card) => card.activity_token === selected) && (
            <button
              type="button"
              className="mt-2 inline-flex min-h-11 items-center gap-2 text-sm font-semibold text-[#0c789d]"
              onClick={() => onEdit(currentDay.activities.find((card) => card.activity_token === selected)!)}
            >
              地点详情与编辑 <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
            </button>
          )}
        </section>
      </div>

      <aside data-testid="stay-panel" className="space-y-4 lg:sticky lg:top-24" aria-label="住宿建议">
        <details open={currentStay.candidates.length > 0 || undefined} className="rounded-[1.75rem] bg-white/90 p-5">
          <summary className="min-h-11 cursor-pointer text-sm font-semibold text-[#0c789d]">住宿</summary>
          <p className="mt-2 text-sm leading-6 text-slate-600">{currentStay.message}</p>
          {currentStay.area_summary && <p className="mt-2 rounded-xl bg-sky-50 p-3 text-sm text-slate-700">{currentStay.area_summary}</p>}
          <div className="mt-4 space-y-3">
            {currentStay.candidates.map((candidate) => (
              <article key={candidate.candidate_token} className="rounded-2xl border border-slate-200 p-4">
                <h3 className="text-sm font-semibold text-slate-900">{candidate.name}</h3>
                <p className="mt-1 text-xs leading-5 text-slate-500">{candidate.area_or_address}</p>
                <p className="mt-2 text-xs leading-5 text-slate-600">{candidate.commute_summary}</p>
                <button
                  data-testid="choose-stay"
                  type="button"
                  disabled={disabled || candidate.selected}
                  onClick={() => onSelectStay(candidate.candidate_token)}
                  className="mt-3 min-h-12 w-full rounded-xl border border-[#0c789d]/20 bg-sky-50 text-sm font-semibold text-[#0c789d] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] disabled:opacity-50"
                >
                  {candidate.selected ? '已选择' : '选择这家住宿'}
                </button>
              </article>
            ))}
          </div>
        </details>
      </aside>
    </section>
  )
}
