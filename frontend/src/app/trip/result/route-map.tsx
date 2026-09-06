'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { LocateFixed, MapPin, Minus, Plus, RefreshCw } from 'lucide-react'
import type {
  MapRenderView,
  PlacePosition,
  PlaceCandidateView,
  UserFacingTripResult,
} from '@/lib/trip-understanding-v3'

import { DAY_COLORS } from './result-presentation'

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
  days,
  selected,
  onSelect,
  mode,
  visible,
  focusSelected,
  previewCandidate = null,
  simulationPosition = null,
  dayColor = '#0c789d',
}: {
  view: MapRenderView | null
  day: UserFacingTripResult['days'][number] | undefined
  days?: UserFacingTripResult['days']
  selected: string | null
  onSelect: (token: string) => void
  mode: 'recommended' | 'walking' | 'transit'
  visible: boolean
  focusSelected: boolean
  previewCandidate?: PlaceCandidateView | null
  simulationPosition?: { longitude: number; latitude: number } | null
  dayColor?: string
}) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MapInstance | null>(null)
  const sdk = useRef<MapSDK | null>(null)
  const markers = useRef(new Map<string, HTMLElement>())
  const selectionCallback = useRef(onSelect)
  const currentOverlays = useRef<unknown[]>([])
  const fittedDay = useRef<string | null>(null)
  const [activated, setActivated] = useState(visible)
  useEffect(() => { if (visible) setActivated(true) }, [visible])
  const [ready, setReady] = useState(false)
  const [tilesReady, setTilesReady] = useState(false)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  selectionCallback.current = onSelect
  const visibleDays = useMemo(() => days || (day ? [day] : []), [days, day])
  const points = useMemo(
    () =>
      (view?.points || []).filter(
        (point) =>
          visibleDays.some((visibleDay) => visibleDay.activities.some(
            (activity) => activity.activity_token === point.activity_token,
          )) && validPosition(point.position),
      ),
    [view?.points, visibleDays],
  )

  useEffect(() => {
    if (!container.current || !activated) return
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
            setError('地图暂不可用')
        }, 15000)
        setReady(true)
      })
      .catch(() => {
        if (!cancelled)
          setError('地图暂不可用')
      })
    return () => {
      cancelled = true
      clearTimeout(mapTimeout)
      map.current?.destroy()
      map.current = null
      sdk.current = null
    }
  }, [attempt, activated])

  useEffect(() => {
    if (!visible || !ready || !map.current || !sdk.current) return
    const instance = map.current
    const api = sdk.current
    const overlays: unknown[] = []
    markers.current.clear()
    points.forEach((point) => {
      const position = point.position!
      const button = document.createElement('button')
      button.type = 'button'
      button.className = 'e-map-marker'
      const dayOffset = visibleDays.findIndex((item) => item.activities.some((card) => card.activity_token === point.activity_token))
      const pointDay = visibleDays[dayOffset]
      const index =
        pointDay?.activities.findIndex(
          (activity) => activity.activity_token === point.activity_token,
        ) ?? 0
      button.textContent = String(index + 1)
      button.style.backgroundColor = days ? DAY_COLORS[dayOffset % DAY_COLORS.length] : dayColor
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
      visibleDays.forEach((routeDay, dayOffset) => {
      const routes = view.days.find((item) => item.label === routeDay.label)?.routes || []
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
            strokeColor: days ? DAY_COLORS[dayOffset % DAY_COLORS.length] : dayColor,
            strokeOpacity: 0.8,
            strokeWeight: 5,
            strokeStyle: routeMode === 'walking' ? 'dashed' : 'solid',
            showDir: true,
            lineJoin: 'round',
            lineCap: 'round',
          }),
        )
      })
      })
    }
    instance.add(overlays)
    currentOverlays.current = overlays
    const fitKey = points.map((point) => `${point.activity_token}:${point.position?.longitude},${point.position?.latitude}`).join('|')
    if (points.length && fittedDay.current !== fitKey) {
      instance.setFitView(overlays, false, [70, 70, 70, 70], 15)
      fittedDay.current = fitKey
    }
    return () => {
      // The map lifecycle effect may already have destroyed this SDK instance.
      if (map.current === instance) instance.remove(overlays)
      currentOverlays.current = []
      markers.current.clear()
    }
  }, [visible, ready, points, visibleDays, days, view, mode, dayColor])

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
      if (map.current === instance) instance.remove([marker])
    }
  }, [previewCandidate, ready])

  useEffect(() => {
    if (
      !ready ||
      !map.current ||
      !sdk.current ||
      !simulationPosition ||
      !Number.isFinite(simulationPosition.longitude) ||
      !Number.isFinite(simulationPosition.latitude)
    )
      return
    const dot = document.createElement('span')
    dot.className = 'e-map-simulation-marker'
    dot.setAttribute('aria-label', '计划路线模拟位置')
    const marker = new sdk.current.Marker({
      position: [simulationPosition.longitude, simulationPosition.latitude],
      content: dot,
      anchor: 'center',
      title: '计划路线模拟位置',
    })
    const instance = map.current
    instance.add([marker])
    return () => { if (map.current === instance) instance.remove([marker]) }
  }, [ready, simulationPosition])

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
      <div className="e-map-canvas" ref={container} aria-label="路线地图" />
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
            aria-label="查看所有地点"
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
                : '暂无已确认地点')}
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
