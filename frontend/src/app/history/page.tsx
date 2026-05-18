'use client'

import { useState, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { motion } from 'framer-motion'
import { ArrowLeft, MapPin, Calendar, Route, Download, ArrowRight, Clock } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import { api } from '@/lib/api'

interface RoomRecord {
  room_id: string
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
  const [tab, setTab] = useState<Tab>('rooms')
  const [rooms, setRooms] = useState<RoomRecord[]>([])
  const [itineraries, setItineraries] = useState<ItineraryRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [exporting, setExporting] = useState<string | null>(null)

  useEffect(() => {
    if (isHydrated && !user) router.replace('/login')
  }, [isHydrated, user, router])

  useEffect(() => {
    if (!user) return
    setLoading(true)
    Promise.all([
      api.get<RoomRecord[]>('/api/user/rooms'),
      api.get<ItineraryRecord[]>('/api/user/itineraries'),
    ]).then(([r, i]) => {
      setRooms(r)
      setItineraries(i)
    }).catch(() => {}).finally(() => setLoading(false))
  }, [user])

  const handleExport = async (item: ItineraryRecord) => {
    setExporting(item.id)
    try {
      await api.download(
        `/api/itinerary/${item.id}/export`,
        `BreezeTravel_${item.city || '旅行'}_${item.trip_days}天路线.html`,
      )
    } catch (e: unknown) {
      alert(e instanceof Error ? e.message : '导出失败')
    } finally {
      setExporting(null)
    }
  }

  const formatDate = (iso: string) =>
    new Date(iso).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })

  const phaseLabel: Record<string, string> = {
    exploring: '探索中',
    selecting: '选择中',
    optimizing: '排线中',
    planned: '已排线',
  }

  if (!isHydrated || !user) return null

  return (
    <div className="min-h-screen bg-gradient-to-br from-coral-50/40 via-white to-blue-50/30">
      {/* 顶栏 */}
      <div className="sticky top-0 z-10 bg-white/80 backdrop-blur-md border-b border-gray-100 px-4 py-3 flex items-center gap-3">
        <button onClick={() => router.back()} className="p-2 rounded-xl hover:bg-gray-100 transition-colors">
          <ArrowLeft className="w-5 h-5 text-gray-600" />
        </button>
        <h1 className="font-semibold text-gray-800">旅行历史</h1>
      </div>

      {/* Tab 切换 */}
      <div className="sticky top-[53px] z-10 bg-white/80 backdrop-blur-md border-b border-gray-100 px-4">
        <div className="flex gap-1 max-w-md mx-auto">
          {([['rooms', '规划房间'], ['itineraries', '已排路线']] as [Tab, string][]).map(([key, label]) => (
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
          <div className="flex justify-center py-16">
            <span className="w-6 h-6 border-2 border-coral-200 border-t-coral-500 rounded-full animate-spin" />
          </div>
        ) : tab === 'rooms' ? (
          <div className="space-y-3 pt-2">
            {rooms.length === 0 ? (
              <EmptyState icon={<MapPin className="w-8 h-8 text-gray-300" />} text="还没有规划记录" />
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
                    <div className="flex items-center gap-2 mb-1">
                      <span className="font-semibold text-gray-800 text-base">
                        {room.city || '未知目的地'}
                      </span>
                      <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${
                        room.phase === 'planned'
                          ? 'bg-emerald-50 text-emerald-600'
                          : 'bg-blue-50 text-blue-500'
                      }`}>
                        {phaseLabel[room.phase] || room.phase}
                      </span>
                    </div>
                    <div className="flex items-center gap-3 text-xs text-gray-400">
                      <span className="flex items-center gap-1">
                        <Calendar className="w-3 h-3" />{room.trip_days} 天
                      </span>
                      <span className="flex items-center gap-1">
                        <MapPin className="w-3 h-3" />{room.place_count} 个景点
                      </span>
                      <span className="flex items-center gap-1">
                        <Clock className="w-3 h-3" />{formatDate(room.created_at)}
                      </span>
                    </div>
                  </div>
                  <button
                    onClick={() => router.push(`/room/${room.room_id}`)}
                    className="flex-shrink-0 flex items-center gap-1 text-xs text-coral-500 font-medium hover:text-coral-600 transition-colors py-1"
                  >
                    继续规划 <ArrowRight className="w-3 h-3" />
                  </button>
                </div>
                <div className="mt-2 pt-2 border-t border-gray-50 flex items-center justify-between">
                  <span className="text-[10px] text-gray-300 font-mono">房间号 {room.room_id}</span>
                  {room.itinerary_count > 0 && (
                    <span className="text-[10px] text-emerald-500 flex items-center gap-1">
                      <Route className="w-3 h-3" />{room.itinerary_count} 条路线已保存
                    </span>
                  )}
                </div>
              </motion.div>
            ))}
          </div>
        ) : (
          <div className="space-y-3 pt-2">
            {itineraries.length === 0 ? (
              <EmptyState icon={<Route className="w-8 h-8 text-gray-300" />} text="还没有保存路线" />
            ) : itineraries.map((item, i) => (
              <motion.div
                key={item.id}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.05 }}
                className="glass-panel-solid rounded-2xl p-4 shadow-sm"
              >
                <div className="flex items-center justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <p className="font-semibold text-gray-800 text-base">
                      {item.city || '旅行路线'}
                    </p>
                    <div className="flex items-center gap-3 mt-1 text-xs text-gray-400">
                      <span className="flex items-center gap-1">
                        <Calendar className="w-3 h-3" />{item.trip_days} 天
                      </span>
                      <span className="flex items-center gap-1">
                        <Clock className="w-3 h-3" />{formatDate(item.created_at)}
                      </span>
                    </div>
                  </div>
                  <button
                    onClick={() => handleExport(item)}
                    disabled={exporting === item.id}
                    className="flex-shrink-0 flex items-center gap-1.5 text-xs text-white bg-coral-500 hover:bg-coral-600 disabled:opacity-60 px-3 py-2 rounded-xl font-medium transition-colors"
                  >
                    {exporting === item.id ? (
                      <span className="w-3 h-3 border border-white/40 border-t-white rounded-full animate-spin" />
                    ) : (
                      <Download className="w-3 h-3" />
                    )}
                    导出 HTML
                  </button>
                </div>
              </motion.div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function EmptyState({ icon, text }: { icon: React.ReactNode; text: string }) {
  return (
    <div className="flex flex-col items-center py-16 gap-3 text-gray-300">
      {icon}
      <p className="text-sm">{text}</p>
    </div>
  )
}
