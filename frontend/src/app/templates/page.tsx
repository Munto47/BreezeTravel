'use client'

import { Suspense, useEffect, useMemo, useState } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { ArrowLeft, CalendarDays, CheckCircle2, Loader2, Route, ShieldAlert, Sparkles } from 'lucide-react'

import { api } from '@/lib/api'
import { randomUuid } from '@/lib/randomUuid'
import { useAuthStore } from '@/stores/authStore'
import type { CityRouteTemplate, TemplateApplyResponse, TripWorkspace } from '@/types/workspace'


const SUPPORTED_CITIES = ['北京', '上海', '杭州']

function todayPlus(days: number): string {
  const date = new Date()
  date.setDate(date.getDate() + days)
  return date.toISOString().slice(0, 10)
}

function addDays(start: string, days: number): string {
  const date = new Date(`${start}T00:00:00Z`)
  date.setUTCDate(date.getUTCDate() + days)
  return date.toISOString().slice(0, 10)
}

function idempotencyKey(scope: string): string {
  const key = `breeze:${scope}`
  const existing = sessionStorage.getItem(key)
  if (existing) return existing
  const created = randomUuid()
  sessionStorage.setItem(key, created)
  return created
}

/**
 * The explicit new-trip route.  Import and the previous room workflow retain
 * their own entrances; this one creates a server workspace then applies an
 * immutable route-template revision before opening the same workspace UI.
 */
function TemplateEntryContent() {
  const router = useRouter()
  const query = useSearchParams()
  const { user, isHydrated, hydrate } = useAuthStore()
  const requestedCity = query.get('city') ?? '北京'
  const requestedDays = Number(query.get('days') ?? 3)
  const [roomId, setRoomId] = useState(query.get('roomId') ?? '')
  const [city, setCity] = useState(SUPPORTED_CITIES.includes(requestedCity) ? requestedCity : '北京')
  const [days, setDays] = useState([2, 3, 4, 5].includes(requestedDays) ? requestedDays : 3)
  const [startDate, setStartDate] = useState(query.get('startDate') ?? todayPlus(7))
  const [workspaceId, setWorkspaceId] = useState(query.get('workspaceId'))
  const [templates, setTemplates] = useState<CityRouteTemplate[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => { hydrate() }, [hydrate])
  useEffect(() => {
    if (isHydrated && !user) router.replace('/login')
  }, [isHydrated, router, user])

  useEffect(() => {
    if (!user) return
    setBusy('load')
    setError(null)
    api.get<CityRouteTemplate[]>(`/api/route-templates?city=${encodeURIComponent(city)}&status=DRAFT`)
      .then(setTemplates)
      .catch(reason => {
        setTemplates([])
        setError(reason instanceof Error ? reason.message : '无法加载路线模板')
      })
      .finally(() => setBusy(current => current === 'load' ? null : current))
  }, [city, user])

  const endDate = useMemo(() => addDays(startDate, Math.max(days - 1, 0)), [days, startDate])

  if (!isHydrated || !user) return null

  const ensureWorkspace = async (): Promise<TripWorkspace> => {
    if (workspaceId) {
      return {
        workspace_id: workspaceId,
        room_id: roomId,
        city,
        trip_date_range: { start: startDate, end: endDate },
        current_itinerary_revision: null,
        current_import_id: null,
        current_report_id: null,
        current_member_constraint_revision: null,
        status: 'DRAFT',
      }
    }
    if (!roomId.trim()) throw new Error('缺少协同房间。请从首页先创建一个房间后再选择路线骨架。')
    const created = await api.post<TripWorkspace>('/api/trip-workspaces', {
      room_id: roomId.trim(), city, trip_date_range: { start: startDate, end: endDate },
    })
    setWorkspaceId(created.workspace_id)
    const params = new URLSearchParams({ roomId: roomId.trim(), city, days: String(days), startDate, workspaceId: created.workspace_id })
    router.replace(`/templates?${params.toString()}`)
    return created
  }

  const apply = async (template: CityRouteTemplate) => {
    if (template.status !== 'DRAFT') return
    setBusy(template.template_id)
    setError(null)
    try {
      const workspace = await ensureWorkspace()
      const scope = `template-apply:${workspace.workspace_id}:${template.template_id}:v${template.template_version}`
      const result = await api.postWithHeaders<TemplateApplyResponse>(
        `/api/trip-workspaces/${workspace.workspace_id}/templates/${template.template_id}/apply`,
        {},
        { 'Idempotency-Key': idempotencyKey(scope) },
      )
      // The route is only opened after the server has created (or replayed)
      // the authoritative template revision.
      router.push(`/workspace/${result.workspace_id}`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '路线骨架应用失败')
    } finally {
      setBusy(null)
    }
  }

  return (
    <main className="min-h-screen bg-slate-50 px-4 py-6 text-slate-900">
      <div className="mx-auto max-w-4xl">
        <button onClick={() => router.back()} className="mb-5 inline-flex items-center gap-1 text-sm text-slate-600 hover:text-slate-950"><ArrowLeft className="h-4 w-4" />返回</button>
        <section className="rounded-2xl border border-indigo-200 bg-indigo-50 p-5 shadow-sm">
          <div className="flex gap-3">
            <Sparkles className="mt-0.5 h-5 w-5 shrink-0 text-indigo-700" />
            <div>
              <h1 className="font-semibold">从路线骨架开始</h1>
              <p className="mt-1 text-sm leading-6 text-indigo-950">这些模板均为 GPT-5.6-sol 生成的合成 DRAFT，用于本地规划和离线校准；不是已核验 POI、真实住宿建议或人工审核路线。</p>
            </div>
          </div>
        </section>

        <section className="mt-4 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="grid gap-3 sm:grid-cols-4">
            <label className="text-xs font-medium text-slate-600">协同房间
              <input data-testid="template-room-id" value={roomId} onChange={event => setRoomId(event.target.value)} className="mt-1.5 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" placeholder="首页创建后自动带入" />
            </label>
            <label className="text-xs font-medium text-slate-600">目的地
              <select value={city} onChange={event => setCity(event.target.value)} disabled={Boolean(workspaceId)} className="mt-1.5 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-100">
                {SUPPORTED_CITIES.map(item => <option key={item}>{item}</option>)}
              </select>
            </label>
            <label className="text-xs font-medium text-slate-600">出发日期
              <input type="date" value={startDate} onChange={event => setStartDate(event.target.value)} disabled={Boolean(workspaceId)} className="mt-1.5 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-100" />
            </label>
            <label className="text-xs font-medium text-slate-600">行程天数
              <select value={days} onChange={event => setDays(Number(event.target.value))} disabled={Boolean(workspaceId)} className="mt-1.5 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-100">
                {[2, 3, 4, 5].map(item => <option key={item} value={item}>{item} 天</option>)}
              </select>
            </label>
          </div>
          <p className="mt-3 flex items-center gap-1.5 text-xs text-slate-500"><CalendarDays className="h-3.5 w-3.5" />{startDate} 至 {endDate}{workspaceId ? ' · 已创建工作台，日期与城市已冻结' : ''}</p>
        </section>

        {error && <p role="alert" className="mt-4 rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}
        <section className="mt-4">
          <div className="mb-2 flex items-center gap-2"><Route className="h-4 w-4 text-indigo-700" /><h2 className="text-sm font-semibold">{city} 模型生成路线草稿</h2></div>
          {busy === 'load' ? <p className="rounded-xl bg-white p-5 text-sm text-slate-500"><Loader2 className="mr-2 inline h-4 w-4 animate-spin" />正在读取模板…</p> : templates.length === 0 ? (
            <p className="rounded-xl border border-dashed border-slate-300 bg-white p-5 text-sm text-slate-500">当前没有可用草稿。不会用展示文案补造路线。</p>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {templates.map(template => (
                <article key={template.template_id} data-testid={`route-template-${template.template_id}`} className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
                  <div className="flex items-start justify-between gap-2"><h3 className="text-sm font-semibold">{template.name}</h3><span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-900">MODEL_GENERATED · DRAFT</span></div>
                  <p className="mt-2 text-xs text-slate-500">{template.suitable_days.join(' / ')} 天 · {template.intensity} · {template.route_zones.map(zone => zone.district || zone.zone_id).join(' → ')}</p>
                  <p className="mt-2 flex items-start gap-1 text-[11px] leading-5 text-amber-800"><ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />未经过真人审核；酒店区域分数也只在工作台中以可用/不可用证据状态展示。</p>
                  <button data-testid={`apply-template-${template.template_id}`} disabled={Boolean(busy)} onClick={() => void apply(template)} className="mt-3 flex w-full items-center justify-center gap-1.5 rounded-lg bg-slate-900 px-3 py-2 text-xs font-medium text-white disabled:opacity-40">
                    {busy === template.template_id ? <><Loader2 className="h-3.5 w-3.5 animate-spin" />应用中</> : <><CheckCircle2 className="h-3.5 w-3.5" />应用草稿并进入工作台</>}
                  </button>
                </article>
              ))}
            </div>
          )}
        </section>
      </div>
    </main>
  )
}

export default function TemplateEntryPage() {
  return (
    <Suspense fallback={<main className="flex min-h-screen items-center justify-center bg-slate-50 text-sm text-slate-500">正在打开路线骨架入口…</main>}>
      <TemplateEntryContent />
    </Suspense>
  )
}
