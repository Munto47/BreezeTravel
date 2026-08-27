'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import {
  ArrowLeft,
  ArrowRight,
  BedDouble,
  CalendarDays,
  CheckCircle2,
  Compass,
  Map,
  MapPin,
  Sparkles,
  Users,
  X,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import {
  type ActivityCardView,
  type UserFacingTripResult,
  readTripUnderstandingResult,
  streamTripUnderstandingEvents,
} from '@/lib/trip-understanding-v3'


const ASSUMPTION_ICONS = {
  destination: MapPin,
  calendar: CalendarDays,
  party_size: Users,
}


export default function TripResultPage() {
  const router = useRouter()
  const [resourceRef, setResourceRef] = useState<string | null>(null)
  const [result, setResult] = useState<UserFacingTripResult | null>(null)
  const [message, setMessage] = useState('正在整理每天行程')
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<ActivityCardView | null>(null)

  const refresh = useCallback(async (reference: string) => {
    try {
      const response = await readTripUnderstandingResult(reference)
      if (response.body.status !== 'PROCESSING') {
        setResult(response.body)
        if (response.etag) sessionStorage.setItem('bt_active_trip_etag', response.etag)
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

  useEffect(() => {
    const reference = sessionStorage.getItem('bt_active_trip_ref')
    if (!reference) {
      setError('当前标签页里没有可恢复的体验，请返回首页重新开始。')
      return
    }
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
                  <div key={assumption.key} className="rounded-2xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
                    <div className="flex items-center gap-2 text-xs text-slate-400">
                      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
                      {assumption.label}
                    </div>
                    <p className="mt-1 text-sm font-medium text-slate-700">{assumption.value}</p>
                  </div>
                )
              })}
            </div>
          </div>

          <div data-testid="trip-days" className="mt-8 grid gap-5 lg:grid-cols-3">
            {result.days.map((day) => (
              <section key={day.label} className="rounded-3xl border border-slate-200/80 bg-white p-5 shadow-lg shadow-slate-200/45">
                <div className="mb-4 flex items-center justify-between">
                  <h2 className="text-lg font-semibold">{day.label}</h2>
                  <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs text-slate-500">{day.activities.length} 个地点</span>
                </div>
                <div className="space-y-3">
                  {day.activities.map((activity, index) => (
                    <button
                      key={activity.activity_token}
                      type="button"
                      onClick={() => setSelected(activity)}
                      className="group w-full rounded-2xl border border-slate-100 bg-[#fbfaf7] p-4 text-left transition hover:-translate-y-0.5 hover:border-emerald-200 hover:shadow-md"
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
                  ))}
                  {day.activities.length === 0 && (
                    <div className="rounded-2xl bg-slate-50 p-4 text-sm leading-6 text-slate-500">
                      还没有能确认的地点，可以稍后补充或调整文字。
                    </div>
                  )}
                </div>
              </section>
            ))}
          </div>

          <div className="mt-6 grid gap-4 sm:grid-cols-2">
            <StatusCard icon={Map} title="路线地图" message={result.map.message} />
            <StatusCard icon={BedDouble} title="住宿" message={result.stay.message} />
          </div>
        </section>
      </div>

      {selected && (
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-950/25 p-4 backdrop-blur-sm sm:items-center" role="dialog" aria-modal="true" aria-label="地点详情">
          <div className="w-full max-w-md rounded-3xl bg-white p-6 shadow-2xl">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs font-medium text-emerald-700">{selected.category}</p>
                <h2 className="mt-1 text-2xl font-semibold">{selected.name}</h2>
              </div>
              <button type="button" onClick={() => setSelected(null)} className="rounded-full bg-slate-100 p-2 text-slate-500" aria-label="关闭地点详情">
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            <div className="mt-5 space-y-3 rounded-2xl bg-slate-50 p-4 text-sm text-slate-600">
              <p className="flex items-center gap-2"><MapPin className="h-4 w-4 text-slate-400" aria-hidden="true" />{selected.area_or_address}</p>
              <p className="flex items-center gap-2"><CalendarDays className="h-4 w-4 text-slate-400" aria-hidden="true" />{selected.time_hint || '时间待定'}</p>
            </div>
            <p className="mt-4 text-xs leading-5 text-slate-400">替换、删除和排序会在下一切片开放；这里不展示攻略原文或内部判断数字。</p>
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
