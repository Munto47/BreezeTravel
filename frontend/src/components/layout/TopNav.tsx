'use client'

import { motion, AnimatePresence } from 'framer-motion'
import { MapPin, Copy, Check, Route, Users, MessageCircle, Compass, ArrowLeft, ArrowRight, RefreshCw } from 'lucide-react'
import { useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
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
  saveStatus: 'idle' | 'saving' | 'saved' | 'error'
  canTransfer: boolean
  isTransferring: boolean
  onTransfer: () => void
  onRetrySave: () => void
}

export default function TopNav({
  roomId,
  tripCity,
  tripDays,
  isConnected,
  members: _members,
  isChatOpen,
  onToggleChat,
  selectedCount,
  isOptimizing,
  hasItinerary,
  onOptimize,
  onViewItinerary,
  saveStatus,
  canTransfer,
  isTransferring,
  onTransfer,
  onRetrySave,
}: TopNavProps) {
  const [copyTip, setCopyTip] = useState(false)
  const router = useRouter()

  const handleCopyLink = useCallback(async () => {
    // Invitees confirm joining from the authenticated collaboration entry.
    const origin = typeof window !== 'undefined' ? window.location.origin : ''
    const url = `${origin}/collaborate?join=${encodeURIComponent(roomId)}`
    const shareText = `邀请你一起规划${tripCity || ''}${tripDays ? ` ${tripDays} 天` : ''}行程：${url}\n房间号：${roomId}`

    // 优先调用系统原生分享（移动端微信/iMessage 可直接转发）
    const nav = typeof navigator !== 'undefined' ? (navigator as Navigator & { share?: (data: ShareData) => Promise<void> }) : null
    try {
      if (nav?.share) {
        await nav.share({
          title: 'BreezeTravel 协同规划邀请',
          text: shareText,
          url,
        })
        setCopyTip(true)
        setTimeout(() => setCopyTip(false), 2000)
        return
      }
      if (nav?.clipboard) {
        await nav.clipboard.writeText(shareText)
      } else {
        throw new Error('no clipboard')
      }
    } catch {
      const input = document.createElement('textarea')
      input.value = shareText
      document.body.appendChild(input)
      input.select()
      document.execCommand('copy')
      document.body.removeChild(input)
    }
    setCopyTip(true)
    setTimeout(() => setCopyTip(false), 2000)
  }, [roomId, tripCity, tripDays])

  return (
    <motion.header
      initial={{ y: -20, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      transition={{ duration: 0.4, ease: 'easeOut' }}
      className="glass-panel overlay-interactive flex flex-col items-stretch gap-2 px-3 py-2.5 mx-3 mt-3 rounded-glass lg:mx-4 lg:flex-row lg:items-center lg:gap-3 lg:px-4"
    >
      {/* ===== 左区：返回 + Logo + 聊天切换 + 房间信息 ===== */}
      <div className="flex w-full min-w-0 items-center gap-2 flex-shrink-0 lg:w-auto lg:gap-3">
        {/* 返回主界面按钮 */}
        <button
          onClick={() => router.push('/')}
          title="返回主界面"
          className="flex min-h-11 min-w-11 items-center justify-center gap-1 rounded-lg border border-transparent px-2 py-1.5 text-xs text-gray-400 transition-all duration-200 hover:border-sky-100 hover:bg-sky-50 hover:text-sky-700"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          <span className="hidden sm:inline">主界面</span>
        </button>

        {/* 分割线 */}
        <div className="hidden w-px h-5 bg-gray-200/60 lg:block" />

        {/* Logo */}
        <div className="flex items-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-sky-700 shadow-sm">
            <Compass className="w-4.5 h-4.5 text-white" strokeWidth={2.5} />
          </div>
          <span className="font-bold text-gray-900 text-sm tracking-tight hidden lg:inline">
            BreezeTravel
          </span>
        </div>

        {/* 分割线 */}
        <div className="hidden w-px h-5 bg-gray-200/60 lg:block" />

        {/* AI 聊天切换按钮 */}
        <button
          onClick={onToggleChat}
          className={`flex min-h-11 items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg transition-all duration-200 ${
            isChatOpen
              ? 'border border-sky-100 bg-sky-50 text-sky-700'
              : 'bg-white/50 text-gray-500 border border-gray-200/60 hover:bg-white/70'
          }`}
        >
          <MessageCircle className="w-3.5 h-3.5" />
          <span className="hidden sm:inline">AI 顾问</span>
        </button>

        {/* 房间信息 */}
        <div className="flex min-w-0 flex-1 items-center gap-1.5 lg:flex-initial lg:gap-2">
          <MapPin className="w-3.5 h-3.5 text-sky-700" />
          <span className="truncate font-semibold text-gray-900 text-sm">{tripCity}</span>
          <span className="whitespace-nowrap text-xs text-gray-400">{tripDays} 天</span>
          <span
            className={`w-1.5 h-1.5 rounded-full transition-colors ${
              isConnected ? 'bg-emerald-400' : 'bg-gray-300 animate-pulse'
            }`}
          />
        </div>

        <button
          type="button"
          onClick={handleCopyLink}
          aria-label={copyTip ? '邀请已复制' : '复制房间邀请'}
          className="flex min-h-11 min-w-11 items-center justify-center rounded-lg border border-sky-100 bg-white/70 text-sky-700 transition-colors hover:bg-sky-50 lg:hidden"
        >
          {copyTip ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
        </button>
      </div>

      {/* ===== 中区：房间号 + 复制 ===== */}
      <div className="hidden items-center gap-2 flex-1 justify-center lg:flex">
        <div className="flex items-center gap-1.5 bg-white/50 rounded-lg px-3 py-1 border border-gray-100/60">
          <span className="text-[11px] text-gray-400">房间</span>
          <code className="text-sm font-mono font-bold text-gray-700 tracking-wider">
            {roomId}
          </code>
        </div>
        <button
          onClick={handleCopyLink}
          className="flex items-center gap-1 text-[11px] text-gray-400 transition-colors hover:text-sky-700"
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
                已复制邀请
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
                复制邀请
              </motion.span>
            )}
          </AnimatePresence>
        </button>
      </div>

      {/* ===== 右区：在线成员 + 操作按钮 ===== */}
      <div className="grid w-full grid-cols-2 gap-2 flex-shrink-0 lg:flex lg:w-auto lg:items-center lg:gap-3">
        {/* Awareness 只证明连接状态，不用于宣称具体在线身份。 */}
        <div
          className="hidden items-center gap-1.5 text-[11px] text-gray-500 lg:flex"
          title="连接状态不代表具体在线人数"
        >
          <Users className="w-3 h-3" />
          {isConnected ? '协同已连接' : '正在重连'}
        </div>

        {/* 分割线 */}
        <div className="hidden w-px h-5 bg-gray-200/60 lg:block" />

        {/* 查看行程按钮 */}
        {hasItinerary && (
          <div className="flex min-w-0 items-center gap-2">
            <button
              onClick={onViewItinerary}
              className="btn-glass flex min-h-11 w-full items-center justify-center gap-1.5 px-3 py-1.5 text-xs lg:w-auto"
            >
              <Route className="w-3.5 h-3.5 text-emerald-500" />
              查看行程
            </button>
            {saveStatus === 'error' ? (
              <button
                type="button"
                onClick={onRetrySave}
                className="btn-glass text-xs px-3 py-1.5 flex items-center gap-1.5 min-h-11"
              >
                <RefreshCw className="w-3.5 h-3.5" />
                重试保存
              </button>
            ) : (
              <span className="hidden text-[11px] text-gray-500 lg:inline" aria-live="polite">
                {saveStatus === 'saving' ? '正在保存…' : saveStatus === 'saved' ? '已保存' : '尚未保存'}
              </span>
            )}
          </div>
        )}

        {canTransfer && (
          <button
            type="button"
            onClick={onTransfer}
            disabled={isTransferring}
            className="btn-primary-sky flex min-h-11 w-full items-center justify-center gap-1.5 px-3 py-2 text-xs lg:w-auto"
          >
            {isTransferring ? '正在转入…' : '转入行程查'}
            <ArrowRight className="w-3.5 h-3.5" />
          </button>
        )}

        {/* 智能排线主按钮 */}
        <div className="relative group flex w-full flex-col items-center lg:w-auto">
          <button
            onClick={onOptimize}
            disabled={isOptimizing || selectedCount < 2}
            className="btn-coral flex min-h-11 w-full items-center justify-center gap-1.5 px-4 py-2 text-xs shadow-sm lg:w-auto"
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
          {!isOptimizing && selectedCount < 2 && (
            <div className="absolute top-full mt-1.5 left-1/2 -translate-x-1/2 whitespace-nowrap text-[11px] text-gray-400 bg-white/90 backdrop-blur-sm border border-gray-100 rounded-lg px-2.5 py-1 shadow-sm opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none z-50">
              {selectedCount === 0 ? '点击地点卡片心形 ♡ 选择要去的地点' : `再选 ${2 - selectedCount} 个地点即可排线`}
            </div>
          )}
        </div>

        {/* 分割线 */}
        <div className="hidden w-px h-5 bg-gray-200/60 lg:block" />

        {/* 用户头像菜单 */}
        <div className="flex min-h-11 items-center justify-center">
          <UserMenu />
        </div>
      </div>
    </motion.header>
  )
}
