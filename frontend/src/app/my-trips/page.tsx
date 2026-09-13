'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { ArrowRight, Plus } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import ExperienceHeader from '@/components/experience/experience-header'
import {
  clearTripUnderstandingSession,
  deleteTripUnderstanding,
  listMyTrips,
  readTripSource,
  readTripUnderstandingResult,
  createTripRequestKey,
  type MyTripListItem,
} from '@/lib/trip-understanding-v3'
import { forgetBrowserTripReference, readBrowserTripReferences } from '@/lib/browser-resource-ref'
import { TRIP_INPUT_DRAFT_KEY } from '@/lib/trip-input-recovery'
import '../experience.css'
import './my-trips.css'

function formatted(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '时间待确认'
  const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000))
  return minutes < 1 ? '刚刚' : minutes < 60 ? `${minutes} 分钟前`
    : minutes < 1440 ? `${Math.floor(minutes / 60)} 小时前` : `${Math.floor(minutes / 1440)} 天前`
}

const stateText = { PROCESSING: '正在整理', READY: '已有行程', PARTIAL: '部分完成', FAILED: '整理失败', CANCELLED: '已停止' }
type LibraryTrip = MyTripListItem & { browserOnly?: boolean }

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
  const [browserItems, setBrowserItems] = useState<LibraryTrip[]>([])
  const [browserLoading, setBrowserLoading] = useState(false)
  const [browserError, setBrowserError] = useState('')
  const [recovering, setRecovering] = useState<string | null>(null)
  const [replacementDraft, setReplacementDraft] = useState<{text: string; demo: boolean} | null>(null)
  const recoverInFlight = useRef(false)
  const browserLoadGeneration = useRef(0)
  const inFlight = useRef(false)
  const loadGeneration = useRef(0)
  const deleteInFlight = useRef(false)
  const dialog = useRef<HTMLDialogElement>(null)
  const retryCursor = useRef<string | null>(null)
  const deleteTrigger = useRef<HTMLElement | null>(null)

  const loadBrowser = useCallback(async (signal?: AbortSignal) => {
    const generation = ++browserLoadGeneration.current
    const controller = new AbortController()
    const cancel = () => controller.abort()
    signal?.addEventListener('abort', cancel, { once: true })
    const timeout = window.setTimeout(cancel, 15000)
    setBrowserLoading(true)
    setBrowserError('')
    let unreadable = false
    const rows = await Promise.all(readBrowserTripReferences().map(async ({ resource }): Promise<LibraryTrip | null> => {
      const base = { public_resource_id: resource, city: '目的地待确认', day_count: 0,
        updated_at: '', expires_at: '', is_demo: false, browserOnly: true, has_result: false }
      try {
        const { body } = await readTripUnderstandingResult(resource, controller.signal)
        if (body.status === 'PROCESSING') return { ...base, title: '正在整理的行程', state: 'PROCESSING' }
        if (body.ownership === 'ACCOUNT') {
          forgetBrowserTripReference(resource)
          return null
        }
        const city = body.assumptions.find((item) => item.key === 'destination')?.value || '行程'
        return { ...base, title: `${city}行程`, city, day_count: body.days.length,
          updated_at: body.updated_at || '', is_demo: body.is_demo || false,
          state: body.coverage?.complete ? 'READY' : 'PARTIAL', has_result: true }
      } catch (failure) {
        if (signal?.aborted) return null
        const code = failure instanceof Error ? failure.message : ''
        if (['TRIP_GONE', 'TRIP_NOT_AVAILABLE'].includes(code)) {
          forgetBrowserTripReference(resource)
          return null
        }
        if (['UNDERSTANDING_FAILED', 'INPUT_CAPACITY_EXCEEDED', 'INPUT_DAY_CAPACITY_EXCEEDED'].includes(code))
          return { ...base, title: '未整理完成的行程', state: 'FAILED' }
        if (code === 'UNDERSTANDING_CANCELLED') return { ...base, title: '已停止的行程', state: 'CANCELLED' }
        unreadable = true
        return null
      }
    }))
    window.clearTimeout(timeout)
    signal?.removeEventListener('abort', cancel)
    if (signal?.aborted || generation !== browserLoadGeneration.current) return
    setBrowserItems(rows.filter((row): row is LibraryTrip => row !== null))
    setBrowserError(unreadable ? '有本机记录暂时无法读取，引用已保留，可以重新读取。' : '')
    setBrowserLoading(false)
  }, [])

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
        if (signal?.aborted || generation !== loadGeneration.current) return
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
        if (signal?.aborted || generation !== loadGeneration.current) return
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
    const controller = new AbortController()
    void loadBrowser(controller.signal)
    return () => controller.abort()
  }, [loadBrowser, user?.userId])
  useEffect(() => {
    ++loadGeneration.current
    setItems([])
    setLoaded(false)
    setReplacementDraft(null)
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
  function openTrip(item: LibraryTrip) {
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
    sessionStorage.setItem('bt_active_trip_mode', item.browserOnly ? 'FULL' : 'CLAIMED')
    sessionStorage.setItem('bt_active_trip_is_demo', String(item.is_demo))
    router.push(
      `/trip/result#trip=${encodeURIComponent(item.public_resource_id)}`,
    )
  }

  async function recoverSource(item: LibraryTrip) {
    if (recoverInFlight.current) return
    try {
      const pending = JSON.parse(sessionStorage.getItem('bt_pending_operation') || 'null')
      const reference = pending?.claimedResource || pending?.resource
      if (typeof reference === 'string' && /^[A-Za-z0-9_-]{20,80}$/.test(reference)) {
        setPendingReference(reference)
        setNotice('还有一次行程修改等待确认，请先返回行程确认结果，再恢复原文。')
        return
      }
    } catch { /* Invalid local data is not a pending server operation. */ }
    recoverInFlight.current = true
    const generation = loadGeneration.current
    setRecovering(item.public_resource_id)
    setNotice('')
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 15000)
    try {
      const source = await readTripSource(item.public_resource_id, controller.signal)
      if (generation !== loadGeneration.current) return
      if (source.status !== 'AVAILABLE' || !source.text) {
        setNotice(source.status === 'DELETED' ? '这份原文已删除。请重新输入攻略后再整理。' : '这份原文已无法恢复。请重新输入攻略后再整理。')
        return
      }
      // Explicit recovery uses the existing short-lived input draft. Merely
      // listing a task never downloads or caches its private source text.
      const draft = { text: source.text, demo: item.is_demo }
      let existing
      try { existing = JSON.parse(sessionStorage.getItem(TRIP_INPUT_DRAFT_KEY) || 'null') } catch { /* Invalid local draft grants no access. */ }
      if (existing?.text && existing.text !== source.text && existing.expires > Date.now()) {
        setReplacementDraft(draft)
      } else useRecoveredDraft(draft)
    } catch {
      setNotice('暂时无法恢复原文。请确认使用原浏览器或保存它的账号后重试；也可以重新输入攻略。')
    } finally {
      window.clearTimeout(timeout)
      recoverInFlight.current = false
      setRecovering(null)
    }
  }
  function useRecoveredDraft(draft: {text: string; demo: boolean}) {
    try {
      sessionStorage.setItem(TRIP_INPUT_DRAFT_KEY, JSON.stringify({ ...draft,
        key: createTripRequestKey(), expires: Date.now() + 24 * 60 * 60 * 1000 }))
      setReplacementDraft(null)
      router.push('/')
    } catch {
      setNotice('浏览器暂时无法保存恢复的输入，请允许此站点使用浏览器存储后重试。')
    }
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
      forgetBrowserTripReference(target.public_resource_id)
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
      <ExperienceHeader active="my-trips" />
      <section className="e-library" aria-labelledby="my-trips-heading">
        <div className="e-library-heading">
          <div>
            <h1 id="my-trips-heading" tabIndex={-1}>
              我的行程
            </h1>
            <p className="e-muted">正在整理和已保存的安排，都从这里继续。</p>
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
        {notice && <p className="e-message" role="status">{notice}</p>}
        {replacementDraft && <div className="e-message" role="alert">
          <p>首页还保留着另一份输入。是否用这次恢复的原文替换它？已保存的行程不会改变。</p>
          <div className="e-actions">
            <button className="e-button" onClick={() => setReplacementDraft(null)}>保留首页输入</button>
            <button className="e-button" onClick={() => useRecoveredDraft(replacementDraft)}>替换输入并恢复原文</button>
          </div>
        </div>}
        {pendingReference && <div className="e-message" role="status">
          <p>另一份行程还有一次修改等待确认，先确认结果再切换。</p>
          <Link className="e-button" href={`/trip/result#trip=${encodeURIComponent(pendingReference)}`}>返回并确认修改</Link>
        </div>}
        {(browserItems.length > 0 || browserLoading || browserError) && (
          <section className="e-browser-trips" aria-labelledby="browser-trips-heading">
            <h2 id="browser-trips-heading">当前浏览器的行程</h2>
            <p className="e-small e-muted">关闭页面后可继续。浏览器凭据到期或被清除后，未保存到账号的行程无法凭引用找回。</p>
            {browserLoading && <p role="status">正在读取本机记录…</p>}
            {browserError && <p role="alert">{browserError}</p>}
            {browserError && <button className="e-button" onClick={() => void loadBrowser()}>重新读取</button>}
            <ul className="e-trip-list" aria-label="当前浏览器行程">
              {browserItems.map((item) => <li className="e-trip-list-row" key={item.public_resource_id}>
                <div className="e-trip-list-main"><h3>{item.title}</h3><p className="e-trip-state" data-state={item.state}>{stateText[item.state || 'READY']}</p></div>
                <div className="e-trip-list-actions">
                  {item.state === 'PROCESSING' || item.has_result ? <button className="e-button" onClick={() => openTrip(item)}>
                    {item.state === 'PROCESSING' ? '查看进度' : item.state === 'PARTIAL' ? '查看部分结果' : '查看行程'}
                  </button> : <button className="e-button" disabled={recovering !== null} onClick={() => void recoverSource(item)}>
                    {recovering === item.public_resource_id ? '正在恢复…' : '恢复原文再整理'}
                  </button>}
                  <button className="e-button e-button-quiet" onClick={() => { forgetBrowserTripReference(item.public_resource_id); setBrowserItems((rows) => rows.filter((row) => row.public_resource_id !== item.public_resource_id)) }}>移除本机入口</button>
                </div>
              </li>)}
            </ul>
          </section>
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
            {loaded && !items.length && !cursor && !error && (
              <div className="e-library-empty">
                <h2>账号中还没有行程</h2>
                <p className="e-muted">
                  登录后整理的行程会自动出现在这里。未登录时创建的行程，需在结果页保存到账号。
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
                  按最近更新排序；到期记录不再显示。刷新可读取后台的最新状态。
                </p>
                <button className="e-button e-button-quiet e-library-refresh" disabled={loading} onClick={() => void load(null)}>刷新列表</button>
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
                          {item.city}
                          {item.is_demo && <span> · 固定示例</span>}
                        </p>
                        <p className="e-trip-state" data-state={item.state || 'READY'}>{stateText[item.state || 'READY']}</p>
                        <p className="e-small e-muted">
                          最近修改 {formatted(item.updated_at)}
                          <span className="e-trip-list-expiry">
                            {Date.parse(item.expires_at) > Date.now() ? `约 ${Math.max(1, Math.ceil((Date.parse(item.expires_at) - Date.now()) / 86400000))} 天后到期` : '保留期限待确认'}
                          </span>
                        </p>
                      </div>
                      <div className="e-trip-list-actions">
                        {(item.state === 'PROCESSING' || item.has_result !== false) ? <button
                          className="e-button"
                          onClick={() => openTrip(item)}
                          aria-label={`${item.state === 'PROCESSING' ? '查看进度' : item.state === 'PARTIAL' ? '查看部分结果' : '继续编辑'}${item.title}`}
                        >
                          {item.state === 'PROCESSING' ? '查看进度' : item.state === 'PARTIAL' ? '查看部分结果' : '继续编辑'}
                          <ArrowRight aria-hidden="true" />
                        </button> : item.source_status !== 'DELETED' && item.source_status !== 'UNAVAILABLE' ?
                          <button className="e-button" disabled={recovering !== null} onClick={() => void recoverSource(item)}>
                            {recovering === item.public_resource_id ? '正在恢复…' : '恢复原文再整理'}
                          </button> : <Link className="e-button" href="/">重新输入攻略</Link>}
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
