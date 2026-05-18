'use client'

import { useState, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { v4 as uuidv4 } from 'uuid'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Compass, Users, Copy, Check, ArrowRight, Sparkles,
  Map, Route, History, LogOut, MapPin, Calendar,
} from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import { api } from '@/lib/api'

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

interface RoomRecord {
  room_id: string
  city: string
  trip_days: number
  phase: string
  place_count: number
  created_at: string
}

export default function HomePage() {
  const router = useRouter()
  const { user, token, isHydrated, hydrate, logout } = useAuthStore()

  const [joinRoomId, setJoinRoomId] = useState('')
  const [city, setCity] = useState('成都')
  const [days, setDays] = useState(3)
  const [isCreating, setIsCreating] = useState(false)
  const [isJoining, setIsJoining] = useState(false)
  const [createdRoomInfo, setCreatedRoomInfo] = useState<{ roomId: string; threadId: string } | null>(null)
  const [copyTip, setCopyTip] = useState(false)
  const [recentRooms, setRecentRooms] = useState<RoomRecord[]>([])

  // 恢复 auth 状态
  useEffect(() => { hydrate() }, [hydrate])

  // 未登录跳转
  useEffect(() => {
    if (isHydrated && !user) router.replace('/login')
  }, [isHydrated, user, router])

  // 加载最近房间（最多 3 个）
  useEffect(() => {
    if (!user || !token) return
    api.get<RoomRecord[]>('/api/user/rooms')
      .then(rooms => setRecentRooms(rooms.slice(0, 3)))
      .catch(() => {})
  }, [user, token])

  if (!isHydrated || !user) return null

  const handleCreateRoom = async () => {
    setIsCreating(true)
    const roomId = String(Math.floor(100000 + Math.random() * 900000))
    const threadId = uuidv4()

    try {
      await fetch(`${API_BASE}/api/room`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          room_id: roomId,
          thread_id: threadId,
          trip_city: city,
          trip_days: days,
          user_id: user.userId,
          nickname: user.nickname,
        }),
      })
    } catch (e) {
      console.warn('创建房间失败，继续本地流程', e)
    }

    setIsCreating(false)
    setCreatedRoomInfo({ roomId, threadId })
  }

  const handleEnterRoom = () => {
    if (!createdRoomInfo) return
    router.push(
      `/room/${createdRoomInfo.roomId}?threadId=${createdRoomInfo.threadId}&city=${encodeURIComponent(city)}&days=${days}`
    )
  }

  const handleCopyRoomId = async () => {
    if (!createdRoomInfo) return
    try { await navigator.clipboard.writeText(createdRoomInfo.roomId) }
    catch {
      const input = document.createElement('input')
      input.value = createdRoomInfo.roomId
      document.body.appendChild(input); input.select()
      document.execCommand('copy'); document.body.removeChild(input)
    }
    setCopyTip(true)
    setTimeout(() => setCopyTip(false), 2000)
  }

  const handleJoinRoom = async () => {
    if (!joinRoomId.trim()) return alert('请输入房间号')
    setIsJoining(true)
    const trimmedRoomId = joinRoomId.trim()
    try {
      const res = await fetch(`${API_BASE}/api/room/${trimmedRoomId}/join`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ user_id: user.userId, nickname: user.nickname }),
      })
      if (!res.ok && res.status === 404) {
        alert(`房间 ${trimmedRoomId} 不存在，请检查房间号`)
        setIsJoining(false); return
      }
      const data = await res.json()
      const threadId = data.thread_id || trimmedRoomId
      const roomCity = data.trip_city || ''
      const roomDays = data.trip_days || 3
      router.push(
        `/room/${trimmedRoomId}?threadId=${threadId}${roomCity ? `&city=${encodeURIComponent(roomCity)}` : ''}&days=${roomDays}`
      )
    } catch (e) {
      console.warn('加入房间失败，继续本地流程', e)
      router.push(`/room/${trimmedRoomId}`)
    }
    setIsJoining(false)
  }

  const phaseLabel: Record<string, string> = {
    exploring: '探索中', selecting: '选择中', optimizing: '排线中', planned: '已排线',
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-coral-50/40 via-white to-blue-50/30 flex items-center justify-center p-4 overflow-auto">
      {/* 背景装饰 */}
      <div className="fixed inset-0 pointer-events-none overflow-hidden">
        <div className="absolute -top-40 -right-40 w-96 h-96 bg-coral-100/30 rounded-full blur-3xl" />
        <div className="absolute -bottom-20 -left-20 w-72 h-72 bg-blue-100/30 rounded-full blur-3xl" />
      </div>

      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
        className="w-full max-w-md relative z-10"
      >
        {/* Logo + 用户信息行 */}
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-coral-500 flex items-center justify-center shadow-md shadow-coral-200">
              <Compass className="w-5 h-5 text-white" strokeWidth={2} />
            </div>
            <div>
              <h1 className="text-base font-bold text-gray-900">BreezeTravel</h1>
              <p className="text-[11px] text-gray-400">你好，{user.nickname} 👋</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => router.push('/history')}
              className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-coral-500 transition-colors px-3 py-1.5 rounded-xl hover:bg-coral-50 border border-gray-200"
            >
              <History className="w-3.5 h-3.5" />
              历史
            </button>
            <button
              onClick={logout}
              className="p-1.5 rounded-xl text-gray-400 hover:text-red-400 hover:bg-red-50 transition-colors border border-gray-200"
              title="退出登录"
            >
              <LogOut className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>

        {/* 特性标签 */}
        <div className="flex items-center gap-2 mb-6">
          {[
            { icon: <Sparkles className="w-3 h-3" />, label: 'LangGraph Agent' },
            { icon: <Users className="w-3 h-3" />, label: 'Yjs 实时协同' },
            { icon: <Route className="w-3 h-3" />, label: 'K-Means TSP' },
          ].map((f) => (
            <span
              key={f.label}
              className="inline-flex items-center gap-1 text-[10px] text-gray-400 bg-gray-50 px-2 py-1 rounded-full border border-gray-100"
            >
              {f.icon}{f.label}
            </span>
          ))}
        </div>

        {/* 主卡片 */}
        <div className="glass-panel-solid rounded-2xl overflow-hidden shadow-glass mb-4">
          {/* 创建成功后显示房间号 */}
          <AnimatePresence mode="wait">
            {createdRoomInfo ? (
              <motion.div
                key="created"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                className="p-6"
              >
                <div className="bg-emerald-50/80 rounded-xl p-5 border border-emerald-100">
                  <div className="flex items-center gap-2 mb-4">
                    <div className="w-5 h-5 rounded-full bg-emerald-500 flex items-center justify-center">
                      <Check className="w-3 h-3 text-white" />
                    </div>
                    <span className="text-sm font-medium text-emerald-800">房间已创建</span>
                  </div>
                  <p className="text-[10px] text-gray-400 mb-1.5 uppercase tracking-wider">房间号</p>
                  <div className="flex items-center gap-2 mb-4">
                    <code className="flex-1 bg-white rounded-xl px-4 py-3 text-2xl font-mono font-bold text-gray-900 tracking-[0.2em] text-center border border-gray-100 shadow-sm">
                      {createdRoomInfo.roomId}
                    </code>
                    <button
                      onClick={handleCopyRoomId}
                      className="btn-glass text-xs px-3 py-3 flex items-center gap-1"
                    >
                      {copyTip ? <Check className="w-3.5 h-3.5 text-emerald-500" /> : <Copy className="w-3.5 h-3.5" />}
                    </button>
                  </div>
                  <p className="text-xs text-gray-400 mb-4 text-center">
                    {city} · {days} 天 · 分享房间号邀请朋友
                  </p>
                  <button onClick={handleEnterRoom} className="btn-coral w-full py-3 text-sm flex items-center justify-center gap-2">
                    进入规划房间 <ArrowRight className="w-4 h-4" />
                  </button>
                </div>
              </motion.div>
            ) : (
              <motion.div key="form" className="p-6 pb-4">
                <div className="flex gap-3 mb-4">
                  <div className="flex-1">
                    <label className="block text-[11px] font-medium text-gray-500 mb-1.5 uppercase tracking-wider">目的地</label>
                    <div className="relative">
                      <Map className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" />
                      <select
                        value={city}
                        onChange={(e) => setCity(e.target.value)}
                        className="input-glass pl-8 appearance-none"
                      >
                        <option>成都</option>
                        <option>北京</option>
                        <option>上海</option>
                        <option>厦门</option>
                      </select>
                    </div>
                  </div>
                  <div className="w-24">
                    <label className="block text-[11px] font-medium text-gray-500 mb-1.5 uppercase tracking-wider">天数</label>
                    <select value={days} onChange={(e) => setDays(Number(e.target.value))} className="input-glass appearance-none text-center">
                      {[2, 3, 4, 5, 7].map((d) => <option key={d} value={d}>{d} 天</option>)}
                    </select>
                  </div>
                </div>
                <button
                  onClick={handleCreateRoom}
                  disabled={isCreating}
                  className="btn-coral w-full py-3 text-sm flex items-center justify-center gap-2"
                >
                  {isCreating ? (
                    <><span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />创建中...</>
                  ) : (
                    <>创建协同房间 <ArrowRight className="w-4 h-4" /></>
                  )}
                </button>
              </motion.div>
            )}
          </AnimatePresence>

          {/* 分割线 */}
          <div className="mx-6 border-t border-gray-100/60" />

          {/* 加入已有房间 */}
          <div className="px-6 py-5">
            <p className="text-[11px] font-medium text-gray-500 mb-2.5 uppercase tracking-wider">加入房间</p>
            <div className="flex gap-2">
              <input
                type="text"
                value={joinRoomId}
                onChange={(e) => setJoinRoomId(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleJoinRoom()}
                placeholder="6 位房间号"
                maxLength={6}
                className="input-glass flex-1 font-mono tracking-wider text-center"
              />
              <button
                onClick={handleJoinRoom}
                disabled={isJoining}
                className="btn-glass px-5 py-2.5 text-sm font-medium"
              >
                {isJoining
                  ? <span className="w-4 h-4 border-2 border-gray-300 border-t-gray-600 rounded-full animate-spin block" />
                  : '加入'}
              </button>
            </div>
          </div>
        </div>

        {/* 近期规划房间（最多 3 个） */}
        {recentRooms.length > 0 && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.2 }}
          >
            <div className="flex items-center justify-between mb-2 px-1">
              <p className="text-[11px] font-medium text-gray-400 uppercase tracking-wider">继续上次规划</p>
              <button onClick={() => router.push('/history')} className="text-[11px] text-coral-500 hover:underline">
                查看全部
              </button>
            </div>
            <div className="space-y-2">
              {recentRooms.map((room) => (
                <button
                  key={room.room_id}
                  onClick={() => router.push(`/room/${room.room_id}`)}
                  className="w-full glass-panel-solid rounded-xl px-4 py-3 flex items-center gap-3 hover:shadow-md transition-shadow text-left"
                >
                  <div className="w-8 h-8 rounded-lg bg-coral-50 flex items-center justify-center flex-shrink-0">
                    <MapPin className="w-4 h-4 text-coral-400" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-semibold text-gray-800 truncate">{room.city || '未知目的地'}</p>
                    <div className="flex items-center gap-2 text-[11px] text-gray-400">
                      <span className="flex items-center gap-0.5">
                        <Calendar className="w-2.5 h-2.5" />{room.trip_days} 天
                      </span>
                      <span>·</span>
                      <span>{room.place_count} 个景点</span>
                      <span>·</span>
                      <span className={room.phase === 'planned' ? 'text-emerald-500' : 'text-blue-400'}>
                        {phaseLabel[room.phase] || room.phase}
                      </span>
                    </div>
                  </div>
                  <ArrowRight className="w-4 h-4 text-gray-300 flex-shrink-0" />
                </button>
              ))}
            </div>
          </motion.div>
        )}

        <p className="text-center text-[11px] text-gray-300 mt-6">
          BreezeTravel · AI 旅行协同规划
        </p>
      </motion.div>
    </div>
  )
}
