'use client'

import { useState, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { motion } from 'framer-motion'
import { ArrowLeft, MapPin, Calendar, Route, Download, ArrowRight, Clock, RefreshCw } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import { useToastStore } from '@/stores/toastStore'
import { api } from '@/lib/api'

interface RoomRecord {
  room_id: string
  thread_id: string
  city: string
  trip_days: number
  phase: string
  place_count: number
  itinerary_count: number
  created_at: string
}

interface ItineraryRecord {
  id: string
  room_id: string
  city: string
  trip_days: number
  created_at: string
}

type Tab = 'rooms' | 'itineraries'

export default function HistoryPage() {
  const router = useRouter()
  const { user, isHydrated } = useAuthStore()
  const toast = useToastStore(s => s.toast)
  const [tab, setTab] = useState<Tab>('rooms')
  const [rooms, setRooms] = useState<RoomRecord[]>([])
  const [itineraries, setItineraries] = useState<ItineraryRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [exporting, setExporting] = useState<string | null>(null)

  useEffect(() => {
    if (isHydrated && !user) router.replace('/login')
  }, [isHydrated, user, router])

  const loadData = () => {
    if (!user) return
    setLoading(true)
    setError(null)
    Promise.all([
      api.get<RoomRecord[]>('/api/user/rooms'),
      api.get<ItineraryRecord[]>('/api/user/itineraries'),
    ]).then(([r, i]) => {
      setRooms(Array.isArray(r) ? r : [])
      setItineraries(Array.isArray(i) ? i : [])
    }).catch((e) => {
      setError(e instanceof Error ? e.message : '加载失败，请重试')
    }).finally(() => setLoading(false))
  }

  useEffect(() => { loadData() }, [user]) // eslint-disable-line

  const handleEnterRoom = (room: RoomRecord) => {
    const params = new URLSearchParams({
      threadId: room.thread_id,
      city: room.city,
      days: String(room.trip_days),
    })
    router.push(`/room/${room.room_id}?${params.toString()}`)
  }

  const handleExport = async (item: ItineraryRecord) => {
    setExporting(item.id)
    try {
      await api.download(
        `/api/itinerary/${item.id}/export`,
        `BreezeTravel_${item.city || '旅行'}_${item.trip_days}天路线.html`,
      )
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : '导出失败，请重试', 'error')
    } finally {
      setExporting(null)
    }
  }

  const handleViewItinerary = (item: ItineraryRecord) => {
    router.push(`/room/${item.room_id}/itinerary`)
  }

  const formatDate = (iso: string) =>
    new Date(iso).toLocaleDateString('zh-CN', { year: 'numeric', month: 'short', day: 'numeric' })

  const phaseLabel: Record<string, string> = {
    exploring: '探索中', selecting: '选择中', optimizing: '排线中', planned: '已排线',
  }
  const phaseColor: Record<string, string> = {
    exploring: 'bg-blue-50 text-blue-500',
    selecting: 'bg-yellow-50 text-yellow-600',
    optimizing: 'bg-orange-50 text-orange-500',
    planned: 'bg-emerald-50 text-emerald-600',
  }

  if (!isHydrated) return (
    <div className="min-h-screen bg-gradient-to-br from-coral-50/40 via-white to-blue-50/30 flex flex-col">
      <div className="sticky top-0 z-10 bg-white/80 backdrop-blur-md border-b border-gray-100 px-4 py-3 flex items-center gap-3">
        <div className="w-9 h-9 rounded-xl bg-gray-100 animate-pulse" />
        <div className="h-4 w-20 bg-gray-200 rounded animate-pulse" />
      </div>
      <div className="max-w-md mx-auto w-full p-4 space-y-3 mt-2">
        {[1, 2, 3].map(i => (
          <div key={i} className="rounded-2xl bg-white border border-gray-100 p-4 space-y-2 animate-pulse">
            <div className="h-4 w-24 bg-gray-200 rounded" />
            <div className="h-3 w-40 bg-gray-100 rounded" />
          </div>
        ))}
      </div>
    </div>
  )
  if (!user) return null

  return (
    <div className="min-h-screen bg-gradient-to-br from-coral-50/40 via-white to-blue-50/30">
      {/* 顶栏 */}
      <div className="sticky top-0 z-10 bg-white/80 backdrop-blur-md border-b border-gray-100 px-4 py-3 flex items-center gap-3">
        <button onClick={() => router.back()} className="p-2 rounded-xl hover:bg-gray-100 transition-colors">
          <ArrowLeft className="w-5 h-5 text-gray-600" />
        </button>
        <h1 className="font-semibold text-gray-800 flex-1">旅行历史</h1>
        <button onClick={loadData} className="p-2 rounded-xl hover:bg-gray-100 transition-colors" title="刷新">
          <RefreshCw className="w-4 h-4 text-gray-400" />
        </button>
      </div>

      {/* Tab 切换 */}
      <div className="sticky top-[53px] z-10 bg-white/80 backdrop-blur-md border-b border-gray-100 px-4">
        <div className="flex gap-1 max-w-md mx-auto">
          {([
            ['rooms', `规划房间${rooms.length > 0 ? ` (${rooms.length})` : ''}`],
            ['itineraries', `已排路线${itineraries.length > 0 ? ` (${itineraries.length})` : ''}`],
          ] as [Tab, string][]).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={`flex-1 py-3 text-sm font-medium transition-colors border-b-2 ${
                tab === key ? 'border-coral-500 text-coral-500' : 'border-transparent text-gray-400'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="max-w-md mx-auto p-4">
        {loading ? (
          <div className="flex flex-col items-center py-16 gap-3">
            <span className="w-6 h-6 border-2 border-coral-200 border-t-coral-500 rounded-full animate-spin" />
            <p className="text-xs text-gray-400">加载历史记录…</p>
          </div>
        ) : error ? (
          <div className="flex flex-col items-center py-16 gap-3">
            <p className="text-sm text-red-400">{error}</p>
            <button onClick={loadData} className="text-xs text-coral-500 underline">重试</button>
          </div>
        ) : tab === 'rooms' ? (
          <div className="space-y-3 pt-2">
            {rooms.length === 0 ? (
              <EmptyState
                icon={<MapPin className="w-8 h-8 text-gray-300" />}
                text="还没有规划记录"
                actionLabel="开始第一次规划"
                onAction={() => router.push('/')}
              />
            ) : rooms.map((room, i) => (
              <motion.div
                key={room.room_id}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.05 }}
                className="glass-panel-solid rounded-2xl p-4 shadow-sm"
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1.5">
                      <span className="font-bold text-gray-900 text-base">{room.city}</span>
                      <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${phaseColor[room.phase] || 'bg-gray-50 text-gray-500'}`}>
                        {phaseLabel[room.phase] || room.phase}
                      </span>
                    </div>
                    <div className="flex items-center flex-wrap gap-x-3 gap-y-1 text-xs text-gray-400">
                      <span className="flex items-center gap-1">
                        <Calendar className="w-3 h-3" />{room.trip_days} 天
                      </span>
                      {room.place_count > 0 && (
                        <span className="flex items-center gap-1">
                          <MapPin className="w-3 h-3" />{room.place_count} 个景点
                        </span>
                      )}
                      {room.itinerary_count > 0 && (
                        <span className="flex items-center gap-1 text-emerald-500">
                          <Route className="w-3 h-3" />{room.itinerary_count} 条路线
                        </span>
                      )}
                      <span className="flex items-center gap-1">
                        <Clock className="w-3 h-3" />{formatDate(room.created_at)}
                      </span>
                    </div>
                  </div>
                  <button
                    onClick={() => handleEnterRoom(room)}
                    className="flex-shrink-0 flex items-center gap-1 text-xs text-coral-500 font-medium hover:text-coral-600 transition-colors bg-coral-50 hover:bg-coral-100 px-3 py-2 rounded-xl"
                  >
                    继续规划 <ArrowRight className="w-3 h-3" />
                  </button>
                </div>
                <div className="mt-2.5 pt-2.5 border-t border-gray-50">
                  <span className="text-[10px] text-gray-300 font-mono">房间号 {room.room_id}</span>
                </div>
              </motion.div>
            ))}
          </div>
        ) : (
          <div className="space-y-3 pt-2">
            {itineraries.length === 0 ? (
              <EmptyState
                icon={<Route className="w-8 h-8 text-gray-300" />}
                text="还没有保存路线"
                actionLabel="去规划一段旅行"
                onAction={() => router.push('/')}
              />
            ) : itineraries.map((item, i) => (
              <motion.div
                key={item.id}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.05 }}
                className="glass-panel-solid rounded-2xl p-4 shadow-sm"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <p className="font-bold text-gray-900 text-base">{item.city} · {item.trip_days} 天路线</p>
                    <div className="flex items-center gap-3 mt-1 text-xs text-gray-400">
                      <span className="flex items-center gap-1">
                        <Clock className="w-3 h-3" />{formatDate(item.created_at)}
                      </span>
                      <span className="font-mono text-gray-300">房间 {item.room_id}</span>
                    </div>
                  </div>
                  <div className="flex flex-col gap-2 flex-shrink-0">
                    <button
                      onClick={() => handleViewItinerary(item)}
                      className="flex items-center gap-1.5 text-xs text-coral-500 bg-coral-50 hover:bg-coral-100 px-3 py-2 rounded-xl font-medium transition-colors"
                    >
                      <Route className="w-3 h-3" />查看路线
                    </button>
                    <button
                      onClick={() => handleExport(item)}
                      disabled={exporting === item.id}
                      className="flex items-center gap-1.5 text-xs text-white bg-gray-700 hover:bg-gray-800 disabled:opacity-60 px-3 py-2 rounded-xl font-medium transition-colors"
                    >
                      {exporting === item.id
                        ? <span className="w-3 h-3 border border-white/40 border-t-white rounded-full animate-spin" />
                        : <Download className="w-3 h-3" />}
                      导出 HTML
                    </button>
                  </div>
                </div>
              </motion.div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function EmptyState({ icon, text, actionLabel, onAction }: {
  icon: React.ReactNode; text: string; actionLabel?: string; onAction?: () => void
}) {
  return (
    <div className="flex flex-col items-center py-16 gap-3 text-gray-300">
      {icon}
      <p className="text-sm">{text}</p>
      {actionLabel && onAction && (
        <button
          onClick={onAction}
          className="mt-1 flex items-center gap-1.5 text-sm text-coral-500 font-medium hover:text-coral-600 transition-colors bg-coral-50 hover:bg-coral-100 px-4 py-2 rounded-xl"
        >
          {actionLabel} <ArrowRight className="w-3.5 h-3.5" />
        </button>
      )}
    </div>
  )
}
