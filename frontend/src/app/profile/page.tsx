'use client'

import { useState, useEffect, useRef } from 'react'
import { useRouter } from 'next/navigation'
import { motion } from 'framer-motion'
import { ArrowLeft, Camera, User, Calendar, Phone, Save, Check, ShieldCheck } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import { useToastStore } from '@/stores/toastStore'
import { api } from '@/lib/api'
import MemorySettingsPanel from '@/components/profile/MemorySettingsPanel'
import {
  clearTripUnderstandingSession,
  deleteAllTravelData,
  readTravelDataDeletionStatus,
  type TravelDataDeletionStatusView,
} from '@/lib/trip-understanding-v3'

interface UserProfile {
  user_id: string
  nickname: string
  phone: string | null
  avatar_url: string | null
  birthday: string | null
  created_at: string
}

export default function ProfilePage() {
  const router = useRouter()
  const { user, updateUser, isHydrated, hydrate, logout } = useAuthStore()
  const toast = useToastStore(s => s.toast)
  const [profile, setProfile] = useState<UserProfile | null>(null)
  const [profileLoading, setProfileLoading] = useState(false)
  const [profileLoadFailed, setProfileLoadFailed] = useState(false)
  const [profileLoadAttempt, setProfileLoadAttempt] = useState(0)
  const [nickname, setNickname] = useState('')
  const [birthday, setBirthday] = useState('')
  const [avatarUrl, setAvatarUrl] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [showTravelDelete, setShowTravelDelete] = useState(false)
  const [travelDeleteConfirmation, setTravelDeleteConfirmation] = useState('')
  const [travelDeleteBusy, setTravelDeleteBusy] = useState(false)
  const [travelDeleteStatus, setTravelDeleteStatus] = useState<TravelDataDeletionStatusView | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    hydrate()
  }, [hydrate])

  useEffect(() => {
    if (isHydrated && !user) router.replace('/login')
  }, [isHydrated, user, router])

  useEffect(() => {
    if (!user) return
    setProfileLoading(true)
    setProfileLoadFailed(false)
    api.get<UserProfile>('/api/user/me').then(p => {
      setProfile(p)
      setNickname(p.nickname || '')
      setBirthday(p.birthday || '')
      setAvatarUrl(p.avatar_url || '')
    }).catch(() => {
      setProfile(null)
      setProfileLoadFailed(true)
    }).finally(() => {
      setProfileLoading(false)
    })
  }, [user, profileLoadAttempt])

  const handleAvatarChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = ev => {
      const dataUrl = ev.target?.result as string
      setAvatarUrl(dataUrl)
    }
    reader.readAsDataURL(file)
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      await api.put('/api/user/profile', {
        nickname: nickname.trim() || undefined,
        birthday: birthday || undefined,
        avatar_url: avatarUrl || undefined,
      })
      updateUser({ nickname: nickname.trim() || user!.nickname, avatarUrl: avatarUrl || undefined })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch {
      toast('保存失败，请稍后重试', 'error')
    } finally {
      setSaving(false)
    }
  }

  const handleDeleteAllTravelData = async () => {
    if (travelDeleteConfirmation !== '清空全部旅行数据' || travelDeleteBusy) return
    setTravelDeleteBusy(true)
    setTravelDeleteStatus(null)
    try {
      await deleteAllTravelData()
      const freshStatus = await readTravelDataDeletionStatus()
      setTravelDeleteStatus(freshStatus)
      if (freshStatus.status === 'COMPLETED') {
        clearTripUnderstandingSession()
        setTravelDeleteConfirmation('')
      }
    } catch (deleteError) {
      if (deleteError instanceof Error && deleteError.message === 'RECENT_LOGIN_REQUIRED') {
        sessionStorage.setItem('bt_login_return', '/profile')
        toast('为保护你的数据，请重新登录后再确认清空。', 'info')
        logout()
        return
      }
      setTravelDeleteStatus({
        status: 'RETRY_REQUIRED',
        message: '尚未确认清理完成，可以重试。',
        next_action: 'RETRY',
      })
    } finally {
      setTravelDeleteBusy(false)
    }
  }

  if (!isHydrated) return (
    <div className="min-h-screen bg-gradient-to-br from-coral-50/40 via-white to-blue-50/30 flex flex-col">
      <div className="sticky top-0 z-10 bg-white/80 backdrop-blur-md border-b border-gray-100 px-4 py-3 flex items-center gap-3">
        <div className="w-9 h-9 rounded-xl bg-gray-100 animate-pulse" />
        <div className="h-4 w-20 bg-gray-200 rounded animate-pulse" />
      </div>
      <div className="max-w-md mx-auto w-full p-4 space-y-4 mt-4">
        <div className="flex flex-col items-center gap-3">
          <div className="w-20 h-20 rounded-full bg-gray-200 animate-pulse" />
          <div className="h-4 w-28 bg-gray-200 rounded animate-pulse" />
        </div>
        {[1, 2, 3].map(i => (
          <div key={i} className="h-12 rounded-xl bg-gray-100 animate-pulse" />
        ))}
      </div>
    </div>
  )
  if (!user) return null
  if (profileLoading || (!profile && !profileLoadFailed)) return (
    <div className="min-h-screen bg-gradient-to-br from-coral-50/40 via-white to-blue-50/30 flex items-center justify-center px-4">
      <p role="status" className="rounded-2xl bg-white px-5 py-4 text-sm text-gray-500 shadow-glass">
        正在读取个人资料…
      </p>
    </div>
  )
  if (profileLoadFailed) return (
    <div className="min-h-screen bg-gradient-to-br from-coral-50/40 via-white to-blue-50/30 flex items-center justify-center px-4">
      <div data-testid="profile-load-error" role="alert" className="w-full max-w-sm rounded-2xl bg-white p-5 text-center shadow-glass">
        <p className="text-sm font-medium text-gray-800">个人资料暂时无法读取</p>
        <p className="mt-2 text-xs leading-5 text-gray-500">你的资料没有被清空，请稍后重试。</p>
        <button
          data-testid="retry-profile-load"
          type="button"
          onClick={() => setProfileLoadAttempt(attempt => attempt + 1)}
          className="btn-coral mt-4 px-5 py-2.5 text-sm"
        >
          重新读取
        </button>
      </div>
    </div>
  )

  return (
    <div className="min-h-screen bg-gradient-to-br from-coral-50/40 via-white to-blue-50/30">
      {/* 顶栏 */}
      <div className="sticky top-0 z-10 bg-white/80 backdrop-blur-md border-b border-gray-100 px-4 py-3 flex items-center gap-3">
        <button onClick={() => router.back()} className="p-2 rounded-xl hover:bg-gray-100 transition-colors">
          <ArrowLeft className="w-5 h-5 text-gray-600" />
        </button>
        <h1 className="font-semibold text-gray-800">个人信息</h1>
      </div>

      <div className="max-w-md mx-auto p-4 pt-6">
        {/* 头像 */}
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="flex flex-col items-center mb-8"
        >
          <div className="relative">
            <div
              className="w-24 h-24 rounded-full bg-coral-100 flex items-center justify-center overflow-hidden cursor-pointer ring-4 ring-white shadow-lg"
              onClick={() => fileInputRef.current?.click()}
            >
              {avatarUrl ? (
                <img src={avatarUrl} alt="avatar" className="w-full h-full object-cover" />
              ) : (
                <span className="text-3xl font-bold text-coral-400">
                  {(nickname || user.nickname || '?')[0].toUpperCase()}
                </span>
              )}
            </div>
            <button
              onClick={() => fileInputRef.current?.click()}
              className="absolute bottom-0 right-0 w-8 h-8 bg-coral-500 rounded-full flex items-center justify-center shadow-md text-white hover:bg-coral-600 transition-colors"
            >
              <Camera className="w-4 h-4" />
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={handleAvatarChange}
            />
          </div>
          <p className="text-xs text-gray-400 mt-2">点击更换头像</p>
        </motion.div>

        {/* 表单 */}
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.1 }}
          className="glass-panel-solid rounded-2xl overflow-hidden shadow-glass divide-y divide-gray-100"
        >
          {/* 昵称 */}
          <div className="flex items-center gap-3 px-4 py-4">
            <div className="w-8 h-8 rounded-lg bg-coral-50 flex items-center justify-center flex-shrink-0">
              <User className="w-4 h-4 text-coral-500" />
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-[11px] text-gray-400 mb-1">昵称</p>
              <input
                type="text"
                value={nickname}
                onChange={e => setNickname(e.target.value)}
                placeholder="你的旅行代号"
                maxLength={20}
                className="w-full text-sm text-gray-800 bg-transparent outline-none placeholder:text-gray-300"
              />
            </div>
          </div>

          {/* 生日 */}
          <div className="flex items-center gap-3 px-4 py-4">
            <div className="w-8 h-8 rounded-lg bg-blue-50 flex items-center justify-center flex-shrink-0">
              <Calendar className="w-4 h-4 text-blue-400" />
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-[11px] text-gray-400 mb-1">生日（选填）</p>
              <input
                type="date"
                value={birthday}
                onChange={e => setBirthday(e.target.value)}
                max={new Date().toISOString().split('T')[0]}
                className="w-full text-sm text-gray-800 bg-transparent outline-none"
              />
            </div>
          </div>

          {/* 手机号（只读） */}
          <div className="flex items-center gap-3 px-4 py-4">
            <div className="w-8 h-8 rounded-lg bg-gray-50 flex items-center justify-center flex-shrink-0">
              <Phone className="w-4 h-4 text-gray-400" />
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-[11px] text-gray-400 mb-1">手机号</p>
              <p className="text-sm text-gray-500">{profile?.phone || '未绑定'}</p>
            </div>
          </div>
        </motion.div>

        <MemorySettingsPanel />

        <motion.section
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.16 }}
          className="mt-4 rounded-2xl border border-gray-200 bg-white p-4 shadow-glass"
          aria-labelledby="travel-data-privacy-title"
        >
          <div className="flex items-start gap-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-emerald-50 text-emerald-700">
              <ShieldCheck className="h-4 w-4" aria-hidden="true" />
            </div>
            <div>
              <h2 id="travel-data-privacy-title" className="text-sm font-semibold text-gray-800">账号旅行数据</h2>
              <p className="mt-1 text-xs leading-5 text-gray-500">清空所有新版行程原文、卡片、结构化偏好和反馈，并撤销全部分享；不会删除登录账号或个人资料。此操作不可恢复。</p>
            </div>
          </div>

          {!showTravelDelete ? (
            <button
              data-testid="open-account-travel-delete"
              type="button"
              onClick={() => setShowTravelDelete(true)}
              className="mt-4 w-full rounded-xl border border-gray-300 px-3 py-2.5 text-sm font-medium text-gray-600 hover:border-amber-300 hover:text-amber-800"
            >
              清空全部旅行数据
            </button>
          ) : (
            <div className="mt-4 rounded-xl bg-amber-50 p-3">
              <label className="block text-xs font-medium leading-5 text-amber-900">
                再次确认：输入“清空全部旅行数据”
                <input
                  data-testid="account-travel-delete-confirmation"
                  value={travelDeleteConfirmation}
                  onChange={(event) => setTravelDeleteConfirmation(event.target.value)}
                  className="mt-2 w-full rounded-lg border border-amber-200 bg-white px-3 py-2 text-sm text-gray-800 outline-none focus:border-amber-500"
                  autoComplete="off"
                />
              </label>
              <div className="mt-3 flex gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setShowTravelDelete(false)
                    setTravelDeleteConfirmation('')
                  }}
                  disabled={travelDeleteBusy}
                  className="flex-1 rounded-lg border border-amber-200 bg-white px-3 py-2 text-xs text-gray-600"
                >
                  取消
                </button>
                <button
                  data-testid="confirm-account-travel-delete"
                  type="button"
                  onClick={() => void handleDeleteAllTravelData()}
                  disabled={travelDeleteBusy || travelDeleteConfirmation !== '清空全部旅行数据'}
                  className="flex-1 rounded-lg bg-slate-900 px-3 py-2 text-xs font-medium text-white disabled:opacity-40"
                >
                  {travelDeleteBusy ? '正在清理…' : '确认永久清空'}
                </button>
              </div>
            </div>
          )}

          {travelDeleteStatus && (
            <p data-testid="account-travel-delete-status" role="status" className="mt-3 rounded-xl bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-600">
              {travelDeleteStatus.message}
              {travelDeleteStatus.status === 'IN_PROGRESS' ? '，可以稍后回到这里查看。' : ''}
            </p>
          )}
        </motion.section>

        {/* 保存按钮 */}
        <motion.button
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.2 }}
          onClick={handleSave}
          disabled={saving}
          className="btn-coral w-full py-3 mt-6 text-sm flex items-center justify-center gap-2"
        >
          {saved ? (
            <><Check className="w-4 h-4" /> 已保存</>
          ) : saving ? (
            <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
          ) : (
            <><Save className="w-4 h-4" /> 保存修改</>
          )}
        </motion.button>

        {profile && (
          <p className="text-center text-xs text-gray-300 mt-4">
            注册于 {new Date(profile.created_at).toLocaleDateString('zh-CN')}
          </p>
        )}
      </div>
    </div>
  )
}
