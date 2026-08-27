'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import {
  ArrowLeft,
  ArrowRight,
  BedDouble,
  CalendarDays,
  ChevronDown,
  ChevronUp,
  CheckCircle2,
  Compass,
  Map,
  MapPin,
  Pencil,
  Plus,
  Replace,
  ShieldCheck,
  Sparkles,
  Trash2,
  Users,
  X,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import {
  type ActivityCardView,
  type TripUnderstandingCommand,
  type UserFacingTripResult,
  applyTripUnderstandingCommand,
  claimTripUnderstanding,
  clearTripUnderstandingSession,
  deleteTripUnderstanding,
  deleteTripUnderstandingSource,
  readTripUnderstandingResult,
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

  useEffect(() => {
    hydrate()
  }, [hydrate])

  const refresh = useCallback(async (reference: string) => {
    try {
      const response = await readTripUnderstandingResult(reference)
      if (response.body.status !== 'PROCESSING') {
        setResult(response.body)
        if (response.etag) {
          setEtag(response.etag)
          sessionStorage.setItem('bt_active_trip_etag', response.etag)
        }
        setMessage('卡片已可用')
        return true
      }
      setMessage(response.body.message)
      return false
    } catch {
      setError('这份体验暂时无法打开，请返回首页重新开始。')
      return false
    }
  }, [])

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
    setResourceRef(reference)
    let disposed = false
    let interval: ReturnType<typeof setInterval> | undefined
    const eventController = new AbortController()
    void streamTripUnderstandingEvents(
      reference,
      (event) => {
        if (disposed) return
        setMessage(event.message)
        if (event.type === 'result_available') {
          void refresh(reference).then((ready) => {
            if (ready) eventController.abort()
          })
        }
      },
      eventController.signal,
    ).catch((streamError: unknown) => {
      if (!disposed && !(streamError instanceof DOMException && streamError.name === 'AbortError')) {
        void refresh(reference)
      }
    })
    void refresh(reference)
    interval = setInterval(() => {
      if (!disposed) {
        void refresh(reference).then((ready) => {
          if (ready && interval) clearInterval(interval)
        })
      }
    }, 1000)
    return () => {
      disposed = true
      eventController.abort()
      if (interval) clearInterval(interval)
    }
  }, [refresh])

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

          <div className="mt-6 grid gap-4 sm:grid-cols-2">
            <StatusCard icon={Map} title="路线地图" message={result.map.message} />
            <StatusCard icon={BedDouble} title="住宿" message={result.stay.message} />
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
            <p className="mt-4 text-xs leading-5 text-slate-400">调整会保存为新版本；路线不会自动重算。这里不展示攻略原文或内部判断数字。</p>
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


function StatusCard({ icon: Icon, title, message }: { icon: LucideIcon; title: string; message: string }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-5">
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-slate-100 text-slate-500">
          <Icon className="h-5 w-5" aria-hidden="true" />
        </div>
        <div>
          <p className="text-sm font-semibold">{title}</p>
          <p className="mt-1 text-xs text-slate-500">{message}</p>
        </div>
      </div>
    </div>
  )
}
