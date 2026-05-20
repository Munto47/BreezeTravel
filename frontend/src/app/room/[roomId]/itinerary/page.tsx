'use client'

import { useEffect, useState } from 'react'
import { useParams, useRouter } from 'next/navigation'
import { motion } from 'framer-motion'
import { ArrowLeft, MapPin, Calendar, Route, Clock, Car, Star, AlertTriangle, Lightbulb } from 'lucide-react'

import type { Itinerary, DayPlan, TimeSlot } from '@/types/itinerary'

const CLUSTER_COLORS = ['#FF5A5F', '#3B82F6', '#10B981', '#F59E0B', '#8B5CF6', '#06B6D4']

const CATEGORY_ICON: Record<string, string> = {
  attraction: '🏛️', food: '🍜', hotel: '🏨', transport: '🚉',
}
const CATEGORY_LABEL: Record<string, string> = {
  attraction: '景点', food: '餐饮', hotel: '住宿', transport: '交通',
}
const WEATHER_ICON: Record<string, string> = {
  晴: '☀️', 多云: '⛅', 阴: '☁️', 小雨: '🌧️', 雨: '🌧️', 雪: '❄️', 雷: '⛈️',
}

function getWeatherIcon(condition: string): string {
  for (const [key, icon] of Object.entries(WEATHER_ICON)) {
    if (condition.includes(key)) return icon
  }
  return '🌤️'
}

function SlotCard({ slot, isLast, dayColor }: { slot: TimeSlot; isLast: boolean; dayColor: string }) {
  const icon = CATEGORY_ICON[slot.place.category] ?? '📍'
  const label = CATEGORY_LABEL[slot.place.category] ?? slot.place.category
  const hasPhoto = slot.place.amapPhotos && slot.place.amapPhotos.length > 0

  return (
    <div className="relative">
      {!isLast && (
        <div className="absolute left-[19px] top-[52px] w-0.5 bottom-0 z-0" style={{ background: `${dayColor}30` }} />
      )}

      <div className="flex gap-3 relative z-10">
        {/* 时间轴圆点 */}
        <div className="flex flex-col items-center flex-shrink-0">
          <div
            className="w-10 h-10 rounded-full flex items-center justify-center text-lg border-2 border-white shadow-sm"
            style={{ backgroundColor: `${dayColor}15`, borderColor: `${dayColor}40` }}
          >
            {icon}
          </div>
        </div>

        {/* 内容卡片 */}
        <div className="flex-1 pb-6">
          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-gray-100 shadow-sm overflow-hidden hover:shadow-md transition-shadow">
            {/* 图片 */}
            {hasPhoto && (
              <div className="h-24 overflow-hidden">
                <img
                  src={slot.place.amapPhotos[0]}
                  alt={slot.place.name}
                  className="w-full h-full object-cover"
                />
              </div>
            )}
            <div className="p-4">
              {/* 时间 + 标签 */}
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-[11px] font-mono text-gray-400 flex items-center gap-1">
                  <Clock className="w-3 h-3" />
                  {slot.startTime} – {slot.endTime}
                </span>
                <span
                  className="text-[10px] font-medium px-2 py-0.5 rounded-full"
                  style={{ background: `${dayColor}15`, color: dayColor }}
                >
                  {label}
                </span>
              </div>

              <h3 className="font-bold text-gray-900 text-sm">{slot.place.name}</h3>

              {slot.place.description && (
                <p className="text-xs text-gray-500 mt-1 leading-relaxed">{slot.place.description}</p>
              )}
              {!slot.place.description && slot.place.address && (
                <p className="text-xs text-gray-400 mt-1 flex items-center gap-1 truncate">
                  <MapPin className="w-3 h-3 flex-shrink-0" />{slot.place.address}
                </p>
              )}

              {slot.place.tags && slot.place.tags.length > 0 && (
                <div className="flex flex-wrap gap-1 mt-2">
                  {slot.place.tags.slice(0, 3).map((tag: string) => (
                    <span key={tag} className="text-[10px] px-1.5 py-0.5 rounded-md bg-gray-50 text-gray-400 border border-gray-100">{tag}</span>
                  ))}
                </div>
              )}

              {(slot.place.amapRating || slot.place.amapPrice) && (
                <div className="flex items-center gap-3 mt-2">
                  {slot.place.amapRating && (
                    <span className="text-xs text-amber-500 flex items-center gap-0.5">
                      <Star className="w-3 h-3 fill-amber-400 text-amber-400" />
                      {slot.place.amapRating}
                    </span>
                  )}
                  {slot.place.amapPrice && (
                    <span className="text-xs text-gray-400">¥{slot.place.amapPrice}/人</span>
                  )}
                </div>
              )}

              {slot.place.ragMeta?.tipSnippets?.[0] && (
                <div className="mt-2.5 flex gap-1.5 items-start bg-amber-50/80 rounded-lg px-2.5 py-2 border border-amber-100/60">
                  <AlertTriangle className="w-3 h-3 text-amber-400 flex-shrink-0 mt-0.5" />
                  <p className="text-[11px] text-amber-700/80 leading-relaxed">{slot.place.ragMeta.tipSnippets[0]}</p>
                </div>
              )}

              {/* 温馨提示（TipsGenerator 生成） */}
              {slot.tips && slot.tips.length > 0 && (
                <div className="mt-2.5 space-y-1.5">
                  {slot.tips.map((tip: string, i: number) => (
                    <div key={i} className="flex gap-1.5 items-start bg-blue-50/80 rounded-lg px-2.5 py-2 border border-blue-100/60">
                      <Lightbulb className="w-3 h-3 text-blue-400 flex-shrink-0 mt-0.5" />
                      <p className="text-[11px] text-blue-700/80 leading-relaxed">{tip}</p>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* 交通段 */}
          {!isLast && slot.transport && (
            <div className="flex items-center gap-1.5 mt-2 ml-2 text-xs text-gray-400">
              <Car className="w-3.5 h-3.5" />
              <span>驾车约 {slot.transport.durationMins} 分钟</span>
              <span className="text-gray-200">·</span>
              <span>{slot.transport.distanceKm} km</span>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function DaySection({ day, index }: { day: DayPlan; index: number }) {
  const dayColor = CLUSTER_COLORS[index % CLUSTER_COLORS.length]
  const dateLabel = day.date
    ? new Date(day.date).toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'short' })
    : null

  return (
    <motion.section
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.1 }}
      className="mb-8"
    >
      {/* Day 标题 */}
      <div className="flex items-center gap-3 mb-4 sticky top-[57px] bg-gray-50/90 backdrop-blur-sm py-2 z-10 -mx-4 px-4">
        <div
          className="w-9 h-9 rounded-xl text-white text-sm font-bold flex items-center justify-center flex-shrink-0 shadow-sm"
          style={{ backgroundColor: dayColor }}
        >
          D{day.dayIndex + 1}
        </div>
        <div>
          <p className="font-bold text-gray-900 text-sm">第 {day.dayIndex + 1} 天</p>
          {dateLabel && <p className="text-xs text-gray-400">{dateLabel}</p>}
        </div>
        <span className="text-xs text-gray-300 ml-1">{day.slots.length} 个地点</span>

        {/* 天气卡 */}
        {day.weatherSummary && (
          <div className="ml-auto flex items-center gap-2 bg-sky-50/80 border border-sky-100 rounded-lg px-3 py-1.5">
            <span className="text-lg">{getWeatherIcon(day.weatherSummary.condition)}</span>
            <div>
              <p className="text-xs font-medium text-sky-800">
                {day.weatherSummary.condition} {day.weatherSummary.tempLow}°–{day.weatherSummary.tempHigh}°
              </p>
              <p className="text-[10px] text-sky-600">{day.weatherSummary.suggestion}</p>
            </div>
          </div>
        )}
      </div>

      <div className="ml-1">
        {day.slots.map((slot, idx) => (
          <SlotCard
            key={slot.placeId}
            slot={slot}
            isLast={idx === day.slots.length - 1}
            dayColor={dayColor}
          />
        ))}
      </div>
    </motion.section>
  )
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export default function ItineraryPage() {
  const params = useParams()
  const router = useRouter()
  const roomId = params.roomId as string
  const [itinerary, setItinerary] = useState<Itinerary | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (typeof window === 'undefined') return

    const stored = localStorage.getItem(`itinerary_${roomId}`)
    if (stored) {
      try {
        setItinerary(JSON.parse(stored))
        setLoading(false)
        return
      } catch (e) {
        console.error('[ItineraryPage] localStorage parse failed', e)
      }
    }

    // localStorage miss — 从 API 恢复（跨设备/清缓存场景）
    const token = localStorage.getItem('authToken')
    if (!token) { setLoading(false); return }

    fetch(`${API_BASE}/api/room/${roomId}/itinerary`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => {
        const itin = data.itinerary_data as Itinerary
        setItinerary(itin)
        // 回填 localStorage 供下次直接读取
        localStorage.setItem(`itinerary_${roomId}`, JSON.stringify(itin))
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [roomId])

  const totalPlaces = itinerary?.days.reduce((s, d) => s + d.slots.length, 0) ?? 0
  const totalDays = itinerary?.days.length ?? 0

  if (loading) return (
    <div className="min-h-screen bg-gray-50 flex flex-col">
      <header className="bg-white/90 backdrop-blur-md border-b border-gray-100 px-4 py-3 flex items-center gap-3 sticky top-0 z-20 shadow-sm">
        <div className="w-20 h-4 bg-gray-200 rounded animate-pulse" />
        <div className="w-px h-4 bg-gray-200" />
        <div className="w-16 h-4 bg-gray-200 rounded animate-pulse" />
      </header>
      <div className="max-w-2xl mx-auto w-full p-4 space-y-4 mt-4">
        {[1, 2, 3].map(i => (
          <div key={i} className="bg-white rounded-2xl p-4 space-y-3 animate-pulse">
            <div className="h-5 w-16 bg-gray-200 rounded" />
            {[1, 2].map(j => (
              <div key={j} className="flex gap-3">
                <div className="w-10 h-10 rounded-full bg-gray-100 shrink-0" />
                <div className="flex-1 space-y-2">
                  <div className="h-4 w-32 bg-gray-200 rounded" />
                  <div className="h-3 w-48 bg-gray-100 rounded" />
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  )

  return (
    <div className="min-h-screen bg-gray-50">
      {/* 顶部导航 */}
      <header className="bg-white/90 backdrop-blur-md border-b border-gray-100 px-4 py-3 flex items-center gap-3 sticky top-0 z-20 shadow-sm">
        <button
          onClick={() => router.push(`/room/${roomId}`)}
          className="flex items-center gap-1.5 text-sm text-gray-500 hover:text-coral-500 transition-colors"
        >
          <ArrowLeft className="w-4 h-4" />
          返回工作台
        </button>
        <div className="w-px h-4 bg-gray-200" />
        <h1 className="font-bold text-gray-900 text-sm">行程详情</h1>
        {itinerary && (
          <div className="ml-auto flex items-center gap-2 text-xs text-gray-400">
            <MapPin className="w-3.5 h-3.5 text-coral-400" />
            {itinerary.city}
            <span className="text-gray-200">·</span>
            <Calendar className="w-3.5 h-3.5" />
            {totalDays} 天
            <span className="text-gray-200">·</span>
            {totalPlaces} 个地点
          </div>
        )}
      </header>

      <div className="max-w-2xl mx-auto px-4 pb-12">
        {!itinerary ? (
          /* 空状态 */
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            className="mt-20 text-center"
          >
            <div className="w-20 h-20 rounded-3xl bg-gray-100 flex items-center justify-center text-4xl mx-auto mb-5">
              🗓️
            </div>
            <p className="text-base font-semibold text-gray-600">行程尚未生成</p>
            <p className="text-sm text-gray-400 mt-1.5">请返回工作台选择地点后点击「智能排线」</p>
            <button
              onClick={() => router.push(`/room/${roomId}`)}
              className="btn-coral mt-5 px-6 py-2.5 text-sm"
            >
              返回工作台
            </button>
          </motion.div>
        ) : (
          <>
            {/* 概览 Banner */}
            <motion.div
              initial={{ opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              className="mt-5 mb-6 rounded-2xl overflow-hidden shadow-md"
              style={{ background: `linear-gradient(135deg, ${CLUSTER_COLORS[0]}, ${CLUSTER_COLORS[1]})` }}
            >
              <div className="px-6 py-5 text-white">
                <p className="text-xs opacity-70 mb-1 flex items-center gap-1">
                  <Route className="w-3 h-3" /> AI 智能排线结果
                </p>
                <h2 className="text-2xl font-bold mb-4">
                  {itinerary.city} {totalDays} 日游
                </h2>
                <div className="flex gap-6">
                  {[
                    { label: '景点数', value: `${totalPlaces} 个` },
                    { label: '行程天数', value: `${totalDays} 天` },
                    { label: '排线算法', value: 'K-Means + TSP' },
                  ].map((stat) => (
                    <div key={stat.label}>
                      <p className="text-xs opacity-60">{stat.label}</p>
                      <p className="font-bold text-sm mt-0.5">{stat.value}</p>
                    </div>
                  ))}
                </div>
              </div>
              {/* 每日色条 */}
              <div className="flex h-1.5">
                {itinerary.days.map((_, i) => (
                  <div key={i} className="flex-1" style={{ backgroundColor: CLUSTER_COLORS[i % CLUSTER_COLORS.length] }} />
                ))}
              </div>
            </motion.div>

            {/* 每日行程 */}
            {itinerary.days.map((day, i) => (
              <DaySection key={day.dayIndex} day={day} index={i} />
            ))}

            <p className="text-center text-[11px] text-gray-300 mt-4">
              由 BreezeTravel AI 生成 · {new Date(itinerary.generatedAt).toLocaleString('zh-CN')}
            </p>
          </>
        )}
      </div>
    </div>
  )
}
