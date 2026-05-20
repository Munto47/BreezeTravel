'use client'

import { useEffect, useRef, useCallback } from 'react'
import type { YjsPlace } from '@/types/room'
import type { Itinerary } from '@/types/itinerary'
import { useRoomStore } from '@/stores/roomStore'

interface AMapContainerProps {
  places: YjsPlace[]
  itinerary?: Itinerary | null
  tripCity?: string
}

const CLUSTER_COLORS = ['#FF5A5F', '#3B82F6', '#10B981', '#F59E0B', '#8B5CF6', '#06B6D4']

// 每天路线的高级配色：珊瑚红 / 水鸭蓝 / 亮橙 / 深紫红 / 琥珀黄
const ROUTE_COLORS = ['#FF5A5F', '#00A699', '#FC642D', '#7B0051', '#FFB400']

const CATEGORY_ICON: Record<string, string> = {
  attraction: '🏛',
  food: '🍜',
  hotel: '🏨',
  transport: '🚉',
}
const DEFAULT_CENTER: [number, number] = [104.066, 30.659]

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
  const routePolylinesRef = useRef<any[]>([])
  const drivingRef      = useRef<any[]>([])
  const infoWindowRef   = useRef<any>(null)
  // 记录上次渲染时的 place ID 集合，只有 ID 变化（新增/删除地点）才调 setFitView
  // 投票只改 votedBy，不触发地图缩放
  const prevPlaceIdsRef = useRef<Set<string>>(new Set())

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
    const jsKey = process.env.NEXT_PUBLIC_AMAP_JS_KEY
    if (!jsKey) {
      console.warn('[AMap] NEXT_PUBLIC_AMAP_JS_KEY 未配置')
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
          plugins: ['AMap.InfoWindow', 'AMap.Driving', 'AMap.Geocoder'],
        })
        if (destroyed || !containerRef.current) return

        const map = new AMap.Map(containerRef.current, {
          zoom: 13,
          center: DEFAULT_CENTER,
          mapStyle: 'amap://styles/macaron',
          viewMode: '3D',
        })
        mapRef.current = map

        // 通过高德 Geocoder 动态解析城市名 → 经纬度，支持全国任意城市
        if (tripCity) {
          const geocoder = new AMap.Geocoder({ city: tripCity })
          geocoder.getLocation(tripCity, (status: string, result: any) => {
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
        <div style="margin-top:4px;background:${isActive ? bgColor : 'white'};color:${isActive ? 'white' : '#374151'};border-radius:6px;padding:1px 6px;font-size:10px;font-weight:600;white-space:nowrap;box-shadow:0 1px 4px rgba(0,0,0,0.12);max-width:80px;overflow:hidden;text-overflow:ellipsis">${place.name}</div>
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
          ? `<p style="color:#92400E;font-size:11px;margin-top:6px;padding:6px 8px;background:#FEF3C7;border-radius:6px;line-height:1.4">💡 ${place.ragMeta.tipSnippets[0]}</p>`
          : ''
        const photoHtml = place.amapPhotos?.[0]
          ? `<img src="${place.amapPhotos[0]}" style="width:100%;height:80px;object-fit:cover;border-radius:6px;margin-bottom:8px"/>`
          : ''
        infoWindowRef.current.setContent(`
          <div style="min-width:200px;max-width:260px;font-family:Inter,system-ui,sans-serif;padding:2px">
            ${photoHtml}
            <p style="font-weight:700;font-size:14px;margin:0 0 2px;color:#111827">${place.name}</p>
            <p style="color:#9CA3AF;font-size:11px;margin:0 0 6px">${place.address || ''}</p>
            ${place.amapRating ? `<span style="color:#D97706;font-size:12px">⭐ ${place.amapRating}</span>` : ''}
            ${place.amapPrice  ? `<span style="color:#6B7280;margin-left:8px;font-size:12px">¥${place.amapPrice}/人</span>` : ''}
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

  // ── 渲染多色静态驾车路线 ────────────────────────────────────────
  const renderRoutes = useCallback(() => {
    const map = mapRef.current
    if (!map) return

    // ① 内存清理：移除上一版所有折线和 Driving 实例
    if (routePolylinesRef.current.length > 0) {
      map.remove(routePolylinesRef.current)
      routePolylinesRef.current = []
    }
    drivingRef.current.forEach((d) => { try { d.clear() } catch (_) {} })
    drivingRef.current = []

    if (!itinerary?.days?.length) return

    const AMap = (window as any).AMap
    if (!AMap) return

    // ② 数据预加载：Promise.all 静默获取所有天的路径，不触碰地图
    const pathPromises: Promise<any[]>[] = itinerary.days.map((day, dayIdx) =>
      new Promise<any[]>((resolve) => {
        const slots = day?.slots ?? []
        if (slots.length < 2) {
          resolve([])
          return
        }

        const origin: [number, number]      = [slots[0].place.coords.lng, slots[0].place.coords.lat]
        const destination: [number, number] = [slots[slots.length - 1].place.coords.lng, slots[slots.length - 1].place.coords.lat]
        const waypoints = slots
          .slice(1, -1)
          .map((s: any) => [s.place.coords.lng, s.place.coords.lat] as [number, number])

        // 关键：不传 map 参数，Driving 完全静默，不自动渲染、不抢占视图
        const driving = new AMap.Driving({ hideMarkers: true, autoFitView: false })
        drivingRef.current.push(driving)

        // 错开请求，每天间隔 600ms，避免触发高德 QPS 限制
        setTimeout(() => {
          driving.search(origin, destination, { waypoints }, (status: string, result: any) => {
            if (status === 'complete' && result?.routes?.[0]) {
              const path: any[] = []
              result.routes[0].steps.forEach((step: any) => {
                step.path?.forEach((pt: any) => path.push(pt))
              })
              console.log(`[AMap] 第${dayIdx + 1}天路径节点数：${path.length}`)
              resolve(path)
            } else {
              console.warn(`[AMap] 第${dayIdx + 1}天路线规划失败:`, status)
              resolve([])
            }
          })
        }, dayIdx * 600)
      })
    )

    // ③ 全部到齐后，一次性绘制静态多色折线
    Promise.all(pathPromises).then((allPaths) => {
      const drawn: any[] = []

      allPaths.forEach((path, i) => {
        if (path.length < 2) return

        const polyline = new AMap.Polyline({
          path,
          showDir:       true,                               // 方向箭头
          strokeColor:   ROUTE_COLORS[i % ROUTE_COLORS.length],
          strokeWeight:  8,
          strokeOpacity: 0.8,                               // 允许重叠路段颜色叠加
          lineJoin:      'round',                           // 拐角平滑
          lineCap:       'round',                           // 端点平滑
          zIndex:        20 + i,                            // 后绘制的天数层叠在上
        })
        polyline.setMap(map)
        drawn.push(polyline)
      })

      routePolylinesRef.current = drawn

      // ④ 全局自适应：缩放视角到恰好包含所有路线
      if (drawn.length > 0) {
        map.setFitView(drawn, false, [50, 50, 50, 50])
      }
    })
  }, [itinerary])

  // places 变化 → 重绘 Markers
  useEffect(() => {
    if (!mapRef.current) {
      const t = setTimeout(renderMarkers, 1500)
      return () => clearTimeout(t)
    }
    renderMarkers()
  }, [places, renderMarkers])

  // itinerary 变化 → 重绘静态路线
  useEffect(() => {
    if (!mapRef.current) {
      const t = setTimeout(renderRoutes, 1500)
      return () => clearTimeout(t)
    }
    renderRoutes()
  }, [itinerary, renderRoutes])

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
    </div>
  )
}
