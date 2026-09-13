'use client'

import { useCallback, useEffect, useState } from 'react'
import { List, Search } from 'lucide-react'
import './map-workspace.css'

import type {
  MapRenderView,
  StaySuggestionView,
  UserFacingTripResult,
} from '@/lib/trip-understanding-v3'
import RouteMap from './route-map'
import PendingPlaceDropdown from './pending-place-dropdown'
import type { WorkspaceCommandResult } from './itinerary-workspace'
import RoutePlayback from './route-playback'
import { DAY_COLORS, relativeDayLabel } from './result-presentation'
import StayCandidates from './stay-candidates'

type GeometryPoint = { longitude: number; latitude: number }

export default function MapStayWorkspace({
  active,
  result,
  pendingDays = [],
  mapView,
  stay,
  dayIndex,
  selected,
  routeMode,
  disabled,
  onDayChange,
  onSelect,
  onRouteMode,
  onRetryMap,
  onSelectStay,
  onRefreshStay,
  resource,
  onCommand,
}: {
  active: boolean
  result: UserFacingTripResult
  pendingDays?: UserFacingTripResult['days']
  mapView: MapRenderView | null
  stay: StaySuggestionView | null
  dayIndex: number
  selected: string | null
  routeMode: 'recommended' | 'walking' | 'transit'
  disabled: boolean
  onDayChange: (index: number) => void
  onSelect: (token: string) => void
  onRouteMode: (mode: 'recommended' | 'walking' | 'transit') => void
  onRetryMap: () => void
  onSelectStay: (token: string) => void
  onRefreshStay: () => void
  resource: string
  onCommand: (command: import('@/lib/trip-understanding-v3').TripUnderstandingCommand) => Promise<WorkspaceCommandResult>
}) {
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('all')
  const [status, setStatus] = useState('all')
  const [pendingToken, setPendingToken] = useState<string | null>(null)
  const [editing,setEditing]=useState(false)
  useEffect(()=>setEditing(false),[selected,active])
  const currentDay = result.days[dayIndex]
  const [mapScope, setMapScope] = useState<'all' | 'day'>('all')
  useEffect(() => setMapScope('all'), [resource])
  const [directoryOpen, setDirectoryOpen] = useState(false)
  const [simulationPosition, setSimulationPosition] = useState<GeometryPoint | null>(null)
  const updateSimulationPosition = useCallback(
    (point: GeometryPoint | null) => setSimulationPosition(point),
    [],
  )
  const currentStay = stay || result.stay
  const directoryDays = result.days.map((day, index) => ({...day, activities: [...day.activities, ...(pendingDays[index]?.activities || [])]}))
  const categories = [...new Set(directoryDays.flatMap(day => day.activities.map(card => card.category)))]
  const listedDays = directoryDays.map((day, index) => ({...day, index, activities: day.activities.filter(card =>
    (mapScope === 'all' || index === dayIndex) && (!query.trim() || card.name.includes(query.trim())) &&
    (category === 'all' || card.category === category) && (status === 'all' || card.status === status))}))
  const listedCount = listedDays.reduce((sum, day) => sum + day.activities.length, 0)
  const mapUnavailable = (mapView?.status || result.map.status) === 'UNAVAILABLE'
  const dayColor = DAY_COLORS[dayIndex % DAY_COLORS.length]
  const currentRoutes = mapView && ['AVAILABLE', 'LIMITED'].includes(mapView.status)
    ? mapView.days.flatMap((day) => {
        const index = result.days.findIndex((item) => item.label === day.label)
        return index < 0 || (mapScope === 'day' && index !== dayIndex) ? [] : day.routes.map((route) => ({...route, color:DAY_COLORS[index % DAY_COLORS.length]}))
      })
    : []

  const selectCard = (token: string) => {
    setPendingToken(null)
    const index = result.days.findIndex(day => day.activities.some(card => card.activity_token === token))
    if (index >= 0 && index !== dayIndex) onDayChange(index)
    onSelect(token)
  }

  useEffect(() => {
    setDirectoryOpen(window.matchMedia('(min-width: 1024px)').matches)
  }, [])

  return (
    <section data-testid="map-theater" data-map-status={mapView?.status || result.map.status} id="map-stay-view" aria-label="地图" className="fluid-map-workspace">
      <aside id="map-place-directory" data-testid="map-place-directory" data-open={directoryOpen} className="map-place-directory" aria-label="本行程地点列表">
        <div className="map-directory-heading"><h2>地点列表</h2><button className="e-button e-button-quiet map-directory-close" type="button" onClick={() => setDirectoryOpen(false)}>收起</button></div>
        <div className="map-directory-scopes" aria-label="地点列表范围">
          <button type="button" aria-pressed={mapScope === 'all'} onClick={() => setMapScope('all')}>全部 {directoryDays.reduce((n, day) => n + day.activities.length, 0)}</button>
          {result.days.map((day, index) => <button key={index} type="button" aria-pressed={mapScope === 'day' && index === dayIndex} onClick={() => {setMapScope('day'); onDayChange(index); setSimulationPosition(null)}}>{relativeDayLabel(index)}</button>)}
        </div>
        <label className="map-directory-search"><Search size={16} aria-hidden="true"/><input aria-label="搜索本行程地点" placeholder="搜索本行程的地点…" value={query} onChange={event => setQuery(event.target.value)} type="search"/></label>
        <div className="map-directory-filters">
          <label>类别<select aria-label="地点类别" value={category} onChange={event => setCategory(event.target.value)}><option value="all">全部类别</option>{categories.map(item => <option key={item} value={item}>{item}</option>)}</select></label>
          <label>状态<select aria-label="地点确认状态" value={status} onChange={event => setStatus(event.target.value)}><option value="all">全部状态</option><option value="READY">已确认</option><option value="NEEDS_CONFIRMATION">待确认</option></select></label>
        </div>
        <p className="map-directory-count" role="status">{listedCount ? `显示 ${listedCount} 个地点` : '没有符合筛选条件的地点。'}</p>
        <div className="map-directory-days">{listedDays.map(day => day.activities.length > 0 && <section key={day.index}>
          <h3><span style={{background: DAY_COLORS[day.index % DAY_COLORS.length]}}/>{relativeDayLabel(day.index)} <small>{day.activities.length} 个地点</small></h3>
          {day.activities.map(card => <div key={card.activity_token}>
            <button data-testid="map-directory-place" data-day-index={day.index} data-confirmation={card.status} type="button" aria-pressed={selected === card.activity_token || pendingToken === card.activity_token}
              onClick={() => {onDayChange(day.index); if (card.status === 'READY') {setPendingToken(null); onSelect(card.activity_token)} else {setPendingToken(card.activity_token)} }}>
              <span className="map-directory-number" style={{backgroundColor: DAY_COLORS[day.index % DAY_COLORS.length]}}>{card.status === 'READY' ? result.days[day.index].activities.indexOf(card) + 1 : '?'}</span>
              <span><strong>{card.name}</strong><small>{card.category} · {card.status === 'READY' ? '已确认' : '待确认'}</small></span>
            </button>
            {pendingToken === card.activity_token && <PendingPlaceDropdown card={card} resource={resource} disabled={disabled} onCommand={onCommand} onClose={() => setPendingToken(null)}/>}
          </div>)}
        </section>)}</div>
        <p className="map-directory-note">待确认地点只列在这里，确认后才加入地图。</p>
      </aside>
      <div className="fluid-map-stage">
        <button data-testid="map-directory-toggle" type="button" aria-expanded={directoryOpen} aria-controls="map-place-directory" onClick={() => setDirectoryOpen(value => !value)} className="map-directory-toggle e-button"><List size={16} aria-hidden="true"/>{directoryOpen ? '收起地点' : '查看地点'}</button>
          <RouteMap
            view={mapView}
            day={currentDay}
            days={mapScope === 'all' ? result.days : undefined}
            selected={selected}
            onSelect={selectCard}
            mode={routeMode}
            visible={active}
            focusSelected
            simulationPosition={simulationPosition}
            dayColor={dayColor}
          />

        {selected && currentDay?.activities.some(card => card.activity_token === selected) && <div className="fluid-map-place-edit"><button type="button" className="e-button" aria-expanded={editing} onClick={()=>setEditing(value=>!value)}>地点</button>{editing&&<PendingPlaceDropdown key={selected} card={currentDay.activities.find(card=>card.activity_token===selected)!} resource={resource} disabled={disabled} onCommand={onCommand} onClose={()=>{setEditing(false);requestAnimationFrame(()=>document.querySelector<HTMLButtonElement>('.fluid-map-place-edit > button')?.focus({preventScroll:true}))}}/>}</div>}
      </div>
      <div id="map-suggestions-slot" className="map-suggestions-slot"/>
      <div className="fluid-map-bottom">
        <div className="fluid-day-legend" aria-label="日期颜色与预演选择">
          <button type="button" className="e-button e-button-quiet" aria-pressed={mapScope === 'all'}
            onClick={() => { setMapScope('all'); setSimulationPosition(null) }}>全部行程</button>
          {result.days.map((day,index) => <button key={day.label} type="button" className="e-button e-button-quiet"
            aria-pressed={mapScope === 'day' && index === dayIndex}
            onClick={() => { setMapScope('day'); setSimulationPosition(null); onDayChange(index) }}><span className="fluid-day-dot" style={{backgroundColor:DAY_COLORS[index % DAY_COLORS.length]}} />{relativeDayLabel(index)}</button>)}
        </div>
        <div className="fluid-map-tools">
          <details className="fluid-map-popover" name={`map-tools-${resource}`}><summary>路线</summary><div className="fluid-map-popover-content" data-testid="map-route-tools">
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


          </div></details>
          <details className="fluid-map-popover" name={`map-tools-${resource}`} data-testid="stay-panel"><summary>住宿</summary><div className="fluid-map-popover-content" aria-label="住宿建议">
            <button data-testid="retry-stay" type="button" className="e-button" disabled={disabled || currentStay.status === 'PREPARING'} onClick={onRefreshStay}>{currentStay.status === 'PREPARING' ? '正在准备住宿…' : '更新住宿建议'}</button>
                      <p className="mt-2 text-sm leading-6 text-slate-600">{currentStay.message}</p>
          {currentStay.area_summary && <p className="mt-2 rounded-xl bg-sky-50 p-3 text-sm text-slate-700">{currentStay.area_summary}</p>}
          <StayCandidates stay={currentStay} days={result.days} disabled={disabled} onSelect={onSelectStay}/>

          </div></details>
        </div>
      </div>
    </section>
  )
}
