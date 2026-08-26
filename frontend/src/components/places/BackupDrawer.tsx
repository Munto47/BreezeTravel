'use client'

import { motion, AnimatePresence } from 'framer-motion'
import { X, ArchiveRestore, MapPin, Clock } from 'lucide-react'
import type { Place } from '@/types/place'

interface BackupDrawerProps {
  places: Place[]
  isOpen: boolean
  onClose: () => void
  onAddToTrip?: (place: Place) => void  // 将备选加入地点列表
}

const CATEGORY_ICON: Record<string, string> = {
  attraction: '🏛',
  food: '🍜',
  hotel: '🏨',
  transport: '🚉',
}

function formatDuration(mins?: number): string {
  if (!mins) return ''
  const h = Math.floor(mins / 60)
  const m = mins % 60
  return h > 0 ? (m > 0 ? `${h}h${m}min` : `${h}h`) : `${m}min`
}

export default function BackupDrawer({ places, isOpen, onClose, onAddToTrip }: BackupDrawerProps) {
  return (
    <AnimatePresence>
      {isOpen && (
        <>
          {/* 遮罩 */}
          <motion.div
            className="fixed inset-0 z-40 bg-black/20 backdrop-blur-[1px]"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={onClose}
          />

          {/* 抽屉主体 */}
          <motion.div
            className="fixed right-0 top-0 h-full z-50 w-80 bg-white/95 backdrop-blur-md shadow-2xl flex flex-col"
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={{ type: 'spring', damping: 30, stiffness: 300 }}
          >
            {/* 标题栏 */}
            <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
              <div className="flex items-center gap-2">
                <ArchiveRestore className="w-4 h-4 text-amber-500" />
                <span className="font-semibold text-slate-700">备选地点</span>
                {places.length > 0 && (
                  <span className="text-xs bg-amber-100 text-amber-600 rounded-full px-2 py-0.5 font-medium">
                    {places.length}
                  </span>
                )}
              </div>
              <button
                onClick={onClose}
                className="p-1.5 rounded-lg hover:bg-slate-100 transition-colors text-slate-400 hover:text-slate-600"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* 说明文案 */}
            <div className="px-5 py-3 bg-amber-50 border-b border-amber-100">
              <p className="text-xs text-amber-700 leading-relaxed">
                以下地点因时间 / 体力限制未能排入行程，点击「加入」可重新添加。
              </p>
            </div>

            {/* 地点列表 */}
            <div className="flex-1 overflow-y-auto py-3">
              {places.length === 0 ? (
                <div className="flex flex-col items-center justify-center h-full gap-3 text-slate-400">
                  <ArchiveRestore className="w-10 h-10 opacity-30" />
                  <p className="text-sm">暂无备选地点</p>
                </div>
              ) : (
                <ul className="space-y-2 px-3">
                  {places.map((place) => (
                    <motion.li
                      key={place.placeId}
                      layout
                      initial={{ opacity: 0, y: 8 }}
                      animate={{ opacity: 1, y: 0 }}
                      className="rounded-xl border border-slate-100 bg-white shadow-sm p-3 flex flex-col gap-2 hover:border-amber-200 transition-colors"
                    >
                      {/* 地点名 + 品类 */}
                      <div className="flex items-start gap-2">
                        <span className="text-lg leading-none mt-0.5">
                          {CATEGORY_ICON[place.category] ?? '📍'}
                        </span>
                        <div className="flex-1 min-w-0">
                          <p className="font-medium text-slate-800 text-sm truncate">{place.name}</p>
                          {place.description && (
                            <p className="text-xs text-slate-400 mt-0.5 line-clamp-2">{place.description}</p>
                          )}
                        </div>
                      </div>

                      {/* 元数据行 */}
                      <div className="flex items-center gap-3 text-xs text-slate-400">
                        {place.city && (
                          <span className="flex items-center gap-1">
                            <MapPin className="w-3 h-3" /> {place.city}
                          </span>
                        )}
                        {place.estimatedDuration && (
                          <span className="flex items-center gap-1">
                            <Clock className="w-3 h-3" /> {formatDuration(place.estimatedDuration)}
                          </span>
                        )}
                      </div>

                      {/* 操作按钮 */}
                      {onAddToTrip && (
                        <button
                          onClick={() => onAddToTrip(place)}
                          className="w-full text-xs font-medium text-amber-600 border border-amber-200 rounded-lg py-1.5 hover:bg-amber-50 transition-colors"
                        >
                          加入行程
                        </button>
                      )}
                    </motion.li>
                  ))}
                </ul>
              )}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  )
}
