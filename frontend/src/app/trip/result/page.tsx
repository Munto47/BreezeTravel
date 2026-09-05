'use client'

import { useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import {
  ArrowUpRight,
  BusFront,
  Footprints,
  MoreHorizontal,
  Plus,
  RefreshCw,
  Undo2,
} from 'lucide-react'
import {
  createTripShare,
  deleteTripUnderstanding,
  deleteTripUnderstandingSource,
  clearTripUnderstandingSession,
  type ActivityCardView,
  type PlaceCandidateView,
  type PublicTripCheckItem,
  type MapRenderView,
} from '@/lib/trip-understanding-v3'
import { useAuthStore } from '@/stores/authStore'
import { useTripExperience, boundedTripRequest } from './use-trip-experience'
import RouteMap from './route-map'
import PlaceEditor from './place-editor'
import ContextPanel, { type ContextMode } from './context-panel'
import ChangePreviewPanel from './change-preview-panel'
import ChecksWorkspace from './checks-workspace'
import ItineraryPngExport from './itinerary-png-export'
import ItineraryWorkspace from './itinerary-workspace'
import MapStayWorkspace from './map-stay-workspace'
import ResultNavigation from './result-navigation'
import { type ResultViewId } from './result-presentation'
import {
  activityTime,
  findingLabel,
  formatExpiry,
  needsRecheck,
} from './presentation'
import '../../experience.css'

export default function TripResultPage() {
  const trip = useTripExperience()
  const router = useRouter()
  const { user, hydrate, logout } = useAuthStore()
  const [dayIndex, setDayIndex] = useState(0)
  const [selected, setSelected] = useState<string | null>(null)
  const [mobile, setMobile] = useState<'ITINERARY' | 'MAP'>('ITINERARY')
  const [activeView, setActiveView] = useState<ResultViewId>('ITINERARY')
  const [routeMode, setRouteMode] = useState<
    'recommended' | 'walking' | 'transit'
  >('recommended')
  const [context, setContext] = useState<ContextMode>({ kind: 'timeline' })
  const [editorDirty, setEditorDirty] = useState(false)
  const [discard, setDiscard] = useState(false)
  const discardDestination = useRef<string | null>(null)
  const [candidate, setCandidate] = useState<PlaceCandidateView | null>(null)
  const [narrow, setNarrow] = useState(false)
  const [assumptionValue, setAssumptionValue] = useState('')
  const [privacyBusy, setPrivacyBusy] = useState(false)
  const [privacyError, setPrivacyError] = useState('')
  const [share, setShare] = useState('')
  const [sharing, setSharing] = useState(false)
  const returnPosition = useRef<{
    scroll: number
    element: HTMLElement | null
  }>({ scroll: 0, element: null })
  const dayScroll = useRef(new Map<number, number>())
  const editorButtons = useRef(new Map<string, HTMLButtonElement>())
  const left = useRef<HTMLElement>(null)
  const result = trip.result
  const safeDayIndex = Math.min(
    dayIndex,
    Math.max(0, (result?.days.length || 1) - 1),
  )
  const currentDay = result?.days[safeDayIndex]
  const selectedCard =
    currentDay?.activities.find((card) => card.activity_token === selected) ||
    currentDay?.activities[0]
  const editorCard =
    context.kind === 'place'
      ? result?.days[context.dayIndex]?.activities.find(
          (card) => card.activity_token === context.activityToken,
        ) || null
      : null
  const effectiveMap: MapRenderView | null = result
    ? trip.map || {
        status: result.map.status,
        message: result.map.message,
        points: [],
        days: [],
        available_actions: result.map.available_actions,
      }
    : null
  const routeMutationPending =
    (trip.pending !== null &&
      ['command', 'adopt', 'stay'].includes(trip.pending.type)) ||
    (trip.pending === null &&
      ['WRITING', 'UNKNOWN'].includes(trip.writeStatus))
  const resultOutrunsDetailedMap =
    result?.map.status === 'NEEDS_UPDATE' &&
    effectiveMap !== null &&
    ['AVAILABLE', 'LIMITED'].includes(effectiveMap.status)
  const hideOldRoutes = routeMutationPending || resultOutrunsDetailedMap
  const displayMap: MapRenderView | null =
    effectiveMap && hideOldRoutes
      ? {
          ...effectiveMap,
          status: routeMutationPending
            ? 'NEEDS_UPDATE'
            : result?.map.status || 'NEEDS_UPDATE',
          message: routeMutationPending
            ? '行程调整尚在确认，旧路线已隐藏；确认后请手动更新。'
            : result?.map.message || '旧路线已失效，请按当前状态操作。',
          points: [],
          days: [],
          available_actions: result?.map.available_actions || [],
        }
      : effectiveMap
  const effectiveStay = trip.stay || result?.stay || null
  const stayMutationPending = Boolean(
    trip.pending && ['command', 'adopt'].includes(trip.pending.type),
  )
  const resultOutrunsDetailedStay = Boolean(
    result &&
      result.stay.status === 'NEEDS_UPDATE' &&
      effectiveStay &&
      ['AVAILABLE', 'LIMITED'].includes(effectiveStay.status),
  )
  const displayStay =
    effectiveStay && (stayMutationPending || resultOutrunsDetailedStay)
      ? {
          ...effectiveStay,
          status: stayMutationPending
            ? ('NEEDS_UPDATE' as const)
            : result?.stay.status || ('NEEDS_UPDATE' as const),
          message: stayMutationPending
            ? '行程已调整，住宿建议需要重新确认。'
            : result?.stay.message || '住宿建议需要重新确认。',
          area_summary: null,
          candidates: [],
          available_actions: [],
        }
      : effectiveStay
  const assumption =
    context.kind === 'assumption'
      ? result?.assumptions.find((item) => item.key === context.key)
      : null
  const title =
    result?.assumptions.find((item) => item.key === 'destination')?.value ||
    '我的行程'
  const accountSaved = result?.ownership === 'ACCOUNT'
  const disabled = trip.locked || privacyBusy
  const sourceDeleted = trip.supplementary?.status === 'DELETED'
  const dayFindings =
    trip.checks?.items.filter(
      (item) =>
        !item.affected_days.length ||
        item.affected_days.includes(currentDay?.label || ''),
    ) || []
  const otherFindings =
    trip.checks?.items.filter(
      (item) =>
        item.affected_days.length &&
        !item.affected_days.includes(currentDay?.label || ''),
    ) || []
  const dirty =
    editorDirty || Boolean(assumption && assumption.value !== assumptionValue)
  const expiry = formatExpiry(result?.expires_at)
  const persistence =
    trip.writeStatus === 'WRITING'
      ? '正在保留修改…'
      : trip.writeStatus === 'UNKNOWN'
        ? '修改结果待确认'
        : trip.writeStatus === 'FAILED'
          ? '上次修改未保存'
          : accountSaved
            ? '已保存到账号'
            : '草稿已保留'
  const progressTitle =
    trip.phase === 'CHECKING_PLACES'
      ? '正在核对地点'
      : trip.phase === 'CARDS_AVAILABLE'
        ? '行程骨架已经整理好'
        : '正在读懂这份攻略'
  const progressPercent =
    trip.progress.places_total > 0
      ? Math.round(
          (trip.progress.places_checked / trip.progress.places_total) * 100,
        )
      : trip.phase === 'RECEIVED'
        ? 12
        : 34
  const contextOpen = context.kind !== 'timeline'

  useEffect(() => {
    hydrate()
    const query = window.matchMedia('(max-width: 1023px)')
    const update = () => setNarrow(query.matches)
    update()
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [hydrate])
  useEffect(() => {
    setContext({ kind: 'timeline' })
    setActiveView('ITINERARY')
    setDayIndex(0)
    setSelected(null)
    setCandidate(null)
    setEditorDirty(false)
    setDiscard(false)
    dayScroll.current.clear()
  }, [trip.resource])
  useEffect(() => {
    if (!dirty) return
    const protect = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', protect)
    return () => window.removeEventListener('beforeunload', protect)
  }, [dirty])

  function openContext(next: ContextMode) {
    if (dirty || trip.busy) return
    if (context.kind === 'timeline')
      returnPosition.current = {
        scroll: window.scrollY,
        element:
          document.activeElement instanceof HTMLElement
            ? document.activeElement
            : null,
      }
    setContext(next)
    setDiscard(false)
    if (!narrow)
      requestAnimationFrame(() =>
        left.current?.scrollIntoView({ block: 'start', behavior: 'instant' }),
      )
  }
  function closeContext(force = false) {
    if (!force && dirty) {
      setDiscard(true)
      return
    }
    const closingPlace = context.kind === 'place'
    const closingDayIndex =
      closingPlace ? context.dayIndex + 1 : safeDayIndex + 1
    const token = closingPlace ? context.activityToken : selected
    setContext({ kind: 'timeline' })
    setCandidate(null)
    setEditorDirty(false)
    setDiscard(false)
    trip.closePreview()
    if (force && discardDestination.current) {
      const destination = discardDestination.current
      discardDestination.current = null
      router.push(destination)
      return
    }
    requestAnimationFrame(() => {
      const preferred = token
        ? editorButtons.current.get(token)
        : closingPlace
          ? document.querySelector<HTMLElement>(
              `[data-day-add="${closingDayIndex}"]`,
            ) || returnPosition.current.element
          : returnPosition.current.element
      if (preferred?.isConnected) preferred.focus({ preventScroll: true })
      else
        document
          .querySelector<HTMLElement>(`[data-day-heading="${closingDayIndex}"]`)
          ?.focus({ preventScroll: true })
      window.scrollTo({
        top: returnPosition.current.scroll,
        behavior: 'instant',
      })
    })
  }
  function changeDay(index: number) {
    if (context.kind !== 'timeline') return
    dayScroll.current.set(safeDayIndex, window.scrollY)
    setDayIndex(index)
    setSelected(null)
    requestAnimationFrame(() =>
      window.scrollTo({
        top: dayScroll.current.get(index) ?? left.current?.offsetTop ?? 0,
        behavior: 'instant',
      }),
    )
  }
  function changeResultView(next: ResultViewId) {
    if (next === activeView) return
    if (context.kind !== 'timeline') {
      if (dirty) {
        setDiscard(true)
        return
      }
      closeContext()
    }
    setActiveView(next)
  }
  function editCard(
    card: ActivityCardView,
    index = safeDayIndex,
    editorMode: 'EDIT' | 'REPLACE' = 'EDIT',
  ) {
    setSelected(card.activity_token)
    setMobile('ITINERARY')
    openContext({
      kind: 'place',
      activityToken: card.activity_token,
      dayIndex: index,
      editorMode,
    })
  }
  function login(claim = false) {
    sessionStorage.setItem(
      'bt_login_return',
      window.location.pathname + window.location.hash,
    )
    if (claim) sessionStorage.setItem('bt_claim_after_login', 'true')
    if (claim && user && trip.unavailable === 'LOGIN') logout()
    if (user && !claim) {
      logout()
      return
    }
    router.push('/login')
  }
  async function saveToAccount() {
    if (!user) {
      login(true)
      return
    }
    if (!accountSaved) await trip.claim()
  }
  useEffect(() => {
    if (
      user &&
      result &&
      !trip.busy &&
      !trip.pending &&
      !accountSaved &&
      sessionStorage.getItem('bt_claim_after_login') === 'true'
    ) {
      sessionStorage.removeItem('bt_claim_after_login')
      void trip.claim()
    }
  }, [user, result, trip, accountSaved])
  async function confirmDelete() {
    if (disabled || context.kind !== 'privacy') return
    setPrivacyBusy(true)
    setPrivacyError('')
    try {
      if (context.target === 'SOURCE') {
        await boundedTripRequest((signal) =>
          deleteTripUnderstandingSource(trip.resource, signal),
        )
        trip.markSourceDeleted()
        closeContext(true)
        trip.setNotice('导入文字已删除，现有行程与已确认地点仍保留。')
      } else {
        await boundedTripRequest((signal) =>
          deleteTripUnderstanding(trip.resource, signal),
        )
        clearTripUnderstandingSession()
        sessionStorage.removeItem('bt_pending_operation')
        router.replace('/')
      }
    } catch {
      setPrivacyError('尚未确认删除结果，请稍后重试。')
    } finally {
      setPrivacyBusy(false)
    }
  }
  async function shareTrip() {
    if (!accountSaved || sharing) return
    openContext({ kind: 'share' })
    setSharing(true)
    setShare('')
    try {
      const created = await boundedTripRequest(() =>
        createTripShare(trip.resource),
      )
      setShare(new URL(created.share_url, window.location.origin).toString())
    } catch {
      trip.setNotice('暂时未能创建分享链接，请稍后重试。')
    } finally {
      setSharing(false)
    }
  }
  function openPreview(item: PublicTripCheckItem) {
    openContext({ kind: 'preview' })
    void trip.openPreview(item.check_token)
  }
  function locateFinding(item: PublicTripCheckItem) {
    const index =
      result?.days.findIndex((day) =>
        day.activities.some((card) =>
          item.affected_activity_tokens?.includes(card.activity_token),
        ),
      ) ?? -1
    if (index < 0 || !result) return
    const card = result.days[index].activities.find((value) =>
      item.affected_activity_tokens?.includes(value.activity_token),
    )!
    setDayIndex(index)
    setSelected(card.activity_token)
    setContext({
      kind: 'place',
      activityToken: card.activity_token,
      dayIndex: index,
      editorMode: 'EDIT',
    })
  }
  const issue = (item: PublicTripCheckItem) => {
    const stale = needsRecheck(item, displayMap)
    const hard = !stale && item.label === '必须调整'
    return (
      <article
        key={item.check_token}
        className={`e-issue${hard ? ' is-hard' : ''}`}
      >
        <p className="e-issue-heading">
          <span>{findingLabel(item, displayMap)}</span>
          <strong>{stale ? '交通待更新，需复检' : item.title}</strong>
        </p>
        <p>{stale ? '先更新路线，再确认这项安排是否冲突。' : item.message}</p>
        <div className="e-actions">
          {item.can_preview && !stale && (
            <button
              className="e-text-button"
              disabled={disabled || trip.checking}
              onClick={() => openPreview(item)}
            >
              预览调整
            </button>
          )}
          {!!item.affected_activity_tokens?.length && (
            <button
              className="e-text-button"
              disabled={disabled}
              onClick={() => locateFinding(item)}
            >
              手动调整
            </button>
          )}
        </div>
      </article>
    )
  }
  const contextTitle =
    context.kind === 'place'
      ? context.editorMode === 'ADD'
        ? '新增地点'
        : context.editorMode === 'REPLACE'
          ? '替换地点'
          : '编辑卡片文字'
      : context.kind === 'preview'
        ? trip.preview?.title || '调整预览'
        : context.kind === 'issues'
          ? context.allDays
            ? '其他日期的问题'
            : '当天需要留意'
          : context.kind === 'assumption'
              ? `修改${assumption?.label || '行程信息'}`
              : context.kind === 'privacy'
                ? context.target === 'SOURCE'
                  ? '删除攻略原文？'
                  : '永久删除整份行程？'
                : '分享这份行程'
  const summary = [
    dayFindings.filter((item) => findingLabel(item, displayMap) === '必须调整')
      .length
      ? `${dayFindings.filter((item) => findingLabel(item, displayMap) === '必须调整').length} 处冲突`
      : '',
    dayFindings.filter((item) =>
      ['需要确认', '待复检'].includes(findingLabel(item, displayMap)),
    ).length
      ? `${dayFindings.filter((item) => ['需要确认', '待复检'].includes(findingLabel(item, displayMap))).length} 处待确认`
      : '',
    dayFindings.filter((item) => findingLabel(item, displayMap) === '可以更好')
      .length
      ? `${dayFindings.filter((item) => findingLabel(item, displayMap) === '可以更好').length} 条建议`
      : '',
  ]
    .filter(Boolean)
    .join(' · ')
  const mapView = (
    <RouteMap
      key={trip.resource}
      view={displayMap}
      day={currentDay}
      selected={selectedCard?.activity_token || null}
      onSelect={setSelected}
      mode={routeMode}
      visible={!narrow || mobile === 'MAP'}
      focusSelected={Boolean(selected) && !candidate}
      previewCandidate={candidate}
    />
  )

  return (
    <main
      className="experience e-result-page"
      onClickCapture={(event) => {
        if (!dirty || !(event.target instanceof Element)) return
        const anchor = event.target.closest(
          'a[href]',
        ) as HTMLAnchorElement | null
        if (!anchor || anchor.target === '_blank') return
        event.preventDefault()
        event.stopPropagation()
        discardDestination.current =
          anchor.pathname + anchor.search + anchor.hash
        setDiscard(true)
      }}
    >
      <header className="e-header">
        <Link href="/" className="e-brand">
          行程查<span>TRIPCHECK</span>
        </Link>
        <nav className="e-actions" aria-label="全局导航">
          <Link href="/" className="e-button e-button-quiet" aria-current="page">
            行程查
          </Link>
          <Link href="/collaborate" className="e-button e-button-quiet">
            协同规划
          </Link>
          {user && (
            <Link href="/my-trips" className="e-button e-button-quiet">
              我的行程
            </Link>
          )}
          <Link
            className="e-button e-button-quiet"
            href={user ? '/profile' : '/'}
          >
            {user ? '账号' : '首页'}
          </Link>
        </nav>
      </header>
      {!result ? (
        trip.progressSnapshot ? (
          <section className="e-progress-workspace" aria-busy={trip.loading}>
            <div className="e-progress-heading">
              <div>
                <p className="e-eyebrow">临时预览 · 只读</p>
                <h1>{progressTitle}</h1>
                <p role="status">{trip.message}</p>
              </div>
              <div className="e-progress-actions">
                <span>{Math.max(0, Math.min(100, progressPercent))}%</span>
                <button
                  type="button"
                  className="e-button"
                  disabled={trip.cancelling}
                  onClick={() => void trip.stopUnderstanding()}
                >
                  {trip.cancelling ? '正在停止…' : '停止整理并编辑'}
                </button>
              </div>
            </div>
            <div
              className="e-progress-track"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.max(0, Math.min(100, progressPercent))}
            >
              <span style={{ width: `${progressPercent}%` }} />
            </div>
            <p className="e-progress-note">
              地点仍在核对，临时卡片不能修改；停止后会全部标为“待确认”，再由你决定保留或替换。
            </p>
            <div className="e-progress-days">
              {trip.progressSnapshot.days.map((day) => (
                <section className="e-progress-day" key={day.label}>
                  <header>
                    <strong>{day.label}</strong>
                    <span>{day.activities.length} 个地点</span>
                  </header>
                  <div className="e-progress-chain" role="list">
                    {day.activities.map((card, index) => (
                      <div className="e-progress-card-wrap" key={card.activity_token}>
                        {index > 0 && (
                          <span className="e-progress-connector" aria-hidden="true">
                            继续
                          </span>
                        )}
                        <article className="e-progress-card" role="listitem">
                          <span>{card.time_hint || '时间待整理'}</span>
                          <strong>{card.name}</strong>
                          <small>待确认</small>
                        </article>
                      </div>
                    ))}
                  </div>
                </section>
              ))}
            </div>
          </section>
        ) : (
          <section className="e-loading">
            <h1>
              {trip.loading
                ? progressTitle
                : trip.unavailable === 'GONE'
                  ? '这份行程已无法打开'
                  : trip.unavailable === 'FAILED'
                    ? '这次没有整理完成'
                    : trip.unavailable === 'CANCELLED'
                      ? '整理已经停止'
                      : '行程暂时还没打开'}
            </h1>
            <p role="status">{trip.message}</p>
            {trip.unavailable === 'LOGIN' ? (
              <button
                type="button"
                className="e-button e-button-primary"
                onClick={() => login(trip.pending?.type === 'claim')}
              >
                {trip.pending?.type === 'claim'
                  ? '重新登录并恢复保存'
                  : '登录后继续'}
              </button>
            ) : trip.pending ? (
              <button
                type="button"
                className="e-button"
                data-testid="retry-result-readback"
                disabled={trip.busy}
                onClick={() => void trip.reconcile()}
              >
                {trip.pending.type === 'claim'
                  ? '确认账号保存结果'
                  : '确认上次操作结果'}
              </button>
            ) : trip.loading ? (
              <>
                <div className="e-progress" aria-hidden="true" />
                <button
                  type="button"
                  className="e-button"
                  disabled={trip.cancelling}
                  onClick={() => void trip.stopUnderstanding()}
                >
                  {trip.cancelling ? '正在停止…' : '停止整理'}
                </button>
              </>
            ) : (
              <div className="e-actions">
                {trip.unavailable === 'NOT_AVAILABLE' && !user ? (
                  <button
                    className="e-button e-button-primary"
                    onClick={() => login()}
                  >
                    登录后继续
                  </button>
                ) : (
                  !['GONE', 'FAILED', 'CANCELLED'].includes(
                    trip.unavailable,
                  ) && (
                    <button className="e-button" onClick={trip.retry}>
                      继续等待
                    </button>
                  )
                )}
                {user && (
                  <Link className="e-button" href="/my-trips">
                    我的行程
                  </Link>
                )}
                {trip.unavailable === 'NOT_AVAILABLE' && user && (
                  <button
                    type="button"
                    onClick={() => login()}
                    className="e-button"
                  >
                    切换账号后重试
                  </button>
                )}
                <Link
                  className={
                    ['FAILED', 'CANCELLED'].includes(trip.unavailable)
                      ? 'e-button e-button-primary'
                      : 'e-button'
                  }
                  href="/"
                >
                  {['FAILED', 'CANCELLED'].includes(trip.unavailable)
                    ? '返回首页'
                    : '重新整理'}
                </Link>
              </div>
            )}
          </section>
        )
      ) : (
        <>
          <section className="e-trip-head">
            <div className="e-trip-title">
              <div className="e-title-line">
                <h1>
                  {title} · {result.days.length} 天
                </h1>
                <details className="e-trip-information">
                  <summary
                    aria-disabled={disabled || context.kind !== 'timeline'}
                    onClick={(event) => {
                      if (disabled || context.kind !== 'timeline')
                        event.preventDefault()
                    }}
                  >
                    行程信息
                  </summary>
                  <div className="e-assumptions">
                    {result.assumptions.map((item) => (
                      <button
                        type="button"
                        key={item.key}
                        data-testid={`edit-assumption-${item.key}`}
                        disabled={
                          disabled ||
                          !item.editable ||
                          context.kind !== 'timeline'
                        }
                        aria-label={`修改${item.label}`}
                        onClick={() => {
                          setAssumptionValue(item.value)
                          openContext({ kind: 'assumption', key: item.key })
                        }}
                      >
                        {item.label} · {item.value}
                        <ArrowUpRight aria-hidden="true" />
                      </button>
                    ))}
                  </div>
                </details>
              </div>
            </div>
            <div className="e-save-area">
              <p className="e-save-status" role="status">
                {persistence}
                {expiry && <span>保留至 {expiry}</span>}
              </p>
              <div className="e-actions">
                <button
                  type="button"
                  className="e-button e-button-quiet"
                  disabled={disabled || !result.can_undo || dirty}
                  onClick={() => void trip.command({ command_type: 'UNDO' })}
                >
                  <Undo2 aria-hidden="true" />
                  撤销
                </button>
                <button
                  type="button"
                  className="e-button e-button-primary"
                  disabled={disabled || accountSaved || dirty}
                  onClick={() => void saveToAccount()}
                >
                  {accountSaved ? '已保存到账号' : '保存到账号'}
                </button>
                <details className="e-more">
                  <summary aria-label="更多行程操作">
                    <MoreHorizontal aria-hidden="true" />
                  </summary>
                  <div>
                    {[
                      ...(accountSaved
                        ? [
                            {
                              label: '分享行程',
                              testId: 'share-trip',
                              action: () => void shareTrip(),
                            },
                          ]
                        : []),
                      {
                        label: sourceDeleted
                          ? '原文已删除'
                          : '删除导入文字',
                        testId: 'delete-trip-source',
                        action: () => {
                          setPrivacyError('')
                          openContext({ kind: 'privacy', target: 'SOURCE' })
                        },
                      },
                      {
                        label: '删除行程',
                        testId: 'delete-entire-trip',
                        action: () => {
                          setPrivacyError('')
                          openContext({ kind: 'privacy', target: 'TRIP' })
                        },
                      },
                    ].map((item) => (
                      <button
                        key={item.label}
                        type="button"
                        data-testid={item.testId}
                        disabled={
                          disabled || dirty || item.label === '原文已删除'
                        }
                        onClick={(event) => {
                          const details = event.currentTarget.closest('details')
                          const summary = details?.querySelector<HTMLElement>('summary')
                          details?.removeAttribute('open')
                          summary?.focus({ preventScroll: true })
                          item.action()
                        }}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>
                </details>
              </div>
            </div>
          </section>
          {trip.isDemo && (
            <div className="e-demo-note">
              示例行程 · 安排与路线为固定回放；地图底图联网加载。
              <Link href="/">换成自己的攻略</Link>
            </div>
          )}
          <div className="e-page-message">
            {result.status !== 'READY' && (
              <p className="e-message" role="status">
                {result.status === 'BASIC_ONLY'
                  ? '已整理基础行程，部分地点与路线尚未核对。'
                  : '部分内容仍需要确认，已有安排可以继续查看和修改。'}
              </p>
            )}
            {trip.notice && (
              <div
                className="e-message"
                role="status"
                data-testid={
                  trip.pending ? 'result-operation-status' : undefined
                }
              >
                {trip.notice}
                {trip.pending && (
                  <button
                    type="button"
                    className="e-text-button"
                    data-testid="retry-result-readback"
                    autoFocus
                    disabled={trip.busy}
                    onClick={() => void trip.reconcile()}
                  >
                    {trip.pending.type === 'map'
                      ? '确认路线更新请求'
                      : '确认保存结果'}
                  </button>
                )}
              </div>
            )}
          </div>
          <ResultNavigation activeView={activeView} onChange={changeResultView} />
          {!contextOpen && displayMap && (
            <div className="e-horizontal-result-shell">
              <div data-testid="result-view-itinerary" hidden={activeView !== 'ITINERARY'}>
                <section
                  id="itinerary-view"
                  aria-label="行程横链"
                  className="mx-auto max-w-[1600px] px-4 pb-28 pt-5 lg:px-8 lg:pb-12 lg:pl-24"
                >
                  <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-sky-900/10 bg-white/75 p-4 backdrop-blur">
                    <div>
                      <p className="text-xs font-semibold tracking-[0.14em] text-[#0c789d]">横链行程画布</p>
                      <p className="mt-1 text-sm text-slate-600">一天一行，共享浏览节奏；卡片调整后路线只会标记为需要更新。</p>
                    </div>
                    <ItineraryPngExport
                      result={result}
                      mapView={displayMap}
                      etag={trip.etag}
                      disabled={disabled || dirty}
                    />
                  </div>
                  <ItineraryWorkspace
                    days={result.days}
                    disabled={disabled || dirty}
                    routesPending={hideOldRoutes}
                    mapView={displayMap}
                    checkStatus={
                      trip.checking
                        ? '正在检查'
                        : trip.checks?.message || trip.checksError || '待检查'
                    }
                    onCommand={trip.workspaceCommand}
                    onAdd={(targetDayIndex) =>
                      openContext({
                        kind: 'place',
                        activityToken: null,
                        dayIndex: targetDayIndex - 1,
                        editorMode: 'ADD',
                      })
                    }
                    onEdit={(item) =>
                      editCard(item.card, item.dayIndex - 1, 'EDIT')
                    }
                    onReplace={(item) =>
                      editCard(item.card, item.dayIndex - 1, 'REPLACE')
                    }
                  />
                </section>
              </div>
              <div data-testid="result-view-map-stay" hidden={activeView !== 'MAP_STAY'}>
                <MapStayWorkspace
                  active={activeView === 'MAP_STAY'}
                  result={result}
                  mapView={displayMap}
                  stay={displayStay}
                  dayIndex={safeDayIndex}
                  selected={selectedCard?.activity_token || null}
                  routeMode={routeMode}
                  disabled={disabled || dirty}
                  onDayChange={changeDay}
                  onSelect={setSelected}
                  onRouteMode={setRouteMode}
                  onRender={() => void trip.renderMap()}
                  onRetryMap={() => void trip.retryMap()}
                  onSelectStay={(token) => void trip.selectStay(token)}
                  onEdit={(card) => editCard(card, safeDayIndex, 'EDIT')}
                />
              </div>
              <div data-testid="result-view-checks" hidden={activeView !== 'CHECKS'}>
                <ChecksWorkspace
                  checks={trip.checks}
                  mapView={displayMap}
                  checking={trip.checking}
                  error={trip.checksError}
                  disabled={disabled || dirty}
                  onRetry={() => void trip.retryChecks()}
                  onPreview={openPreview}
                  onLocate={locateFinding}
                />
              </div>
            </div>
          )}
          {contextOpen && (
          <div className="e-workspace" data-testid="context-workspace">
            <section
              ref={left}
              className="e-itinerary"
              aria-label="当天行程"
            >
              <nav className="e-days e-day-nav" aria-label="选择行程日期">
                {result.days.map((day, index) => (
                  <button
                    key={index}
                    type="button"
                    disabled
                    aria-pressed={index === safeDayIndex}
                    onClick={() => changeDay(index)}
                  >
                    {day.label}
                  </button>
                ))}
              </nav>
              <nav className="e-mobile-switch" aria-label="行程和地图">
                <button
                  type="button"
                  aria-pressed={mobile === 'ITINERARY'}
                  onClick={() => setMobile('ITINERARY')}
                >
                  行程
                </button>
                <button
                  type="button"
                  aria-pressed={mobile === 'MAP'}
                  onClick={() => setMobile('MAP')}
                >
                  地图
                </button>
              </nav>
              {String(context.kind) === 'timeline' ? (
                <>
                  <div className="e-section-heading">
                    <h2>{currentDay?.label}</h2>
                    <span className="e-muted">
                      {currentDay?.activities.length || 0} 个地点
                    </span>
                  </div>
                  <div className="e-issue-summary">
                    {summary ? (
                      <button
                        type="button"
                        className="e-text-button"
                        onClick={() =>
                          openContext({ kind: 'issues', allDays: false })
                        }
                      >
                        {summary}
                      </button>
                    ) : (
                      <span className="e-muted">
                        {trip.checking
                          ? '正在检查时间与路线…'
                          : trip.checksError ||
                            trip.checks?.message ||
                            '检查结果正在准备。'}
                      </span>
                    )}
                    {!!otherFindings.length && (
                      <button
                        type="button"
                        className="e-text-button"
                        onClick={() =>
                          openContext({ kind: 'issues', allDays: true })
                        }
                      >
                        其他日期 {otherFindings.length} 处
                      </button>
                    )}
                  </div>
                  <ol className="e-stops" data-testid="trip-days">
                    {currentDay?.activities.map((card, index) => {
                      const next = currentDay.activities[index + 1]
                      const route = displayMap?.days
                        .find((day) => day.label === currentDay.label)
                        ?.routes.find(
                          (value) =>
                            value.from_activity_token === card.activity_token &&
                            value.to_activity_token === next?.activity_token,
                        )
                      const currentRoute =
                        displayMap?.status === 'AVAILABLE' ||
                        displayMap?.status === 'LIMITED'
                      const chosen = route?.selected_mode
                        ? route[route.selected_mode]
                        : null
                      const finding = dayFindings.find(
                        (item) =>
                          item.affected_activity_tokens?.[0] ===
                          card.activity_token,
                      )
                      return (
                        <li
                          key={card.activity_token}
                          className={`e-stop-row${selectedCard?.activity_token === card.activity_token ? ' is-selected' : ''}`}
                        >
                          <div className="e-stop-main">
                            <button
                              type="button"
                              className="e-stop-select"
                              aria-pressed={
                                selectedCard?.activity_token ===
                                card.activity_token
                              }
                              onClick={() => setSelected(card.activity_token)}
                            >
                              <span className="e-stop-time">
                                {activityTime(card)}
                              </span>
                              <span className="e-stop-content">
                                <span className="e-stop-name">
                                  <span className="e-stop-number">
                                    {index + 1}
                                  </span>
                                  <strong>{card.name}</strong>
                                  {(card.locked || card.fixed_commitment) && (
                                    <span className="e-locked">
                                      {card.fixed_commitment
                                        ? '固定安排'
                                        : '已锁定'}
                                    </span>
                                  )}
                                </span>
                                <span className="e-stop-sub">
                                  {card.visit_duration_minutes != null
                                    ? `停留 ${card.visit_duration_minutes} 分钟`
                                    : '停留时间待定'}
                                  {card.area_or_address
                                    ? ` · ${card.area_or_address}`
                                    : card.status !== 'READY'
                                      ? ' · 地点待确认'
                                      : ''}
                                </span>
                              </span>
                            </button>
                            <button
                              ref={(node) => {
                                if (node)
                                  editorButtons.current.set(
                                    card.activity_token,
                                    node,
                                  )
                                else
                                  editorButtons.current.delete(
                                    card.activity_token,
                                  )
                              }}
                              type="button"
                              className="e-text-button e-edit-stop"
                              aria-label={`编辑${card.name}`}
                              disabled={disabled}
                              onClick={() => editCard(card)}
                            >
                              编辑
                            </button>
                          </div>
                          {finding && (
                            <div
                              className={`e-inline-issue${findingLabel(finding, displayMap) === '必须调整' ? ' is-hard' : ''}`}
                            >
                              <span>
                                {needsRecheck(finding, displayMap)
                                  ? '交通待更新，需复检'
                                  : finding.title}
                              </span>
                              <button
                                type="button"
                                className="e-text-button"
                                disabled={disabled || trip.checking}
                                onClick={() =>
                                  finding.can_preview &&
                                  !needsRecheck(finding, displayMap)
                                    ? openPreview(finding)
                                    : openContext({
                                        kind: 'issues',
                                        allDays: false,
                                      })
                                }
                              >
                                {finding.can_preview &&
                                !needsRecheck(finding, displayMap)
                                  ? '预览调整'
                                  : '查看问题'}
                              </button>
                            </div>
                          )}
                          {next && (
                            <details className="e-transport">
                              <summary>
                                {route?.selected_mode === 'transit' ? (
                                  <BusFront aria-hidden="true" />
                                ) : (
                                  <Footprints aria-hidden="true" />
                                )}
                                <span>
                                  {!currentRoute
                                    ? displayMap?.status === 'NEEDS_UPDATE'
                                      ? '交通待更新，需复检'
                                      : '到下一站的交通待确认'
                                    : chosen?.status === 'AVAILABLE' &&
                                        chosen.duration_minutes != null
                                      ? `${route?.selected_mode === 'transit' ? '公交' : '步行'} · 约 ${chosen.duration_minutes} 分钟`
                                      : '到下一站的交通待确认'}
                                </span>
                                <span className="e-transport-expand">比较</span>
                              </summary>
                              <div>
                                {currentRoute && route ? (
                                  <>
                                    <p className="e-muted">
                                      {card.name} → {next.name}
                                    </p>
                                    {(['walking', 'transit'] as const).map(
                                      (mode) => {
                                        const data = route[mode]
                                        return (
                                          <p key={mode}>
                                            <strong>
                                              {mode === 'walking'
                                                ? '步行'
                                                : '公交'}
                                            </strong>
                                            {data.status === 'AVAILABLE'
                                              ? ` · ${data.duration_minutes == null ? '时长未提供' : `约 ${data.duration_minutes} 分钟`}${data.distance_meters == null ? '' : ` · 总距离 ${(data.distance_meters / 1000).toFixed(1)} 公里`}${mode === 'transit' && data.transfer_count != null ? ` · 换乘 ${data.transfer_count} 次` : ''}`
                                              : ' · 暂不可用'}
                                          </p>
                                        )
                                      },
                                    )}
                                    <p className="e-small e-muted">
                                      {route.message ||
                                        '当前没有更详细的分段步骤。'}
                                    </p>
                                  </>
                                ) : (
                                  <p className="e-muted">
                                    {displayMap?.status === 'NEEDS_UPDATE'
                                      ? '旧路线已隐藏。更新路线后再比较交通方式。'
                                      : '路线数据尚不完整，暂不能提供可靠的交通比较。'}
                                  </p>
                                )}
                              </div>
                            </details>
                          )}
                        </li>
                      )
                    })}
                  </ol>
                  <button
                    className="e-add"
                    type="button"
                    disabled={disabled}
                    onClick={() =>
                      openContext({
                        kind: 'place',
                        activityToken: null,
                        dayIndex: safeDayIndex,
                        editorMode: 'ADD',
                      })
                    }
                  >
                    <Plus aria-hidden="true" />
                    添加想去的地方
                  </button>
                  <div className="e-check-foot">
                    <p className="e-muted" role="status">
                      {trip.checking
                        ? '正在检查时间与路线…'
                        : trip.checksError ||
                          trip.checks?.message ||
                          '检查结果正在准备。'}
                    </p>
                    <button
                      type="button"
                      className="e-text-button"
                      disabled={disabled || trip.checking}
                      onClick={() => void trip.retryChecks()}
                    >
                      重新检查
                    </button>
                  </div>
                  {trip.supplementary?.status === 'AVAILABLE' &&
                    trip.supplementary.days
                      .filter(
                        (day) =>
                          day.day_index === safeDayIndex + 1 ||
                          day.day_index == null,
                      )
                      .map((day, group) =>
                        (['OPTIONAL', 'EXCLUDED'] as const).map((role) => {
                          const items = day.items.filter(
                            (item) => item.role === role,
                          )
                          return items.length ? (
                            <details
                              className="e-disclosure"
                              key={`${group}-${role}`}
                            >
                              <summary>
                                {day.day_index == null ? '未指定日期 · ' : ''}
                                {role === 'OPTIONAL'
                                  ? '备选地点'
                                  : '已取消的安排'}{' '}
                                · {items.length}
                              </summary>
                              <p className="e-small e-muted">
                                {role === 'OPTIONAL'
                                  ? '这些地点尚未排入执行行程。'
                                  : '原文已排除的安排，不计入路线。'}
                              </p>
                              <ul className="e-secondary-items">
                                {items.map((item, index) => (
                                  <li key={index}>
                                    <strong>{item.name}</strong>
                                    {item.time_hint && (
                                      <span>{item.time_hint}</span>
                                    )}
                                  </li>
                                ))}
                              </ul>
                            </details>
                          ) : null
                        }),
                      )}
                  <details className="e-disclosure">
                    <summary>住宿与出发前建议</summary>
                    <p className="e-muted">
                      {displayStay?.message || result.stay.message}
                    </p>
                    <div className="e-stay-list">
                      {(displayStay?.candidates || []).map(
                        (stay) => (
                          <article key={stay.candidate_token}>
                            <h3>{stay.name}</h3>
                            <p>{stay.area_or_address}</p>
                            <p>{stay.commute_summary}</p>
                            <p>{stay.reason}</p>
                            <button
                              className="e-button"
                              disabled={disabled || stay.selected}
                              onClick={() =>
                                void trip.selectStay(stay.candidate_token)
                              }
                            >
                              {stay.selected ? '已选择' : '选择这家住宿'}
                            </button>
                          </article>
                        ),
                      )}
                    </div>
                  </details>
                </>
              ) : (
                <ContextPanel
                  title={contextTitle}
                  dayLabel={currentDay?.label || '行程'}
                  busy={trip.busy || privacyBusy}
                  modal={
                    context.kind === 'place' ||
                    context.kind === 'privacy' ||
                    context.kind === 'share' ||
                    context.kind === 'assumption'
                  }
                  closeLabel={
                    context.kind === 'place'
                      ? '关闭编辑'
                      : context.kind === 'privacy'
                      ? '关闭删除确认'
                      : context.kind === 'preview'
                        ? '关闭改动预览'
                        : undefined
                  }
                  onClose={() => closeContext()}
                >
                  {discard && (
                    <div className="e-inline-confirm" role="alert">
                      <p>有尚未应用的修改，要放弃并返回吗？</p>
                      <div className="e-actions">
                        <button
                          className="e-button"
                          onClick={() => {
                            setDiscard(false)
                            discardDestination.current = null
                          }}
                        >
                          继续编辑
                        </button>
                        <button
                          className="e-button"
                          onClick={() => closeContext(true)}
                        >
                          放弃修改并返回
                        </button>
                      </div>
                    </div>
                  )}
                  {context.kind === 'place' && (
                    <PlaceEditor
                      key={`${context.editorMode}:${context.activityToken || 'new'}`}
                      editorMode={context.editorMode}
                      card={editorCard}
                      dayIndex={context.dayIndex}
                      days={result.days}
                      resource={trip.resource}
                      busy={disabled}
                      notice={trip.notice}
                      onCommand={trip.command}
                      onApplied={() => closeContext(true)}
                      onDirtyChange={setEditorDirty}
                      onPreviewCandidate={setCandidate}
                      candidateMap={
                        narrow && candidate ? (
                          <div className="e-mobile-candidate-map">
                            <RouteMap
                              view={displayMap}
                              day={currentDay}
                              selected={selected}
                              onSelect={setSelected}
                              mode={routeMode}
                              visible
                              focusSelected={false}
                              previewCandidate={candidate}
                            />
                          </div>
                        ) : null
                      }
                    />
                  )}
                  {context.kind === 'preview' && (
                    <ChangePreviewPanel
                      preview={trip.preview}
                      loading={trip.previewLoading}
                      stale={trip.previewStale}
                      busy={disabled}
                      checking={trip.checking}
                      result={result}
                      notice={trip.notice}
                      onRetry={() => void trip.refreshPreview()}
                      onAdopt={() => {
                        void trip.adopt().then((ok) => {
                          if (ok) closeContext(true)
                        })
                      }}
                      onCancel={() => closeContext()}
                    />
                  )}
                  {context.kind === 'issues' && (
                    <div className="e-issues">
                      {(context.allDays ? otherFindings : dayFindings).map(
                        (item) => (
                          <div key={item.check_token}>
                            {context.allDays && (
                              <p className="e-small e-muted">
                                {item.affected_days.join('、')}
                              </p>
                            )}
                            {issue(item)}
                          </div>
                        ),
                      )}
                    </div>
                  )}
                  {assumption && (
                    <form
                      onSubmit={(event) => {
                        event.preventDefault()
                        if (assumptionValue.trim() !== assumption.value)
                          void trip
                            .command({
                              command_type: 'ASSUMPTION_SET',
                              key: assumption.key,
                              value: assumptionValue.trim(),
                            })
                            .then((ok) => {
                              if (ok) closeContext(true)
                            })
                      }}
                    >
                      <label className="e-field">
                        {assumption.label}
                        <input
                          data-testid="assumption-editor-input"
                          data-initial-focus="true"
                          value={assumptionValue}
                          onChange={(event) =>
                            setAssumptionValue(event.target.value)
                          }
                          required
                          maxLength={100}
                          disabled={disabled}
                        />
                      </label>
                      <p className="e-muted">
                        未明确提供的信息可以在这里调整。
                      </p>
                      <div className="e-panel-actions">
                        <button
                          className="e-button e-button-primary"
                          disabled={
                            disabled ||
                            !assumptionValue.trim() ||
                            assumptionValue.trim() === assumption.value
                          }
                        >
                          应用修改
                        </button>
                      </div>
                    </form>
                  )}
                  {context.kind === 'privacy' && (
                    <div>
                      <p>
                        {context.target === 'SOURCE'
                          ? '导入的攻略文字与识别依据将永久删除。现有行程、已确认地点和路线仍保留。'
                          : `“${title}”及关联数据将永久删除，无法恢复。`}
                      </p>
                      {privacyError && (
                        <p className="e-message" role="alert">
                          {privacyError}
                        </p>
                      )}
                      <div className="e-panel-actions">
                        <button
                          className="e-button"
                          data-initial-focus="true"
                          disabled={privacyBusy}
                          onClick={() => closeContext()}
                        >
                          取消
                        </button>
                        <button
                          className="e-button e-button-primary"
                          data-testid={
                            context.target === 'SOURCE'
                              ? 'confirm-delete-source'
                              : 'confirm-delete-trip'
                          }
                          disabled={privacyBusy}
                          onClick={() => void confirmDelete()}
                        >
                          {privacyBusy ? '正在删除…' : '确认永久删除'}
                        </button>
                      </div>
                    </div>
                  )}
                  {context.kind === 'share' && (
                    <div>
                      {sharing ? (
                        <p role="status">正在创建分享链接…</p>
                      ) : share ? (
                        <>
                          <p className="e-muted">
                            拥有链接的人可查看行程，链接默认 7
                            天后失效。可在账号中撤销。
                          </p>
                          <label className="e-field">
                            分享链接
                            <input
                              readOnly
                              value={share}
                              onFocus={(event) => event.target.select()}
                            />
                          </label>
                          <button
                            className="e-button"
                            onClick={() => {
                              void navigator.clipboard
                                .writeText(share)
                                .then(() => trip.setNotice('分享链接已复制。'))
                                .catch(() =>
                                  trip.setNotice('请选中链接后复制。'),
                                )
                            }}
                          >
                            复制链接
                          </button>
                        </>
                      ) : (
                        <p>{trip.notice || '暂时无法分享，请稍后重试。'}</p>
                      )}
                    </div>
                  )}
                </ContextPanel>
              )}
            </section>
            <section
              className={`e-map-column${mobile !== 'MAP' ? ' e-mobile-hidden' : ''}`}
              aria-label="路线地图"
            >
              <nav className="e-mobile-map-nav">
                <select
                  aria-label="地图日期"
                  value={safeDayIndex}
                  onChange={(event) => changeDay(Number(event.target.value))}
                >
                  {result.days.map((day, index) => (
                    <option key={index} value={index}>
                      {day.label}
                    </option>
                  ))}
                </select>
                <button
                  className="e-button"
                  onClick={() => setMobile('ITINERARY')}
                >
                  返回行程
                </button>
              </nav>
              <div className="e-map-head">
                <div>
                  <h2>这一天，怎么走</h2>
                  <p className="e-muted">
                    {displayMap?.status === 'NEEDS_UPDATE'
                      ? '路线待更新 · 旧路线已隐藏'
                      : displayMap?.status === 'PREPARING'
                        ? '路线准备中'
                        : displayMap?.status === 'AVAILABLE'
                          ? '路线已就绪'
                          : displayMap?.status === 'LIMITED'
                            ? '部分路线待确认'
                            : '路线尚未完整核对'}
                  </p>
                </div>
                <button
                  type="button"
                  className="e-button"
                  disabled={
                    disabled || displayMap?.status === 'PREPARING' || dirty
                  }
                  onClick={() => void trip.renderMap()}
                >
                  <RefreshCw aria-hidden="true" />
                  {displayMap?.status === 'PREPARING' ? '准备中' : '更新路线'}
                </button>
              </div>
              <div className="e-days e-route-modes" aria-label="路线方式">
                {(['recommended', 'walking', 'transit'] as const).map(
                  (mode) => (
                    <button
                      type="button"
                      key={mode}
                      aria-pressed={routeMode === mode}
                      onClick={() => setRouteMode(mode)}
                    >
                      {mode === 'recommended'
                        ? '推荐方式'
                        : mode === 'walking'
                          ? '步行'
                          : '公交'}
                    </button>
                  ),
                )}
              </div>
              {mapView}
              <p className="e-map-note" role="status">
                {displayMap?.message || result.map.message}
              </p>
              {displayMap?.status === 'UNAVAILABLE' && (
                <button
                  type="button"
                  className="e-text-button"
                  onClick={() => void trip.retryMap()}
                >
                  重新读取路线
                </button>
              )}
              {selectedCard && (
                <div className="e-map-detail">
                  <div>
                    <h3>{selectedCard.name}</h3>
                    <p className="e-muted">{activityTime(selectedCard)}</p>
                  </div>
                  <button
                    className="e-text-button"
                    disabled={disabled || dirty}
                    onClick={() => editCard(selectedCard)}
                  >
                    详情与编辑
                    <ArrowUpRight aria-hidden="true" />
                  </button>
                </div>
              )}
            </section>
          </div>
          )}
          <footer className="e-footer">
            <p>
              {accountSaved
                ? '账号行程从创建或领取起保留 30 天。'
                : '匿名草稿从创建起保留 24 小时，保存到账号后保留 30 天。'}
              刷新不会延长期限。
            </p>
            <Link href="/about#privacy">数据与隐私</Link>
          </footer>
        </>
      )}
    </main>
  )
}
