'use client'

import { useEffect, useState } from 'react'
import { useParams } from 'next/navigation'
import { Loader2, LockKeyhole, ShieldAlert } from 'lucide-react'

import type { ShareProjectionView } from '@/lib/trip-understanding-v3'

const UNAVAILABLE_MESSAGE = '此链接不存在、已过期或已被撤销。'

export default function SharedItineraryPage() {
  const params = useParams()
  const shareRef = typeof params.token === 'string' ? params.token : ''
  const [shared, setShared] = useState<ShareProjectionView | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false

    const load = async () => {
      if (!shareRef) {
        if (!cancelled) {
          setError('链接格式无效。')
          setLoading(false)
        }
        return
      }

      setLoading(true)
      setError(null)
      const secret = window.location.hash.startsWith('#s=')
        ? window.location.hash.slice(3)
        : ''
      if (secret) {
        // Remove the fragment before the first network request. The secret is
        // exchanged only in the body and never enters URLs, Referer or logs.
        window.history.replaceState(
          null,
          '',
          window.location.pathname + window.location.search,
        )
        const exchange = await fetch(
          `/api/v3/shares/${encodeURIComponent(shareRef)}/exchange`,
          {
            method: 'POST',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ secret }),
          },
        ).catch(() => null)
        if (!exchange?.ok) {
          if (!cancelled) {
            setError(UNAVAILABLE_MESSAGE)
            setLoading(false)
          }
          return
        }
      }

      const response = await fetch(
        `/api/v3/shares/${encodeURIComponent(shareRef)}`,
        { credentials: 'include', cache: 'no-store' },
      ).catch(() => null)
      if (!response?.ok) {
        if (!cancelled) {
          setError(UNAVAILABLE_MESSAGE)
          setLoading(false)
        }
        return
      }
      const projection = await response.json() as ShareProjectionView
      if (!cancelled) {
        setShared(projection)
        setLoading(false)
      }
    }

    void load()
    return () => { cancelled = true }
  }, [shareRef])

  if (loading) {
    return (
      <main className="mx-auto flex min-h-screen max-w-3xl items-center justify-center p-6 text-sm text-slate-500">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />读取受限行程…
      </main>
    )
  }

  if (!shared) {
    return (
      <main className="mx-auto flex min-h-screen max-w-xl items-center p-6">
        <section className="w-full rounded-2xl border border-amber-200 bg-white p-6 shadow-sm">
          <div className="flex gap-3">
            <ShieldAlert className="h-5 w-5 shrink-0 text-amber-700" />
            <div>
              <h1 className="font-semibold text-slate-900">无法访问此受限分享</h1>
              <p role="alert" className="mt-2 text-sm text-slate-600">
                {error ?? UNAVAILABLE_MESSAGE}
              </p>
            </div>
          </div>
        </section>
      </main>
    )
  }

  return (
    <main
      className="mx-auto min-h-screen max-w-3xl bg-slate-50 p-4 sm:p-8"
      data-testid="g06-shared-trip"
    >
      <section className="rounded-2xl border border-emerald-200 bg-white p-5 shadow-sm">
        <div className="flex items-start gap-3">
          <LockKeyhole className="mt-0.5 h-5 w-5 shrink-0 text-emerald-700" />
          <div>
            <h1 className="text-lg font-semibold text-slate-900">{shared.title}</h1>
            <p className="mt-1 text-sm text-slate-600">{shared.message}</p>
          </div>
        </div>
        <div className="mt-4 grid gap-2 text-xs text-slate-600 sm:grid-cols-3">
          <p>目的地：{shared.destination}</p>
          <p>时间：{shared.schedule}</p>
          <p>出行：{shared.party_size}</p>
        </div>
        {shared.accommodation ? (
          <p className="mt-3 rounded-xl bg-emerald-50 px-3 py-2 text-xs text-emerald-900">
            住宿：{shared.accommodation}
          </p>
        ) : null}
      </section>
      <section className="mt-4 space-y-3" aria-label="只读行程">
        {shared.days.map((day) => (
          <article
            key={day.label}
            className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"
          >
            <h2 className="font-semibold text-slate-900">{day.label}</h2>
            {day.activities.length === 0 ? (
              <p className="mt-3 text-sm text-slate-500">当天暂无地点</p>
            ) : (
              <ol className="mt-3 space-y-2">
                {day.activities.map((activity, index) => (
                  <li
                    key={`${day.label}-${index}`}
                    className="rounded-xl bg-slate-50 p-3"
                  >
                    <p className="text-sm font-medium text-slate-900">{activity.name}</p>
                    <p className="mt-1 text-xs text-slate-600">
                      {activity.time_hint ?? '时间待定'} · {activity.area_or_address}
                    </p>
                    <p className="mt-1 text-[11px] text-slate-500">{activity.note}</p>
                  </li>
                ))}
              </ol>
            )}
          </article>
        ))}
      </section>
      <p className="mt-4 text-center text-xs text-slate-500">
        只读分享；不提供编辑、路线计算或账号权限。
      </p>
    </main>
  )
}
