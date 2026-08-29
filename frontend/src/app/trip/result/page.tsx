'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import {
  ArrowLeft,
  ArrowRight,
  BedDouble,
  BusFront,
  CalendarDays,
  ChevronDown,
  ChevronUp,
  CheckCircle2,
  Compass,
  Footprints,
  Map,
  MapPin,
  Pencil,
  Plus,
  Replace,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Trash2,
  Users,
  X,
} from 'lucide-react'

import {
  type ActivityCardView,
  type MapRenderView,
  type StaySuggestionView,
  type TripUnderstandingCommand,
  type UserFacingTripResult,
  applyTripUnderstandingCommand,
  claimTripUnderstanding,
  clearTripUnderstandingSession,
  deleteTripUnderstanding,
  deleteTripUnderstandingSource,
  readTripUnderstandingResult,
  readTripUnderstandingMap,
  readTripUnderstandingStay,
  requestTripUnderstandingMap,
  selectTripUnderstandingStay,
  streamTripUnderstandingEvents,
} from '@/lib/trip-understanding-v3'
import { useAuthStore } from '@/stores/authStore'


const ASSUMPTION_ICONS = {
  destination: MapPin,
  calendar: CalendarDays,
  party_size: Users,
}


type SelectedCard = {
  card: ActivityCardView
  dayIndex: number
  position: number
}

type EditorState = {
  mode: 'INSERT' | 'EDIT' | 'REPLACE'
  dayIndex: number
  position: number
  card?: ActivityCardView
}


export default function TripResultPage() {
  const router = useRouter()
  const { user, isHydrated, hydrate } = useAuthStore()
  const [resourceRef, setResourceRef] = useState<string | null>(null)
  const [activeMode, setActiveMode] = useState<'DEMO' | 'FULL' | 'CLAIMED' | null>(null)
  const [result, setResult] = useState<UserFacingTripResult | null>(null)
  const [etag, setEtag] = useState<string | null>(null)
  const [message, setMessage] = useState('正在整理每天行程')
  const [error, setError] = useState('')
  const [commandError, setCommandError] = useState('')
  const [isApplying, setIsApplying] = useState(false)
  const [selected, setSelected] = useState<SelectedCard | null>(null)
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [editorName, setEditorName] = useState('')
  const [editorCategory, setEditorCategory] = useState('地点')
  const [editorAddress, setEditorAddress] = useState('地点待确认')
  const [editorTime, setEditorTime] = useState('')
  const [privacyBusy, setPrivacyBusy] = useState<'CLAIM' | 'SOURCE' | 'TRIP' | null>(null)
  const [privacyMessage, setPrivacyMessage] = useState('')
  const [sourceDeleted, setSourceDeleted] = useState(false)
  const [tripDeleted, setTripDeleted] = useState(false)
  const [mapView, setMapView] = useState<MapRenderView | null>(null)
  const [stayView, setStayView] = useState<StaySuggestionView | null>(null)
  const [enhancementBusy, setEnhancementBusy] = useState<'MAP' | 'STAY' | null>(null)
  const activeResourceRef = useRef<string | null>(null)

  useEffect(() => {
    hydrate()
  }, [hydrate])

  const refreshEnhancements = useCallback(async (reference: string) => {
    const [mapResult, stayResult] = await Promise.allSettled([
      readTripUnderstandingMap(reference),
      readTripUnderstandingStay(reference),
    ])
    if (activeResourceRef.current !== reference) return
    if (mapResult.status === 'fulfilled') setMapView(mapResult.value)
    if (stayResult.status === 'fulfilled') setStayView(stayResult.value)
  }, [])

  const refresh = useCallback(async (reference: string) => {
    try {
      const response = await readTripUnderstandingResult(reference)
      if (activeResourceRef.current !== reference) return false
      if (response.body.status !== 'PROCESSING') {
        setResult(response.body)
        if (response.etag) {
          setEtag(response.etag)
          sessionStorage.setItem('bt_active_trip_etag', response.etag)
        }
        setMessage('卡片已可用')
        void refreshEnhancements(reference)
        return true
      }
      setMessage(response.body.message)
      return false
    } catch {
      if (activeResourceRef.current === reference) {
        setError('这份体验暂时无法打开，请返回首页重新开始。')
      }
      return false
    }
  }, [refreshEnhancements])

  useEffect(() => {
    if (!resourceRef || !result) return
    const preparing = mapView?.status === 'PREPARING' || stayView?.status === 'PREPARING'
    if (!preparing) return
    const timer = window.setInterval(() => {
      void refreshEnhancements(resourceRef)
    }, 800)
    return () => window.clearInterval(timer)
  }, [mapView?.status, refreshEnhancements, resourceRef, result, stayView?.status])

  const handleMapRender = useCallback(async () => {
    if (!resourceRef || !etag || enhancementBusy) return
    setEnhancementBusy('MAP')
    setCommandError('')
    try {
      await requestTripUnderstandingMap(resourceRef, etag)
      await refreshEnhancements(resourceRef)
    } catch (mapFailure) {
      if (mapFailure instanceof Error && mapFailure.message === 'REVISION_CONFLICT') {
        setCommandError('行程刚刚有更新，已为你读取最新版本。')
        await refresh(resourceRef)
      } else {
        setCommandError('路线暂时没有开始更新，卡片仍可正常查看。')
      }
    } finally {
      setEnhancementBusy(null)
    }
  }, [enhancementBusy, etag, refresh, refreshEnhancements, resourceRef])

  const handleStaySelection = useCallback(async (candidateToken: string) => {
    if (!resourceRef || !etag || enhancementBusy) return
    setEnhancementBusy('STAY')
    setCommandError('')
    try {
      const selectedStay = await selectTripUnderstandingStay(resourceRef, candidateToken, etag)
      setEtag(selectedStay.etag)
      sessionStorage.setItem('bt_active_trip_etag', selectedStay.etag)
      await refresh(resourceRef)
    } catch (stayFailure) {
      if (stayFailure instanceof Error && stayFailure.message === 'REVISION_CONFLICT') {
        setCommandError('住宿候选已经变化，已为你读取最新版本。')
        await refresh(resourceRef)
      } else {
        setCommandError('这次住宿选择暂时没有保存，请稍后再试。')
      }
    } finally {
      setEnhancementBusy(null)
    }
  }, [enhancementBusy, etag, refresh, resourceRef])

  const runCommand = useCallback(async (command: TripUnderstandingCommand) => {
    if (!resourceRef || !etag || isApplying) return
    setIsApplying(true)
    setCommandError('')
    try {
      const applied = await applyTripUnderstandingCommand(resourceRef, etag, command)
      setEtag(applied.etag)
      sessionStorage.setItem('bt_active_trip_etag', applied.etag)
      await refresh(resourceRef)
      setSelected(null)
      setEditor(null)
    } catch (commandFailure) {
      if (commandFailure instanceof Error && commandFailure.message === 'REVISION_CONFLICT') {
        setCommandError('卡片刚刚有更新，已为你读取最新版本，请再试一次。')
        await refresh(resourceRef)
      } else {
        setCommandError('这次调整暂时没有保存，卡片内容没有丢失。')
      }
    } finally {
      setIsApplying(false)
    }
  }, [etag, isApplying, refresh, resourceRef])

  const openEditor = (state: EditorState) => {
    setSelected(null)
    setEditor(state)
    setEditorName(state.card?.name || '')
    setEditorCategory(state.card?.category || '地点')
    setEditorAddress(state.card?.area_or_address || '地点待确认')
    setEditorTime(state.card?.time_hint || '')
    setCommandError('')
  }

  const submitEditor = async () => {
    if (!editor || !editorName.trim()) {
      setCommandError('请填写地点名称。')
      return
    }
    if (editor.mode === 'INSERT') {
      await runCommand({
        command_type: 'ACTIVITY_INSERT',
        day_index: editor.dayIndex,
        position: editor.position,
        name: editorName.trim(),
        category: editorCategory.trim() || '地点',
        area_or_address: editorAddress.trim() || '地点待确认',
        time_hint: editorTime.trim() || null,
      })
    } else if (editor.mode === 'EDIT' && editor.card) {
      await runCommand({
        command_type: 'ACTIVITY_TEXT_EDIT',
        activity_token: editor.card.activity_token,
        name: editorName.trim(),
        time_hint: editorTime.trim() || null,
      })
    } else if (editor.mode === 'REPLACE' && editor.card) {
      await runCommand({
        command_type: 'PLACE_REPLACE',
        activity_token: editor.card.activity_token,
        replacement: {
          name: editorName.trim(),
          category: editorCategory.trim() || '地点',
          area_or_address: editorAddress.trim() || '地点待确认',
        },
      })
    }
  }

  const handleClaim = async () => {
    if (!resourceRef || privacyBusy) return
    if (!user) {
      sessionStorage.setItem('bt_login_return', '/trip/result')
      router.push('/login')
      return
    }
    setPrivacyBusy('CLAIM')
    setPrivacyMessage('')
    try {
      const claimed = await claimTripUnderstanding(resourceRef)
      const nextReference = claimed.body.public_resource_id
      clearTripUnderstandingSession()
      sessionStorage.setItem('bt_active_trip_ref', nextReference)
      sessionStorage.setItem('bt_active_trip_mode', 'CLAIMED')
      sessionStorage.removeItem('bt_active_trip_event_cursor')
      sessionStorage.setItem('bt_active_trip_etag', claimed.etag)
      activeResourceRef.current = nextReference
      setResourceRef(nextReference)
      setActiveMode('CLAIMED')
      setEtag(claimed.etag)
      if (!(await refresh(nextReference))) throw new Error('CLAIM_READBACK_FAILED')
      setPrivacyMessage('已保存到你的账号，匿名访问凭证已经失效。')
    } catch {
      setPrivacyMessage('暂时没有保存成功，这份卡片仍保持原样。')
    } finally {
      setPrivacyBusy(null)
    }
  }

  const handleDeleteSource = async () => {
    if (!resourceRef || privacyBusy || sourceDeleted) return
    const confirmed = window.confirm(
      '删除原文后，攻略文字将永久不可恢复；当前逐日卡片会保留。确定继续吗？',
    )
    if (!confirmed) return
    setPrivacyBusy('SOURCE')
    setPrivacyMessage('')
    try {
      await deleteTripUnderstandingSource(resourceRef)
      if (!(await refresh(resourceRef))) throw new Error('SOURCE_DELETE_READBACK_FAILED')
      sessionStorage.setItem('bt_active_trip_source_deleted', 'true')
      setSourceDeleted(true)
      setPrivacyMessage('原文已永久删除，逐日卡片仍可继续查看和调整。')
    } catch {
      setPrivacyMessage('原文尚未确认删除，卡片没有变化，可以稍后重试。')
    } finally {
      setPrivacyBusy(null)
    }
  }

  const handleDeleteTrip = async () => {
    if (!resourceRef || privacyBusy) return
    const confirmed = window.confirm(
      '删除整份行程会永久移除原文、卡片和相关结果，之后无法恢复。确定删除吗？',
    )
    if (!confirmed) return
    setPrivacyBusy('TRIP')
    setPrivacyMessage('')
    try {
      await deleteTripUnderstanding(resourceRef)
      clearTripUnderstandingSession()
      activeResourceRef.current = null
      setTripDeleted(true)
    } catch {
      setPrivacyMessage('尚未确认删除完成，这份行程仍保留，可以稍后重试。')
    } finally {
      setPrivacyBusy(null)
    }
  }

  useEffect(() => {
    const reference = sessionStorage.getItem('bt_active_trip_ref')
    if (!reference) {
      setError('当前标签页里没有可恢复的体验，请返回首页重新开始。')
      return
    }
    const storedMode = sessionStorage.getItem('bt_active_trip_mode')
    if (storedMode === 'DEMO' || storedMode === 'FULL' || storedMode === 'CLAIMED') {
      setActiveMode(storedMode)
    }
    setSourceDeleted(sessionStorage.getItem('bt_active_trip_source_deleted') === 'true')
    activeResourceRef.current = reference
    setResourceRef(reference)
  }, [])

  useEffect(() => {
    if (!resourceRef) return
    activeResourceRef.current = resourceRef
    let disposed = false
    let interval: ReturnType<typeof setInterval> | undefined
    const eventController = new AbortController()
    void streamTripUnderstandingEvents(
      resourceRef,
      (event) => {
        if (disposed) return
        setMessage(event.message)
        if (event.type === 'result_available') {
          void refresh(resourceRef).then((ready) => {
            if (ready) eventController.abort()
          })
        }
      },
      eventController.signal,
    ).catch((streamError: unknown) => {
      if (!disposed && !(streamError instanceof DOMException && streamError.name === 'AbortError')) {
        void refresh(resourceRef)
      }
    })
    void refresh(resourceRef)
    interval = setInterval(() => {
      if (!disposed) {
        void refresh(resourceRef).then((ready) => {
          if (ready && interval) clearInterval(interval)
        })
      }
    }, 1000)
    return () => {
      disposed = true
      eventController.abort()
      if (interval) clearInterval(interval)
    }
  }, [refresh, resourceRef])

  useEffect(() => {
    if (!resourceRef || !result || result.map.status !== 'PREPARING') return
    let disposed = false
    const timer = setInterval(() => {
      if (!disposed) void refresh(resourceRef)
    }, 500)
    return () => {
      disposed = true
      clearInterval(timer)
    }
  }, [refresh, resourceRef, result])

  if (tripDeleted) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-[#f8f7f2] p-6">
        <div data-testid="trip-deleted" className="w-full max-w-md rounded-3xl border border-slate-200 bg-white p-8 text-center shadow-xl">
          <ShieldCheck className="mx-auto h-10 w-10 text-emerald-700" aria-hidden="true" />
          <h1 className="mt-4 text-xl font-semibold">这份行程已永久删除</h1>
          <p className="mt-2 text-sm leading-6 text-slate-500">我们已重新读取确认它不再可访问。</p>
          <button type="button" onClick={() => router.push('/')} className="mt-6 rounded-2xl bg-slate-950 px-5 py-3 text-sm font-semibold text-white">
            返回首页
          </button>
        </div>
      </main>
    )
  }

  if (error) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-[#f8f7f2] p-6">
        <div className="w-full max-w-md rounded-3xl border border-slate-200 bg-white p-8 text-center shadow-xl">
          <Compass className="mx-auto h-10 w-10 text-slate-700" aria-hidden="true" />
          <h1 className="mt-4 text-xl font-semibold">暂时无法恢复</h1>
          <p className="mt-2 text-sm leading-6 text-slate-500">{error}</p>
          <button type="button" onClick={() => router.push('/')} className="mt-6 rounded-2xl bg-slate-950 px-5 py-3 text-sm font-semibold text-white">
            返回首页
          </button>
        </div>
      </main>
    )
  }

  if (!resourceRef || !result) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-[#f8f7f2] p-6">
        <div data-testid="trip-progress" className="w-full max-w-md rounded-3xl border border-white bg-white/90 p-8 text-center shadow-xl">
          <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-emerald-50 text-emerald-700">
            <Sparkles className="h-6 w-6 animate-pulse" aria-hidden="true" />
          </div>
          <h1 className="mt-5 text-xl font-semibold">{message}</h1>
          <p className="mt-2 text-sm text-slate-500">页面可以安全刷新，整理结果会继续保留。</p>
          <div className="mt-6 h-2 overflow-hidden rounded-full bg-slate-100">
            <div className="h-full w-2/3 animate-pulse rounded-full bg-emerald-500" />
          </div>
        </div>
      </main>
    )
  }

  return (
    <main className="min-h-screen bg-[#f8f7f2] text-slate-900">
      <div className="mx-auto w-full max-w-6xl px-5 py-6 sm:px-8 lg:px-12">
        <header className="flex items-center justify-between border-b border-slate-200/80 pb-5">
          <button type="button" onClick={() => router.push('/')} className="inline-flex items-center gap-2 rounded-full px-3 py-2 text-sm text-slate-600 transition hover:bg-white">
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
            返回首页
          </button>
          <div className="flex items-center gap-2 text-sm font-semibold">
            <Compass className="h-4 w-4 text-emerald-700" aria-hidden="true" />
            {result.assumptions.find((item) => item.key === 'destination')?.value || '行程卡片'}
          </div>
        </header>

        <section className="py-8">
          <div className="flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <div className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-3 py-1.5 text-xs font-medium text-emerald-700">
                <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                {result.status === 'READY' ? '地点卡片已整理' : '已整理可确认的内容'}
              </div>
              <h1 className="mt-4 text-3xl font-semibold tracking-tight sm:text-4xl">按天查看，随时可以调整</h1>
              <p className="mt-2 text-sm leading-6 text-slate-500">没有日历日期时先用 Day 编号；人数是可修改的软假设。</p>
            </div>
            <div className="flex flex-wrap gap-2" aria-label="当前假设">
              {result.assumptions.map((assumption) => {
                const Icon = ASSUMPTION_ICONS[assumption.key]
                return (
                  <button
                    key={assumption.key}
                    type="button"
                    disabled={isApplying || !assumption.editable}
                    onClick={() => {
                      const value = window.prompt(`修改${assumption.label}`, assumption.value)?.trim()
                      if (value && value !== assumption.value) {
                        void runCommand({ command_type: 'ASSUMPTION_SET', key: assumption.key, value })
                      }
                    }}
                    className="rounded-2xl border border-slate-200 bg-white px-4 py-3 text-left shadow-sm transition hover:border-emerald-200 disabled:cursor-wait"
                    aria-label={`修改${assumption.label}`}
                  >
                    <div className="flex items-center gap-2 text-xs text-slate-400">
                      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
                      {assumption.label}
                      <Pencil className="h-3 w-3" aria-hidden="true" />
                    </div>
                    <p className="mt-1 text-sm font-medium text-slate-700">{assumption.value}</p>
                  </button>
                )
              })}
            </div>
          </div>

          {commandError && (
            <p role="status" className="mt-5 rounded-2xl bg-amber-50 px-4 py-3 text-sm text-amber-800">
              {commandError}
            </p>
          )}

          <div data-testid="trip-days" className="mt-8 grid gap-5 lg:grid-cols-3">
            {result.days.map((day, dayOffset) => (
              <section key={day.label} className="rounded-3xl border border-slate-200/80 bg-white p-5 shadow-lg shadow-slate-200/45">
                <div className="mb-4 flex items-center justify-between">
                  <h2 className="text-lg font-semibold">{day.label}</h2>
                  <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs text-slate-500">{day.activities.length} 个地点</span>
                </div>
                <div className="space-y-3">
                  {day.activities.map((activity, index) => (
                    <div
                      key={activity.activity_token}
                      data-testid="activity-card"
                      className="group overflow-hidden rounded-2xl border border-slate-100 bg-[#fbfaf7] transition hover:-translate-y-0.5 hover:border-emerald-200 hover:shadow-md"
                    >
                      <button
                        type="button"
                        onClick={() => setSelected({ card: activity, dayIndex: dayOffset + 1, position: index })}
                        className="w-full p-4 text-left"
                      >
                        <div className="flex items-start gap-3">
                          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-emerald-700 text-xs font-semibold text-white">{index + 1}</span>
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center justify-between gap-2">
                              <h3 className="truncate font-semibold text-slate-800">{activity.name}</h3>
                              <ArrowRight className="h-4 w-4 shrink-0 text-slate-300 transition group-hover:text-emerald-600" aria-hidden="true" />
                            </div>
                            <p className="mt-1 text-xs text-slate-500">{activity.category} · {activity.time_hint || '时间待定'}</p>
                            <p className="mt-2 line-clamp-2 text-xs leading-5 text-slate-400">{activity.area_or_address}</p>
                          </div>
                        </div>
                      </button>
                      <div className="flex justify-end gap-1 border-t border-slate-100 px-3 py-2">
                        <button
                          type="button"
                          disabled={isApplying || index === 0}
                          onClick={() => void runCommand({
                            command_type: 'ACTIVITY_MOVE',
                            activity_token: activity.activity_token,
                            target_day_index: dayOffset + 1,
                            target_position: index - 1,
                          })}
                          className="rounded-lg p-1.5 text-slate-400 hover:bg-white hover:text-emerald-700 disabled:opacity-30"
                          aria-label={`上移 ${activity.name}`}
                        >
                          <ChevronUp className="h-4 w-4" aria-hidden="true" />
                        </button>
                        <button
                          type="button"
                          disabled={isApplying || index === day.activities.length - 1}
                          onClick={() => void runCommand({
                            command_type: 'ACTIVITY_MOVE',
                            activity_token: activity.activity_token,
                            target_day_index: dayOffset + 1,
                            target_position: index + 1,
                          })}
                          className="rounded-lg p-1.5 text-slate-400 hover:bg-white hover:text-emerald-700 disabled:opacity-30"
                          aria-label={`下移 ${activity.name}`}
                        >
                          <ChevronDown className="h-4 w-4" aria-hidden="true" />
                        </button>
                      </div>
                    </div>
                  ))}
                  {day.activities.length === 0 && (
                    <div className="rounded-2xl bg-slate-50 p-4 text-sm leading-6 text-slate-500">
                      还没有能确认的地点，可以稍后补充或调整文字。
                    </div>
                  )}
                  <button
                    type="button"
                    disabled={isApplying}
                    onClick={() => openEditor({
                      mode: 'INSERT',
                      dayIndex: dayOffset + 1,
                      position: day.activities.length,
                    })}
                    className="inline-flex w-full items-center justify-center gap-2 rounded-2xl border border-dashed border-slate-300 px-4 py-3 text-sm text-slate-500 transition hover:border-emerald-400 hover:text-emerald-700 disabled:cursor-wait"
                    aria-label={`新增地点到 ${day.label}`}
                  >
                    <Plus className="h-4 w-4" aria-hidden="true" />
                    新增地点
                  </button>
                </div>
              </section>
            ))}
          </div>

          <div className="mt-6 grid gap-5 xl:grid-cols-[1.35fr_1fr]">
            <MapTheater
              view={mapView || {
                status: result.map.status,
                message: result.map.message,
                days: [],
                available_actions: result.map.available_actions,
              }}
              busy={enhancementBusy === 'MAP'}
              onRender={() => void handleMapRender()}
            />
            <StayPanel
              view={stayView || result.stay}
              busy={enhancementBusy === 'STAY'}
              onChoose={(candidateToken) => void handleStaySelection(candidateToken)}
            />
          </div>

          <section className="mt-6 rounded-3xl border border-slate-200 bg-white p-5" aria-labelledby="trip-privacy-title">
            <div className="flex items-start gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-emerald-50 text-emerald-700">
                <ShieldCheck className="h-5 w-5" aria-hidden="true" />
              </div>
              <div>
                <h2 id="trip-privacy-title" className="font-semibold">隐私与保留</h2>
                <p className="mt-1 text-xs leading-5 text-slate-500">你可以只删除攻略原文并保留卡片，也可以永久删除整份行程。完成提示只会在服务端回读确认后出现。</p>
              </div>
            </div>
            {privacyMessage && (
              <p role="status" className="mt-4 rounded-2xl bg-amber-50 px-4 py-3 text-sm text-amber-800">{privacyMessage}</p>
            )}
            <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:flex-wrap">
              {activeMode === 'DEMO' && (
                <button
                  data-testid={user ? 'claim-demo-trip' : 'login-to-claim-demo'}
                  type="button"
                  disabled={privacyBusy !== null || !isHydrated}
                  onClick={() => void handleClaim()}
                  className="rounded-xl bg-emerald-700 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {privacyBusy === 'CLAIM' ? '正在保存…' : user ? '保存到我的账号' : '登录后保存这份体验'}
                </button>
              )}
              {user && activeMode !== 'DEMO' && (
                <button
                  data-testid="delete-trip-source"
                  type="button"
                  disabled={privacyBusy !== null || sourceDeleted}
                  onClick={() => void handleDeleteSource()}
                  className="rounded-xl border border-slate-300 px-4 py-2.5 text-sm font-medium text-slate-700 disabled:opacity-50"
                >
                  {sourceDeleted ? '原文已删除' : privacyBusy === 'SOURCE' ? '正在删除原文…' : '删除原文，保留卡片'}
                </button>
              )}
              <button
                data-testid="delete-entire-trip"
                type="button"
                disabled={privacyBusy !== null}
                onClick={() => void handleDeleteTrip()}
                className="rounded-xl border border-slate-300 px-4 py-2.5 text-sm font-medium text-slate-500 hover:border-rose-200 hover:text-rose-700 disabled:opacity-50"
              >
                {privacyBusy === 'TRIP' ? '正在删除行程…' : '永久删除整份行程'}
              </button>
            </div>
          </section>
        </section>
      </div>

      {selected && (
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-950/25 p-4 backdrop-blur-sm sm:items-center" role="dialog" aria-modal="true" aria-label="地点详情">
          <div className="w-full max-w-md rounded-3xl bg-white p-6 shadow-2xl">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs font-medium text-emerald-700">{selected.card.category}</p>
                <h2 className="mt-1 text-2xl font-semibold">{selected.card.name}</h2>
              </div>
              <button type="button" onClick={() => setSelected(null)} className="rounded-full bg-slate-100 p-2 text-slate-500" aria-label="关闭地点详情">
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            <div className="mt-5 space-y-3 rounded-2xl bg-slate-50 p-4 text-sm text-slate-600">
              <p className="flex items-center gap-2"><MapPin className="h-4 w-4 text-slate-400" aria-hidden="true" />{selected.card.area_or_address}</p>
              <p className="flex items-center gap-2"><CalendarDays className="h-4 w-4 text-slate-400" aria-hidden="true" />{selected.card.time_hint || '时间待定'}</p>
            </div>
            <div className="mt-5 grid grid-cols-2 gap-2">
              <button
                type="button"
                disabled={isApplying}
                onClick={() => openEditor({ ...selected, mode: 'EDIT', card: selected.card })}
                className="inline-flex items-center justify-center gap-2 rounded-xl bg-slate-100 px-3 py-2.5 text-sm text-slate-700"
              >
                <Pencil className="h-4 w-4" aria-hidden="true" />编辑文字
              </button>
              <button
                type="button"
                disabled={isApplying}
                onClick={() => openEditor({ ...selected, mode: 'REPLACE', card: selected.card })}
                className="inline-flex items-center justify-center gap-2 rounded-xl bg-slate-100 px-3 py-2.5 text-sm text-slate-700"
              >
                <Replace className="h-4 w-4" aria-hidden="true" />替换地点
              </button>
              <button
                type="button"
                disabled={isApplying || selected.dayIndex === 1}
                onClick={() => void runCommand({
                  command_type: 'ACTIVITY_MOVE',
                  activity_token: selected.card.activity_token,
                  target_day_index: selected.dayIndex - 1,
                  target_position: result.days[selected.dayIndex - 2]?.activities.length || 0,
                })}
                className="rounded-xl border border-slate-200 px-3 py-2.5 text-sm text-slate-600 disabled:opacity-40"
              >
                移到前一天
              </button>
              <button
                type="button"
                disabled={isApplying || selected.dayIndex === 14}
                onClick={() => void runCommand({
                  command_type: 'ACTIVITY_MOVE',
                  activity_token: selected.card.activity_token,
                  target_day_index: selected.dayIndex + 1,
                  target_position: result.days[selected.dayIndex]?.activities.length || 0,
                })}
                className="rounded-xl border border-slate-200 px-3 py-2.5 text-sm text-slate-600 disabled:opacity-40"
              >
                移到后一天
              </button>
            </div>
            <button
              type="button"
              disabled={isApplying}
              onClick={() => {
                if (window.confirm(`删除“${selected.card.name}”这张卡片？`)) {
                  void runCommand({
                    command_type: 'ACTIVITY_DELETE',
                    activity_token: selected.card.activity_token,
                  })
                }
              }}
              className="mt-3 inline-flex w-full items-center justify-center gap-2 rounded-xl border border-slate-200 px-3 py-2.5 text-sm text-slate-500 hover:border-rose-200 hover:text-rose-700"
            >
              <Trash2 className="h-4 w-4" aria-hidden="true" />删除这张卡片
            </button>
            <p className="mt-4 text-xs leading-5 text-slate-400">调整会自动保存；路线不会自动重算，需要时可稍后手动更新。</p>
          </div>
        </div>
      )}

      {editor && (
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-950/25 p-4 backdrop-blur-sm sm:items-center" role="dialog" aria-modal="true" aria-label="编辑地点卡片">
          <div className="w-full max-w-md rounded-3xl bg-white p-6 shadow-2xl">
            <div className="flex items-center justify-between gap-4">
              <h2 className="text-xl font-semibold">
                {editor.mode === 'INSERT' ? '新增地点' : editor.mode === 'REPLACE' ? '替换地点' : '编辑卡片文字'}
              </h2>
              <button type="button" onClick={() => setEditor(null)} className="rounded-full bg-slate-100 p-2 text-slate-500" aria-label="关闭编辑">
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            <div className="mt-5 space-y-4">
              <label className="block text-sm font-medium text-slate-700">
                地点名称
                <input
                  data-testid="card-editor-name"
                  value={editorName}
                  onChange={(event) => setEditorName(event.target.value)}
                  maxLength={40}
                  className="mt-2 w-full rounded-xl border border-slate-200 px-3 py-2.5 outline-none focus:border-emerald-500"
                />
              </label>
              {editor.mode !== 'EDIT' && (
                <>
                  <label className="block text-sm font-medium text-slate-700">
                    类别
                    <input value={editorCategory} onChange={(event) => setEditorCategory(event.target.value)} maxLength={40} className="mt-2 w-full rounded-xl border border-slate-200 px-3 py-2.5 outline-none focus:border-emerald-500" />
                  </label>
                  <label className="block text-sm font-medium text-slate-700">
                    区域或地址
                    <input value={editorAddress} onChange={(event) => setEditorAddress(event.target.value)} maxLength={120} className="mt-2 w-full rounded-xl border border-slate-200 px-3 py-2.5 outline-none focus:border-emerald-500" />
                  </label>
                </>
              )}
              {editor.mode !== 'REPLACE' && (
                <label className="block text-sm font-medium text-slate-700">
                  时间提示（可选）
                  <input value={editorTime} onChange={(event) => setEditorTime(event.target.value)} maxLength={80} className="mt-2 w-full rounded-xl border border-slate-200 px-3 py-2.5 outline-none focus:border-emerald-500" />
                </label>
              )}
            </div>
            <button
              data-testid="save-card-editor"
              type="button"
              disabled={isApplying}
              onClick={() => void submitEditor()}
              className="mt-6 w-full rounded-2xl bg-emerald-700 px-4 py-3 text-sm font-semibold text-white disabled:cursor-wait disabled:opacity-70"
            >
              {isApplying ? '正在保存…' : '保存调整'}
            </button>
          </div>
        </div>
      )}
    </main>
  )
}


const DAY_COLORS = ['#047857', '#2563eb', '#7c3aed', '#d97706', '#0f766e', '#be185d']


function MapTheater({
  view,
  busy,
  onRender,
}: {
  view: MapRenderView
  busy: boolean
  onRender: () => void
}) {
  const [mode, setMode] = useState<'walking' | 'transit'>('walking')
  const allPoints = view.days.flatMap((day) => day.routes.flatMap((route) => route[mode].geometry))
  const longitudes = allPoints.map((point) => point.longitude)
  const latitudes = allPoints.map((point) => point.latitude)
  const minimumLongitude = Math.min(...longitudes)
  const maximumLongitude = Math.max(...longitudes)
  const minimumLatitude = Math.min(...latitudes)
  const maximumLatitude = Math.max(...latitudes)
  const width = 640
  const height = 300
  const padding = 28
  const svgPoint = (longitude: number, latitude: number) => {
    const longitudeRange = Math.max(0.0001, maximumLongitude - minimumLongitude)
    const latitudeRange = Math.max(0.0001, maximumLatitude - minimumLatitude)
    const x = padding + ((longitude - minimumLongitude) / longitudeRange) * (width - padding * 2)
    const y = padding + ((maximumLatitude - latitude) / latitudeRange) * (height - padding * 2)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }
  const renderAction = view.available_actions.includes('RENDER_MAP') || ['NEEDS_UPDATE', 'LIMITED', 'UNAVAILABLE'].includes(view.status)

  return (
    <section data-testid="map-theater" className="overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm" aria-labelledby="map-theater-title">
      <div className="flex flex-col gap-4 border-b border-slate-100 p-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-emerald-50 text-emerald-700">
            <Map className="h-5 w-5" aria-hidden="true" />
          </span>
          <div>
            <h2 id="map-theater-title" className="font-semibold">路线地图</h2>
            <p role="status" className="mt-1 text-xs leading-5 text-slate-500">{view.message}</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex rounded-xl bg-slate-100 p-1" aria-label="路线方式">
            <button
              data-testid="map-mode-walking"
              type="button"
              onClick={() => setMode('walking')}
              className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium ${mode === 'walking' ? 'bg-white text-emerald-700 shadow-sm' : 'text-slate-500'}`}
            >
              <Footprints className="h-3.5 w-3.5" aria-hidden="true" />步行
            </button>
            <button
              data-testid="map-mode-transit"
              type="button"
              onClick={() => setMode('transit')}
              className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium ${mode === 'transit' ? 'bg-white text-blue-700 shadow-sm' : 'text-slate-500'}`}
            >
              <BusFront className="h-3.5 w-3.5" aria-hidden="true" />公交
            </button>
          </div>
          {renderAction && (
            <button
              data-testid="render-map"
              type="button"
              disabled={busy || view.status === 'PREPARING'}
              onClick={onRender}
              className="inline-flex items-center gap-1.5 rounded-xl bg-emerald-700 px-3 py-2.5 text-xs font-semibold text-white disabled:opacity-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${busy ? 'animate-spin' : ''}`} aria-hidden="true" />
              重新渲染地图
            </button>
          )}
        </div>
      </div>

      <div className="bg-[#f4f1e8] p-4">
        {allPoints.length >= 2 ? (
          <svg viewBox={`0 0 ${width} ${height}`} className="h-64 w-full rounded-2xl bg-[#eef2e9]" role="img" aria-label={`${mode === 'walking' ? '步行' : '公交'}路线图`}>
            <defs>
              <pattern id="map-grid" width="32" height="32" patternUnits="userSpaceOnUse">
                <path d="M 32 0 L 0 0 0 32" fill="none" stroke="#dbe3d5" strokeWidth="1" />
              </pattern>
            </defs>
            <rect width="100%" height="100%" fill="url(#map-grid)" />
            {view.days.map((day, dayIndex) => day.routes.map((route, routeIndex) => {
              const points = route[mode].geometry
              if (points.length < 2) return null
              const color = DAY_COLORS[dayIndex % DAY_COLORS.length]
              return (
                <g key={`${day.label}-${routeIndex}`}>
                  <polyline
                    points={points.map((point) => svgPoint(point.longitude, point.latitude)).join(' ')}
                    fill="none"
                    stroke={color}
                    strokeWidth="6"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeDasharray={mode === 'walking' ? '3 10' : undefined}
                  />
                  {points.filter((_point, index) => index === 0 || index === points.length - 1).map((point, pointIndex) => {
                    const [x, y] = svgPoint(point.longitude, point.latitude).split(',')
                    return <circle key={pointIndex} cx={x} cy={y} r="7" fill="white" stroke={color} strokeWidth="4" />
                  })}
                </g>
              )
            }))}
          </svg>
        ) : (
          <div className="flex h-64 items-center justify-center rounded-2xl border border-dashed border-slate-300 bg-white/60 px-8 text-center text-sm leading-6 text-slate-500">
            {view.status === 'PREPARING' ? '路线正在后台准备，卡片可以先查看和调整。' : '地图线条暂不可用，下面的路线摘要仍然有效。'}
          </div>
        )}
      </div>

      <div className="space-y-4 p-5">
        {view.days.map((day, dayIndex) => (
          <div key={day.label}>
            <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-slate-600">
              <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: DAY_COLORS[dayIndex % DAY_COLORS.length] }} />
              {day.label}
            </div>
            <div className="space-y-2">
              {day.routes.map((route, routeIndex) => (
                <div key={`${route.from_name}-${route.to_name}-${routeIndex}`} className="flex items-center justify-between gap-3 rounded-xl bg-slate-50 px-3 py-2.5 text-xs">
                  <span className="min-w-0 truncate text-slate-600">{route.from_name} → {route.to_name}</span>
                  <span className="shrink-0 font-medium text-slate-700">
                    {route[mode].status === 'AVAILABLE' ? `${route[mode].duration_minutes} 分钟` : '暂不可用'}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}


function StayPanel({
  view,
  busy,
  onChoose,
}: {
  view: StaySuggestionView
  busy: boolean
  onChoose: (candidateToken: string) => void
}) {
  return (
    <section data-testid="stay-panel" className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm" aria-labelledby="stay-panel-title">
      <div className="flex items-start gap-3">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-blue-50 text-blue-700">
          <BedDouble className="h-5 w-5" aria-hidden="true" />
        </span>
        <div>
          <h2 id="stay-panel-title" className="font-semibold">整程住宿</h2>
          <p role="status" className="mt-1 text-xs leading-5 text-slate-500">{view.message}</p>
        </div>
      </div>
      {view.area_summary && (
        <div className="mt-4 rounded-2xl bg-blue-50/70 p-3 text-xs leading-5 text-blue-900">
          建议区域：{view.area_summary}
          {view.searched_scopes.length > 0 && <span className="block text-blue-700">已比较：{view.searched_scopes.join('、')}</span>}
        </div>
      )}
      <div className="mt-4 space-y-3">
        {view.candidates.map((candidate, index) => (
          <article key={candidate.candidate_token} data-testid="stay-candidate" className="rounded-2xl border border-slate-200 p-4">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-xs font-medium text-blue-700">候选 {index + 1} · {candidate.brand}</p>
                <h3 className="mt-1 truncate font-semibold text-slate-800">{candidate.name}</h3>
                <p className="mt-1 text-xs leading-5 text-slate-400">{candidate.area_or_address}</p>
              </div>
              {candidate.selected && <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-600" aria-label="已选择" />}
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-slate-600">
              <span className="rounded-xl bg-slate-50 px-2.5 py-2">最久约 {candidate.max_single_leg_minutes} 分钟</span>
              <span className="rounded-xl bg-slate-50 px-2.5 py-2">共 {candidate.transfer_count} 次换乘</span>
            </div>
            <p className="mt-3 text-xs leading-5 text-slate-500">{candidate.reason}</p>
            {candidate.evidence_gap && <p className="mt-2 text-xs leading-5 text-amber-700">{candidate.evidence_gap}</p>}
            {!candidate.selected && candidate.available_actions.includes('CHOOSE_STAY') && (
              <button
                data-testid="choose-stay"
                type="button"
                disabled={busy}
                onClick={() => onChoose(candidate.candidate_token)}
                className="mt-3 w-full rounded-xl bg-blue-700 px-3 py-2.5 text-sm font-semibold text-white disabled:opacity-50"
              >
                {busy ? '正在保存…' : '整程住这里'}
              </button>
            )}
          </article>
        ))}
        {view.candidates.length === 0 && view.status !== 'PREPARING' && (
          <div className="rounded-2xl bg-slate-50 p-4 text-sm leading-6 text-slate-500">
            当前没有合格候选，不影响继续查看和调整行程。
          </div>
        )}
      </div>
    </section>
  )
}
