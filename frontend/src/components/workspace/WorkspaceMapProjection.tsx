'use client'

import { useEffect, useRef, useState } from 'react'
import { CircleHelp, MapPin } from 'lucide-react'

import type { WorkspaceMapProjection } from '@/types/workspace'

const DAY_COLORS = ['#2563eb', '#059669', '#d97706', '#7c3aed', '#db2777']

interface WorkspaceMapProjectionProps {
  projection: WorkspaceMapProjection | null
  selectedStopId: string | null
  onSelectStop: (stopId: string) => void
}

/**
 * A deliberately small workspace-specific AMap view.
 *
 * It is not allowed to geocode names, substitute a city centre or ask AMap to
 * route a revised itinerary.  Markers and links are drawn solely from the
 * persisted canonical projection returned by the server.
 */
export default function WorkspaceMapProjection({
  projection, selectedStopId, onSelectStop,
}: WorkspaceMapProjectionProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<any>(null)
  const markersRef = useRef<Map<string, any>>(new Map())
  const overlaysRef = useRef<any[]>([])
  const [mapState, setMapState] = useState<'loading' | 'ready' | 'unavailable'>('loading')

  const canRender = Boolean(projection && projection.stops.length > 0)

  useEffect(() => {
    if (!canRender || !projection || !containerRef.current) {
      return
    }
    const jsKey = process.env.NEXT_PUBLIC_AMAP_JS_KEY
    if (!jsKey) {
      setMapState('unavailable')
      return
    }
    let cancelled = false
    setMapState('loading')
    ;(async () => {
      try {
        const securityCode = process.env.NEXT_PUBLIC_AMAP_SECURITY_CODE
        if (securityCode) window._AMapSecurityConfig = { securityJsCode: securityCode }
        const AMapLoader = (await import('@amap/amap-jsapi-loader')).default
        const AMap = await AMapLoader.load({ key: jsKey, version: '2.0' })
        if (cancelled || !containerRef.current) return
        const first = projection.stops[0]
        mapRef.current = new AMap.Map(containerRef.current, {
          zoom: 13,
          center: [first.coords.lng, first.coords.lat],
          mapStyle: 'amap://styles/macaron',
          viewMode: '2D',
        })
        setMapState('ready')
      } catch (error) {
        console.error('[WorkspaceMapProjection] AMap initialization failed', error)
        if (!cancelled) setMapState('unavailable')
      }
    })()
    return () => {
      cancelled = true
      markersRef.current.clear()
      overlaysRef.current = []
      mapRef.current?.destroy?.()
      mapRef.current = null
    }
  }, [canRender, projection?.revision])

  useEffect(() => {
    const map = mapRef.current
    const AMap = window.AMap as any
    if (!map || !AMap || !projection) return

    markersRef.current.forEach(marker => marker.setMap(null))
    markersRef.current.clear()
    if (overlaysRef.current.length) map.remove(overlaysRef.current)
    overlaysRef.current = []

    const byId = new Map(projection.stops.map(stop => [stop.stop_id, stop]))
    projection.coordinate_links.forEach(link => {
      const from = byId.get(link.from_stop_id)
      const to = byId.get(link.to_stop_id)
      // The server already performs this check.  Keep the renderer equally
      // strict so a malformed response cannot create a guessed point.
      if (!from || !to) return
      const line = new AMap.Polyline({
        path: [[from.coords.lng, from.coords.lat], [to.coords.lng, to.coords.lat]],
        strokeColor: DAY_COLORS[link.day_index % DAY_COLORS.length],
        strokeWeight: 5,
        strokeOpacity: 0.8,
        strokeStyle: 'dashed',
        lineJoin: 'round',
        zIndex: 10 + link.day_index,
      })
      line.setMap(map)
      overlaysRef.current.push(line)
    })

    projection.stops.forEach(stop => {
      const selected = stop.stop_id === selectedStopId
      const color = DAY_COLORS[stop.day_index % DAY_COLORS.length]
      const marker = new AMap.Marker({
        position: [stop.coords.lng, stop.coords.lat],
        anchor: 'bottom-center',
        zIndex: selected ? 200 : 100,
        content: markerContent(stop.name, stop.day_index, stop.order_index, color, selected),
      })
      marker.on('click', () => onSelectStop(stop.stop_id))
      marker.setMap(map)
      markersRef.current.set(stop.stop_id, marker)
    })
    if (markersRef.current.size) map.setFitView([...markersRef.current.values()], false, [32, 32, 32, 32])
  }, [projection, selectedStopId, onSelectStop])

  useEffect(() => {
    if (!selectedStopId || !mapRef.current) return
    const marker = markersRef.current.get(selectedStopId)
    if (marker) mapRef.current.panTo(marker.getPosition())
  }, [selectedStopId])

  if (!projection || projection.status === 'UNAVAILABLE') {
    return <UnavailableMap reason={projection?.unavailable_reason ?? 'MAP_PROJECTION_LOADING'} />
  }
  return (
    <div>
      <div className="relative mt-3 h-64 overflow-hidden rounded-xl border border-slate-200 bg-slate-100">
        <div ref={containerRef} className="h-full w-full" />
        {mapState === 'loading' && <div className="absolute inset-0 grid place-items-center bg-slate-100/90 text-xs text-slate-500">正在加载权威坐标投影…</div>}
        {mapState === 'unavailable' && <div className="absolute inset-0 grid place-items-center bg-amber-50 p-4 text-center text-xs text-amber-800">地图 JS Key 未配置或不可用；未渲染任何替代坐标。</div>}
      </div>
      <p className="mt-2 text-[11px] text-slate-500">
        虚线是已存储坐标的几何连线，不代表驾车、步行或公共交通路线。
      </p>
      {projection.status === 'PARTIAL' && (
        <p className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800">
          {projection.missing_stop_ids.length} 个行程点没有权威坐标，未显示标记或连线。
        </p>
      )}
    </div>
  )
}

function UnavailableMap({ reason }: { reason: string }) {
  return (
    <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
      <p className="flex items-center gap-1 font-medium"><CircleHelp className="h-3.5 w-3.5" />地图投影不可用</p>
      <p className="mt-1">当前版本没有可核验的地点坐标（{reason}）；未回填城市中心、地名搜索或猜测位置。</p>
    </div>
  )
}

function markerContent(name: string, dayIndex: number, orderIndex: number, color: string, selected: boolean) {
  return `<div style="display:flex;flex-direction:column;align-items:center;transform:scale(${selected ? 1.18 : 1});transform-origin:bottom center">
    <div style="width:28px;height:28px;border-radius:50% 50% 50% 0;background:${color};color:white;transform:rotate(-45deg);border:2px solid white;box-shadow:0 2px 6px rgba(15,23,42,.28);display:grid;place-items:center">
      <span style="transform:rotate(45deg);font-size:11px;font-weight:700">${dayIndex + 1}-${orderIndex + 1}</span>
    </div>
    <span style="margin-top:3px;max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;border-radius:4px;background:white;padding:1px 4px;font-size:10px;color:#334155;box-shadow:0 1px 3px rgba(15,23,42,.16)">${escapeHtml(name)}</span>
  </div>`
}

function escapeHtml(value: string) {
  return value.replace(/[&<>'"]/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[character] ?? character))
}

declare global {
  interface Window {
    _AMapSecurityConfig?: { securityJsCode: string }
    AMap?: unknown
  }
}
