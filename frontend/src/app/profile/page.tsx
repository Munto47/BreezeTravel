'use client'

import { useState, useEffect, useRef } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import { ArrowLeft, Camera, Save, Check } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import { useToastStore } from '@/stores/toastStore'
import { api } from '@/lib/api'
import MemorySettingsPanel from '@/components/profile/MemorySettingsPanel'
import '../experience.css'
import './profile.css'
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
  const profileUserId = user?.userId
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
  const [memoryVersion, setMemoryVersion] = useState(0)
  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    hydrate()
  }, [hydrate])

  useEffect(() => {
    if (isHydrated && !user) {
      sessionStorage.setItem('bt_login_return', '/profile')
      router.replace('/login')
    }
  }, [isHydrated, user, router])

  useEffect(() => {
    if (!profileUserId) return
    sessionStorage.setItem('bt_login_return', '/profile')
    let current = true
    setProfileLoading(true)
    setProfileLoadFailed(false)
    api.get<UserProfile>('/api/user/me').then(p => {
      if (!current) return
      setProfile(p)
      setNickname(p.nickname || '')
      setBirthday(p.birthday || '')
      setAvatarUrl(p.avatar_url || '')
    }).catch(() => {
      if (!current) return
      setProfile(null)
      setProfileLoadFailed(true)
    }).finally(() => {
      if (current) setProfileLoading(false)
    })
    return () => { current = false }
  }, [profileUserId, profileLoadAttempt])

  const handleAvatarChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = ev => {
      const dataUrl = ev.target?.result as string
      setAvatarUrl(dataUrl)
      setSaved(false)
    }
    reader.readAsDataURL(file)
  }

  const handleSave = async () => {
    if (saving || !user) return
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
        for (const key of ['bt_input_draft', 'bt_pending_operation', 'bt_claim_after_login']) sessionStorage.removeItem(key)
        setMemoryVersion(version => version + 1)
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

  const loading = !isHydrated || profileLoading || (Boolean(user) && !profile && !profileLoadFailed)

  return (
    <main className="experience profile-page">
      <header className="e-header">
        <Link href="/" className="e-brand">行程查<span>TRIPCHECK</span></Link>
        <nav className="e-actions" aria-label="全局导航">
          <Link href="/" className="e-button e-button-quiet">整理新行程</Link>
          <Link href="/my-trips" className="e-button e-button-quiet">我的行程</Link>
        </nav>
      </header>
      <div className="profile-content">
        <Link href="/my-trips" className="profile-back"><ArrowLeft aria-hidden="true" />返回我的行程</Link>
        <div className="profile-heading">
          <h1>账号设置</h1>
          <p>管理个人资料、旅行偏好与数据。</p>
        </div>

        {loading ? <p className="profile-state" role="status">正在读取个人资料…</p>
          : !user ? <div className="profile-state" role="status"><p>登录后继续查看账号设置。</p><Link href="/login" className="e-button e-button-primary" onClick={() => sessionStorage.setItem('bt_login_return', '/profile')}>前往登录</Link></div>
          : profileLoadFailed ? <section data-testid="profile-load-error" className="profile-state" role="alert">
            <h2>个人资料暂时无法读取</h2>
            <p>已保存的资料仍会保留，可以稍后重试。</p>
            <button data-testid="retry-profile-load" type="button" className="e-button" onClick={() => setProfileLoadAttempt(attempt => attempt + 1)}>重新读取</button>
          </section>
          : <>
            <form className="profile-section" aria-labelledby="profile-details-title" onSubmit={event => { event.preventDefault(); void handleSave() }}>
              <h2 id="profile-details-title">个人资料</h2>
              <div className="profile-avatar-row">
                <button type="button" className="profile-avatar" aria-label="更换头像" onClick={() => fileInputRef.current?.click()}>
                  {avatarUrl ? <img src={avatarUrl} alt="当前头像" /> : <span aria-hidden="true">{(nickname || user.nickname || '?')[0].toUpperCase()}</span>}
                </button>
                <div><button type="button" className="e-button" onClick={() => fileInputRef.current?.click()}><Camera aria-hidden="true" />选择头像</button><p className="profile-help">选择后，点击“保存资料”生效。</p></div>
                <input ref={fileInputRef} type="file" accept="image/*" onChange={handleAvatarChange} aria-label="头像文件" hidden />
              </div>
              <div className="profile-fields">
                <label htmlFor="profile-nickname">称呼<input id="profile-nickname" name="nickname" autoComplete="nickname" value={nickname} onChange={event => { setNickname(event.target.value); setSaved(false) }} maxLength={20} placeholder="你希望我们怎样称呼你" /></label>
                <label htmlFor="profile-birthday">生日（选填）<input id="profile-birthday" name="birthday" type="date" autoComplete="bday" value={birthday} onChange={event => { setBirthday(event.target.value); setSaved(false) }} max={new Date().toISOString().split('T')[0]} /></label>
              </div>
              {profile?.phone && <div className="profile-readonly"><span>手机号</span><span>{profile.phone}</span></div>}
              <div className="profile-save-row"><button type="submit" className="e-button e-button-primary" disabled={saving}>{saved ? <Check aria-hidden="true" /> : <Save aria-hidden="true" />}{saving ? '正在保存…' : saved ? '已保存' : '保存资料'}</button><span role="status" className="profile-help">{saved ? '资料已更新。' : ''}</span></div>
            </form>

            <MemorySettingsPanel key={memoryVersion} />

            <section className="profile-section" aria-labelledby="travel-data-privacy-title">
              <h2 id="travel-data-privacy-title">账号旅行数据</h2>
              <p className="profile-help">清空已保存的行程原文、卡片、旅行偏好和反馈，并撤销全部分享。登录账号和个人资料保留。清空后无法恢复。</p>
              {!showTravelDelete ? <button data-testid="open-account-travel-delete" type="button" className="e-button profile-delete-entry" onClick={() => setShowTravelDelete(true)}>清空全部旅行数据</button>
                : <div className="profile-delete-confirmation">
                  <label htmlFor="travel-delete-confirmation">输入“清空全部旅行数据”以确认<input id="travel-delete-confirmation" data-testid="account-travel-delete-confirmation" value={travelDeleteConfirmation} onChange={event => setTravelDeleteConfirmation(event.target.value)} autoComplete="off" disabled={travelDeleteBusy} /></label>
                  <div className="profile-button-row">
                    <button type="button" className="e-button" disabled={travelDeleteBusy} onClick={() => { setShowTravelDelete(false); setTravelDeleteConfirmation('') }}>取消</button>
                    <button data-testid="confirm-account-travel-delete" type="button" className="e-button profile-delete-button" disabled={travelDeleteBusy || travelDeleteConfirmation !== '清空全部旅行数据'} onClick={() => void handleDeleteAllTravelData()}>{travelDeleteBusy ? '正在清理…' : '确认永久清空'}</button>
                  </div>
                </div>}
              {travelDeleteStatus && <p data-testid="account-travel-delete-status" role="status" className="profile-notice">{travelDeleteStatus.message}{travelDeleteStatus.status === 'IN_PROGRESS' ? '，可以稍后回到这里查看。' : ''}</p>}
            </section>
            {profile && <p className="profile-help profile-joined">注册于 {new Date(profile.created_at).toLocaleDateString('zh-CN')}</p>}
          </>}
      </div>
    </main>
  )
}
