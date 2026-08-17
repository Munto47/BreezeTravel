'use client'

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Star, Clock, MapPin, AlertTriangle, Heart, ChevronRight, BookOpen, ArrowLeftRight } from 'lucide-react'
import type { YjsPlace, RoomMember } from '@/types/room'
import type { PlaceRecommendation } from '@/types/place'

interface PlaceCardProps {
  place: YjsPlace
  currentUserId: string
  members: RoomMember[]
  isSelected?: boolean
  onToggleVote: (placeId: string) => void
  onRemove: (placeId: string) => void
  onHover?: (placeId: string | null) => void
  onClickCard?: (placeId: string) => void
  recommendation?: PlaceRecommendation  // Phase B：地点卡背面数据
}

const CATEGORY_CONFIG: Record<string, { label: string; icon: string; bg: string; text: string }> = {
  attraction: { label: '景点', icon: '🏛', bg: 'bg-blue-50', text: 'text-blue-600' },
  food:       { label: '美食', icon: '🍜', bg: 'bg-orange-50', text: 'text-orange-600' },
  hotel:      { label: '住宿', icon: '🏨', bg: 'bg-purple-50', text: 'text-purple-600' },
  transport:  { label: '交通', icon: '🚉', bg: 'bg-gray-100', text: 'text-gray-600' },
}

const CONFIDENCE_CONFIG = {
  high:   { label: '高可信', color: 'text-green-600 bg-green-50' },
  medium: { label: '中可信', color: 'text-amber-600 bg-amber-50' },
  low:    { label: '低可信', color: 'text-gray-500 bg-gray-50' },
}

function formatDuration(mins?: number): string {
  if (!mins) return ''
  const h = Math.floor(mins / 60)
  const m = mins % 60
  return h > 0 ? (m > 0 ? `${h}h${m}min` : `${h}h`) : `${m}min`
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
  recommendation,
}: PlaceCardProps) {
  const [isFlipped, setIsFlipped] = useState(false)

  const isVoted = place.votedBy.includes(currentUserId)
  const voteCount = place.votedBy.length
  const votedMembers = members.filter((m) => place.votedBy.includes(m.userId))
  const cat = CATEGORY_CONFIG[place.category] || CATEGORY_CONFIG.attraction
  const hasPhoto = place.amapPhotos && place.amapPhotos.length > 0
  const hasRecommendation = recommendation && (recommendation.reason || recommendation.avoidTips.length > 0)

  return (
    <div
      className="relative"
      style={{ perspective: '1000px' }}
      onMouseEnter={() => onHover?.(place.placeId)}
      onMouseLeave={() => onHover?.(null)}
    >
      <motion.div
        layout
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95 }}
        transition={{ duration: 0.2 }}
        style={{
          transformStyle: 'preserve-3d',
          transition: 'transform 0.45s cubic-bezier(0.4,0,0.2,1)',
          transform: isFlipped ? 'rotateY(180deg)' : 'rotateY(0deg)',
        }}
        className="relative"
      >
        {/* ── 正面 ─────────────────────────────────────────────────── */}
        <div
          data-place-id={place.placeId}
          style={{ backfaceVisibility: 'hidden' }}
          className={`group rounded-xl overflow-hidden cursor-pointer transition-all duration-200 ${
            isSelected
              ? 'ring-2 ring-blue-400 shadow-lg bg-white'
              : isVoted
                ? 'ring-2 ring-coral-200 shadow-card-hover bg-white'
                : 'bg-white/80 border border-gray-100/80 hover:shadow-card hover:border-gray-200/80'
          }`}
          onClick={() => onClickCard?.(place.placeId)}
        >
          {/* 顶部图片 */}
          {hasPhoto && (
            <div className="relative h-28 overflow-hidden">
              <img
                src={place.amapPhotos[0]}
                alt={place.name}
                className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"
              />
              <div className="absolute inset-0 bg-gradient-to-t from-black/30 to-transparent" />
              <div className="absolute top-2 left-2">
                <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-md backdrop-blur-sm bg-white/80 ${cat.text}`}>
                  {cat.icon} {cat.label}
                </span>
              </div>
              <div className="absolute top-2 right-2">
                <div
                  className={`w-7 h-7 rounded-full flex items-center justify-center transition-all duration-200 ${
                    isVoted ? 'bg-coral-500 text-white shadow-md' : 'bg-white/80 backdrop-blur-sm text-gray-400 group-hover:text-coral-400'
                  }`}
                  onClick={(e) => { e.stopPropagation(); onToggleVote(place.placeId) }}
                >
                  <Heart className={`w-3.5 h-3.5 ${isVoted ? 'fill-white' : ''}`} />
                </div>
              </div>
            </div>
          )}

          <div className="p-3.5">
            {!hasPhoto && (
              <div className="flex items-center justify-between mb-2">
                <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-md ${cat.bg} ${cat.text}`}>
                  {cat.icon} {cat.label}
                </span>
                <div
                  className={`w-6 h-6 rounded-full flex items-center justify-center transition-all duration-200 ${
                    isVoted ? 'bg-coral-500 text-white' : 'bg-gray-100 text-gray-400 group-hover:text-coral-400 group-hover:bg-coral-50'
                  }`}
                  onClick={(e) => { e.stopPropagation(); onToggleVote(place.placeId) }}
                >
                  <Heart className={`w-3 h-3 ${isVoted ? 'fill-white' : ''}`} />
                </div>
              </div>
            )}

            <div className="flex items-center gap-2">
              <h3 className="font-bold text-gray-900 text-sm leading-tight flex-1 truncate">{place.name}</h3>
              {place.amapRating && (
                <span className="text-xs text-amber-500 font-semibold flex items-center gap-0.5 flex-shrink-0">
                  <Star className="w-3 h-3 fill-amber-400 text-amber-400" />
                  {place.amapRating}
                </span>
              )}
            </div>

            {place.description && (
              <p className="text-xs text-gray-500 mt-1 leading-relaxed line-clamp-2">{place.description}</p>
            )}

            {place.tags && place.tags.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-2">
                {place.tags.slice(0, 4).map((tag) => (
                  <span key={tag} className="text-[10px] leading-none px-1.5 py-1 rounded-md bg-gray-50 text-gray-500 border border-gray-100/80">
                    {tag}
                  </span>
                ))}
              </div>
            )}

            {place.constraintEvidence?.length > 0 && (
              <div className="mt-2 space-y-1.5 rounded-lg border border-slate-100 bg-slate-50/70 p-2">
                {place.constraintEvidence.slice(0, 4).map((item) => (
                  <div key={item.constraint} className="flex items-start gap-1.5 text-[10px] leading-relaxed">
                    <span className={`mt-0.5 rounded px-1 py-0.5 font-medium ${
                      item.status === 'VERIFIED'
                        ? 'bg-emerald-100 text-emerald-700'
                        : item.status === 'REQUIRES_CONFIRMATION'
                          ? 'bg-amber-100 text-amber-700'
                          : 'bg-slate-200 text-slate-600'
                    }`}>
                      {item.status === 'VERIFIED' ? '已核验' : item.status === 'UNKNOWN' ? '未知' : '需确认'}
                    </span>
                    <span className="text-slate-600" title={item.detail}>{item.label}</span>
                  </div>
                ))}
              </div>
            )}
            {place.confirmationActions?.map((action) => (
              <p key={action} className="mt-2 rounded-lg bg-amber-50 px-2.5 py-2 text-xs text-amber-800">
                {action}
              </p>
            ))}

            <div className="flex items-center gap-2 mt-2.5 text-[11px] text-gray-400">
              <MapPin className="w-3 h-3 flex-shrink-0" />
              <span className="truncate flex-1">{place.district || place.address}</span>
              {place.estimatedDuration && (
                <>
                  <span className="text-gray-200">|</span>
                  <span className="flex items-center gap-0.5 flex-shrink-0">
                    <Clock className="w-2.5 h-2.5" /> {formatDuration(place.estimatedDuration)}
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

            {place.ragMeta?.tipSnippets?.[0] && (
              <div className="mt-2.5 flex gap-1.5 items-start bg-amber-50/60 rounded-lg px-2.5 py-2 border border-amber-100/60">
                <AlertTriangle className="w-3 h-3 text-amber-400 flex-shrink-0 mt-0.5" />
                <p className="text-[11px] text-amber-700/80 leading-relaxed line-clamp-2">
                  {place.ragMeta.tipSnippets[0]}
                </p>
              </div>
            )}

            {/* 底部行：投票头像 + 删除 + 「为什么推荐」按钮 */}
            <div className="flex items-center justify-between mt-3 pt-2.5 border-t border-gray-100/60">
              {voteCount > 0 && votedMembers.length > 0 ? (
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
              ) : <div />}

              <div className="flex items-center gap-2">
                {place.addedBy === currentUserId && (
                  <button
                    onClick={(e) => { e.stopPropagation(); onRemove(place.placeId) }}
                    className="text-[10px] text-gray-300 hover:text-red-400 transition-colors"
                  >
                    移除
                  </button>
                )}
                {/* Phase B：为什么推荐按钮 */}
                {hasRecommendation && (
                  <button
                    onClick={(e) => { e.stopPropagation(); setIsFlipped(true) }}
                    className="flex items-center gap-0.5 text-[10px] text-blue-500 hover:text-blue-600 font-medium transition-colors"
                  >
                    <BookOpen className="w-3 h-3" /> 为什么推荐
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* ── 背面（为什么推荐） ────────────────────────────────────── */}
        {hasRecommendation && (
          <div
            style={{
              backfaceVisibility: 'hidden',
              transform: 'rotateY(180deg)',
              position: 'absolute',
              top: 0, left: 0, right: 0,
            }}
            className="rounded-xl bg-white border border-blue-100 shadow-lg overflow-hidden"
          >
            <div className="p-3.5 flex flex-col gap-3">
              {/* 标题行 */}
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-1.5">
                  <BookOpen className="w-3.5 h-3.5 text-blue-500" />
                  <span className="text-xs font-semibold text-gray-800">为什么推荐</span>
                  <span className={`text-[9px] px-1.5 py-0.5 rounded-full font-medium ${CONFIDENCE_CONFIG[recommendation.confidence]?.color}`}>
                    {CONFIDENCE_CONFIG[recommendation.confidence]?.label}
                  </span>
                </div>
                <button
                  onClick={() => setIsFlipped(false)}
                  className="text-[10px] text-gray-400 hover:text-gray-600 flex items-center gap-0.5"
                >
                  <ArrowLeftRight className="w-3 h-3" /> 返回
                </button>
              </div>

              {/* 推荐理由 */}
              {recommendation.reason && (
                <div className="bg-blue-50/60 rounded-lg p-2.5 border border-blue-100/60">
                  <p className="text-[11px] text-blue-800 leading-relaxed">{recommendation.reason}</p>
                  {recommendation.sourceChunkIds.length > 0 && (
                    <p className="text-[9px] text-blue-400 mt-1">
                      来源：游记 {recommendation.sourceChunkIds.slice(0, 2).join('、')}
                    </p>
                  )}
                </div>
              )}

              {/* 适合人群 */}
              {recommendation.suitableFor.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {recommendation.suitableFor.map((s) => (
                    <span key={s} className="text-[10px] px-1.5 py-0.5 rounded-full bg-green-50 text-green-600 border border-green-100">
                      {s}
                    </span>
                  ))}
                </div>
              )}

              {/* 避坑提示 */}
              {recommendation.avoidTips.length > 0 && (
                <div className="space-y-1.5">
                  <p className="text-[10px] font-medium text-amber-600 flex items-center gap-1">
                    <AlertTriangle className="w-3 h-3" /> 避坑提示
                  </p>
                  {recommendation.avoidTips.map((tip, i) => (
                    <p key={i} className="text-[11px] text-amber-700 bg-amber-50 rounded px-2 py-1 leading-relaxed">
                      {tip}
                    </p>
                  ))}
                </div>
              )}

              {/* 替代方案 */}
              {recommendation.alternatives.length > 0 && (
                <div>
                  <p className="text-[10px] font-medium text-gray-500 mb-1.5 flex items-center gap-1">
                    <ChevronRight className="w-3 h-3" /> 还可以考虑
                  </p>
                  {recommendation.alternatives.map((alt) => (
                    <div key={alt.placeId} className="flex items-start gap-1.5 py-1.5 border-t border-gray-50">
                      <span className="text-[11px] font-medium text-gray-700 flex-shrink-0">{alt.name}</span>
                      <span className="text-[10px] text-gray-400 leading-relaxed">{alt.whyAlternative}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </motion.div>
    </div>
  )
}
