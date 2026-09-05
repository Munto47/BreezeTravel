'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { LocateFixed, MapPin, Minus, Plus, RefreshCw } from 'lucide-react'
import type {
  MapRenderView,
  PlacePosition,
  PlaceCandidateView,
  UserFacingTripResult,
} from '@/lib/trip-understanding-v3'

type MapInstance = {
  destroy(): void
  add(overlays: unknown[]): void
  remove(overlays: unknown[]): void
  on(event: string, listener: () => void): void
  setFitView(
    overlays: unknown[],
    immediate?: boolean,
    padding?: number[],
    maxZoom?: number,
  ): void
  setCenter(center: [number, number]): void
  resize?(): void
  zoomIn?(): void
  zoomOut?(): void
}
type MapSDK = {
  Map: new (container: HTMLElement, options: object) => MapInstance
  Marker: new (options: object) => unknown
  Polyline: new (options: object) => unknown
}
let sdkPromise: Promise<MapSDK> | null = null

function loadMap(): Promise<MapSDK> {
  if (window.AMap) return Promise.resolve(window.AMap as MapSDK)
  if (sdkPromise) return sdkPromise
  const key = process.env.NEXT_PUBLIC_AMAP_KEY
  if (!key) return Promise.reject(new Error('MAP_NOT_CONFIGURED'))
  const security = process.env.NEXT_PUBLIC_AMAP_SECURITY_CODE
  if (security) window._AMapSecurityConfig = { securityJsCode: security }
  sdkPromise = new Promise<MapSDK>((resolve, reject) => {
    const script = document.createElement('script')
    let settled = false
    const done = (ok: boolean) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (ok && window.AMap) resolve(window.AMap as MapSDK)
      else {
        script.remove()
        reject(new Error('MAP_LOAD_FAILED'))
      }
    }
    const timer = window.setTimeout(() => done(false), 12000)
    script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(key)}`
    script.async = true
    script.onload = () => done(true)
    script.onerror = () => done(false)
    document.head.appendChild(script)
  }).catch((error) => {
    sdkPromise = null
    throw error
  })
  return sdkPromise
}

function validPosition(
  point: PlacePosition | null | undefined,
): point is PlacePosition {
  return Boolean(
    point &&
      point.coordinate_system === 'GCJ02' &&
      Number.isFinite(point.longitude) &&
      Number.isFinite(point.latitude) &&
      Math.abs(point.longitude) <= 180 &&
      Math.abs(point.latitude) <= 90,
  )
}

export default function RouteMap({
  view,
  day,
  selected,
  onSelect,
  mode,
  visible,
  focusSelected,
  previewCandidate = null,
}: {
  view: MapRenderView | null
  day: UserFacingTripResult['days'][number] | undefined
  selected: string | null
  onSelect: (token: string) => void
  mode: 'recommended' | 'walking' | 'transit'
  visible: boolean
  focusSelected: boolean
  previewCandidate?: PlaceCandidateView | null
}) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MapInstance | null>(null)
  const sdk = useRef<MapSDK | null>(null)
  const markers = useRef(new Map<string, HTMLElement>())
  const selectionCallback = useRef(onSelect)
  const currentOverlays = useRef<unknown[]>([])
  const fittedDay = useRef<string | null>(null)
  const [ready, setReady] = useState(false)
  const [tilesReady, setTilesReady] = useState(false)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  selectionCallback.current = onSelect
  const points = useMemo(
    () =>
      (view?.points || []).filter(
        (point) =>
          day?.activities.some(
            (activity) => activity.activity_token === point.activity_token,
          ) && validPosition(point.position),
      ),
    [view?.points, day],
  )

  useEffect(() => {
    if (!container.current) return
    let cancelled = false
    let mapTimeout: ReturnType<typeof setTimeout> | undefined
    setError('')
    setReady(false)
    setTilesReady(false)
    loadMap()
      .then((api) => {
        if (cancelled || !container.current) return
        sdk.current = api
        map.current = new api.Map(container.current, {
          viewMode: '2D',
          zoom: 12,
          resizeEnable: true,
          mapStyle: 'amap://styles/whitesmoke',
        })
        map.current.on('complete', () => {
          if (!cancelled) {
            clearTimeout(mapTimeout)
            setTilesReady(true)
            setError('')
          }
        })
        mapTimeout = setTimeout(() => {
          if (!cancelled)
            setError('地图底图暂时没有加载完成。行程与路线摘要仍可查看。')
        }, 15000)
        setReady(true)
      })
      .catch(() => {
        if (!cancelled)
          setError(
            process.env.NEXT_PUBLIC_AMAP_KEY
              ? '地图底图暂时无法加载。行程与已核对的路线摘要仍可查看。'
              : '当前环境尚未启用地图底图，行程仍可查看和修改。',
          )
      })
    return () => {
      cancelled = true
      clearTimeout(mapTimeout)
      map.current?.destroy()
      map.current = null
      sdk.current = null
    }
  }, [attempt])

  useEffect(() => {
    if (!ready || !map.current || !sdk.current) return
    const instance = map.current
    const api = sdk.current
    const overlays: unknown[] = []
    markers.current.clear()
    points.forEach((point) => {
      const position = point.position!
      const button = document.createElement('button')
      button.type = 'button'
      button.className = 'e-map-marker'
      const index =
        day?.activities.findIndex(
          (activity) => activity.activity_token === point.activity_token,
        ) ?? 0
      button.textContent = String(index + 1)
      button.setAttribute('aria-label', `查看${point.name}`)
      button.onclick = () => selectionCallback.current(point.activity_token)
      markers.current.set(point.activity_token, button)
      overlays.push(
        new api.Marker({
          position: [position.longitude, position.latitude],
          content: button,
          anchor: 'center',
          title: point.name,
        }),
      )
    })
    if (view?.status === 'AVAILABLE' || view?.status === 'LIMITED') {
      const routes =
        view.days.find((routeDay) => routeDay.label === day?.label)?.routes ||
        []
      routes.forEach((route) => {
        const routeMode = mode === 'recommended' ? route.selected_mode : mode
        if (!routeMode) return
        const segment = route[routeMode]
        if (segment.status !== 'AVAILABLE' || segment.geometry.length < 2)
          return
        if (
          !segment.geometry.every(
            (point) =>
              Number.isFinite(point.longitude) &&
              Number.isFinite(point.latitude),
          )
        )
          return
        overlays.push(
          new api.Polyline({
            path: segment.geometry.map((point) => [
              point.longitude,
              point.latitude,
            ]),
            strokeColor: '#2f6558',
            strokeOpacity: 0.8,
            strokeWeight: 5,
            strokeStyle: routeMode === 'walking' ? 'dashed' : 'solid',
            showDir: true,
            lineJoin: 'round',
            lineCap: 'round',
          }),
        )
      })
    }
    instance.add(overlays)
    currentOverlays.current = overlays
    if (overlays.length && fittedDay.current !== day?.label) {
      instance.setFitView(overlays, false, [70, 70, 70, 70], 15)
      fittedDay.current = day?.label || null
    }
    return () => {
      instance.remove(overlays)
      currentOverlays.current = []
      markers.current.clear()
    }
  }, [ready, points, day, view, mode])

  useEffect(() => {
    markers.current.forEach((button, token) => {
      button.classList.toggle('is-selected', token === selected)
      button.setAttribute('aria-pressed', String(token === selected))
    })
    const point = points.find((point) => point.activity_token === selected)
    if (focusSelected && point?.position)
      map.current?.setCenter([
        point.position.longitude,
        point.position.latitude,
      ])
  }, [selected, points, ready, mode, focusSelected])

  useEffect(() => {
    if (
      !ready ||
      !map.current ||
      !sdk.current ||
      !validPosition(previewCandidate?.position)
    )
      return
    const position = previewCandidate.position
    const label = document.createElement('span')
    label.className = 'e-map-candidate-marker'
    label.textContent = '候选'
    const marker = new sdk.current.Marker({
      position: [position.longitude, position.latitude],
      content: label,
      anchor: 'center',
      title: previewCandidate.name,
    })
    const instance = map.current
    instance.add([marker])
    instance.setCenter([position.longitude, position.latitude])
    return () => {
      instance.remove([marker])
    }
  }, [previewCandidate, ready])

  useEffect(() => {
    if (visible) {
      const timer = setTimeout(() => {
        map.current?.resize?.()
        window.dispatchEvent(new Event('resize'))
      }, 50)
      return () => clearTimeout(timer)
    }
  }, [visible])
  return (
    <div className="e-map-surface" data-testid="route-map">
      <div className="e-map-canvas" ref={container} aria-label="高德路线地图" />
      {tilesReady && !error && (
        <div className="e-map-controls" aria-label="地图控制">
          <button
            type="button"
            aria-label="放大地图"
            onClick={() => map.current?.zoomIn?.()}
          >
            <Plus aria-hidden="true" />
          </button>
          <button
            type="button"
            aria-label="缩小地图"
            onClick={() => map.current?.zoomOut?.()}
          >
            <Minus aria-hidden="true" />
          </button>
          <button
            type="button"
            aria-label="查看当天所有地点"
            onClick={() => {
              if (currentOverlays.current.length)
                map.current?.setFitView(
                  currentOverlays.current,
                  false,
                  [60, 60, 60, 60],
                  15,
                )
            }}
          >
            <LocateFixed aria-hidden="true" />
          </button>
        </div>
      )}
      {previewCandidate && (
        <p className="e-map-preview-label">
          候选位置：{previewCandidate.name} · 尚未使用
        </p>
      )}
      {(!tilesReady || error || (!points.length && !previewCandidate)) && (
        <div className="e-map-empty" role="status">
          <MapPin aria-hidden="true" />
          <p>
            {error ||
              (!tilesReady
                ? '正在打开地图…'
                : '这一天还没有可显示的地点坐标。选择待确认地点，搜索并确认后即可定位。')}
          </p>
          {error && process.env.NEXT_PUBLIC_AMAP_KEY && (
            <button
              className="e-button"
              type="button"
              onClick={() => setAttempt((value) => value + 1)}
            >
              <RefreshCw aria-hidden="true" />
              重试加载地图
            </button>
          )}
        </div>
      )}
    </div>
  )
}
