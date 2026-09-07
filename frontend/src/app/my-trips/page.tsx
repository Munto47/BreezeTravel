'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { ArrowRight, Plus } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import {
  clearTripUnderstandingSession,
  deleteTripUnderstanding,
  listMyTrips,
  type MyTripListItem,
} from '@/lib/trip-understanding-v3'
import '../experience.css'
import './my-trips.css'

function formatted(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? '时间待确认'
    : new Intl.DateTimeFormat('zh-CN', {
        month: 'long',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      }).format(date)
}

export default function MyTripsPage() {
  const router = useRouter()
  const { user, isHydrated, hydrate, logout } = useAuthStore()
  const [items, setItems] = useState<MyTripListItem[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [needsLogin, setNeedsLogin] = useState(false)
  const [pendingReference, setPendingReference] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<MyTripListItem | null>(null)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const [notice, setNotice] = useState('')
  const inFlight = useRef(false)
  const loadGeneration = useRef(0)
  const deleteInFlight = useRef(false)
  const dialog = useRef<HTMLDialogElement>(null)
  const retryCursor = useRef<string | null>(null)
  const deleteTrigger = useRef<HTMLElement | null>(null)

  const load = useCallback(
    async (next: string | null = null, signal?: AbortSignal) => {
      if (inFlight.current && !signal) return
      const generation = ++loadGeneration.current
      inFlight.current = true
      retryCursor.current = next
      setLoading(true)
      setError('')
      const controller = new AbortController()
      const cancel = () => controller.abort()
      signal?.addEventListener('abort', cancel, { once: true })
      const timeout = window.setTimeout(cancel, 15000)
      try {
        const result = await listMyTrips(next, controller.signal)
        if (signal?.aborted) return
        setItems((previous) => {
          const combined = next ? [...previous, ...result.items] : result.items
          return Array.from(
            new Map(
              combined.map((item) => [item.public_resource_id, item]),
            ).values(),
          )
        })
        setCursor(result.next_cursor)
        setLoaded(true)
        setNeedsLogin(false)
      } catch (failure) {
        if (signal?.aborted) return
        if (failure instanceof Error && failure.message === 'LOGIN_REQUIRED')
          setNeedsLogin(true)
        else if (
          failure instanceof Error &&
          failure.message === 'LIST_CURSOR_CHANGED'
        ) {
          retryCursor.current = null
          setError('行程列表已有变化，请重新载入。')
        } else setError('暂时无法载入行程。已保存的内容不会因此丢失，请重试。')
      } finally {
        window.clearTimeout(timeout)
        signal?.removeEventListener('abort', cancel)
        if (generation === loadGeneration.current) {
          inFlight.current = false
          if (!signal?.aborted) setLoading(false)
        }
      }
    },
    [],
  )

  useEffect(() => {
    hydrate()
  }, [hydrate])
  useEffect(() => {
    if (!isHydrated || !user) return
    const controller = new AbortController()
    void load(null, controller.signal)
    return () => controller.abort()
  }, [isHydrated, user?.userId, load])
  useEffect(() => {
    if (deleting && !dialog.current?.open) dialog.current?.showModal()
    if (!deleting && dialog.current?.open) dialog.current.close()
  }, [deleting])

  function login() {
    sessionStorage.setItem('bt_login_return', '/my-trips')
    if (needsLogin) {
      logout()
      return
    }
    router.push('/login')
  }
  function openTrip(item: MyTripListItem) {
    try {
      const pending = JSON.parse(
        sessionStorage.getItem('bt_pending_operation') || 'null',
      )
      const reference = pending?.claimedResource || pending?.resource
      if (
        typeof reference === 'string' &&
        reference !== item.public_resource_id
      ) {
        setPendingReference(reference)
        return
      }
    } catch {
      /* Invalid local data does not grant access to a trip. */
    }
    if (
      sessionStorage.getItem('bt_active_trip_ref') !== item.public_resource_id
    )
      clearTripUnderstandingSession()
    sessionStorage.setItem('bt_active_trip_ref', item.public_resource_id)
    sessionStorage.setItem('bt_active_trip_mode', 'CLAIMED')
    sessionStorage.setItem('bt_active_trip_is_demo', String(item.is_demo))
    router.push(
      `/trip/result#trip=${encodeURIComponent(item.public_resource_id)}`,
    )
  }
  function closeDelete() {
    if (deleteInFlight.current) return
    setDeleting(null)
    setDeleteError('')
    deleteTrigger.current?.focus()
  }
  async function remove() {
    if (!deleting || deleteInFlight.current) return
    deleteInFlight.current = true
    setDeleteBusy(true)
    setDeleteError('')
    const target = deleting
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 15000)
    try {
      await deleteTripUnderstanding(
        target.public_resource_id,
        controller.signal,
      )
      setItems((previous) =>
        previous.filter(
          (item) => item.public_resource_id !== target.public_resource_id,
        ),
      )
      if (
        sessionStorage.getItem('bt_active_trip_ref') ===
        target.public_resource_id
      ) {
        clearTripUnderstandingSession()
        sessionStorage.removeItem('bt_pending_operation')
      }
      setDeleting(null)
      setNotice(`“${target.title}”已删除。`)
      document.getElementById('my-trips-heading')?.focus()
    } catch {
      setDeleteError(
        '暂时未能确认删除结果。重试会确认同一次删除；不会重复处理其他行程。',
      )
    } finally {
      window.clearTimeout(timeout)
      deleteInFlight.current = false
      setDeleteBusy(false)
    }
  }

  return (
    <main className="experience">
      <header className="e-header">
        <Link href="/" className="e-brand">
          行程查<span>TRIPCHECK</span>
        </Link>
        <nav className="e-actions" aria-label="全局导航">
          <Link href="/" className="e-button e-button-quiet">
            整理新行程
          </Link>
          {user && (
            <Link href="/profile" className="e-button e-button-quiet">
              账号
            </Link>
          )}
          {user && (
            <button
              className="e-button e-button-quiet"
              onClick={() => {
                sessionStorage.setItem('bt_login_return', '/my-trips')
                logout()
              }}
            >
              退出
            </button>
          )}
        </nav>
      </header>
      <section className="e-library" aria-labelledby="my-trips-heading">
        <div className="e-library-heading">
          <div>
            <h1 id="my-trips-heading" tabIndex={-1}>
              我的行程
            </h1>
            <p className="e-muted">保存过的安排，从这里接着走。</p>
          </div>
          {user && !needsLogin && (
            <Link href="/" className="e-button e-button-primary">
              <Plus aria-hidden="true" />
              新建行程
            </Link>
          )}
        </div>
        {(!isHydrated || (!loaded && loading)) && (
          <p className="e-library-empty" role="status">
            正在找回你的行程…
          </p>
        )}
        {isHydrated && (!user || needsLogin) ? (
          <div className="e-library-empty">
            <h2>
              {needsLogin ? '重新登录后查看行程' : '登录，找回保存过的行程'}
            </h2>
            <p className="e-muted">
              账号中的行程可在其他浏览器继续编辑，默认保留 30 天。
            </p>
            <button className="e-button e-button-primary" onClick={login}>
              登录并查看
            </button>
            <Link href="/" className="e-button e-button-quiet">
              先整理一份行程
            </Link>
          </div>
        ) : (
          <>
            {error && (
              <div className="e-message" role="alert">
                <p>{error}</p>
                <button
                  className="e-button"
                  disabled={loading}
                  onClick={() => void load(retryCursor.current)}
                >
                  重新载入
                </button>
              </div>
            )}
            {notice && (
              <p className="e-message" role="status">
                {notice}
              </p>
            )}
            {pendingReference && (
              <div className="e-message" role="status">
                <p>另一份行程还有一次修改等待确认，先确认结果再切换。</p>
                <Link
                  className="e-button"
                  href={`/trip/result#trip=${encodeURIComponent(pendingReference)}`}
                >
                  返回并确认修改
                </Link>
              </div>
            )}
            {loaded && !items.length && !cursor && !error && (
              <div className="e-library-empty">
                <h2>这里还没有保存的行程</h2>
                <p className="e-muted">
                  先粘贴一份攻略，整理后选择“保存到账号”。
                </p>
                <Link href="/" className="e-button e-button-primary">
                  整理第一份行程
                  <ArrowRight aria-hidden="true" />
                </Link>
              </div>
            )}
            {items.length > 0 && (
              <>
                <p className="e-small e-muted e-library-retention">
                  按最近修改排序。到期时间以每份行程标注为准，你也可以随时删除。
                </p>
                <ul className="e-trip-list" aria-label="已保存行程">
                  {items.map((item) => (
                    <li
                      key={item.public_resource_id}
                      className="e-trip-list-row"
                    >
                      <div className="e-trip-list-main">
                        <h2>
                          <button onClick={() => openTrip(item)}>
                            {item.title}
                          </button>
                        </h2>
                        <p className="e-trip-list-meta">
                          {item.city} · {item.day_count} 天
                          {item.is_demo && <span> · 固定示例</span>}
                        </p>
                        <p className="e-small e-muted">
                          最近修改 {formatted(item.updated_at)}
                          <span className="e-trip-list-expiry">
                            保留至 {formatted(item.expires_at)}
                          </span>
                        </p>
                      </div>
                      <div className="e-trip-list-actions">
                        <button
                          className="e-button"
                          onClick={() => openTrip(item)}
                          aria-label={`继续编辑${item.title}`}
                        >
                          继续编辑
                          <ArrowRight aria-hidden="true" />
                        </button>
                        <button
                          className="e-button e-button-quiet"
                          aria-label={`删除${item.title}`}
                          onClick={(event) => {
                            deleteTrigger.current = event.currentTarget
                            setDeleting(item)
                            setDeleteError('')
                          }}
                        >
                          删除
                        </button>
                      </div>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {cursor && (
              <div className="e-library-more">
                <button
                  className="e-button"
                  disabled={loading}
                  onClick={() => void load(cursor)}
                >
                  {loading ? '正在载入…' : '查看更多行程'}
                </button>
              </div>
            )}
          </>
        )}
      </section>
      <dialog
        ref={dialog}
        className="e-delete-dialog"
        aria-labelledby="delete-trip-title"
        onCancel={(event) => {
          event.preventDefault()
          closeDelete()
        }}
      >
        <h2 id="delete-trip-title">删除这份行程？</h2>
        <p>
          “{deleting?.title}
          ”的攻略、安排和修改记录将被永久删除，无法恢复。其他行程不受影响。
        </p>
        {deleteError && (
          <p className="e-message" role="alert">
            {deleteError}
          </p>
        )}
        <div className="e-actions">
          <button
            className="e-button"
            autoFocus
            disabled={deleteBusy}
            onClick={closeDelete}
          >
            保留行程
          </button>
          <button
            className="e-button e-button-danger"
            disabled={deleteBusy}
            onClick={() => void remove()}
          >
            {deleteBusy
              ? '正在确认删除…'
              : deleteError
                ? '重试确认删除'
                : '确认永久删除'}
          </button>
        </div>
      </dialog>
    </main>
  )
}
