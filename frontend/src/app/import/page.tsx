'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { ArrowLeft, FileSearch, Loader2, ShieldCheck } from 'lucide-react'

import AuditDrawer from '@/components/workspace/AuditDrawer'
import ImportResolutionPanel from '@/components/workspace/ImportResolutionPanel'
import PreTripRecheckDiff from '@/components/workspace/PreTripRecheckDiff'
import RepairCompare from '@/components/workspace/RepairCompare'
import { api } from '@/lib/api'
import { useAuthStore } from '@/stores/authStore'
import type {
  AuditReport,
  EvidenceSnapshot,
  FinalTipsArtifact,
  ItineraryImport,
  ItineraryRevision,
  RepairApplyResult,
  RepairOption,
  PreTripRecheckResult,
  TripWorkspace,
  WorkspaceResume,
} from '@/types/workspace'


function todayPlus(days: number): string {
  const value = new Date()
  value.setDate(value.getDate() + days)
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}


function addDays(start: string, days: number): string {
  const value = new Date(`${start}T00:00:00Z`)
  value.setUTCDate(value.getUTCDate() + days)
  return value.toISOString().slice(0, 10)
}


export default function ImportItineraryPage() {
  const router = useRouter()
  const { user, isHydrated, hydrate } = useAuthStore()
  const [roomId, setRoomId] = useState('')
  const [city, setCity] = useState('北京')
  const [days, setDays] = useState(3)
  const [startDate, setStartDate] = useState(todayPlus(7))
  const [sourceType, setSourceType] = useState<'AI_TEXT' | 'MANUAL_TEXT'>('AI_TEXT')
  const [rawText, setRawText] = useState('')
  const [workspace, setWorkspace] = useState<TripWorkspace | null>(null)
  const [itineraryImport, setItineraryImport] = useState<ItineraryImport | null>(null)
  const [revision, setRevision] = useState<ItineraryRevision | null>(null)
  const [report, setReport] = useState<AuditReport | null>(null)
  const [evidence, setEvidence] = useState<EvidenceSnapshot | null>(null)
  const [repairOptions, setRepairOptions] = useState<RepairOption[]>([])
  const [finalTips, setFinalTips] = useState<FinalTipsArtifact | null>(null)
  const [preTripRecheck, setPreTripRecheck] = useState<PreTripRecheckResult | null>(null)
  const [tipsNotice, setTipsNotice] = useState<string | null>(null)
  const [selections, setSelections] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const idempotencyKeys = useRef(new Map<string, { fingerprint: string; key: string }>())

  const commandKey = (scope: string, payload: unknown): string => {
    const fingerprint = JSON.stringify(payload)
    const existing = idempotencyKeys.current.get(scope)
    if (existing?.fingerprint === fingerprint) return existing.key
    const key = crypto.randomUUID()
    idempotencyKeys.current.set(scope, { fingerprint, key })
    return key
  }

  const completeCommand = (scope: string) => {
    idempotencyKeys.current.delete(scope)
  }

  const updateResumeUrl = (values: { workspaceId?: string; importId?: string }) => {
    const params = new URLSearchParams(window.location.search)
    if (values.workspaceId) params.set('workspaceId', values.workspaceId)
    if (values.importId) params.set('importId', values.importId)
    window.history.replaceState(null, '', `${window.location.pathname}?${params.toString()}`)
  }

  useEffect(() => { hydrate() }, [hydrate])
  useEffect(() => {
    if (!isHydrated) return
    if (!user) {
      router.replace('/login')
      return
    }
    const restore = async () => {
      const params = new URLSearchParams(window.location.search)
      const requestedRoomId = params.get('roomId') ?? ''
      const requestedWorkspaceId = params.get('workspaceId')
      const requestedImportId = params.get('importId')
      setRoomId(requestedRoomId)
      const requestedCity = params.get('city')
      if (requestedCity && ['北京', '上海', '杭州'].includes(requestedCity)) setCity(requestedCity)
      const requestedDays = Number(params.get('days'))
      if (requestedDays >= 2 && requestedDays <= 5) setDays(requestedDays)
      if (!requestedWorkspaceId) return

      setBusy('restore')
      setError(null)
      try {
        const resumed = await api.get<WorkspaceResume>(
          `/api/trip-workspaces/${requestedWorkspaceId}/resume`,
        )
        const restoredWorkspace = resumed.workspace
        setWorkspace(restoredWorkspace)
        setRoomId(restoredWorkspace.room_id)
        setCity(restoredWorkspace.city)
        setStartDate(restoredWorkspace.trip_date_range.start)
        const start = new Date(`${restoredWorkspace.trip_date_range.start}T00:00:00`)
        const end = new Date(`${restoredWorkspace.trip_date_range.end}T00:00:00`)
        setDays(Math.round((end.getTime() - start.getTime()) / 86_400_000) + 1)

        let restoredImport = resumed.current_import
        if (!restoredImport && requestedImportId) {
          restoredImport = await api.get<ItineraryImport>(
            `/api/trip-workspaces/${requestedWorkspaceId}/imports/${requestedImportId}`,
          )
        }
        if (restoredImport) {
          setItineraryImport(restoredImport)
          setRawText(restoredImport.raw_text)
        }
        setRevision(resumed.current_revision)
        setReport(resumed.current_report)
        setEvidence(resumed.current_evidence)
        setRepairOptions([
          ...resumed.proposed_repairs,
          ...(resumed.applied_repair ? [resumed.applied_repair] : []),
        ])
        setFinalTips(resumed.current_tips)
        setTipsNotice(
          resumed.tips_state === 'INELIGIBLE'
            ? 'Tips 尚未生成：当前报告仍有 BLOCKER/HIGH 风险'
            : null,
        )
        if (resumed.current_import) {
          updateResumeUrl({
            workspaceId: requestedWorkspaceId,
            importId: resumed.current_import.import_id,
          })
        }
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught))
      } finally {
        setBusy(null)
      }
    }
    void restore()
  }, [isHydrated, router, user])

  const unresolved = useMemo(() => (
    itineraryImport?.resolutions.filter(item => ['AMBIGUOUS', 'NOT_FOUND'].includes(item.resolution_status)) ?? []
  ), [itineraryImport])

  const run = async <T,>(label: string, action: () => Promise<T>): Promise<T | null> => {
    setBusy(label)
    setError(null)
    try {
      return await action()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
      return null
    } finally {
      setBusy(null)
    }
  }

  const ensureWorkspace = async (): Promise<TripWorkspace> => {
    if (workspace) return workspace
    if (!roomId) throw new Error('缺少房间引用，请从首页的“导入已有行程”入口进入')
    const created = await api.post<TripWorkspace>('/api/trip-workspaces', {
      room_id: roomId,
      city,
      trip_date_range: { start: startDate, end: addDays(startDate, days - 1) },
    })
    setWorkspace(created)
    updateResumeUrl({ workspaceId: created.workspace_id })
    return created
  }

  const createImport = async () => {
    if (!rawText.trim()) {
      setError('请先粘贴行程原文')
      return
    }
    await run('import', async () => {
      const target = await ensureWorkspace()
      const body = { source_type: sourceType, raw_text: rawText }
      const scope = `create-import:${target.workspace_id}`
      const created = await api.postWithHeaders<ItineraryImport>(
        `/api/trip-workspaces/${target.workspace_id}/imports`,
        body,
        { 'Idempotency-Key': commandKey(scope, body) },
      )
      completeCommand(scope)
      setItineraryImport(created)
      setSelections({})
      updateResumeUrl({ workspaceId: target.workspace_id, importId: created.import_id })
      return created
    })
  }

  const confirmResolutions = async () => {
    if (!workspace || !itineraryImport) return
    const confirmations = unresolved
      .map(item => ({ raw_stop_id: item.raw_stop_id, place_id: selections[item.raw_stop_id] }))
      .filter(item => Boolean(item.place_id))
    if (confirmations.length !== unresolved.length) {
      setError('仍有低置信度或未匹配地点没有可靠选择，不能静默应用')
      return
    }
    await run('confirm', async () => {
      const updated = await api.patchWithHeaders<ItineraryImport>(
        `/api/trip-workspaces/${workspace.workspace_id}/imports/${itineraryImport.import_id}/resolutions`,
        { confirmations },
        { 'If-Match': `"${itineraryImport.state_version}"` },
      )
      setItineraryImport(updated)
      return updated
    })
  }

  const searchCandidates = async (rawStopId: string, query: string) => {
    if (!workspace || !itineraryImport) return
    await run(`search:${rawStopId}`, async () => {
      const updated = await api.postWithHeaders<ItineraryImport>(
        (
          `/api/trip-workspaces/${workspace.workspace_id}/imports/${itineraryImport.import_id}`
          + `/raw-stops/${rawStopId}/candidates:search`
        ),
        { query },
        { 'If-Match': `"${itineraryImport.state_version}"` },
      )
      setItineraryImport(updated)
      setSelections(current => {
        const next = { ...current }
        delete next[rawStopId]
        return next
      })
      return updated
    })
  }

  const loadAudit = async (auditReport: AuditReport) => {
    const snapshot = await api.get<EvidenceSnapshot>(`/api/audits/${auditReport.report_id}/evidence`)
    setReport(auditReport)
    setEvidence(snapshot)
    setPreTripRecheck(null)
    setFinalTips(null)
    setTipsNotice(null)
    return auditReport
  }

  const generateFinalTips = async (auditReport: AuditReport) => {
    try {
      const artifact = await api.postWithHeaders<FinalTipsArtifact>(
        `/api/audits/${auditReport.report_id}/tips`,
        {},
        { 'Idempotency-Key': commandKey(`tips:${auditReport.report_id}`, {}) },
      )
      completeCommand(`tips:${auditReport.report_id}`)
      setFinalTips(artifact)
      setTipsNotice(null)
      return artifact
    } catch (caught) {
      setFinalTips(null)
      setTipsNotice(
        `Tips 尚未生成：${caught instanceof Error ? caught.message : String(caught)}`,
      )
      return null
    }
  }

  const applyImportAndAudit = async () => {
    if (!workspace || !itineraryImport) return
    await run('audit', async () => {
      const applyScope = `apply-import:${itineraryImport.import_id}`
      const applyBody = { state_version: itineraryImport.state_version }
      const applied = await api.postWithHeaders<{ itinerary_import: ItineraryImport; revision: ItineraryRevision }>(
        `/api/trip-workspaces/${workspace.workspace_id}/imports/${itineraryImport.import_id}/apply`,
        {},
        {
          'If-Match': `"${itineraryImport.state_version}"`,
          'Idempotency-Key': commandKey(applyScope, applyBody),
        },
      )
      completeCommand(applyScope)
      setItineraryImport(applied.itinerary_import)
      setRevision(applied.revision)
      const auditScope = `create-audit:${workspace.workspace_id}:${applied.revision.revision}`
      const auditReport = await api.postWithHeaders<AuditReport>(
        `/api/trip-workspaces/${workspace.workspace_id}/audits`,
        {},
        { 'Idempotency-Key': commandKey(auditScope, {}) },
      )
      completeCommand(auditScope)
      await loadAudit(auditReport)
      return auditReport
    })
  }

  const proposeRepairs = async () => {
    if (!report) return
    await run('propose', async () => {
      const scope = `propose-repairs:${report.report_id}`
      const options = await api.postWithHeaders<RepairOption[]>(
        `/api/audits/${report.report_id}/repairs`,
        {},
        { 'Idempotency-Key': commandKey(scope, {}) },
      )
      completeCommand(scope)
      setRepairOptions(options)
      return options
    })
  }

  const runPreTripRecheck = async () => {
    if (!report) return
    await run('pre-trip-recheck', async () => {
      const scope = `pre-trip-recheck:${report.report_id}`
      const result = await api.postWithHeaders<PreTripRecheckResult>(
        `/api/audits/${report.report_id}/pre-trip-recheck`,
        {},
        { 'Idempotency-Key': commandKey(scope, {}) },
      )
      completeCommand(scope)
      setPreTripRecheck(result)
      setReport(result.report)
      setEvidence(result.evidence_snapshot)
      setFinalTips(null)
      setTipsNotice(null)
      return result
    })
  }

  const applyRepair = async (option: RepairOption) => {
    await run(option.repair_id, async () => {
      const scope = `apply-repair:${option.repair_id}`
      const body = { base_revision: option.base_itinerary_revision }
      const result = await api.postWithHeaders<RepairApplyResult>(
        `/api/audits/${option.source_report_id}/repairs/${option.repair_id}/apply`,
        body,
        {
          'If-Match': `"${option.base_itinerary_revision}"`,
          'Idempotency-Key': commandKey(scope, body),
        },
      )
      completeCommand(scope)
      setRevision(option.result_preview)
      const postcheck = await api.get<AuditReport>(`/api/audits/${result.postcheck_report_id}`)
      await loadAudit(postcheck)
      await generateFinalTips(postcheck)
      setRepairOptions(current => current.map(item => (
        item.repair_id === option.repair_id
          ? result.repair
          : { ...item, status: item.status === 'PROPOSED' ? 'STALE' : item.status }
      )))
      return result
    })
  }

  const rejectRepair = async (option: RepairOption, reason: string) => {
    await run(option.repair_id, async () => {
      const rejected = await api.post<RepairOption>(
        `/api/audits/${option.source_report_id}/repairs/${option.repair_id}/reject`,
        { reason },
      )
      setRepairOptions(current => current.map(item => item.repair_id === option.repair_id ? rejected : item))
      return rejected
    })
  }

  if (!isHydrated || !user) return null

  return (
    <main className="min-h-screen bg-slate-50">
      <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/90 px-4 py-3 backdrop-blur">
        <div className="mx-auto flex max-w-5xl items-center gap-3">
          <button onClick={() => router.push('/')} className="inline-flex items-center gap-1 text-sm text-slate-500 hover:text-coral-500">
            <ArrowLeft className="h-4 w-4" /> 返回
          </button>
          <div className="h-4 w-px bg-slate-200" />
          <FileSearch className="h-4 w-4 text-coral-500" />
          <h1 className="text-sm font-semibold text-slate-900">导入 → 消歧 → 排雷 → Repair</h1>
          {workspace && <span className="ml-auto font-mono text-[10px] text-slate-400">workspace {workspace.workspace_id.slice(0, 8)}</span>}
        </div>
      </header>

      <div className="mx-auto max-w-5xl space-y-5 px-4 py-6">
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex items-start gap-3">
            <div className="rounded-xl bg-coral-50 p-2 text-coral-600"><ShieldCheck className="h-5 w-5" /></div>
            <div>
              <h2 className="font-semibold text-slate-900">把已有行程变成可验证 revision</h2>
              <p className="mt-1 text-xs text-slate-500">当前范围仅北京、上海、杭州，2–5 天。解析文本只作为不可信数据，不会改变系统规则或事实来源。</p>
            </div>
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-4">
            <select value={city} onChange={event => setCity(event.target.value)} disabled={Boolean(workspace)} className="rounded-xl border border-slate-200 px-3 py-2 text-sm">
              {['北京', '上海', '杭州'].map(item => <option key={item}>{item}</option>)}
            </select>
            <select value={days} onChange={event => setDays(Number(event.target.value))} disabled={Boolean(workspace)} className="rounded-xl border border-slate-200 px-3 py-2 text-sm">
              {[2, 3, 4, 5].map(item => <option key={item} value={item}>{item} 天</option>)}
            </select>
            <input type="date" value={startDate} onChange={event => setStartDate(event.target.value)} disabled={Boolean(workspace)} className="rounded-xl border border-slate-200 px-3 py-2 text-sm" />
            <select value={sourceType} onChange={event => setSourceType(event.target.value as 'AI_TEXT' | 'MANUAL_TEXT')} className="rounded-xl border border-slate-200 px-3 py-2 text-sm">
              <option value="AI_TEXT">AI 行程文本</option>
              <option value="MANUAL_TEXT">手写行程文本</option>
            </select>
          </div>
          <textarea
            value={rawText}
            onChange={event => setRawText(event.target.value)}
            disabled={Boolean(itineraryImport && itineraryImport.status !== 'FAILED')}
            placeholder={'示例：\nDay 1 北京\n09:00-12:00 故宫（已预约，不可移动）\n11:00-13:00 景山公园'}
            maxLength={12000}
            className="mt-3 min-h-52 w-full rounded-2xl border border-slate-200 p-4 text-sm leading-6 outline-none focus:border-coral-400 disabled:bg-slate-50"
          />
          {(!itineraryImport || itineraryImport.status === 'FAILED') && (
            <button onClick={createImport} disabled={busy !== null} className="mt-3 inline-flex items-center gap-2 rounded-xl bg-coral-500 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50">
              {busy === 'import' && <Loader2 className="h-4 w-4 animate-spin" />}
              {itineraryImport?.status === 'FAILED' ? '按修改后的原文重新解析' : '解析并生成 POI 候选'}
            </button>
          )}
          {itineraryImport?.status === 'FAILED' && (
            <p className="mt-2 text-xs text-amber-700">失败草稿和原文已保存；重新解析会创建新的导入记录，不覆盖旧记录。</p>
          )}
        </section>

        {error && <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div>}

        {itineraryImport && (
          <>
            <ImportResolutionPanel
              itineraryImport={itineraryImport}
              selections={selections}
              onSelect={(rawStopId, placeId) => setSelections(current => ({ ...current, [rawStopId]: placeId }))}
              onSearchCandidates={searchCandidates}
              searchingRawStopId={busy?.startsWith('search:') ? busy.slice('search:'.length) : null}
            />
            {unresolved.length > 0 && itineraryImport.status !== 'READY' && (
              <button onClick={confirmResolutions} disabled={busy !== null} className="rounded-xl bg-slate-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50">
                确认全部低置信度地点
              </button>
            )}
            {itineraryImport.status === 'READY' && !revision && (
              <button onClick={applyImportAndAudit} disabled={busy !== null} className="rounded-xl bg-slate-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50">
                {busy === 'audit' ? '创建 revision 并审计中…' : '应用为 revision 1 并运行完整审计'}
              </button>
            )}
          </>
        )}

        {revision && (
          <section className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white p-4 text-xs text-slate-600">
            <span>权威行程 revision {revision.revision} · content hash <span className="font-mono">{revision.content_hash}</span></span>
            {workspace && <button onClick={() => router.push(`/workspace/${workspace.workspace_id}`)} className="ml-auto rounded-lg bg-slate-900 px-3 py-2 font-medium text-white">打开时间轴工作台</button>}
          </section>
        )}
        {report && (
          <AuditDrawer
            report={report}
            evidence={evidence}
            onProposeRepairs={proposeRepairs}
            proposing={busy === 'propose'}
            onPreTripRecheck={runPreTripRecheck}
            rechecking={busy === 'pre-trip-recheck'}
          />
        )}
        {preTripRecheck && <PreTripRecheckDiff result={preTripRecheck} />}
        {repairOptions.length > 0 && <RepairCompare options={repairOptions} busyRepairId={busy} onApply={applyRepair} onReject={rejectRepair} />}
        {tipsNotice && (
          <section className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-xs text-amber-800">
            {tipsNotice}。系统不会在 BLOCKER/HIGH 尚未处理时提前生成提示。
          </section>
        )}
        {finalTips && (
          <section className="rounded-2xl border border-emerald-200 bg-emerald-50 p-5">
            <div className="flex items-center justify-between gap-3">
              <h2 className="text-sm font-semibold text-emerald-950">最终 revision 的出行提示</h2>
              <span className="font-mono text-[10px] text-emerald-700">
                revision {finalTips.itinerary_revision} · report {finalTips.report_id.slice(0, 8)}
              </span>
            </div>
            <div className="mt-3 space-y-3">
              {finalTips.itinerary.days.map(day => (
                <div key={day.day_index}>
                  <p className="text-xs font-medium text-emerald-900">第 {day.day_index + 1} 天</p>
                  {day.slots.flatMap(slot => slot.tips.map((tip, index) => (
                    <p key={`${slot.place_id}-${index}`} className="mt-1 text-xs leading-5 text-emerald-800">
                      {slot.place.name || slot.place_id}：{tip}
                    </p>
                  )))}
                </div>
              ))}
            </div>
            <p className="mt-3 break-all font-mono text-[10px] text-emerald-700">
              basis {finalTips.basis_content_hash}
            </p>
          </section>
        )}
      </div>
    </main>
  )
}
