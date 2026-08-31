'use client'

import { useEffect, useMemo, useState } from 'react'
import Link from 'next/link'
import { useParams } from 'next/navigation'
import { CheckCircle2, Loader2, LockKeyhole, ShieldAlert } from 'lucide-react'

import { api, ApiRequestError } from '@/lib/api'
import { randomUuid } from '@/lib/randomUuid'
import { useAuthStore } from '@/stores/authStore'
import type { SharedWorkspaceView } from '@/types/workspace'
import type { ShareProjectionView } from '@/lib/trip-understanding-v3'

type ConstraintForm = {
  type: string
  value: string
  hardness: 'HARD' | 'SOFT'
}

function errorMessage(reason: unknown) {
  if (reason instanceof ApiRequestError) {
    if (reason.code === 'SHARE_LINK_UNAVAILABLE') return '此链接不存在、已过期或已被撤销。'
    if (reason.code === 'RESOURCE_SCOPE_DENIED') return '此链接不授予当前账号此项能力，或仅限指定接收成员使用。'
  }
  return reason instanceof Error ? reason.message : '读取受限分享失败。'
}

function statusClass(status: string) {
  if (status === 'SATISFIED') return 'bg-emerald-100 text-emerald-800'
  if (status === 'VIOLATED') return 'bg-rose-100 text-rose-800'
  return 'bg-amber-100 text-amber-800'
}

export default function SharedItineraryPage() {
  const params = useParams()
  const token = typeof params.token === 'string' ? params.token : ''
  const { user, isHydrated, hydrate } = useAuthStore()
  const [shared, setShared] = useState<SharedWorkspaceView | null>(null)
  const [modernShare, setModernShare] = useState<ShareProjectionView | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<'acknowledge' | 'constraint' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [constraint, setConstraint] = useState<ConstraintForm>({
    type: 'latest_return_time', value: '20:30', hardness: 'HARD',
  })

  const scopes = useMemo(() => new Set(shared?.scopes ?? []), [shared?.scopes])
  const canAcknowledge = scopes.has('ACKNOWLEDGE') && !!user && !shared?.acknowledgement.acknowledged
  const canWriteConstraint = scopes.has('CONSTRAINT_WRITE') && !!user && !!shared?.constraint_write_context

  const load = async () => {
    if (!token) {
      setError('链接格式无效。')
      setLoading(false)
      return
    }
    setLoading(true)
    setError(null)
    try {
      setShared(await api.get<SharedWorkspaceView>(`/api/share/${encodeURIComponent(token)}`))
    } catch (reason) {
      setShared(null)
      setError(errorMessage(reason))
    } finally {
      setLoading(false)
    }
  }

  const loadModernOrLegacy = async () => {
    if (!token) {
      setError('链接格式无效。')
      setLoading(false)
      return
    }
    setLoading(true)
    setError(null)
    const secret = window.location.hash.startsWith('#s=')
      ? window.location.hash.slice(3)
      : ''
    if (secret) {
      // Remove the fragment before the first network request. The secret is
      // exchanged only in the request body and never becomes a URL, Referer,
      // log or analytics value.
      window.history.replaceState(null, '', window.location.pathname + window.location.search)
      const exchange = await fetch(`/api/v3/shares/${encodeURIComponent(token)}/exchange`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ secret }),
      }).catch(() => null)
      if (!exchange?.ok) {
        setError('此链接不存在、已过期或已被撤销。')
        setLoading(false)
        return
      }
    }
    const modern = await fetch(`/api/v3/shares/${encodeURIComponent(token)}`, {
      credentials: 'include',
      cache: 'no-store',
    }).catch(() => null)
    if (modern?.ok) {
      setModernShare(await modern.json() as ShareProjectionView)
      setLoading(false)
      return
    }
    if (secret) {
      setError('此链接不存在、已过期或已被撤销。')
      setLoading(false)
      return
    }
    await load()
  }

  useEffect(() => { hydrate() }, [hydrate])
  useEffect(() => { if (isHydrated) void loadModernOrLegacy() }, [isHydrated, token])

  const acknowledge = async () => {
    if (!canAcknowledge || busy) return
    setBusy('acknowledge')
    setError(null)
    setNotice(null)
    try {
      await api.post(`/api/share/${encodeURIComponent(token)}/responses`, { action: 'ACKNOWLEDGE' })
      await load()
      setNotice('已记录对当前锁定版本的确认。')
    } catch (reason) {
      setError(errorMessage(reason))
    } finally {
      setBusy(null)
    }
  }

  const submitConstraint = async () => {
    if (!canWriteConstraint || busy || !user || !shared?.constraint_write_context) return
    const type = constraint.type.trim()
    const value = constraint.value.trim()
    if (!type || !value) {
      setError('请填写约束类型和值。')
      return
    }
    setBusy('constraint')
    setError(null)
    setNotice(null)
    try {
      await api.post(`/api/share/${encodeURIComponent(token)}/responses`, {
        action: 'CONSTRAINT',
        expected_base_revision: shared.constraint_write_context.expected_base_revision,
        constraint: {
          constraint_id: randomUuid(),
          owner_member_id: user.userId,
          type,
          operator: 'EQ',
          value,
          hardness: constraint.hardness,
          priority: constraint.hardness === 'HARD' ? 100 : 50,
          source: 'MEMBER_EXPLICIT',
          confirmation_status: constraint.hardness === 'HARD' ? 'CONFIRMED' : 'PENDING',
          waivable_by: [],
        },
      })
      await load()
      setNotice('已提交本人约束。它会进入服务端版本控制，可能使旧审计需要重新执行。')
    } catch (reason) {
      setError(errorMessage(reason))
    } finally {
      setBusy(null)
    }
  }

  if (!isHydrated || loading) {
    return <main className="mx-auto flex min-h-screen max-w-3xl items-center justify-center p-6 text-sm text-slate-500"><Loader2 className="mr-2 h-4 w-4 animate-spin" />读取受限行程…</main>
  }

  if (modernShare) {
    return (
      <main className="mx-auto min-h-screen max-w-3xl bg-slate-50 p-4 sm:p-8" data-testid="g06-shared-trip">
        <section className="rounded-2xl border border-emerald-200 bg-white p-5 shadow-sm">
          <div className="flex items-start gap-3"><LockKeyhole className="mt-0.5 h-5 w-5 shrink-0 text-emerald-700" /><div><h1 className="text-lg font-semibold text-slate-900">{modernShare.title}</h1><p className="mt-1 text-sm text-slate-600">{modernShare.message}</p></div></div>
          <div className="mt-4 grid gap-2 text-xs text-slate-600 sm:grid-cols-3"><p>目的地：{modernShare.destination}</p><p>时间：{modernShare.schedule}</p><p>出行：{modernShare.party_size}</p></div>
          {modernShare.accommodation ? <p className="mt-3 rounded-xl bg-emerald-50 px-3 py-2 text-xs text-emerald-900">住宿：{modernShare.accommodation}</p> : null}
        </section>
        <section className="mt-4 space-y-3" aria-label="只读行程">{modernShare.days.map((day) => <article key={day.label} className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"><h2 className="font-semibold text-slate-900">{day.label}</h2>{day.activities.length === 0 ? <p className="mt-3 text-sm text-slate-500">当天暂无地点</p> : <ol className="mt-3 space-y-2">{day.activities.map((activity, index) => <li key={`${day.label}-${index}`} className="rounded-xl bg-slate-50 p-3"><p className="text-sm font-medium text-slate-900">{activity.name}</p><p className="mt-1 text-xs text-slate-600">{activity.time_hint ?? '时间待定'} · {activity.area_or_address}</p><p className="mt-1 text-[11px] text-slate-500">{activity.note}</p></li>)}</ol>}</article>)}</section>
        <p className="mt-4 text-center text-xs text-slate-500">只读分享；不提供编辑、路线计算或账号权限。</p>
      </main>
    )
  }

  if (!shared) {
    return <main className="mx-auto flex min-h-screen max-w-xl items-center p-6"><section className="w-full rounded-2xl border border-rose-200 bg-white p-6 shadow-sm"><div className="flex gap-3"><ShieldAlert className="h-5 w-5 shrink-0 text-rose-700" /><div><h1 className="font-semibold text-slate-900">无法访问此受限分享</h1><p role="alert" className="mt-2 text-sm text-slate-600">{error ?? '链接不可用。'}</p>{!user && <Link className="mt-4 inline-block rounded-lg bg-slate-900 px-3 py-2 text-sm text-white" href="/login">登录指定账号后重试</Link>}</div></div></section></main>
  }

  return (
    <main className="mx-auto min-h-screen max-w-4xl bg-slate-50 p-4 sm:p-8">
      <section className="rounded-2xl border border-indigo-200 bg-white p-5 shadow-sm">
        <div className="flex items-start gap-3">
          <LockKeyhole className="mt-0.5 h-5 w-5 shrink-0 text-indigo-700" />
          <div><h1 className="text-lg font-semibold text-slate-900">受限行程分享</h1><p className="mt-1 text-sm text-slate-600">这是锁定的 r{shared.itinerary.revision}，不是工作台入口；不提供地点编辑、排线或工作区权限。</p></div>
        </div>
        <div className="mt-4 grid gap-2 text-xs text-slate-600 sm:grid-cols-3"><p>城市：{shared.itinerary.city}</p><p>日期：{shared.itinerary.trip_start_date} 至 {shared.itinerary.trip_end_date}</p><p>版本摘要：{shared.itinerary.content_hash.slice(0, 12)}</p></div>
      </section>

      <section className="mt-4 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <h2 className="font-semibold text-slate-900">锁定行程</h2>
        <div className="mt-3 space-y-3">{shared.itinerary.days.map(day => <article key={day.day_index} className="rounded-xl bg-slate-50 p-3"><h3 className="text-sm font-medium text-slate-800">第 {day.day_index + 1} 天{day.date ? ` · ${day.date}` : ''}</h3>{day.stops.length === 0 ? <p className="mt-2 text-xs text-slate-400">暂无地点</p> : <ol className="mt-2 space-y-2">{day.stops.map(stop => <li key={stop.stop_id} className="rounded-lg border border-slate-200 bg-white p-2 text-sm"><div className="flex gap-2"><span className="text-xs text-slate-400">{stop.order_index + 1}</span><div><p className="font-medium text-slate-800">{stop.raw_name ?? stop.place_id}{stop.locked ? <span className="ml-2 rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] text-indigo-800">已锁定</span> : null}</p><p className="mt-0.5 text-xs text-slate-500">{stop.start_time ?? '时间待定'}{stop.end_time ? `–${stop.end_time}` : ''}{stop.fixed_commitment ? ' · 固定安排' : ''}</p>{stop.notes ? <p className="mt-1 text-xs text-slate-600">{stop.notes}</p> : null}</div></div></li>)}</ol>}</article>)}</div>
      </section>

      <section className="mt-4 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"><h2 className="font-semibold text-slate-900">审计摘要</h2>{shared.report ? <><div className="mt-3 flex flex-wrap items-center gap-2 text-sm"><span className={`rounded-full px-2 py-1 text-xs font-medium ${statusClass(shared.report.overall_status)}`}>{shared.report.overall_status}</span><span className="text-slate-500">规则集 {shared.report.audit_rule_set_version} · {shared.report.findings.length} 项发现</span></div>{shared.report.findings.length === 0 ? <p className="mt-3 text-sm text-slate-500">当前报告没有发现项。</p> : <ul className="mt-3 space-y-2">{shared.report.findings.map(finding => <li key={finding.finding_id} className="rounded-xl border border-slate-200 p-3 text-sm"><div className="flex flex-wrap gap-2"><span className={`rounded px-1.5 py-0.5 text-[10px] ${statusClass(finding.status)}`}>{finding.status}</span><span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-700">{finding.severity}</span><span className="text-xs text-slate-500">{finding.rule_id}</span></div><p className="mt-2 text-slate-800">{finding.message}</p>{finding.confirmation_action ? <p className="mt-1 text-xs text-slate-500">建议动作：{finding.confirmation_action}</p> : null}</li>)}</ul>}</> : <p className="mt-3 text-sm text-slate-500">签发时没有绑定可展示的审计报告。</p>}</section>

      <section className="mt-4 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"><h2 className="font-semibold text-slate-900">仅限此链接授予的操作</h2>{shared.recipient_bound && !user ? <p className="mt-2 text-sm text-amber-700">此链接绑定了指定成员。请用指定账号登录后才能确认或写入本人约束。</p> : null}{scopes.has('ACKNOWLEDGE') && <div className="mt-3 rounded-xl bg-slate-50 p-3"><p className="text-sm text-slate-700">当前版本确认：{shared.acknowledgement.acknowledged ? `已于 ${shared.acknowledgement.acknowledged_at ?? '服务端记录时间'} 确认` : '待确认'}</p>{!shared.acknowledgement.acknowledged && <button onClick={() => void acknowledge()} disabled={!canAcknowledge || !!busy} className="mt-2 inline-flex items-center gap-1 rounded-lg bg-emerald-700 px-3 py-2 text-xs font-medium text-white disabled:opacity-40">{busy === 'acknowledge' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}确认锁定版本</button>}</div>}{scopes.has('CONSTRAINT_WRITE') && <div className="mt-3 rounded-xl bg-slate-50 p-3"><p className="text-sm text-slate-700">写入我的约束（仅当前指定成员；不会取得工作台编辑权）。</p><div className="mt-2 grid gap-2 sm:grid-cols-3"><input aria-label="约束类型" value={constraint.type} onChange={event => setConstraint(current => ({ ...current, type: event.target.value }))} disabled={!canWriteConstraint || !!busy} className="rounded-lg border border-slate-200 px-2 py-1.5 text-xs" /><input aria-label="约束值" value={constraint.value} onChange={event => setConstraint(current => ({ ...current, value: event.target.value }))} disabled={!canWriteConstraint || !!busy} className="rounded-lg border border-slate-200 px-2 py-1.5 text-xs" /><select aria-label="约束强度" value={constraint.hardness} onChange={event => setConstraint(current => ({ ...current, hardness: event.target.value as ConstraintForm['hardness'] }))} disabled={!canWriteConstraint || !!busy} className="rounded-lg border border-slate-200 px-2 py-1.5 text-xs"><option value="HARD">HARD（本人确认）</option><option value="SOFT">SOFT（偏好）</option></select></div><button onClick={() => void submitConstraint()} disabled={!canWriteConstraint || !!busy} className="mt-2 rounded-lg bg-slate-900 px-3 py-2 text-xs font-medium text-white disabled:opacity-40">{busy === 'constraint' ? '提交中…' : '提交本人约束'}</button></div>}{!scopes.has('ACKNOWLEDGE') && !scopes.has('CONSTRAINT_WRITE') ? <p className="mt-2 text-sm text-slate-500">此链接仅允许阅读。</p> : null}</section>
      {notice ? <p className="mt-4 rounded-xl bg-emerald-50 p-3 text-sm text-emerald-800">{notice}</p> : null}
      {error ? <p role="alert" className="mt-4 rounded-xl bg-rose-50 p-3 text-sm text-rose-800">{error}</p> : null}
    </main>
  )
}
