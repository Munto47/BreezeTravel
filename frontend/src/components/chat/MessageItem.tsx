'use client'

import { useState } from 'react'
import { motion } from 'framer-motion'
import { MapPin, Star, AlertCircle } from 'lucide-react'
import type { ChatMessage } from '@/types/chat'

interface MessageItemProps {
  message: ChatMessage
  onClickPlace?: (placeId: string) => void
}

function PlaceThumbnail({ name, photos }: { name: string; photos: string[] }) {
  const [failed, setFailed] = useState(false)
  const photo = photos.find((url) => /^https?:\/\//i.test(url))

  if (!photo || failed) {
    return (
      <div className="w-20 h-14 rounded-lg bg-slate-100 text-[10px] text-slate-400 flex items-center justify-center text-center flex-shrink-0">
        暂无实景图
      </div>
    )
  }

  return (
    <img
      src={photo}
      alt={`${name}实景`}
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
      className="w-20 h-14 rounded-lg object-cover flex-shrink-0 bg-slate-100"
    />
  )
}

export default function MessageItem({ message, onClickPlace }: MessageItemProps) {
  const isUser = message.role === 'user'

  if (isUser) {
    return (
      <div className="flex justify-end">
        <div className="bg-coral-500 text-white text-sm rounded-2xl rounded-tr-md px-4 py-2.5 max-w-[85%] shadow-sm">
          {message.content}
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-2.5">
      {/* AI 推荐的地点卡片 */}
      {message.placesGenerated && message.placesGenerated.length > 0 && (
        <div className="space-y-2">
          <p className="text-[11px] text-gray-400 px-1 flex items-center gap-1">
            <MapPin className="w-3 h-3" />
            {message.status === 'streaming'
              ? `已在地图预览 ${message.placesGenerated.length} 个地点 · AI 正在完善说明`
              : `推荐了 ${message.placesGenerated.length} 个地点 · 已加入右侧候选区`}
          </p>
          {message.placesGenerated.map((place, i) => {
            return (
              <motion.div
                key={place.placeId}
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: i * 0.06 }}
                onClick={() => onClickPlace?.(place.placeId)}
                className="bg-white/70 rounded-xl border border-gray-100/80 p-3 hover:border-coral-200 hover:shadow-card transition-all duration-200 cursor-pointer active:scale-[0.98]"
              >
                <div className="flex items-start gap-2.5">
                  <PlaceThumbnail name={place.name} photos={place.amapPhotos || []} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className="text-sm font-semibold text-gray-900 truncate">{place.name}</span>
                      {place.amapRating && (
                        <span className="text-[11px] text-amber-500 flex items-center gap-0.5 flex-shrink-0">
                          <Star className="w-2.5 h-2.5 fill-amber-400 text-amber-400" />
                          {place.amapRating}
                        </span>
                      )}
                    </div>
                    {place.description && (
                      <p className="text-[11px] text-gray-500 mt-0.5 line-clamp-1 leading-relaxed">{place.description}</p>
                    )}
                    {place.tags && place.tags.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1.5">
                        {place.tags.slice(0, 3).map((tag) => (
                          <span
                            key={tag}
                            className="text-[10px] leading-none px-1.5 py-0.5 rounded-md bg-coral-50/80 text-coral-600 border border-coral-100/60"
                          >
                            {tag}
                          </span>
                        ))}
                      </div>
                    )}
                    {place.constraintEvidence?.length > 0 && (
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        {place.constraintEvidence.slice(0, 3).map((item) => (
                          <span
                            key={item.constraint}
                            className={`text-[10px] leading-none px-1.5 py-1 rounded-md border ${
                              item.status === 'VERIFIED'
                                ? 'bg-emerald-50 text-emerald-700 border-emerald-100'
                                : item.status === 'REQUIRES_CONFIRMATION'
                                  ? 'bg-amber-50 text-amber-700 border-amber-100'
                                  : 'bg-slate-50 text-slate-500 border-slate-100'
                            }`}
                          >
                            {item.label} · {item.status === 'VERIFIED' ? '有数据支持' : item.status === 'UNKNOWN' ? '暂无数据' : '需联系确认'}
                          </span>
                        ))}
                      </div>
                    )}
                    {place.confirmationActions?.map((action) => (
                      <p key={action} className="mt-2 rounded-lg bg-amber-50 px-2 py-1.5 text-[11px] text-amber-800">
                        {action}
                      </p>
                    ))}
                  </div>
                </div>
              </motion.div>
            )
          })}
        </div>
      )}

      {/* AI 文字回复 */}
      {message.content && (
        <div className="bg-white/70 text-gray-800 text-sm rounded-2xl rounded-tl-md px-4 py-3 max-w-[95%] leading-relaxed border border-gray-100/60 shadow-sm">
          {message.content}
          {message.status === 'streaming' && (
            <span className="inline-flex gap-0.5 ml-1 align-middle">
              <span className="w-1 h-1 bg-coral-400 rounded-full animate-pulse-dot" />
              <span className="w-1 h-1 bg-coral-400 rounded-full animate-pulse-dot" style={{ animationDelay: '0.2s' }} />
              <span className="w-1 h-1 bg-coral-400 rounded-full animate-pulse-dot" style={{ animationDelay: '0.4s' }} />
            </span>
          )}
        </div>
      )}

      {message.resultStatus === 'LIMITED' && message.status === 'done' && (
        <p className="max-w-[95%] rounded-xl bg-sky-50 px-3 py-2 text-xs text-slate-600">
          已返回目前可确认的部分建议；暂不可用的内容没有被补写成成功结果。
        </p>
      )}

      {/* 错误状态 */}
      {message.status === 'error' && !message.content && (
        <div className="flex items-center gap-2 text-red-500 text-xs bg-red-50/80 rounded-lg px-3 py-2.5 border border-red-100">
          <AlertCircle className="w-3.5 h-3.5 flex-shrink-0" />
          请求失败，请重试
        </div>
      )}
    </div>
  )
}
