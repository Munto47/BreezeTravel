'use client'

import { useEffect, useState, useCallback, useMemo, useRef } from 'react'
import { useParams, useRouter } from 'next/navigation'
import dynamic from 'next/dynamic'
import { AnimatePresence } from 'framer-motion'

import { useYjsRoom } from '@/hooks/useYjsRoom'
import { useAIChat } from '@/hooks/useAIChat'
import { useOptimize } from '@/hooks/useOptimize'
import { useRoomStore } from '@/stores/roomStore'
import { useAuthStore } from '@/stores/authStore'
import { useToastStore } from '@/stores/toastStore'
import { api, ApiRequestError } from '@/lib/api'
import { currentLoginReturnPath } from '@/lib/request-safety'
import type { TripUnderstandingAcceptedView } from '@/lib/trip-understanding-v3'
import TopNav from '@/components/layout/TopNav'
import ChatPanel from '@/components/chat/ChatPanel'
import PlaceList from '@/components/places/PlaceList'
import BackupDrawer from '@/components/places/BackupDrawer'
import GlassPanel from '@/components/ui/GlassPanel'
import type { YjsPlace } from '@/types/room'
import { parseSavedItinerary, type Itinerary } from '@/types/itinerary'
import type { TripTaskSpec } from '@/types/taskSpec'
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

function stableFingerprint(value: unknown): string {
  const normalize = (item: unknown): unknown => {
    if (Array.isArray(item)) return item.map(normalize)
    if (item && typeof item === 'object')
      return Object.fromEntries(
        Object.entries(item as Record<string, unknown>)
          .sort(([left], [right]) => left.localeCompare(right))
          .map(([key, child]) => [key, normalize(child)]),
      )
    return item
  }
  return JSON.stringify(normalize(value))
}

export default function RoomPage() {
  const params = useParams()
  const roomId = params.roomId as string

  return <RoomWorkspace key={roomId} roomId={roomId} />
}

function RoomWorkspace({ roomId }: { roomId: string }) {
  const router = useRouter()

  // ── 认证 ──────────────────────────────────────────────────────────────
  const { user, token, isHydrated, hydrate } = useAuthStore()
  const toast = useToastStore(s => s.toast)
  useEffect(() => { hydrate() }, [hydrate])
  useEffect(() => {
    if (!isHydrated || user) return
    sessionStorage.setItem('bt_login_return', currentLoginReturnPath())
    router.replace('/login')
  }, [isHydrated, roomId, user, router])

  const userId = user?.userId ?? ''
  const nickname = user?.nickname ?? '旅行者'

  // ── 房间元数据 ─────────────────────────────────────────────────────────
  const [roomData, setRoomData] = useState({
    threadId: '',
    tripCity: '',
    tripDays: 0,
    loaded: false,
  })
  const [roomLoadError, setRoomLoadError] = useState('')
  const [roomLoadAttempt, setRoomLoadAttempt] = useState(0)

  useEffect(() => {
    if (!isHydrated || !user || !token) return
    let cancelled = false
    setRoomLoadError('')
    setRoomData((current) => ({ ...current, loaded: false }))
    ;(async () => {
      try {
        const data = await api.get<{
          thread_id?: string
          trip_city?: string
          trip_days?: number
        }>(`/api/room/${encodeURIComponent(roomId)}/state`)
        if (cancelled) return
        setRoomData({ threadId: data.thread_id || roomId, tripCity: data.trip_city || '', tripDays: data.trip_days || 3, loaded: true })
      } catch (failure) {
        if (failure instanceof ApiRequestError && failure.status === 403) {
          router.replace(`/collaborate?join=${encodeURIComponent(roomId)}`)
          return
        }
        if (!cancelled) setRoomLoadError('无法读取这个房间。请确认房间号和成员身份后重试。')
      }
    })()
    return () => { cancelled = true }
  }, [roomId, isHydrated, roomLoadAttempt, token, user, router])

  const threadId = roomData.threadId || roomId
  const tripCity = roomData.tripCity || ''
  const tripDays = roomData.tripDays || 3

  // ── Yjs 协同 ───────────────────────────────────────────────────────────
  const { places, members, isConnected, addPlace, removePlace, toggleVote, setPhase, initRoom } = useYjsRoom(
    roomId,
    userId,
    nickname,
    roomData.loaded && Boolean(user),
  )

  // ── AI 聊天 ────────────────────────────────────────────────────────────
  const { messages, isStreaming, sendMessage } = useAIChat(
    threadId,
    userId,
    roomId,
  )

  // SSE preview cards arrive before Synthesizer/Critic finish. Feed the same
  // provider-backed POIs to the map immediately, but do not persist them to
  // the collaborative candidate pool until the request reaches `done`.
  const mapPlaces = useMemo<YjsPlace[]>(() => {
    const byId = new Map(places.map((place) => [place.placeId, place]))
    const latestAssistant = [...messages].reverse().find((message) => message.role === 'assistant')
    for (const place of latestAssistant?.placesGenerated || []) {
      if (byId.has(place.placeId)) continue
      byId.set(place.placeId, {
        ...place,
        votedBy: [],
        addedBy: 'ai-preview',
        addedAt: latestAssistant?.createdAt || new Date().toISOString(),
        note: '',
        isPinned: false,
      })
    }
    return [...byId.values()]
  }, [places, messages])

  // ── 路线优化 ───────────────────────────────────────────────────────────
  const { itinerary, isOptimizing, backupPool, optimize, restoreItinerary } = useOptimize(threadId, roomId)
  const [isBackupOpen, setIsBackupOpen] = useState(false)
  const [isPlanning, setIsPlanning] = useState(false)

  const { isChatOpen, tripDays: storeDays, setTripDays, setIsChatOpen, setRightTab, setSelectedPlaceId } = useRoomStore()
  const [mobileView, setMobileView] = useState<'map' | 'places' | 'chat'>('places')

  const selectMobileView = useCallback((view: 'map' | 'places' | 'chat') => {
    setMobileView(view)
    setIsChatOpen(view === 'chat')
  }, [setIsChatOpen])

  const toggleChat = useCallback(() => {
    const nextOpen = !isChatOpen
    setIsChatOpen(nextOpen)
    setMobileView(nextOpen ? 'chat' : 'places')
  }, [isChatOpen, setIsChatOpen])

  // ── 天气 ───────────────────────────────────────────────────────────────
  const [weather, setWeather] = useState<null | {
    city: string
    days: { date: string; condition: string; icon: string; temp_high: number; temp_low: number; suggestion: string }[]
  }>(null)

  // ── 持久化：从 DB 恢复景点（进入房间时） ─────────────────────────────
  const dbLoadedRef = useRef(false)
  const isSyncingFromDB = useRef(false)
  const [dbReady, setDbReady] = useState(false)

  useEffect(() => {
    if (!roomData.loaded || dbLoadedRef.current) return
    dbLoadedRef.current = true
    ;(async () => {
      try {
        const dbPlaces = await api.get<Record<string, unknown>[]>(`/api/room/${roomId}/places`)
        if (!dbPlaces.length) {
          setDbReady(true)
          return
        }
        isSyncingFromDB.current = true
        dbPlaces.forEach((raw) => {
          try {
            const place = parsePlaceFromAPI(raw)
            if (!places.find(p => p.placeId === place.placeId)) {
              addPlace(place as any)
              if (raw.room_selected === true) toggleVote(place.placeId)
            }
          } catch { /* 格式错误跳过 */ }
        })
        // voted_by 恢复：通过 updateNote 的方式处理 votedBy（Yjs addPlace 会重置 votedBy，这里额外回写）
        // 简化处理：voted_by 在协同房间内是实时的，历史数据只恢复景点列表即可
        setTimeout(() => {
          isSyncingFromDB.current = false
          setDbReady(true)
        }, 500)
      } catch {
        toast('候选地点暂时无法从房间记录恢复，已暂停同步以保护原数据', 'warning')
      }
    })()
  }, [roomData.loaded]) // eslint-disable-line

  // ── 持久化：Yjs places 变化时同步到 DB（防抖 2s） ────────────────────
  const syncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const placeSyncChainRef = useRef<Promise<void>>(Promise.resolve())
  const placeSyncUncertainRef = useRef(false)

  useEffect(() => {
    // 初始加载阶段 / 从 DB 恢复时不触发同步
    if (isSyncingFromDB.current || !roomData.loaded || !dbReady) return

    if (syncTimerRef.current) clearTimeout(syncTimerRef.current)
    syncTimerRef.current = setTimeout(() => {
      const snapshot = places.map(placeToRaw)
      placeSyncChainRef.current = placeSyncChainRef.current
        .then(async () => {
          if (placeSyncUncertainRef.current) return
          await api.post(`/api/room/${roomId}/places/sync`, {
            places: snapshot,
          })
        })
        .catch(() => {
          if (placeSyncUncertainRef.current) return
          placeSyncUncertainRef.current = true
          toast(
            '候选地点保存结果暂时无法确认；本会话内容仍保留，刷新房间前不会继续覆盖服务端记录。',
            'warning',
          )
        })
    }, 2000)

    return () => { if (syncTimerRef.current) clearTimeout(syncTimerRef.current) }
  }, [places, roomData.loaded, dbReady]) // eslint-disable-line

  // ── 持久化：排线完成后自动保存路线 ────────────────────────────────────
  const savedItineraryRef = useRef<string | null>(null)
  const savingItineraryRef = useRef<string | null>(null)
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [savedFingerprint, setSavedFingerprint] = useState<string | null>(null)
  const [saveRetry, setSaveRetry] = useState(0)
  useEffect(() => {
    if (!itinerary || !user || !roomData.loaded) return
    const key = stableFingerprint(itinerary)
    if (savedItineraryRef.current === key || savingItineraryRef.current === key) return
    let cancelled = false
    let writeAttempted = false
    savingItineraryRef.current = key
    setSaveStatus('saving')
    setSavedFingerprint(null)
    const markSaved = () => {
      if (cancelled) return
      savedItineraryRef.current = key
      setSavedFingerprint(key)
      setSaveStatus('saved')
    }
    const latestMatches = async () => {
      try {
        const latest = await api.get<{ itinerary_data: unknown }>(
          `/api/room/${encodeURIComponent(roomId)}/itinerary`,
        )
        const parsed = parseSavedItinerary(latest.itinerary_data)
        return parsed !== null && stableFingerprint(latest.itinerary_data) === key
      } catch (failure) {
        if (failure instanceof ApiRequestError && failure.status === 404)
          return false
        throw failure
      }
    }
    ;(async () => {
      try {
        if (await latestMatches()) {
          markSaved()
          return
        }
        writeAttempted = true
        const saved = await api.post<{ ok: boolean; itinerary_id: string }>(
          `/api/room/${encodeURIComponent(roomId)}/itinerary`,
          {
            itinerary_data: itinerary,
            city: tripCity,
            trip_days: storeDays || tripDays,
          },
        )
        if (!saved.ok || !saved.itinerary_id)
          throw new Error('INVALID_SAVE_RESPONSE')
        markSaved()
      } catch {
        if (cancelled) return
        if (writeAttempted) {
          try {
            if (await latestMatches()) {
              markSaved()
              return
            }
          } catch {
            /* The explicit retry will always read before another write. */
          }
        }
        if (!cancelled) setSaveStatus('error')
      } finally {
        if (savingItineraryRef.current === key)
          savingItineraryRef.current = null
      }
    })()
    return () => { cancelled = true }
  }, [itinerary, roomData.loaded, roomId, saveRetry, storeDays, tripCity, tripDays, user])

  const restoredRoomRef = useRef<string | null>(null)
  useEffect(() => {
    if (!roomData.loaded || !user || restoredRoomRef.current === roomId || itinerary) return
    restoredRoomRef.current = roomId
    let cancelled = false
    api.get<{ itinerary_data: unknown }>(`/api/room/${roomId}/itinerary`)
      .then((result) => {
        if (cancelled || !result.itinerary_data) return
        const restored = restoreItinerary(result.itinerary_data)
        if (!restored) throw new Error('INVALID_SAVED_ITINERARY')
        const fingerprint = stableFingerprint(restored)
        savedItineraryRef.current = fingerprint
        setSavedFingerprint(fingerprint)
        setSaveStatus('saved')
      })
      .catch((failure) => {
        if (cancelled) return
        if (!(failure instanceof Error && 'status' in failure && failure.status === 404)) {
          toast('已进入房间，但上次保存的路线暂时无法读取', 'warning')
        }
      })
    return () => { cancelled = true }
  }, [itinerary, restoreItinerary, roomData.loaded, roomId, toast, user])

  const [isTransferring, setIsTransferring] = useState(false)
  const transferAttemptRef = useRef<{ fingerprint: string; key: string } | null>(null)
  const handleTransfer = useCallback(async () => {
    if (!savedFingerprint || isTransferring) return
    setIsTransferring(true)
    const previous = transferAttemptRef.current
    const idempotencyKey = previous?.fingerprint === savedFingerprint
      ? previous.key
      : crypto.randomUUID()
    transferAttemptRef.current = { fingerprint: savedFingerprint, key: idempotencyKey }
    try {
      const accepted = await api.postWithHeaders<TripUnderstandingAcceptedView>(
        '/api/v3/trip-understandings/from-collaboration',
        { room_id: roomId },
        { 'Idempotency-Key': idempotencyKey },
      )
      sessionStorage.setItem('bt_active_trip_mode', 'CLAIMED')
      router.push(`/trip/result#trip=${encodeURIComponent(accepted.public_resource_id)}`)
    } catch (failure) {
      if (
        failure instanceof ApiRequestError &&
        failure.code === 'IDEMPOTENCY_KEY_REUSED'
      ) {
        transferAttemptRef.current = null
        try {
          const latest = await api.get<{ itinerary_data: unknown }>(
            `/api/room/${encodeURIComponent(roomId)}/itinerary`,
          )
          const restored = restoreItinerary(latest.itinerary_data)
          if (!restored) throw new Error('INVALID_SAVED_ITINERARY')
          const fingerprint = stableFingerprint(restored)
          savedItineraryRef.current = fingerprint
          setSavedFingerprint(fingerprint)
          setSaveStatus('saved')
          toast('已保存路线发生变化，请核对最新版后再次转入行程查。', 'warning')
        } catch {
          setSavedFingerprint(null)
          setSaveStatus('error')
          toast('最新版路线暂时无法读取，请先重试保存或稍后再试。', 'error')
        }
      } else if (
        failure instanceof ApiRequestError &&
        failure.code === 'COLLABORATION_ROUTE_UNAVAILABLE'
      ) {
        transferAttemptRef.current = null
        setSavedFingerprint(null)
        setSaveStatus('error')
        toast('当前没有可转入的已保存路线，请先重新排线并保存。', 'warning')
      } else {
        toast('暂时没有转入成功。再次尝试会安全地续用同一次请求。', 'error')
      }
    } finally {
      setIsTransferring(false)
    }
  }, [isTransferring, restoreItinerary, roomId, router, savedFingerprint, toast])

  // ── 初始化 ─────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!roomData.loaded) return
    setTripDays(tripDays)
    initRoom({ roomId, threadId, tripCity, tripDays })
  }, [roomId, roomData.loaded]) // eslint-disable-line

  useEffect(() => {
    if (!tripCity) return
    let cancelled = false
    ;(async () => {
      try {
        const data = await api.get<typeof weather>(
          `/api/weather?city=${encodeURIComponent(tripCity)}`,
        )
        if (!cancelled && data) setWeather(data)
      } catch { /* 静默降级 */ }
    })()
    return () => { cancelled = true }
  }, [tripCity])

  useEffect(() => {
    if (itinerary) setRightTab('itinerary')
  }, [itinerary]) // eslint-disable-line

  // ── 首次进入房间：自动唤起 AI 旅行顾问发送初始化「美景/美食/美梦」请求 ──
  // 取代过去的 /api/recommend 兜底（结果太单调），改用完整 AI 链路（RAG + 高德 + LLM 增强）
  // 触发条件：房间元数据已加载 / Yjs 已连接 / 没有任何已有地点（防止他人加入时重放）/ 当前会话还没消息
  const [autoInitFired, setAutoInitFired] = useState(false)
  useEffect(() => {
    if (!roomData.loaded || autoInitFired) return
    if (!isConnected) return
    if (places.length > 0 || messages.length > 0 || isStreaming) return
    if (!tripCity || !threadId) return
    setAutoInitFired(true)
    const days = storeDays || tripDays
    const prompt = `你好！欢迎来到 ${tripCity} ${days} 天行程的协同规划房间 ✨

请帮我搭建初版推荐清单，**总数控制在 15 个以内**（无论我几天几人），三类各约 5 个，宁缺毋滥：
🏞 美景：约 5 个必去地标（覆盖核心片区即可，不要堆数量）
🍜 美食：约 5 家代表性餐厅（本地老字号 / 高分性价比为主，不要重复分店）
🏨 美梦：约 5 家不同价位的酒店或民宿（标注大致价位与所在片区）

每个地点一句话特色描述。优先高评分、知名度高的，剩余可在用户追问时再补充。`
    sendMessage(prompt, [], tripCity)
  }, [roomData.loaded, autoInitFired, isConnected, places.length, messages.length, isStreaming, tripCity, threadId, tripDays, storeDays, sendMessage])

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
    if (isPlanning || isOptimizing) return
    if (saveStatus === 'saving') {
      toast('正在保存上一版路线，请稍候再重新排线', 'info')
      return
    }
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
    setIsPlanning(true)
    try {
      const latestUserText = [...messages].reverse().find(message => message.role === 'user')?.content
        || `${tripCity}${storeDays || tripDays}日游`
      let parsedTaskSpec
      try {
        const parsed = await api.post<{
          needs_clarification?: boolean
          clarification_message?: string
          task_spec?: TripTaskSpec
        }>(`/api/room/${encodeURIComponent(roomId)}/task/parse`, {
          text: latestUserText,
          default_city: tripCity,
          default_days: storeDays || tripDays,
        })
        if (parsed.needs_clarification) {
          toast(parsed.clarification_message || '关键约束仍需确认，暂不生成可能误导的行程', 'warning')
          return
        }
        parsedTaskSpec = parsed.task_spec
      } catch {
        toast('任务约束解析失败，未开始排线', 'error')
        return
      }
      setPhase('optimizing')
      const optimized = await optimize(selectedPlaces, storeDays || tripDays, undefined, parsedTaskSpec)
      if (!optimized) {
        setPhase('selecting')
        toast('路线暂不可用，候选地点仍已保留，可以稍后重试', 'error')
        return
      }
      setPhase('planned')
      // 备选池提示（A7）
      if (backupPool.length > 0) {
        toast(`${backupPool.length} 个地点因时间限制未能排入，已放入「备选」`, 'info')
        setIsBackupOpen(true)
      }
    } finally {
      setIsPlanning(false)
    }
  }

  const selectedCount = places.filter((p) => p.votedBy.length > 0).length

  if (!isHydrated || !user) return null

  if (!roomData.loaded) {
    return (
      <main className="experience">
        <header className="e-header">
          <button type="button" className="e-button e-button-quiet" onClick={() => router.push('/collaborate')}>
            返回协同规划
          </button>
        </header>
        <section className="e-lock-state" role={roomLoadError ? 'alert' : 'status'}>
          <h1>{roomLoadError ? '这个房间暂时无法打开' : '正在读取房间…'}</h1>
          {roomLoadError && (
            <>
              <p className="e-muted">{roomLoadError}</p>
              <button type="button" className="e-button e-button-primary" onClick={() => setRoomLoadAttempt((value) => value + 1)}>
                重试
              </button>
            </>
          )}
        </section>
      </main>
    )
  }

  return (
    <div className="h-[100dvh] w-screen overflow-hidden relative">
      <AMapContainer places={mapPlaces} itinerary={itinerary} tripCity={tripCity} />

      {/* 备选抽屉（A7） */}
      <BackupDrawer
        places={backupPool}
        isOpen={isBackupOpen}
        onClose={() => setIsBackupOpen(false)}
        onAddToTrip={(place) => {
          addPlace(place)
          toggleVote(place.placeId)
          setIsBackupOpen(false)
        }}
      />

      <div className="overlay-layer flex flex-col">
        <TopNav
          roomId={roomId}
          tripCity={tripCity}
          tripDays={storeDays || tripDays}
          isConnected={isConnected}
          members={members}
          isChatOpen={isChatOpen}
          onToggleChat={toggleChat}
          selectedCount={selectedCount}
          isOptimizing={isPlanning || isOptimizing}
          hasItinerary={!!itinerary}
          onOptimize={handleOptimize}
          onViewItinerary={() => router.push(`/room/${roomId}/itinerary`)}
          saveStatus={saveStatus}
          canTransfer={saveStatus === 'saved' && Boolean(itinerary) && savedFingerprint === stableFingerprint(itinerary)}
          isTransferring={isTransferring}
          onTransfer={() => void handleTransfer()}
          onRetrySave={() => setSaveRetry((value) => value + 1)}
        />

        <div className="hidden lg:flex flex-1 min-h-0 items-start gap-3 px-4 pb-3 mt-3">
          <AnimatePresence>
            {isChatOpen && (
              <GlassPanel
                solid
                className="overlay-interactive w-[380px] h-full flex-shrink-0 flex flex-col overflow-hidden"
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
            className="overlay-interactive w-[360px] h-full flex-shrink-0 flex flex-col overflow-hidden"
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

        <div className="lg:hidden flex-1 min-h-0 px-3 pt-3 pb-[calc(72px+env(safe-area-inset-bottom))]">
          {mobileView === 'chat' && (
            <GlassPanel data-testid="mobile-chat-panel" solid className="overlay-interactive w-full h-full flex flex-col overflow-hidden">
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

          {mobileView === 'places' && (
            <GlassPanel data-testid="mobile-place-panel" solid className="overlay-interactive w-full h-full flex flex-col overflow-hidden">
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
          )}
        </div>

        <nav
          aria-label="协同工作区"
          className="overlay-interactive lg:hidden fixed inset-x-3 bottom-[calc(8px+env(safe-area-inset-bottom))] grid grid-cols-3 gap-1 rounded-2xl border border-white/80 bg-white/95 p-1.5 shadow-xl backdrop-blur"
        >
          {([
            ['map', '地图'],
            ['places', `地点${selectedCount > 0 ? ` · ${selectedCount}` : ''}`],
            ['chat', 'AI 顾问'],
          ] as const).map(([view, label]) => (
            <button
              key={view}
              type="button"
              aria-pressed={mobileView === view}
              onClick={() => selectMobileView(view)}
              className={`min-h-11 rounded-xl px-2 text-sm font-semibold transition-colors ${
                mobileView === view
                  ? 'bg-sky-700 text-white shadow-sm'
                  : 'text-slate-600 hover:bg-sky-50 focus-visible:bg-sky-50'
              }`}
            >
              {label}
            </button>
          ))}
        </nav>
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
    constraint_evidence: p.constraintEvidence,
    estimated_duration: p.estimatedDuration,
    room_selected: p.votedBy.length > 0,
  }
}
