'use client'

import { useEffect, useState } from 'react'
import { useParams } from 'next/navigation'
import { Loader2, LockKeyhole, ShieldAlert } from 'lucide-react'

import type { ShareProjectionView } from '@/lib/trip-understanding-v3'
import {relativeDayLabel} from '@/app/trip/result/result-presentation'

const UNAVAILABLE_MESSAGE = '此链接不存在、已过期或已被撤销。'

function SharedDetails({items}: {items?: Array<{name: string; optional: boolean}>}) {
  if (!items?.length) return null
  return <div className="mt-2 border-l-2 border-sky-100 pl-3 text-xs leading-6 text-slate-600">
    <p>原文安排 · 门口及内部地点未单独核验</p>
    <ol>{items.map((item, index) => <li key={index}>{index + 1}. {item.name}{item.optional ? '（备选）' : ''}</li>)}</ol>
  </div>
}

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
          <p>行程：共 {shared.days.length} 天</p>
          <p>出行：{shared.party_size}</p>
        </div>
        {shared.accommodation ? (
          <p className="mt-3 rounded-xl bg-emerald-50 px-3 py-2 text-xs text-emerald-900">
            住宿：{shared.accommodation}
          </p>
        ) : null}
        {!!shared.warnings?.length && <ul data-testid="shared-incomplete" className="mt-4 rounded-xl bg-amber-50 p-3 text-sm leading-6 text-amber-900">{shared.warnings.map((text, i) => <li key={i}>{text}</li>)}</ul>}
        {!!shared.lodging_arrangements?.length && <section className="mt-4 rounded-xl bg-sky-50 p-3 text-sm leading-6"><h2 className="font-semibold">原文住宿安排</h2>{shared.lodging_arrangements.map((text, i) => <p key={i}>{text}</p>)}</section>}
      </section>
      <section className="mt-4 space-y-3" aria-label="只读行程">
        {shared.days.map((storedDay, dayIndex) => {
          // Older immutable shares may still contain unresolved entries.
          const day = { ...storedDay, activities: storedDay.activities.filter(activity => activity.note === '可直接查看') }
          return (
          <article
            key={day.label}
            data-testid={`shared-day-${dayIndex + 1}`}
            className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"
          >
            <h2 className="font-semibold text-slate-900">{relativeDayLabel(dayIndex)}</h2>
            {!!day.unprocessed_count && <p className="mt-2 text-sm text-amber-800">原文未整理：{day.unprocessed_count} 处</p>}
            {!!day.pending_count && <p className="mt-2 text-sm text-amber-800">待确认地点：{day.pending_count} 处</p>}
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
                      第 {index + 1} 站 · {activity.area_or_address}
                    </p>
                    <p className="mt-1 text-[11px] text-slate-500">{activity.note}</p>
                    <SharedDetails items={activity.details} />
                  </li>
                ))}
              </ol>
            )}
            {!!day.pending_activities?.length && <section className="mt-4 rounded-xl border border-amber-200 p-3">
              <h3 className="text-sm font-semibold text-amber-900">待确认安排 · 不作为已核验主线</h3>
              {day.pending_activities.map((activity, index) => <div className="mt-2 text-sm" key={index}><p>{activity.name} · 地点待确认</p><SharedDetails items={activity.details} /></div>)}
            </section>}
            {!!day.meal_arrangements?.length && <section className="mt-4 rounded-xl bg-orange-50 p-3 text-sm leading-6" data-testid="shared-meals">
              <h3 className="font-semibold">原文用餐安排</h3>{day.meal_arrangements.map((meal, index) => <p key={index}>{meal}</p>)}
            </section>}
            {!!day.alternatives?.length && <section className="mt-4 rounded-xl bg-sky-50 p-3" data-testid="shared-alternatives">
              <h3 className="text-sm font-semibold">原文备选与方案 · 未选择的内容不属于主线</h3>
              {day.alternatives.map((item, index) => <div className="mt-3 text-sm leading-6" key={index}>
                <p className="font-medium">{item.branch_label ? `${item.branch_label} · ` : ''}{item.name}</p><p className="text-xs text-slate-600">{item.state}</p><SharedDetails items={item.details} />
              </div>)}
            </section>}
          </article>
          )
        })}
      </section>
      {!!shared.unassigned_alternatives?.length && <section data-testid="shared-unassigned" className="mt-4 rounded-2xl border border-sky-200 bg-white p-5 text-sm leading-7">
        <h2 className="font-semibold">未指定日期 · 备选地点</h2><p className="text-xs text-slate-500">尚未排入任何一天的主线。</p>{shared.unassigned_alternatives.map((name, index) => <p key={index}>{name}</p>)}
      </section>}
      <p className="mt-4 text-center text-xs text-slate-500">
        只读分享；不提供编辑、路线计算或账号权限。
      </p>
    </main>
  )
}
