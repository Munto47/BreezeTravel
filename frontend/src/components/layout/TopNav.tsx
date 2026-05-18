'use client'

import { motion, AnimatePresence } from 'framer-motion'
import { MapPin, Copy, Check, Route, Users, MessageCircle, Compass } from 'lucide-react'
import { useState, useCallback } from 'react'
import type { RoomMember } from '@/types/room'
import { UserMenu } from '@/components/layout/UserMenu'

interface TopNavProps {
  roomId: string
  tripCity: string
  tripDays: number
  isConnected: boolean
  members: RoomMember[]
  isChatOpen: boolean
  onToggleChat: () => void
  selectedCount: number
  isOptimizing: boolean
  hasItinerary: boolean
  onOptimize: () => void
  onViewItinerary: () => void
}

export default function TopNav({
  roomId,
  tripCity,
  tripDays,
  isConnected,
  members,
  isChatOpen,
  onToggleChat,
  selectedCount,
  isOptimizing,
  hasItinerary,
  onOptimize,
  onViewItinerary,
}: TopNavProps) {
  const [copyTip, setCopyTip] = useState(false)

  const handleCopyLink = useCallback(async () => {
    // 复制纯 6 位房间号，方便口头分享
    try {
      await navigator.clipboard.writeText(roomId)
    } catch {
      const input = document.createElement('input')
      input.value = roomId
      document.body.appendChild(input)
      input.select()
      document.execCommand('copy')
      document.body.removeChild(input)
    }
    setCopyTip(true)
    setTimeout(() => setCopyTip(false), 2000)
  }, [roomId])

  return (
    <motion.header
      initial={{ y: -20, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      transition={{ duration: 0.4, ease: 'easeOut' }}
      className="glass-panel overlay-interactive flex items-center gap-3 px-4 py-2.5 mx-4 mt-3 rounded-glass"
    >
      {/* ===== 左区：Logo + 聊天切换 + 房间信息 ===== */}
      <div className="flex items-center gap-3 flex-shrink-0">
        {/* Logo */}
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg bg-coral-500 flex items-center justify-center shadow-sm">
            <Compass className="w-4.5 h-4.5 text-white" strokeWidth={2.5} />
          </div>
          <span className="font-bold text-gray-900 text-sm tracking-tight hidden lg:inline">
            BreezeTravel
          </span>
        </div>

        {/* 分割线 */}
        <div className="w-px h-5 bg-gray-200/60" />

        {/* AI 聊天切换按钮 */}
        <button
          onClick={onToggleChat}
          className={`flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg transition-all duration-200 ${
            isChatOpen
              ? 'bg-coral-50 text-coral-600 border border-coral-100'
              : 'bg-white/50 text-gray-500 border border-gray-200/60 hover:bg-white/70'
          }`}
        >
          <MessageCircle className="w-3.5 h-3.5" />
          <span className="hidden sm:inline">AI 顾问</span>
        </button>

        {/* 房间信息 */}
        <div className="flex items-center gap-2">
          <MapPin className="w-3.5 h-3.5 text-coral-500" />
          <span className="font-semibold text-gray-900 text-sm">{tripCity}</span>
          <span className="text-xs text-gray-400">{tripDays} 天</span>
          <span
            className={`w-1.5 h-1.5 rounded-full transition-colors ${
              isConnected ? 'bg-emerald-400' : 'bg-gray-300 animate-pulse'
            }`}
          />
        </div>
      </div>

      {/* ===== 中区：房间号 + 复制 ===== */}
      <div className="flex items-center gap-2 flex-1 justify-center">
        <div className="flex items-center gap-1.5 bg-white/50 rounded-lg px-3 py-1 border border-gray-100/60">
          <span className="text-[11px] text-gray-400">房间</span>
          <code className="text-sm font-mono font-bold text-gray-700 tracking-wider">
            {roomId}
          </code>
        </div>
        <button
          onClick={handleCopyLink}
          className="flex items-center gap-1 text-[11px] text-gray-400 hover:text-coral-500 transition-colors"
        >
          <AnimatePresence mode="wait">
            {copyTip ? (
              <motion.span
                key="check"
                initial={{ scale: 0.8, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                exit={{ scale: 0.8, opacity: 0 }}
                className="flex items-center gap-0.5 text-emerald-500"
              >
                <Check className="w-3 h-3" />
                已复制
              </motion.span>
            ) : (
              <motion.span
                key="copy"
                initial={{ scale: 0.8, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                exit={{ scale: 0.8, opacity: 0 }}
                className="flex items-center gap-0.5"
              >
                <Copy className="w-3 h-3" />
                复制号码
              </motion.span>
            )}
          </AnimatePresence>
        </button>
      </div>

      {/* ===== 右区：在线成员 + 操作按钮 ===== */}
      <div className="flex items-center gap-3 flex-shrink-0">
        {/* 在线成员头像组 */}
        <div className="flex items-center gap-1.5">
          <div className="flex -space-x-2">
            {members.slice(0, 5).map((m, i) => (
              <motion.div
                key={m.userId}
                initial={{ scale: 0, x: -10 }}
                animate={{ scale: 1, x: 0 }}
                transition={{ delay: i * 0.05, type: 'spring', stiffness: 300 }}
                title={m.nickname}
                className="avatar-ring w-7 h-7 text-[11px]"
                style={{ backgroundColor: m.color, zIndex: 5 - i }}
              >
                {m.nickname[0]}
              </motion.div>
            ))}
          </div>
          <div className="flex items-center gap-1 text-[11px] text-gray-400">
            <Users className="w-3 h-3" />
            {members.length}
          </div>
        </div>

        {/* 分割线 */}
        <div className="w-px h-5 bg-gray-200/60" />

        {/* 查看行程按钮 */}
        {hasItinerary && (
          <button
            onClick={onViewItinerary}
            className="btn-glass text-xs px-3 py-1.5 flex items-center gap-1.5"
          >
            <Route className="w-3.5 h-3.5 text-emerald-500" />
            查看行程
          </button>
        )}

        {/* 智能排线主按钮 */}
        <button
          onClick={onOptimize}
          disabled={isOptimizing || selectedCount < 2}
          className="btn-coral text-xs px-4 py-2 flex items-center gap-1.5 shadow-sm"
        >
          {isOptimizing ? (
            <>
              <span className="w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              排线中...
            </>
          ) : (
            <>
              <Route className="w-3.5 h-3.5" />
              智能排线{selectedCount > 0 ? ` · ${selectedCount}` : ''}
            </>
          )}
        </button>

        {/* 分割线 */}
        <div className="w-px h-5 bg-gray-200/60" />

        {/* 用户头像菜单 */}
        <UserMenu />
      </div>
    </motion.header>
  )
}
