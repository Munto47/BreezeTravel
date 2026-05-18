'use client'

import { AnimatePresence, motion } from 'framer-motion'
import { Route, Heart, Layers, Mountain, UtensilsCrossed, BedDouble, ArrowUpDown, Star } from 'lucide-react'
import type { YjsPlace, RoomMember } from '@/types/room'
import type { Itinerary } from '@/types/itinerary'
import { useRoomStore, type RightPanelTab } from '@/stores/roomStore'
import PlaceCard from './PlaceCard'

interface PlaceListProps {
  places: YjsPlace[]
  currentUserId: string
  members: RoomMember[]
  itinerary: Itinerary | null
  onToggleVote: (placeId: string) => void
  onRemove: (placeId: string) => void
}

const CLUSTER_COLORS = ['#FF5A5F', '#3B82F6', '#10B981', '#F59E0B', '#8B5CF6', '#06B6D4']

// 三大板块定义
type CategoryFilter = 'sights' | 'food' | 'hotel'
const CATEGORY_TABS: {
  key: CategoryFilter
  label: string
  icon: React.ReactNode
  categories: string[]
  emptyText: string
  color: string
  bgColor: string
}[] = [
  {
    key: 'sights',
    label: '美景',
    icon: <Mountain className="w-3.5 h-3.5" />,
    categories: ['attraction', 'transport'],
    emptyText: '暂无景点推荐',
    color: 'text-blue-600',
    bgColor: 'bg-blue-50',
  },
  {
    key: 'food',
    label: '美食',
    icon: <UtensilsCrossed className="w-3.5 h-3.5" />,
    categories: ['food'],
    emptyText: '暂无美食推荐',
    color: 'text-orange-600',
    bgColor: 'bg-orange-50',
  },
  {
    key: 'hotel',
    label: '美梦',
    icon: <BedDouble className="w-3.5 h-3.5" />,
    categories: ['hotel'],
    emptyText: '暂无住宿推荐',
    color: 'text-purple-600',
    bgColor: 'bg-purple-50',
  },
]

const OUTER_TABS: { key: RightPanelTab; label: string; icon: React.ReactNode }[] = [
  { key: 'candidates', label: '候选地点', icon: <Layers className="w-3.5 h-3.5" /> },
  { key: 'itinerary', label: '已排路线', icon: <Route className="w-3.5 h-3.5" /> },
]

export default function PlaceList({
  places,
  currentUserId,
  members,
  itinerary,
  onToggleVote,
  onRemove,
}: PlaceListProps) {
  const { rightTab, setRightTab } = useRoomStore()

  // 当前用户已心形的总数
  const myVoteCount = places.filter((p) => p.votedBy.includes(currentUserId)).length

  // 每个分类的地点数量（用于徽章）
  const countByFilter = (filter: CategoryFilter) =>
    places.filter((p) => CATEGORY_TABS.find((t) => t.key === filter)?.categories.includes(p.category)).length

  return (
    <div className="h-full flex flex-col">
      {/* 外层 Tab：候选地点 / 已排路线 */}
      <div className="px-4 pt-4 pb-0 flex-shrink-0">
        <div className="flex bg-gray-100/60 rounded-lg p-0.5">
          {OUTER_TABS.map((tab) => (
            <button
              key={tab.key}
              onClick={() => setRightTab(tab.key)}
              className={`flex-1 flex items-center justify-center gap-1.5 text-xs font-medium py-2 rounded-md transition-all duration-200 ${
                rightTab === tab.key
                  ? 'bg-white text-gray-900 shadow-sm'
                  : 'text-gray-400 hover:text-gray-600'
              }`}
            >
              {tab.icon}
              {tab.label}
              {tab.key === 'candidates' && places.length > 0 && (
                <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${
                  rightTab === tab.key ? 'bg-coral-50 text-coral-500' : 'bg-gray-200/60 text-gray-400'
                }`}>
                  {places.length}
                </span>
              )}
            </button>
          ))}
        </div>
      </div>

      {/* ===== 候选地点 Tab ===== */}
      {rightTab === 'candidates' && (
        <CandidatesPanel
          places={places}
          currentUserId={currentUserId}
          members={members}
          myVoteCount={myVoteCount}
          countByFilter={countByFilter}
          onToggleVote={onToggleVote}
          onRemove={onRemove}
        />
      )}

      {/* ===== 已排路线 Tab ===== */}
      {rightTab === 'itinerary' && (
        <ItineraryPanel itinerary={itinerary} />
      )}
    </div>
  )
}

/* ─── 候选地点面板（含三大板块） ─── */
type SortOrder = 'default' | 'rating' | 'votes'

const SORT_OPTIONS: { key: SortOrder; label: string; icon: React.ReactNode }[] = [
  { key: 'default', label: 'AI 推荐', icon: <ArrowUpDown className="w-3 h-3" /> },
  { key: 'rating',  label: '评分最高', icon: <Star className="w-3 h-3" /> },
  { key: 'votes',   label: '心愿最多', icon: <Heart className="w-3 h-3" /> },
]

function CandidatesPanel({
  places, currentUserId, members, myVoteCount, countByFilter, onToggleVote, onRemove,
}: {
  places: YjsPlace[]
  currentUserId: string
  members: RoomMember[]
  myVoteCount: number
  countByFilter: (f: CategoryFilter) => number
  onToggleVote: (id: string) => void
  onRemove: (id: string) => void
}) {
  const [categoryFilter, setCategoryFilter] = useRoomCategoryFilter()
  const [sortOrder, setSortOrder] = useState<SortOrder>('default')
  const { selectedPlaceId, setHoveredPlaceId } = useRoomStore()
  const listRef = useRef<HTMLDivElement>(null)

  const currentTabConfig = CATEGORY_TABS.find((t) => t.key === categoryFilter)!
  const filteredPlaces = places
    .filter((p) => currentTabConfig.categories.includes(p.category))
    .sort((a, b) => {
      if (sortOrder === 'rating') {
        return (b.amapRating ?? 0) - (a.amapRating ?? 0)
      }
      if (sortOrder === 'votes') {
        return b.votedBy.length - a.votedBy.length
      }
      // default：当前用户已心形的排前面
      const aV = a.votedBy.includes(currentUserId) ? 1 : 0
      const bV = b.votedBy.includes(currentUserId) ? 1 : 0
      return bV - aV
    })

  // Marker 点击后，自动切换到该地点所在分类，并滚动到对应卡片
  useEffect(() => {
    if (!selectedPlaceId) return
    const place = places.find((p) => p.placeId === selectedPlaceId)
    if (!place) return

    // 自动切换到对应分类 tab
    const targetTab = CATEGORY_TABS.find((t) => t.categories.includes(place.category))
    if (targetTab && targetTab.key !== categoryFilter) {
      setCategoryFilter(targetTab.key)
    }

    // 用 requestAnimationFrame 等待 tab 切换后再滚动
    requestAnimationFrame(() => {
      const card = listRef.current?.querySelector(`[data-place-id="${selectedPlaceId}"]`) as HTMLElement | null
      card?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
  }, [selectedPlaceId]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <>
      {/* 我的心愿统计 + 排序控件 */}
      {places.length > 0 && (
        <div className="px-4 py-2 flex items-center justify-between flex-shrink-0 gap-2">
          <div className="flex items-center gap-1.5 text-xs text-gray-400 shrink-0">
            <Heart className="w-3 h-3 text-coral-400" />
            <span>心愿 <span className="text-coral-500 font-semibold">{myVoteCount}</span></span>
          </div>
          <div className="flex items-center gap-1">
            {SORT_OPTIONS.map(opt => (
              <button
                key={opt.key}
                onClick={() => setSortOrder(opt.key)}
                className={`flex items-center gap-1 text-[10px] px-2 py-1 rounded-lg transition-all ${
                  sortOrder === opt.key
                    ? 'bg-coral-50 text-coral-500 font-semibold'
                    : 'text-gray-400 hover:text-gray-600 hover:bg-gray-50'
                }`}
              >
                {opt.icon}
                {opt.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* 三大板块分类 Tab */}
      <div className="px-4 pb-2 flex-shrink-0">
        <div className="flex gap-1.5">
          {CATEGORY_TABS.map((tab) => {
            const count = countByFilter(tab.key)
            const isActive = categoryFilter === tab.key
            return (
              <button
                key={tab.key}
                onClick={() => setCategoryFilter(tab.key)}
                className={`flex-1 flex flex-col items-center gap-0.5 py-2 rounded-lg border transition-all duration-200 ${
                  isActive
                    ? `${tab.bgColor} ${tab.color} border-current/20`
                    : 'bg-white/50 text-gray-400 border-gray-100/80 hover:bg-gray-50'
                }`}
              >
                <div className={`flex items-center gap-1 text-xs font-semibold ${isActive ? tab.color : ''}`}>
                  {tab.icon}
                  {tab.label}
                </div>
                <span className={`text-[10px] font-medium ${isActive ? tab.color : 'text-gray-300'}`}>
                  {count} 处
                </span>
              </button>
            )
          })}
        </div>
      </div>

      {/* 地点列表 */}
      <div ref={listRef} className="flex-1 overflow-y-auto px-3 pb-3 space-y-2.5 scrollbar-thin">
        <AnimatePresence mode="wait">
          {places.length === 0 ? (
            <motion.div
              key="empty-all"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="text-center mt-14 px-4"
            >
              <div className="w-16 h-16 rounded-2xl bg-coral-50 flex items-center justify-center mx-auto mb-4">
                <Layers className="w-8 h-8 text-coral-300" />
              </div>
              <p className="text-sm font-medium text-gray-500">候选地点为空</p>
              <p className="text-xs text-gray-400 mt-1.5 leading-relaxed">
                向左侧 AI 顾问提问<br />推荐地点会自动出现在这里
              </p>
            </motion.div>
          ) : filteredPlaces.length === 0 ? (
            <motion.div
              key={`empty-${categoryFilter}`}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              className="text-center mt-14 px-4"
            >
              <div className={`w-16 h-16 rounded-2xl ${currentTabConfig.bgColor} flex items-center justify-center mx-auto mb-4`}>
                <span className="text-3xl">
                  {categoryFilter === 'sights' ? '🏛' : categoryFilter === 'food' ? '🍜' : '🏨'}
                </span>
              </div>
              <p className="text-sm font-medium text-gray-500">{currentTabConfig.emptyText}</p>
              <p className="text-xs text-gray-400 mt-1.5">向 AI 询问相关推荐</p>
            </motion.div>
          ) : (
            <motion.div
              key={categoryFilter}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="space-y-2.5"
            >
              {filteredPlaces.map((place) => (
                <PlaceCard
                  key={place.placeId}
                  place={place}
                  currentUserId={currentUserId}
                  members={members}
                  isSelected={selectedPlaceId === place.placeId}
                  onToggleVote={onToggleVote}
                  onRemove={onRemove}
                  onHover={setHoveredPlaceId}
                />
              ))}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </>
  )
}

/* ─── 已排路线面板 ─── */
function ItineraryPanel({ itinerary }: { itinerary: Itinerary | null }) {
  return (
    <div className="flex-1 overflow-y-auto px-3 pb-3 pt-3 scrollbar-thin">
      {!itinerary ? (
        <div className="text-center mt-16 px-4">
          <div className="w-16 h-16 rounded-2xl bg-gray-50 flex items-center justify-center mx-auto mb-4">
            <Route className="w-8 h-8 text-gray-300" />
          </div>
          <p className="text-sm font-medium text-gray-500">尚未生成行程</p>
          <p className="text-xs text-gray-400 mt-1.5 leading-relaxed">
            心形选择至少 2 个地点<br />然后点击「智能排线」按钮
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {itinerary.days.map((day) => {
            const dayColor = CLUSTER_COLORS[day.dayIndex % CLUSTER_COLORS.length]
            return (
              <motion.div
                key={day.dayIndex}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: day.dayIndex * 0.08 }}
              >
                <div className="flex items-center gap-2 mb-2">
                  <div
                    className="w-6 h-6 rounded-md text-white text-[10px] font-bold flex items-center justify-center"
                    style={{ backgroundColor: dayColor }}
                  >
                    D{day.dayIndex + 1}
                  </div>
                  <span className="text-xs font-semibold text-gray-700">第 {day.dayIndex + 1} 天</span>
                  <span className="text-[10px] text-gray-400">{day.slots.length} 个地点</span>
                </div>

                <div className="ml-3 border-l-2 border-gray-200/60 pl-3 space-y-2.5">
                  {day.slots.map((slot, slotIdx) => (
                    <div key={slot.placeId} className="relative">
                      <div
                        className="absolute -left-[19px] top-2.5 w-2.5 h-2.5 rounded-full border-2 border-white"
                        style={{ backgroundColor: dayColor }}
                      />
                      <div className="bg-white/70 rounded-lg border border-gray-100/80 p-3">
                        <div className="flex items-center gap-1.5 mb-1">
                          <span className="text-[10px] font-mono text-gray-400">
                            {slot.startTime} - {slot.endTime}
                          </span>
                        </div>
                        <p className="text-sm font-semibold text-gray-900">{slot.place.name}</p>
                        {slot.place.description && (
                          <p className="text-[11px] text-gray-500 mt-0.5 line-clamp-1">{slot.place.description}</p>
                        )}
                        {slot.transport && slotIdx < day.slots.length - 1 && (
                          <div className="mt-2 flex items-center gap-1 text-[10px] text-gray-400">
                            <span>🚗</span>
                            <span>{slot.transport.durationMins} 分钟 · {slot.transport.distanceKm} km</span>
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </motion.div>
            )
          })}
        </div>
      )}
    </div>
  )
}

/* ─── 分类筛选状态（模块内 useState 封装） ─── */
import { useState, useEffect, useRef } from 'react'
function useRoomCategoryFilter() {
  return useState<CategoryFilter>('sights')
}
