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
  const mapMessage = mapView?.message || result.map.message
  const mapUnavailable = (mapView?.status || result.map.status) === 'UNAVAILABLE'
  const stayUnavailable = currentStay.status === 'UNAVAILABLE'
  const canRender = mapView?.available_actions.includes('RENDER_MAP') ?? false
  const dayColor = DAY_COLORS[dayIndex % DAY_COLORS.length]
  const currentRoutes = mapView && ['AVAILABLE', 'LIMITED'].includes(mapView.status)
    ? mapView.days.find((day) => day.label === currentDay?.label)?.routes || []
    : []

  useEffect(() => {
    setDirectoryOpen(window.matchMedia('(min-width: 1024px)').matches)
  }, [])

  return (
    <section data-testid="map-theater" id="map-stay-view" aria-label="地图与住宿" className="mx-auto grid max-w-[1500px] gap-5 px-4 pb-28 pt-6 lg:grid-cols-[minmax(0,1fr)_19rem] lg:px-8 lg:pb-10 lg:pl-24">
      <div className="min-w-0 space-y-4">
        <header className="flex flex-wrap items-end justify-between gap-4 rounded-2xl border border-sky-900/10 bg-white/80 p-4 backdrop-blur">
          <div>
            <p className="text-xs font-semibold tracking-[0.14em] text-[#0c789d]">地图与住宿</p>
            <h2 className="mt-1 text-2xl font-semibold text-slate-900">当天路线一眼看清</h2>
            <p className="mt-1 text-sm text-slate-600">{mapMessage}</p>
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
            <p>
              {mapUnavailable && stayUnavailable
                ? '路线和住宿暂时未读取完整，行程卡片仍可继续使用。'
                : mapUnavailable
                  ? '路线暂时未读取完整，行程卡片仍可继续使用。'
                  : '住宿建议暂时未读取完整，不影响查看行程和路线。'}
            </p>
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
                  : '重新读取住宿'}
            </button>
          </div>
        )}

        <div className="flex gap-2 overflow-x-auto pb-1" aria-label="选择地图日期">
          {result.days.map((day, index) => (
            <button
              key={`${day.label}-${index}`}
              type="button"
              aria-pressed={index === dayIndex}
              onClick={() => onDayChange(index)}
              className={`min-h-11 shrink-0 rounded-xl px-4 text-sm font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] ${index === dayIndex ? 'bg-[#0c789d] text-white' : 'border border-sky-900/10 bg-white/80 text-slate-700'}`}
            >
              {day.label}
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
            <p className="px-2 py-2 text-xs font-semibold tracking-[0.12em] text-slate-500">当天地点</p>
            {currentDay?.activities.map((card, index) => (
              <button
                key={card.activity_token}
                data-day-index={dayIndex}
                type="button"
                onClick={() => onSelect(card.activity_token)}
                className={`flex min-h-11 w-full items-center gap-2 rounded-xl px-2 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] ${selected === card.activity_token ? 'bg-sky-100 text-sky-950' : 'hover:bg-slate-50'}`}
              >
                <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-bold text-white" style={{ backgroundColor: dayColor }}>{index + 1}</span>
                <span className="truncate">{card.name}</span>
              </button>
            ))}
          </div>
          <RouteMap
            view={mapView}
            day={currentDay}
            selected={selected}
            onSelect={onSelect}
            mode={routeMode}
            visible={active}
            focusSelected
            simulationPosition={simulationPosition}
            dayColor={dayColor}
          />
        </div>

        {!!currentRoutes.length && (
          <div className="grid gap-2" aria-label="当天路线文字摘要">
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
                        <path data-testid="map-route-line" d="M2 11 Q19 1 36 11" fill="none" stroke={dayColor} strokeWidth="3" strokeLinecap="round" />
                      </svg>
                    )}
                    <strong className="text-slate-900">{route.from_name} → {route.to_name}</strong>
                  </div>
                  <p className="mt-1">{route.message}</p>
                  <p className="mt-1 text-xs text-slate-500">
                    {selectedMode && selectedRoute?.status === 'AVAILABLE'
                      ? `${selectedMode === 'walking' ? '步行' : '公交'}${selectedRoute.duration_minutes == null ? '' : ` ${selectedRoute.duration_minutes} 分钟`}`
                      : '路线暂不可用'}
                  </p>
                </article>
              )
            })}
          </div>
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

        <section className="rounded-[1.75rem] border border-sky-900/10 bg-white/85 p-4 shadow-sm" aria-label={`${currentDay?.label || ''} 横链`}>
          <div className="flex snap-x gap-3 overflow-x-auto pb-2">
            {currentDay?.activities.map((card, index) => (
              <button
                key={card.activity_token}
                type="button"
                onClick={() => onSelect(card.activity_token)}
                className={`min-h-28 w-[min(72vw,13rem)] shrink-0 snap-start rounded-2xl border p-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] ${selected === card.activity_token ? 'border-[#0c789d] bg-sky-50' : 'border-slate-200 bg-white'}`}
              >
                <span className="text-xs text-slate-500">{index + 1} · {card.time_hint || '时间待定'}</span>
                <strong className="mt-2 block text-sm text-slate-900">{card.name}</strong>
                <span className="mt-2 block text-xs text-[#0c789d]">{card.status === 'READY' ? '已确认' : '待确认'}</span>
              </button>
            ))}
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
        <section className="rounded-[1.75rem] border border-sky-900/10 bg-white/90 p-5 shadow-sm">
          <p className="text-xs font-semibold tracking-[0.12em] text-[#0c789d]">住宿建议</p>
          <h2 className="mt-1 text-xl font-semibold text-slate-900">住在哪里更顺路</h2>
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
        </section>
      </aside>
    </section>
  )
}
