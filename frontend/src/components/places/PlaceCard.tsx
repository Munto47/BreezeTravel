'use client'

import { motion } from 'framer-motion'
import { Star, Clock, MapPin, AlertTriangle, Heart } from 'lucide-react'
import type { YjsPlace, RoomMember } from '@/types/room'

interface PlaceCardProps {
  place: YjsPlace
  currentUserId: string
  members: RoomMember[]
  isSelected?: boolean
  onToggleVote: (placeId: string) => void
  onRemove: (placeId: string) => void
  onHover?: (placeId: string | null) => void
  onClickCard?: (placeId: string) => void  // 点击卡片主体 → 地图定位
}

const CATEGORY_CONFIG: Record<string, { label: string; icon: string; bg: string; text: string }> = {
  attraction: { label: '景点', icon: '🏛', bg: 'bg-blue-50', text: 'text-blue-600' },
  food:       { label: '美食', icon: '🍜', bg: 'bg-orange-50', text: 'text-orange-600' },
  hotel:      { label: '住宿', icon: '🏨', bg: 'bg-purple-50', text: 'text-purple-600' },
  transport:  { label: '交通', icon: '🚉', bg: 'bg-gray-100', text: 'text-gray-600' },
}

function formatDuration(mins?: number): string {
  if (!mins) return ''
  if (mins >= 60) {
    const h = Math.floor(mins / 60)
    const m = mins % 60
    return m > 0 ? `${h}h${m}min` : `${h}h`
  }
  return `${mins}min`
}

export default function PlaceCard({
  place,
  currentUserId,
  members,
  isSelected = false,
  onToggleVote,
  onRemove,
  onHover,
  onClickCard,
}: PlaceCardProps) {
  const isVoted = place.votedBy.includes(currentUserId)
  const voteCount = place.votedBy.length
  const votedMembers = members.filter((m) => place.votedBy.includes(m.userId))
  const cat = CATEGORY_CONFIG[place.category] || CATEGORY_CONFIG.attraction
  const hasPhoto = place.amapPhotos && place.amapPhotos.length > 0

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.95 }}
      whileHover={{ y: -2 }}
      transition={{ duration: 0.2 }}
      data-place-id={place.placeId}
      className={`group relative rounded-xl overflow-hidden cursor-pointer transition-all duration-200 ${
        isSelected
          ? 'ring-2 ring-blue-400 shadow-lg bg-white'
          : isVoted
            ? 'ring-2 ring-coral-200 shadow-card-hover bg-white'
            : 'bg-white/80 border border-gray-100/80 hover:shadow-card hover:border-gray-200/80'
      }`}
      onClick={() => onClickCard?.(place.placeId)}
      onMouseEnter={() => onHover?.(place.placeId)}
      onMouseLeave={() => onHover?.(null)}
    >
      {/* 顶部图片区域 */}
      {hasPhoto && (
        <div className="relative h-28 overflow-hidden">
          <img
            src={place.amapPhotos[0]}
            alt={place.name}
            className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"
          />
          <div className="absolute inset-0 bg-gradient-to-t from-black/30 to-transparent" />

          {/* 图片上的类别标签 */}
          <div className="absolute top-2 left-2">
            <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-md backdrop-blur-sm bg-white/80 ${cat.text}`}>
              {cat.icon} {cat.label}
            </span>
          </div>

          {/* 选中/投票按钮 */}
          <div className="absolute top-2 right-2">
            <div
              className={`w-7 h-7 rounded-full flex items-center justify-center transition-all duration-200 ${
                isVoted
                  ? 'bg-coral-500 text-white shadow-md'
                  : 'bg-white/80 backdrop-blur-sm text-gray-400 group-hover:text-coral-400'
              }`}
              onClick={(e) => { e.stopPropagation(); onToggleVote(place.placeId) }}
            >
              <Heart className={`w-3.5 h-3.5 ${isVoted ? 'fill-white' : ''}`} />
            </div>
          </div>
        </div>
      )}

      {/* 内容区域 */}
      <div className="p-3.5">
        {/* 无图片时显示类别 + 投票 */}
        {!hasPhoto && (
          <div className="flex items-center justify-between mb-2">
            <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-md ${cat.bg} ${cat.text}`}>
              {cat.icon} {cat.label}
            </span>
            <div
              className={`w-6 h-6 rounded-full flex items-center justify-center transition-all duration-200 ${
                isVoted
                  ? 'bg-coral-500 text-white'
                  : 'bg-gray-100 text-gray-400 group-hover:text-coral-400 group-hover:bg-coral-50'
              }`}
              onClick={(e) => { e.stopPropagation(); onToggleVote(place.placeId) }}
            >
              <Heart className={`w-3 h-3 ${isVoted ? 'fill-white' : ''}`} />
            </div>
          </div>
        )}

        {/* 名称 + 评分 */}
        <div className="flex items-center gap-2">
          <h3 className="font-bold text-gray-900 text-sm leading-tight flex-1 truncate">{place.name}</h3>
          {place.amapRating && (
            <span className="text-xs text-amber-500 font-semibold flex items-center gap-0.5 flex-shrink-0">
              <Star className="w-3 h-3 fill-amber-400 text-amber-400" />
              {place.amapRating}
            </span>
          )}
        </div>

        {/* 描述 */}
        {place.description && (
          <p className="text-xs text-gray-500 mt-1 leading-relaxed line-clamp-2">{place.description}</p>
        )}

        {/* 标签 */}
        {place.tags && place.tags.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-2">
            {place.tags.slice(0, 4).map((tag) => (
              <span
                key={tag}
                className="text-[10px] leading-none px-1.5 py-1 rounded-md bg-gray-50 text-gray-500 border border-gray-100/80"
              >
                {tag}
              </span>
            ))}
          </div>
        )}

        {/* 信息行：地址 + 时长 + 价格 */}
        <div className="flex items-center gap-2 mt-2.5 text-[11px] text-gray-400">
          <MapPin className="w-3 h-3 flex-shrink-0" />
          <span className="truncate flex-1">{place.district || place.address}</span>
          {place.estimatedDuration && (
            <>
              <span className="text-gray-200">|</span>
              <span className="flex items-center gap-0.5 flex-shrink-0">
                <Clock className="w-2.5 h-2.5" />
                {formatDuration(place.estimatedDuration)}
              </span>
            </>
          )}
          {place.amapPrice && (
            <>
              <span className="text-gray-200">|</span>
              <span className="flex-shrink-0">¥{place.amapPrice}/人</span>
            </>
          )}
        </div>

        {/* 游记攻略提示 */}
        {place.ragMeta?.tipSnippets?.[0] && (
          <div className="mt-2.5 flex gap-1.5 items-start bg-amber-50/60 rounded-lg px-2.5 py-2 border border-amber-100/60">
            <AlertTriangle className="w-3 h-3 text-amber-400 flex-shrink-0 mt-0.5" />
            <p className="text-[11px] text-amber-700/80 leading-relaxed line-clamp-2">
              {place.ragMeta.tipSnippets[0]}
            </p>
          </div>
        )}

        {/* 底部：投票成员头像 + 删除 */}
        {(voteCount > 0 || place.addedBy === currentUserId) && (
          <div className="flex items-center justify-between mt-3 pt-2.5 border-t border-gray-100/60">
            {votedMembers.length > 0 && (
              <div className="flex items-center gap-1.5">
                <div className="flex -space-x-1.5">
                  {votedMembers.slice(0, 4).map((m) => (
                    <div
                      key={m.userId}
                      title={m.nickname}
                      className="avatar-ring text-[9px]"
                      style={{ backgroundColor: m.color, width: 20, height: 20 }}
                    >
                      {m.nickname[0]}
                    </div>
                  ))}
                </div>
                <span className="text-[10px] text-gray-400">{voteCount}人想去</span>
              </div>
            )}
            {place.addedBy === currentUserId && (
              <button
                onClick={(e) => { e.stopPropagation(); onRemove(place.placeId) }}
                className="text-[10px] text-gray-300 hover:text-red-400 transition-colors ml-auto"
              >
                移除
              </button>
            )}
          </div>
        )}
      </div>
    </motion.div>
  )
}
