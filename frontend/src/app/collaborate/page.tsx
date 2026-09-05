'use client'

import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { ArrowRight, Clock3, LogIn, Plus, Users } from 'lucide-react'

import { api, ApiRequestError } from '@/lib/api'
import { useAuthStore } from '@/stores/authStore'
import '../experience.css'

interface RoomRecord {
  room_id: string
  thread_id: string
  city: string
  trip_days: number
  phase: string
  place_count: number
  itinerary_count: number
  created_at: string
}

interface JoinRoomResult {
  room_id: string
  thread_id: string
  trip_city?: string | null
  trip_days?: number | null
}

interface CreateRoomAttempt {
  signature: string
  roomId: string
  threadId: string
}

function roomHref(room: Pick<RoomRecord, 'room_id' | 'thread_id' | 'city' | 'trip_days'>) {
  return `/room/${encodeURIComponent(room.room_id)}`
}

function safeRoomCode(value: string) {
  return value.trim().replaceAll(/\s+/g, '').slice(0, 80)
}

export default function CollaboratePage() {
  const router = useRouter()
  const { user, isHydrated, hydrate } = useAuthStore()
  const [rooms, setRooms] = useState<RoomRecord[]>([])
  const [city, setCity] = useState('')
  const [days, setDays] = useState(3)
  const [joinCode, setJoinCode] = useState('')
  const [busy, setBusy] = useState<'create' | 'join' | 'rooms' | null>('rooms')
  const [error, setError] = useState('')
  const redirected = useRef(false)
  const hydratedJoinCode = useRef(false)
  const createAttempt = useRef<CreateRoomAttempt | null>(null)

  useEffect(() => hydrate(), [hydrate])
  useEffect(() => {
    if (hydratedJoinCode.current) return
    hydratedJoinCode.current = true
    setJoinCode(
      safeRoomCode(new URLSearchParams(window.location.search).get('join') || ''),
    )
  }, [])
  useEffect(() => {
    if (!isHydrated || user || redirected.current) return
    redirected.current = true
    sessionStorage.setItem(
      'bt_login_return',
      `${window.location.pathname}${window.location.search}${window.location.hash}`,
    )
    router.replace('/login')
  }, [isHydrated, router, user])

  const loadRooms = useCallback(async () => {
    if (!user) return
    setBusy('rooms')
    setError('')
    try {
      const result = await api.get<RoomRecord[]>('/api/user/rooms')
      setRooms(Array.isArray(result) ? result : [])
    } catch {
      setError('最近房间暂时没有加载成功，你仍可以创建或加入房间。')
    } finally {
      setBusy(null)
    }
  }, [user])

  useEffect(() => {
    void loadRooms()
  }, [loadRooms])

  async function createRoom(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!user || busy) return
    setBusy('create')
    setError('')
    const normalizedCity = city.trim()
    const signature = JSON.stringify({ city: normalizedCity, days })
    const pending = createAttempt.current
    const roomId = pending?.signature === signature
      ? pending.roomId
      : crypto.randomUUID().replaceAll('-', '').slice(0, 8).toUpperCase()
    const threadId = pending?.signature === signature
      ? pending.threadId
      : crypto.randomUUID()
    createAttempt.current = { signature, roomId, threadId }
    try {
      await api.post('/api/room', {
        room_id: roomId,
        thread_id: threadId,
        trip_city: normalizedCity || null,
        trip_days: days,
        nickname: user.nickname,
      })
      router.push(
        roomHref({
          room_id: roomId,
          thread_id: threadId,
          city: normalizedCity,
          trip_days: days,
        }),
      )
    } catch {
      setError('房间暂时没有创建成功，请重试。')
      setBusy(null)
    }
  }

  async function joinRoom(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!user || busy) return
    const roomId = safeRoomCode(joinCode)
    if (!roomId) return
    setBusy('join')
    setError('')
    try {
      const joined = await api.post<JoinRoomResult>(
        `/api/room/${encodeURIComponent(roomId)}/join`,
        { nickname: user.nickname },
      )
      router.push(
        roomHref({
          room_id: joined.room_id,
          thread_id: joined.thread_id,
          city: joined.trip_city || '',
          trip_days: joined.trip_days || 3,
        }),
      )
    } catch (failure) {
      setError(
        failure instanceof ApiRequestError && failure.status === 404
          ? '没有找到这个房间，请检查房间号。'
          : '暂时无法加入房间，请稍后重试。',
      )
      setBusy(null)
    }
  }

  if (!isHydrated || !user) {
    return (
      <main className="experience">
        <div className="e-collaborate-loading" role="status">
          正在打开协同规划…
        </div>
      </main>
    )
  }

  return (
    <main className="experience">
      <header className="e-header">
        <Link href="/" className="e-brand">
          行程查<span>TRIPCHECK</span>
        </Link>
        <nav className="e-actions" aria-label="模式导航">
          <Link href="/" className="e-button e-button-quiet">
            行程查
          </Link>
          <span className="e-button e-button-primary" aria-current="page">
            协同规划
          </span>
        </nav>
      </header>

      <section className="e-collaborate-shell">
        <header className="e-collaborate-intro">
          <p className="e-eyebrow">BreezeTravel 协同工作台</p>
          <h1>一起找地点，再由你决定何时排线</h1>
          <p className="e-muted">
            创建或加入房间，用 AI 问答整理候选地点。只有点击“智能排线”时才会计算路线。
          </p>
        </header>

        {error && (
          <p className="e-message" role="alert">
            {error}
          </p>
        )}

        <div className="e-collaborate-actions">
          <form className="e-collaborate-card" onSubmit={createRoom}>
            <div className="e-collaborate-card-title">
              <Plus aria-hidden="true" />
              <div>
                <h2>创建房间</h2>
                <p>目的地可以稍后在对话中补充。</p>
              </div>
            </div>
            <label className="e-field">
              目的地（选填）
              <input
                value={city}
                maxLength={40}
                disabled={Boolean(busy)}
                placeholder="例如：杭州"
                onChange={(event) => {
                  createAttempt.current = null
                  setCity(event.target.value)
                }}
              />
            </label>
            <label className="e-field">
              行程天数
              <select
                value={days}
                disabled={Boolean(busy)}
                onChange={(event) => {
                  createAttempt.current = null
                  setDays(Number(event.target.value))
                }}
              >
                {[1, 2, 3, 4, 5, 6, 7].map((value) => (
                  <option value={value} key={value}>
                    {value} 天
                  </option>
                ))}
              </select>
            </label>
            <button className="e-button e-button-primary" type="submit" disabled={Boolean(busy)}>
              {busy === 'create' ? '正在创建…' : '创建协同房间'}
              <ArrowRight aria-hidden="true" />
            </button>
          </form>

          <form className="e-collaborate-card" onSubmit={joinRoom}>
            <div className="e-collaborate-card-title">
              <LogIn aria-hidden="true" />
              <div>
                <h2>加入房间</h2>
                <p>输入朋友分享的房间号。</p>
              </div>
            </div>
            <label className="e-field">
              房间号
              <input
                value={joinCode}
                maxLength={80}
                required
                disabled={Boolean(busy)}
                autoCapitalize="characters"
                autoComplete="off"
                placeholder="例如：8F3A2C1D"
                onChange={(event) => setJoinCode(event.target.value)}
              />
            </label>
            <button className="e-button" type="submit" disabled={Boolean(busy) || !joinCode.trim()}>
              {busy === 'join' ? '正在加入…' : '加入协同房间'}
              <Users aria-hidden="true" />
            </button>
          </form>
        </div>

        <section className="e-recent-rooms" aria-labelledby="recent-rooms-title">
          <div className="e-toolbar">
            <div>
              <p className="e-eyebrow">继续上次规划</p>
              <h2 id="recent-rooms-title">最近房间</h2>
            </div>
            <button className="e-button e-button-quiet" type="button" onClick={() => void loadRooms()} disabled={Boolean(busy)}>
              刷新
            </button>
          </div>
          {busy === 'rooms' ? (
            <p className="e-muted" role="status">正在读取最近房间…</p>
          ) : rooms.length === 0 ? (
            <p className="e-collaborate-empty">还没有协同房间。创建一个，就可以邀请同行者一起规划。</p>
          ) : (
            <div className="e-room-grid">
              {rooms.map((room) => (
                <Link className="e-room-card" href={roomHref(room)} key={room.room_id}>
                  <div>
                    <strong>{room.city}</strong>
                    <span>{room.trip_days} 天 · {room.place_count} 个候选地点</span>
                  </div>
                  <small><Clock3 aria-hidden="true" />{new Date(room.created_at).toLocaleDateString('zh-CN')}</small>
                </Link>
              ))}
            </div>
          )}
        </section>
      </section>
    </main>
  )
}
