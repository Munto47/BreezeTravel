'use client'

import { useEffect, useRef, useCallback, useState } from 'react'
import type { YjsPlace } from '@/types/room'
import type { Itinerary } from '@/types/itinerary'
import { useRoomStore } from '@/stores/roomStore'

interface AMapContainerProps {
  places: YjsPlace[]
  itinerary?: Itinerary | null
  tripCity?: string
}

const CLUSTER_COLORS = ['#0C789D', '#5C7CFA', '#10B981', '#B7791F', '#8B5CF6', '#06B6D4']

const CATEGORY_ICON: Record<string, string> = {
  attraction: '🏛',
  food: '🍜',
  hotel: '🏨',
  transport: '🚉',
}
const DEFAULT_CENTER: [number, number] = [116.407, 39.904] // 无城市时默认北京

function escapeHtml(value: unknown): string {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;')
}

// 7 个支持城市的地图中心坐标（高德 WGS-84 偏移后的 GCJ-02）
const CITY_CENTERS: Record<string, [number, number]> = {
  '北京': [116.407, 39.904],
  '上海': [121.474, 31.231],
  '成都': [104.066, 30.659],
  '杭州': [120.153, 30.287],
  '西安': [108.948, 34.264],
  '厦门': [118.089, 24.479],
  '广州': [113.264, 23.129],
  '深圳': [114.057, 22.543],
}

declare global {
  interface Window {
    _AMapSecurityConfig?: { securityJsCode: string }
    AMap?: unknown
    NEXT_PUBLIC_AMAP_SECURITY_CODE?: string
  }
}

export default function AMapContainer({ places, itinerary, tripCity }: AMapContainerProps) {
  const containerRef    = useRef<HTMLDivElement>(null)
  const mapRef          = useRef<any>(null)
  // key=placeId, value=marker 实例，便于 hover 联动时按 id 查找
  const markersRef      = useRef<Map<string, any>>(new Map())
  const infoWindowRef   = useRef<any>(null)
  const [isMapReady, setIsMapReady] = useState(false)
  // 记录上次渲染时的 place ID 集合，只有 ID 变化（新增/删除地点）才调 setFitView
  // 投票只改 votedBy，不触发地图缩放
  const prevPlaceIdsRef = useRef<Set<string>>(new Set())
  // ref 透传给异步 init effect，避免闭包捕获初始空值
  const tripCityRef     = useRef<string | undefined>(tripCity)
  useEffect(() => { tripCityRef.current = tripCity }, [tripCity])

  const { selectedPlaceId, setSelectedPlaceId, setHoveredPlaceId, hoveredPlaceId } = useRoomStore()

  // ── selectedPlaceId 变化（卡片点击/AI推荐点击）→ 地图居中并弹出信息窗 ────
  useEffect(() => {
    if (!selectedPlaceId || !mapRef.current) return
    const marker = markersRef.current.get(selectedPlaceId)
    if (!marker) return
    mapRef.current.panTo(marker.getPosition())
    // 触发 marker 点击回调，复用已有信息窗逻辑
    marker.emit('click')
  }, [selectedPlaceId])

  // ── 初始化地图（仅运行一次）────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return
    const jsKey = process.env.NEXT_PUBLIC_AMAP_KEY
    if (!jsKey) {
      console.warn('[AMap] NEXT_PUBLIC_AMAP_KEY 未配置')
      return
    }

    let destroyed = false
    ;(async () => {
      try {
        // 仅在安全码非空时设置，避免空字符串触发 AMap 2.0 安全校验失败
        const secCode = process.env.NEXT_PUBLIC_AMAP_SECURITY_CODE
        if (secCode) {
          window._AMapSecurityConfig = { securityJsCode: secCode }
        }

        const AMapLoader = (await import('@amap/amap-jsapi-loader')).default
        const AMap = await AMapLoader.load({
          key: jsKey,
          version: '2.0',
          plugins: ['AMap.InfoWindow', 'AMap.Geocoder'],
        })
        if (destroyed || !containerRef.current) return

        const city = tripCityRef.current
        const initialCenter: [number, number] =
          (city && CITY_CENTERS[city]) ? CITY_CENTERS[city] : DEFAULT_CENTER

        const map = new AMap.Map(containerRef.current, {
          zoom: 13,
          center: initialCenter,
          mapStyle: 'amap://styles/macaron',
          viewMode: '3D',
        })
        mapRef.current = map
        setIsMapReady(true)

        // 城市不在预置表里时，用 Geocoder 定位（如用户自定义城市名）
        if (city && !CITY_CENTERS[city]) {
          const geocoder = new AMap.Geocoder({ city })
          geocoder.getLocation(city, (status: string, result: any) => {
            if (status === 'complete' && result.geocodes?.length > 0) {
              map.setCenter(result.geocodes[0].location)
            }
          })
        }

        infoWindowRef.current = new AMap.InfoWindow({
          offset: new AMap.Pixel(0, -30),
          closeWhenClickMap: true,
        })
      } catch (e: any) {
        const msg = e?.message || String(e)
        if (msg.includes('域名') || msg.includes('domain') || msg.includes('whitelist') || msg.includes('KEY')) {
          console.error('[AMap] Key 或域名校验失败，请在高德控制台将 localhost:3000 加入 JS API key 的域名白名单。错误：', msg)
        } else {
          console.error('[AMap] 初始化失败', e)
        }
      }
    })()

    return () => {
      destroyed = true
      if (mapRef.current) {
        mapRef.current.destroy()
        mapRef.current = null
      }
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // ── tripCity 变化时重新定位地图中心 ────────────────────────────────
  useEffect(() => {
    if (!mapRef.current || !tripCity) return
    // 优先用预置坐标（即时，无网络请求）
    if (CITY_CENTERS[tripCity]) {
      mapRef.current.setCenter(CITY_CENTERS[tripCity])
      return
    }
    // 不在预置表里的城市，走 Geocoder 降级
    const AMap = (window as any).AMap
    if (!AMap?.Geocoder) return
    const geocoder = new AMap.Geocoder({ city: tripCity })
    geocoder.getLocation(tripCity, (status: string, result: any) => {
      if (status === 'complete' && result.geocodes?.length > 0) {
        mapRef.current?.setCenter(result.geocodes[0].location)
      }
    })
  }, [tripCity])

  // ── 构建 Marker HTML（抽成辅助函数，hover 时复用） ───────────────
  const buildMarkerContent = useCallback((
    place: YjsPlace,
    isActive: boolean  // 是否高亮（hover 或 selected 状态）
  ) => {
    const isVoted = place.votedBy.length > 0
    const color   = CLUSTER_COLORS[place.clusterId ?? 0] ?? '#6B7280'
    const icon    = CATEGORY_ICON[place.category] ?? '📍'
    const bgColor = isVoted ? color : '#9CA3AF'
    const scale   = isActive ? 1.25 : 1
    const shadow  = isActive
      ? 'drop-shadow(0 4px 12px rgba(0,0,0,0.4))'
      : 'drop-shadow(0 2px 6px rgba(0,0,0,0.25))'
    return `
      <div style="display:flex;flex-direction:column;align-items:center;cursor:pointer;filter:${shadow};transform:scale(${scale});transform-origin:bottom center;transition:transform 0.15s,filter 0.15s">
        <div style="width:40px;height:40px;background:${bgColor};border-radius:50% 50% 50% 0;transform:rotate(-45deg);border:${isActive ? '3px' : '2.5px'} solid white;display:flex;align-items:center;justify-content:center">
          <span style="transform:rotate(45deg);font-size:18px">${icon}</span>
        </div>
        <div style="margin-top:4px;background:${isActive ? bgColor : 'white'};color:${isActive ? 'white' : '#374151'};border-radius:6px;padding:1px 6px;font-size:10px;font-weight:600;white-space:nowrap;box-shadow:0 1px 4px rgba(0,0,0,0.12);max-width:80px;overflow:hidden;text-overflow:ellipsis">${escapeHtml(place.name)}</div>
      </div>`
  }, [])

  // ── 渲染地点 Markers ────────────────────────────────────────────
  const renderMarkers = useCallback(() => {
    const map = mapRef.current
    if (!map) return

    // 判断 place ID 集合是否变化（新增 / 删除地点），仅此时才调 setFitView
    const currentIds = new Set(places.map((p) => p.placeId))
    const idsChanged =
      currentIds.size !== prevPlaceIdsRef.current.size ||
      [...currentIds].some((id) => !prevPlaceIdsRef.current.has(id))
    prevPlaceIdsRef.current = currentIds

    markersRef.current.forEach((m) => m.setMap(null))
    markersRef.current.clear()
    if (!places.length) return

    const AMap = (window as any).AMap
    if (!AMap) return

    places.forEach((place) => {
      const { lng, lat } = place.coords

      const marker = new AMap.Marker({
        position: new AMap.LngLat(lng, lat),
        content: buildMarkerContent(place, false),
        anchor: 'bottom-center',
        zIndex: place.votedBy.length > 0 ? 100 : 50,
      })

      marker.on('click', () => {
        // 通知右侧面板高亮并滚动到该卡片
        setSelectedPlaceId(place.placeId)

        const tipHtml = place.ragMeta?.tipSnippets?.[0]
          ? `<p style="color:#6B5624;font-size:11px;margin-top:6px;padding:6px 8px;background:#FEF3C7;border-radius:6px;line-height:1.4">提示：${escapeHtml(place.ragMeta.tipSnippets[0])}</p>`
          : ''
        infoWindowRef.current.setContent(`
          <div style="min-width:200px;max-width:260px;font-family:Inter,system-ui,sans-serif;padding:2px">
            <p style="font-weight:700;font-size:14px;margin:0 0 2px;color:#111827">${escapeHtml(place.name)}</p>
            <p style="color:#6B7280;font-size:11px;margin:0 0 6px">${escapeHtml(place.address || '')}</p>
            ${place.amapRating ? `<span style="color:#8A6518;font-size:12px">评分 ${escapeHtml(place.amapRating)}</span>` : ''}
            ${place.amapPrice  ? `<span style="color:#6B7280;margin-left:8px;font-size:12px">人均 ¥${escapeHtml(place.amapPrice)}</span>` : ''}
            ${tipHtml}
          </div>`)
        infoWindowRef.current.open(map, marker.getPosition())
      })

      marker.on('mouseover', () => setHoveredPlaceId(place.placeId))
      marker.on('mouseout',  () => setHoveredPlaceId(null))

      marker.setMap(map)
      markersRef.current.set(place.placeId, marker)
    })

    if (markersRef.current.size > 0 && idsChanged) {
      map.setFitView([...markersRef.current.values()], false, [60, 420, 60, 420])
    }
  }, [places, buildMarkerContent, setSelectedPlaceId, setHoveredPlaceId])

  // places 变化或 SDK 刚完成初始化 → 重绘 Markers。过去只做一次固定
  // 1.5s 重试，SDK 初始化更慢时会永久错过首批地点。
  useEffect(() => {
    if (!isMapReady || !mapRef.current) return
    renderMarkers()
  }, [places, isMapReady, renderMarkers])

  // hoveredPlaceId 变化 → 更新对应 Marker 高亮样式
  useEffect(() => {
    // 恢复上一个高亮 marker（遍历所有，按当前 places 状态重绘）
    markersRef.current.forEach((marker, placeId) => {
      const place = places.find((p) => p.placeId === placeId)
      if (!place) return
      const isActive = placeId === hoveredPlaceId
      marker.setContent(buildMarkerContent(place, isActive))
      // AMap 2.0 Marker 不暴露 setZIndex，视觉层级由 CSS scale 已体现
      ;(marker as any).setZIndex?.(isActive ? 200 : (place.votedBy.length > 0 ? 100 : 50))
    })
  }, [hoveredPlaceId, places, buildMarkerContent])

  return (
    <div className="map-fullscreen">
      <div ref={containerRef} className="w-full h-full" />
      {itinerary?.days.some((day) => day.slots.length > 1) && (
        <div
          role="status"
          className="absolute bottom-5 left-1/2 z-10 -translate-x-1/2 rounded-xl border border-sky-200 bg-white/95 px-4 py-2 text-xs text-slate-600 shadow-lg"
        >
          计划站点已显示；地图路线暂不可用，不会绘制估算线。
        </div>
      )}
    </div>
  )
}
