'use client'

import { useEffect, useState, useCallback, useRef } from 'react'
import { useParams, useSearchParams, useRouter } from 'next/navigation'
import dynamic from 'next/dynamic'
import { AnimatePresence } from 'framer-motion'

import { useYjsRoom } from '@/hooks/useYjsRoom'
import { useAIChat } from '@/hooks/useAIChat'
import { useOptimize } from '@/hooks/useOptimize'
import { useRoomStore } from '@/stores/roomStore'
import { useAuthStore } from '@/stores/authStore'
import { useToastStore } from '@/stores/toastStore'
import { api } from '@/lib/api'
import TopNav from '@/components/layout/TopNav'
import ChatPanel from '@/components/chat/ChatPanel'
import PlaceList from '@/components/places/PlaceList'
import GlassPanel from '@/components/ui/GlassPanel'
import type { YjsPlace } from '@/types/room'
import { parsePlaceFromAPI } from '@/types/place'

const AMapContainer = dynamic(
  () => import('@/components/map/AMapContainer'),
  { ssr: false, loading: () => <MapFallback /> }
)

function MapFallback() {
  return (
    <div className="map-fullscreen bg-gradient-to-br from-slate-200 via-blue-50 to-emerald-50">
      <svg className="absolute inset-0 w-full h-full opacity-10" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <pattern id="grid" width="60" height="60" patternUnits="userSpaceOnUse">
            <path d="M 60 0 L 0 0 0 60" fill="none" stroke="#64748b" strokeWidth="0.5"/>
          </pattern>
          <pattern id="grid2" width="300" height="300" patternUnits="userSpaceOnUse">
            <path d="M 300 0 L 0 0 0 300" fill="none" stroke="#64748b" strokeWidth="1.5"/>
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#grid)" />
        <rect width="100%" height="100%" fill="url(#grid2)" />
      </svg>
      <svg className="absolute inset-0 w-full h-full opacity-20" xmlns="http://www.w3.org/2000/svg">
        <line x1="0" y1="35%" x2="100%" y2="38%" stroke="#94a3b8" strokeWidth="6"/>
        <line x1="0" y1="65%" x2="100%" y2="62%" stroke="#94a3b8" strokeWidth="4"/>
        <line x1="28%" y1="0" x2="30%" y2="100%" stroke="#94a3b8" strokeWidth="6"/>
        <line x1="62%" y1="0" x2="60%" y2="100%" stroke="#94a3b8" strokeWidth="4"/>
        <line x1="0" y1="20%" x2="100%" y2="22%" stroke="#cbd5e1" strokeWidth="2"/>
        <line x1="0" y1="80%" x2="100%" y2="78%" stroke="#cbd5e1" strokeWidth="2"/>
        <line x1="45%" y1="0" x2="47%" y2="100%" stroke="#cbd5e1" strokeWidth="2"/>
      </svg>
    </div>
  )
}

export default function RoomPage() {
  const params = useParams()
  const searchParams = useSearchParams()
  const router = useRouter()
  const roomId = params.roomId as string

  // ── 认证 ──────────────────────────────────────────────────────────────
  const { user, token, isHydrated, hydrate } = useAuthStore()
  const toast = useToastStore(s => s.toast)
  useEffect(() => { hydrate() }, [hydrate])
  useEffect(() => {
    if (isHydrated && !user) router.replace('/login')
  }, [isHydrated, user, router])

  // userId / nickname：优先用已登录账号，回退 localStorage UUID（兼容旧访客）
  const userId = user?.userId ?? (typeof window !== 'undefined' ? localStorage.getItem('userId') ?? '' : '')
  const nickname = user?.nickname ?? (typeof window !== 'undefined' ? localStorage.getItem('nickname') ?? '旅行者' : '旅行者')

  // ── 房间元数据 ─────────────────────────────────────────────────────────
  const [roomData, setRoomData] = useState({
    threadId: searchParams.get('threadId') || '',
    tripCity: searchParams.get('city') || '',
    tripDays: Number(searchParams.get('days')) || 0,
    loaded: !!searchParams.get('threadId'),
  })

  const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

  useEffect(() => {
    if (roomData.loaded) return
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/room/${roomId}/state`)
        if (!res.ok) throw new Error(`${res.status}`)
        const data = await res.json()
        if (cancelled) return
        setRoomData({ threadId: data.thread_id || roomId, tripCity: data.trip_city || '成都', tripDays: data.trip_days || 3, loaded: true })
      } catch {
        if (!cancelled) setRoomData({ threadId: roomId, tripCity: '成都', tripDays: 3, loaded: true })
      }
    })()
    return () => { cancelled = true }
  }, [roomId, roomData.loaded, API_BASE])

  const threadId = roomData.threadId || roomId
  const tripCity = roomData.tripCity || '成都'
  const tripDays = roomData.tripDays || 3

  // ── Yjs 协同 ───────────────────────────────────────────────────────────
  const { places, members, phase, isConnected, addPlace, removePlace, toggleVote, setPhase, initRoom } = useYjsRoom(roomId, userId, nickname)

  // ── AI 聊天 ────────────────────────────────────────────────────────────
  const { messages, isStreaming, sendMessage } = useAIChat(threadId, userId)

  // ── 路线优化 ───────────────────────────────────────────────────────────
  const { itinerary, isOptimizing, optimize } = useOptimize(threadId, roomId)

  const { isChatOpen, tripDays: storeDays, setTripDays, setIsChatOpen, setRightTab, setSelectedPlaceId } = useRoomStore()

  // ── 天气 ───────────────────────────────────────────────────────────────
  const [weather, setWeather] = useState<null | {
    city: string
    days: { date: string; condition: string; icon: string; temp_high: number; temp_low: number; suggestion: string }[]
  }>(null)

  // ── 持久化：从 DB 恢复景点（进入房间时） ─────────────────────────────
  const dbLoadedRef = useRef(false)
  const isSyncingFromDB = useRef(false)

  useEffect(() => {
    if (!roomData.loaded || dbLoadedRef.current) return
    dbLoadedRef.current = true
    ;(async () => {
      try {
        const dbPlaces = await api.get<Record<string, unknown>[]>(`/api/room/${roomId}/places`)
        if (!dbPlaces.length) return
        isSyncingFromDB.current = true
        dbPlaces.forEach((raw) => {
          try {
            const place = parsePlaceFromAPI(raw)
            if (!places.find(p => p.placeId === place.placeId)) {
              addPlace(place as any)
            }
          } catch { /* 格式错误跳过 */ }
        })
        // voted_by 恢复：通过 updateNote 的方式处理 votedBy（Yjs addPlace 会重置 votedBy，这里额外回写）
        // 简化处理：voted_by 在协同房间内是实时的，历史数据只恢复景点列表即可
        setTimeout(() => { isSyncingFromDB.current = false }, 500)
      } catch { /* 获取失败静默 */ }
    })()
  }, [roomData.loaded]) // eslint-disable-line

  // ── 持久化：Yjs places 变化时同步到 DB（防抖 2s） ────────────────────
  const syncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    // 初始加载阶段 / 从 DB 恢复时不触发同步
    if (isSyncingFromDB.current || !roomData.loaded || places.length === 0) return

    if (syncTimerRef.current) clearTimeout(syncTimerRef.current)
    syncTimerRef.current = setTimeout(() => {
      const votedByMap: Record<string, string[]> = {}
      places.forEach(p => { votedByMap[p.placeId] = p.votedBy })

      api.post(`/api/room/${roomId}/places/sync`, {
        places: places.map(placeToRaw),
        voted_by_map: votedByMap,
      }).catch(() => {})
    }, 2000)

    return () => { if (syncTimerRef.current) clearTimeout(syncTimerRef.current) }
  }, [places, roomData.loaded]) // eslint-disable-line

  // ── 持久化：排线完成后自动保存路线 ────────────────────────────────────
  const savedItineraryRef = useRef<string | null>(null)
  useEffect(() => {
    if (!itinerary || !user) return
    const key = JSON.stringify(itinerary).slice(0, 100) // 简单指纹去重
    if (savedItineraryRef.current === key) return
    savedItineraryRef.current = key
    api.post(`/api/room/${roomId}/itinerary`, {
      itinerary_data: itinerary,
      city: tripCity,
      trip_days: storeDays || tripDays,
    }).catch(() => {})
  }, [itinerary]) // eslint-disable-line

  // ── 初始化 ─────────────────────────────────────────────────────────────
  useEffect(() => {
    setTripDays(tripDays)
    initRoom({ roomId, threadId, tripCity, tripDays })
  }, [roomId]) // eslint-disable-line

  useEffect(() => {
    if (!tripCity) return
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/weather?city=${encodeURIComponent(tripCity)}`)
        if (!res.ok) return
        const data = await res.json()
        if (!cancelled && data) setWeather(data)
      } catch { /* 静默降级 */ }
    })()
    return () => { cancelled = true }
  }, [tripCity, API_BASE])

  useEffect(() => {
    if (itinerary) setRightTab('itinerary')
  }, [itinerary]) // eslint-disable-line

  // ── 推荐景点初始加载 ───────────────────────────────────────────────────
  const [recommendLoaded, setRecommendLoaded] = useState(false)
  useEffect(() => {
    if (!roomData.loaded || recommendLoaded || places.length > 0) return
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/recommend`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ city: tripCity, trip_days: storeDays || tripDays, user_id: userId || undefined }),
        })
        if (!res.ok) throw new Error(`${res.status}`)
        const data = await res.json()
        if (cancelled || !data.places?.length) return
        isSyncingFromDB.current = true
        data.places.forEach((raw: Record<string, unknown>) => {
          const place = {
            placeId: raw.place_id as string,
            name: raw.name as string,
            category: raw.category as string,
            address: raw.address as string,
            coords: raw.coords as { lng: number; lat: number },
            city: raw.city as string,
            district: raw.district as string | undefined,
            source: raw.source as string,
            amapRating: raw.amap_rating as number | undefined,
            amapPrice: raw.amap_price as number | undefined,
            openingHours: raw.opening_hours as string | undefined,
            phone: raw.phone as string | undefined,
            amapPhotos: (raw.amap_photos as string[]) || [],
            description: raw.description as string | undefined,
            tags: (raw.tags as string[]) || [],
            estimatedDuration: raw.estimated_duration as number | undefined,
          }
          if (!places.find((p) => p.placeId === place.placeId)) addPlace(place as any)
        })
        setTimeout(() => { isSyncingFromDB.current = false }, 500)
        setRecommendLoaded(true)
      } catch (e) { console.warn('[RoomPage] 推荐加载失败', e) }
    })()
    return () => { cancelled = true }
  }, [roomData.loaded, recommendLoaded, tripCity, tripDays]) // eslint-disable-line

  // AI 推荐地点自动加入工作台
  useEffect(() => {
    const lastMsg = messages[messages.length - 1]
    if (lastMsg?.role === 'assistant' && lastMsg.status === 'done' && lastMsg.placesGenerated) {
      lastMsg.placesGenerated.forEach((place) => {
        if (!places.find((p) => p.placeId === place.placeId)) addPlace(place)
      })
    }
  }, [messages]) // eslint-disable-line

  // ── 排线 ───────────────────────────────────────────────────────────────
  const handleOptimize = async () => {
    const selectedPlaces = places.filter((p) => p.votedBy.length > 0)
    if (selectedPlaces.length < 2) {
      toast('请先在候选地点中点击心形，至少选择 2 个地点再排线', 'warning')
      return
    }
    const hasAttraction = selectedPlaces.some((p) => p.category === 'attraction')
    const hasFood = selectedPlaces.some((p) => p.category === 'food')
    const hasHotel = selectedPlaces.some((p) => p.category === 'hotel')
    const missing: string[] = []
    if (!hasAttraction) missing.push('景点（美景）')
    if (!hasFood) missing.push('餐饮（美食）')
    if (!hasHotel) missing.push('住宿（美梦）')
    if (missing.length > 0) {
      toast(`行程缺少：${missing.join('、')}，请在候选地点中补充选择`, 'warning')
      return
    }
    setPhase('optimizing')
    await optimize(selectedPlaces, storeDays || tripDays)
    setPhase('planned')
  }

  const selectedCount = places.filter((p) => p.votedBy.includes(userId)).length

  if (!isHydrated) return null

  return (
    <div className="h-screen w-screen overflow-hidden relative">
      <AMapContainer places={places} itinerary={itinerary} tripCity={tripCity} />

      <div className="overlay-layer">
        <TopNav
          roomId={roomId}
          tripCity={tripCity}
          tripDays={storeDays || tripDays}
          isConnected={isConnected}
          members={members}
          isChatOpen={isChatOpen}
          onToggleChat={() => setIsChatOpen(!isChatOpen)}
          selectedCount={selectedCount}
          isOptimizing={isOptimizing}
          hasItinerary={!!itinerary}
          onOptimize={handleOptimize}
          onViewItinerary={() => router.push(`/room/${roomId}/itinerary`)}
        />

        <div className="flex items-start gap-3 px-4 mt-3" style={{ height: 'calc(100vh - 72px)' }}>
          <AnimatePresence>
            {isChatOpen && (
              <GlassPanel
                solid
                className="overlay-interactive w-[380px] flex-shrink-0 flex flex-col overflow-hidden"
                style={{ height: 'calc(100vh - 84px)' }}
                initial={{ x: -30, opacity: 0, scale: 0.97 }}
                animate={{ x: 0, opacity: 1, scale: 1 }}
                exit={{ x: -30, opacity: 0, scale: 0.97 }}
                transition={{ duration: 0.3, ease: 'easeOut' }}
              >
                <ChatPanel
                  messages={messages}
                  isStreaming={isStreaming}
                  weather={weather}
                  tripCity={tripCity}
                  onSend={(text) =>
                    sendMessage(text, places.filter((p) => p.votedBy.length > 0).map((p) => p.placeId), tripCity)
                  }
                  onClickPlace={setSelectedPlaceId}
                />
              </GlassPanel>
            )}
          </AnimatePresence>

          <div className="flex-1 min-w-0" />

          <GlassPanel
            solid
            className="overlay-interactive w-[360px] flex-shrink-0 flex flex-col overflow-hidden"
            style={{ height: 'calc(100vh - 84px)' }}
            initial={{ x: 30, opacity: 0, scale: 0.97 }}
            animate={{ x: 0, opacity: 1, scale: 1 }}
            transition={{ duration: 0.3, ease: 'easeOut', delay: 0.1 }}
          >
            <PlaceList
              places={places}
              currentUserId={userId}
              members={members}
              itinerary={itinerary}
              onToggleVote={toggleVote}
              onRemove={removePlace}
              onClickPlace={setSelectedPlaceId}
            />
          </GlassPanel>
        </div>
      </div>
    </div>
  )
}

// ── 格式转换辅助函数 ──────────────────────────────────────────────────────

function placeToRaw(p: YjsPlace) {
  return {
    place_id: p.placeId,
    name: p.name,
    category: p.category,
    address: p.address,
    coords: p.coords,
    city: p.city,
    district: p.district,
    source: p.source,
    amap_rating: p.amapRating,
    amap_price: p.amapPrice,
    opening_hours: p.openingHours,
    phone: p.phone,
    amap_photos: p.amapPhotos,
    description: p.description,
    tags: p.tags,
    estimated_duration: p.estimatedDuration,
  }
}
