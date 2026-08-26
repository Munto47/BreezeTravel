'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useRouter } from 'next/navigation'
import { ArrowLeft, CircleHelp, Loader2, Map, RotateCcw, Route, ShieldCheck } from 'lucide-react'

import TimelineBoard from '@/components/workspace/TimelineBoard'
import MemberConfirmationPanel from '@/components/workspace/MemberConfirmationPanel'
import WorkspaceMapProjection from '@/components/workspace/WorkspaceMapProjection'
import PreTripRecheckDiff from '@/components/workspace/PreTripRecheckDiff'
import HotelAreaPanel from '@/components/workspace/HotelAreaPanel'
import SuggestionSetPanel from '@/components/workspace/SuggestionSetPanel'
import { useSuggestionSet } from '@/hooks/useSuggestionSet'
import { useWorkspaceCollaboration } from '@/hooks/useWorkspaceCollaboration'
import { api, ApiRequestError } from '@/lib/api'
import { applyOptimisticCommand } from '@/lib/workspaceCommands'
import { useAuthStore } from '@/stores/authStore'
import type {
  AuditFinding,
  AuditReport,
  ChangedRouteEdgeRefreshResult,
  EvidenceSnapshot,
  ItineraryPatchResult,
  ItineraryRevision,
  PreTripRecheckResult,
  RouteDelta,
  SuggestionCandidateV1,
  SuggestionIntent,
  WorkspaceEditRequest,
  WorkspaceHotelAreasResponse,
  WorkspaceMapProjection as WorkspaceMapProjectionContract,
  WorkspaceResume,
} from '@/types/workspace'


type PendingWorkspaceAction = {
  label: string
  baseRevision: number
}

type ConflictRecovery = {
  kind: 'ITINERARY_REVISION_CONFLICT' | 'CURRENT_AUDIT_REQUIRED'
  action: PendingWorkspaceAction
  expectedRevision: number | null
  actualRevision: number | null
  auditReason: string | null
  refreshed: boolean
}

function describeCommand(command: WorkspaceEditRequest): string {
  const stopId = typeof command.payload.stop_id === 'string' ? command.payload.stop_id : null
  const stopName = typeof command.payload.raw_name === 'string' ? command.payload.raw_name : null
  const labels: Record<WorkspaceEditRequest['operation'], string> = {
    ADD_STOP: `加入地点${stopName ? `「${stopName}」` : ''}`,
    MOVE_STOP: '移动地点',
    MOVE_TO_DAY: '移动到另一天',
    REORDER_STOP: '调整地点顺序',
    ADJUST_TIME: '调整停留时间',
    REPLACE_STOP: '替换地点',
    REMOVE_STOP: '删除地点',
    LOCK_STOP: '锁定地点',
    UNLOCK_STOP: '解锁地点',
    UNDO: '撤销上一版',
  }
  return `${labels[command.operation]}${stopId ? `（${stopId.slice(0, 8)}）` : ''}`
}

function recoverableConflict(reason: unknown, action: PendingWorkspaceAction): ConflictRecovery | null {
  if (!(reason instanceof ApiRequestError)) return null
  if (reason.code !== 'ITINERARY_REVISION_CONFLICT' && reason.code !== 'CURRENT_AUDIT_REQUIRED') return null
  return {
    kind: reason.code,
    action,
    expectedRevision: typeof reason.context.expected_revision === 'number'
      ? reason.context.expected_revision
      : typeof reason.context.current_revision === 'number' ? reason.context.current_revision : null,
    actualRevision: typeof reason.context.actual_revision === 'number' ? reason.context.actual_revision : null,
    auditReason: typeof reason.context.reason === 'string' ? reason.context.reason : null,
    refreshed: false,
  }
}


export default function WorkspacePage() {
  const params = useParams()
  const router = useRouter()
  const workspaceId = params.workspaceId as string
  const { user, isHydrated, hydrate } = useAuthStore()
  const [resume, setResume] = useState<WorkspaceResume | null>(null)
  const [revision, setRevision] = useState<ItineraryRevision | null>(null)
  const [mapProjection, setMapProjection] = useState<WorkspaceMapProjectionContract | null>(null)
  const [selectedStopId, setSelectedStopId] = useState<string | null>(null)
  const [selectedDay, setSelectedDay] = useState(0)
  const [routeDelta, setRouteDelta] = useState<RouteDelta | null>(null)
  const [incrementalFindings, setIncrementalFindings] = useState<AuditFinding[]>([])
  const [suggestionIntents, setSuggestionIntents] = useState<SuggestionIntent[]>(['NEARBY', 'POPULAR', 'FUN', 'FOOD'])
  const [hotelAreas, setHotelAreas] = useState<WorkspaceHotelAreasResponse | null>(null)
  const [hotelAreasLoading, setHotelAreasLoading] = useState(false)
  const [finalReport, setFinalReport] = useState<AuditReport | null>(null)
  const [evidence, setEvidence] = useState<EvidenceSnapshot | null>(null)
  const [preTripRecheck, setPreTripRecheck] = useState<PreTripRecheckResult | null>(null)
  const [confirmationMessage, setConfirmationMessage] = useState<string | null>(null)
  const [conflict, setConflict] = useState<ConflictRecovery | null>(null)
  const [busy, setBusy] = useState<string | null>('restore')
  const [error, setError] = useState<string | null>(null)
  const previousRevision = useRef<number | null>(null)
  const suggestions = useSuggestionSet(workspaceId)
  const collaboration = useWorkspaceCollaboration(
    resume?.workspace.room_id ?? null, user?.userId ?? null, user?.nickname ?? null,
  )

  const load = async () => {
    suggestions.clear()
    const restored = await api.get<WorkspaceResume>(`/api/trip-workspaces/${workspaceId}/resume`)
    setResume(restored)
    setRevision(restored.current_revision)
    setFinalReport(restored.current_report)
    setEvidence(restored.current_evidence)
    if (restored.current_report) {
      try {
        setPreTripRecheck(await api.get<PreTripRecheckResult>(
          `/api/audits/${restored.current_report.report_id}/pre-trip-recheck-result`,
        ))
      } catch (reason) {
        // Normal full audits intentionally return 404 here.  Do not give them
        // a plausible P8 label simply because they supersede an older report.
        if (!(reason instanceof ApiRequestError && reason.status === 404)) throw reason
        setPreTripRecheck(null)
      }
    } else {
      setPreTripRecheck(null)
    }
    return restored
  }

  const reloadAuthoritativeWorkspace = async () => {
    if (busy) return
    setBusy('RELOAD')
    setError(null)
    try {
      const restored = await load()
      setRouteDelta(null)
      setIncrementalFindings([])
      setPreTripRecheck(null)
      setConflict(current => current ? {
        ...current,
        actualRevision: restored.current_revision?.revision ?? null,
        refreshed: true,
      } : null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法回读服务端权威版本')
    } finally {
      setBusy(null)
    }
  }

  useEffect(() => { hydrate() }, [hydrate])
  useEffect(() => {
    if (!isHydrated) return
    if (!user) {
      router.replace('/login')
      return
    }
    setBusy('restore')
    load().catch(reason => setError(reason instanceof Error ? reason.message : '工作台加载失败')).finally(() => setBusy(null))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isHydrated, user, workspaceId])

  useEffect(() => {
    if (!revision) {
      setHotelAreas(null)
      return
    }
    let active = true
    setHotelAreasLoading(true)
    api.get<WorkspaceHotelAreasResponse>(`/api/trip-workspaces/${workspaceId}/hotel-areas`)
      .then(value => { if (active) setHotelAreas(value) })
      .catch(() => { if (active) setHotelAreas(null) })
      .finally(() => { if (active) setHotelAreasLoading(false) })
    return () => { active = false }
  }, [revision?.revision, workspaceId])

  useEffect(() => {
    if (!revision) {
      setMapProjection(null)
      return
    }
    api.get<WorkspaceMapProjectionContract>(
      `/api/trip-workspaces/${workspaceId}/revisions/${revision.revision}/map-projection`,
    ).then(setMapProjection).catch(() => setMapProjection(null))
  }, [revision?.revision, workspaceId])

  useEffect(() => {
    if (!revision || !resume) return
    collaboration.publishReferences({
      itineraryRevision: revision.revision,
      itineraryContentHash: revision.content_hash,
      auditReportId: finalReport?.report_id ?? null,
      auditRevision: finalReport?.itinerary_revision ?? null,
      memberConstraintRevision: resume.workspace.current_member_constraint_revision,
    })
  }, [collaboration.publishReferences, finalReport?.itinerary_revision, finalReport?.report_id, resume, revision])

  const selected = useMemo(() => revision?.days.flatMap(day => day.stops).find(stop => stop.stop_id === selectedStopId) ?? null, [revision, selectedStopId])
  const selectAnchor = (stopId: string) => {
    setSelectedStopId(stopId)
    const stop = revision?.days.flatMap(day => day.stops).find(item => item.stop_id === stopId)
    if (stop) setSelectedDay(stop.day_index)
    if (suggestions.suggestionSet?.insert_after_stop_id !== stopId) suggestions.clear()
  }
  // A recheck is meaningful only against the report and evidence for the
  // revision currently rendered by this workspace.  We deliberately do not
  // offer a convenient-looking button for an older full report.
  const recheckableReport = useMemo(
    () => finalReport && evidence && finalReport.itinerary_revision === revision?.revision ? finalReport : null,
    [evidence, finalReport, revision?.revision],
  )

  const envelope = () => {
    if (!revision) throw new Error('当前版本尚未加载')
    return {
      command_id: crypto.randomUUID(),
      base_revision: revision.revision,
      client_timestamp: new Date().toISOString(),
    }
  }

  const sendCommand = async (command: WorkspaceEditRequest) => {
    if (!revision || busy) return
    suggestions.clear()
    collaboration.publishEditIntent(command.operation, command.base_revision)
    const rollback = revision
    previousRevision.current = revision.revision
    setRevision(applyOptimisticCommand(revision, command))
    setBusy(command.operation)
    setError(null)
    try {
      const result = await api.postWithHeaders<ItineraryPatchResult>(
        `/api/trip-workspaces/${workspaceId}/edits`,
        command,
        { 'If-Match': String(command.base_revision), 'Idempotency-Key': command.command_id },
      )
      if (!result.new_revision) throw new Error('服务端未返回新版本')
      const current = await api.get<ItineraryRevision>(
        `/api/trip-workspaces/${workspaceId}/revisions/${result.new_revision}`,
      )
      setRevision(current)
      setRouteDelta(result.route_delta)
      setIncrementalFindings(result.incremental_findings)
      setFinalReport(null)
      setEvidence(null)
      setPreTripRecheck(null)
      setConflict(null)
    } catch (reason) {
      setRevision(rollback)
      const recovery = recoverableConflict(reason, { label: describeCommand(command), baseRevision: command.base_revision })
      if (recovery) setConflict(recovery)
      else setError(reason instanceof Error ? reason.message : '编辑未保存，已回滚预览')
    } finally {
      setBusy(null)
    }
  }

  const undo = async () => {
    if (!revision || !previousRevision.current || busy) return
    const commandId = crypto.randomUUID()
    collaboration.publishEditIntent('UNDO', revision.revision)
    setBusy('UNDO')
    suggestions.clear()
    setError(null)
    try {
      const result = await api.postWithHeaders<ItineraryPatchResult>(
        `/api/trip-workspaces/${workspaceId}/undo`,
        {
          command_id: commandId,
          base_revision: revision.revision,
          target_revision: previousRevision.current,
          client_timestamp: new Date().toISOString(),
        },
        { 'If-Match': String(revision.revision), 'Idempotency-Key': commandId },
      )
      if (result.new_revision) {
        setRevision(await api.get(`/api/trip-workspaces/${workspaceId}/revisions/${result.new_revision}`))
        setRouteDelta(result.route_delta)
        setIncrementalFindings(result.incremental_findings)
        setFinalReport(null)
        setEvidence(null)
        setPreTripRecheck(null)
      }
      previousRevision.current = null
      setConflict(null)
    } catch (reason) {
      const recovery = recoverableConflict(reason, { label: '撤销上一版', baseRevision: revision.revision })
      if (recovery) setConflict(recovery)
      else setError(reason instanceof Error ? reason.message : '撤销失败')
    } finally {
      setBusy(null)
    }
  }

  const toggleSuggestionIntent = (intent: SuggestionIntent) => {
    setSuggestionIntents(current => current.includes(intent)
      ? current.filter(item => item !== intent)
      : [...current, intent])
  }

  const createSuggestionSet = async () => {
    if (!revision || !selected || busy) return
    const day = revision.days.find(item => item.day_index === selected.day_index)
    if (!day) return
    const anchorIndex = day.stops.findIndex(item => item.stop_id === selected.stop_id)
    if (anchorIndex < 0) return
    setError(null)
    await suggestions.create({
      base_revision: revision.revision,
      day_index: selected.day_index,
      insert_after_stop_id: selected.stop_id,
      insert_before_stop_id: day.stops[anchorIndex + 1]?.stop_id ?? null,
      intents: suggestionIntents,
    })
  }

  const acceptSuggestion = async (candidate: SuggestionCandidateV1) => {
    if (!revision || busy) return
    const acceptedBaseRevision = revision.revision
    collaboration.publishEditIntent('ADD_STOP', acceptedBaseRevision)
    setBusy('ACCEPT_SUGGESTION')
    setError(null)
    try {
      const result = await suggestions.accept(candidate)
      if (!result) return

      // There is deliberately no optimistic ADD_STOP here. The returned
      // revision is already authoritative, and an explicit revision readback
      // keeps this view aligned with persistence/collaboration state.
      let current = result.revision
      try {
        current = await api.get<ItineraryRevision>(
          `/api/trip-workspaces/${workspaceId}/revisions/${result.new_revision}`,
        )
      } catch {
        setError('候选已由服务端加入，但最新 revision 回读失败；当前显示 accept 返回的权威版本。')
      }
      previousRevision.current = acceptedBaseRevision
      setRevision(current)
      setResume(value => value ? {
        ...value,
        workspace: { ...value.workspace, current_itinerary_revision: result.new_revision },
        current_revision: current,
        current_report: null,
        current_evidence: null,
      } : value)
      setSelectedStopId(result.stop_id)
      const inserted = current.days.flatMap(day => day.stops).find(stop => stop.stop_id === result.stop_id)
      if (inserted) setSelectedDay(inserted.day_index)
      setRouteDelta(null)
      setIncrementalFindings([])
      setFinalReport(null)
      setEvidence(null)
      setPreTripRecheck(null)
      setConflict(null)
    } finally {
      setBusy(null)
    }
  }

  const createFullAudit = async (): Promise<AuditReport> => {
    const report = await api.postWithHeaders<AuditReport>(
      `/api/trip-workspaces/${workspaceId}/audits`, {}, { 'Idempotency-Key': crypto.randomUUID() },
    )
    setFinalReport(report)
    setEvidence(await api.get<EvidenceSnapshot>(`/api/audits/${report.report_id}/evidence`))
    setPreTripRecheck(null)
    return report
  }

  const runFullAudit = async () => {
    if (!revision || busy) return
    setBusy('AUDIT')
    setError(null)
    setConfirmationMessage(null)
    try {
      await createFullAudit()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '完整审计失败')
    } finally {
      setBusy(null)
    }
  }

  const runPreTripRecheck = async () => {
    if (!recheckableReport || busy) return
    setBusy('PRE_TRIP_RECHECK')
    setError(null)
    setConfirmationMessage(null)
    try {
      const result = await api.postWithHeaders<PreTripRecheckResult>(
        `/api/audits/${recheckableReport.report_id}/pre-trip-recheck`,
        {},
        { 'Idempotency-Key': crypto.randomUUID() },
      )

      // Re-read the workspace's authoritative references.  This matters when
      // another collaborator writes while this local recheck is in flight:
      // the diff stays visible as its immutable historical result, while the
      // active header/Yjs references follow the server's current revision.
      const restored = await load()
      setPreTripRecheck(result)
      if (restored.current_report?.report_id !== result.report.report_id) {
        setError('复检结果已保存，但协同工作台随后出现了更新；当前引用已切换到服务端最新版本。')
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '临行复检失败')
    } finally {
      setBusy(null)
    }
  }

  const refreshChangedRouteEdges = async () => {
    if (!revision || !routeDelta?.async_route_refresh_required || busy) return
    setBusy('ROUTE_EDGE_REFRESH')
    setError(null)
    setConfirmationMessage(null)
    try {
      const result = await api.postWithHeaders<ChangedRouteEdgeRefreshResult>(
        `/api/trip-workspaces/${workspaceId}/revisions/${revision.revision}/changed-route-edges/refresh`,
        {},
        { 'Idempotency-Key': crypto.randomUUID() },
      )
      // This is a new immutable audit bundle for the current revision.  The
      // historical pre-edit report remains untouched on the server.
      setRouteDelta(result.route_delta)
      setFinalReport(result.report)
      setEvidence(result.evidence_snapshot)
      setPreTripRecheck(null)
    } catch (reason) {
      const recovery = recoverableConflict(reason, { label: '刷新变更路线边证据', baseRevision: revision.revision })
      if (recovery) setConflict(recovery)
      else setError(reason instanceof Error ? reason.message : '路线证据刷新失败')
    } finally {
      setBusy(null)
    }
  }

  const confirmAfterFullAudit = async () => {
    if (!revision || busy) return
    setBusy('CONFIRM')
    setError(null)
    setConfirmationMessage(null)
    try {
      // Confirmation is deliberately a two-step server contract: persist a
      // full audit for this revision, then let /confirm re-check its immutable
      // report binding under the command transaction lock.
      // Always create a fresh report immediately before confirmation.  A
      // previously displayed report can be stale after a member constraint or
      // task-spec update even when this browser's itinerary revision is same.
      const report = await createFullAudit()
      const commandId = crypto.randomUUID()
      const result = await api.postWithHeaders<ItineraryPatchResult>(
        `/api/trip-workspaces/${workspaceId}/confirm`,
        {
          command_id: commandId,
          base_revision: revision.revision,
          client_timestamp: new Date().toISOString(),
        },
        { 'If-Match': String(revision.revision), 'Idempotency-Key': commandId },
      )
      if (!result.new_revision) throw new Error('服务端未返回确认版本')
      await load()
      setRouteDelta(null)
      setIncrementalFindings([])
      setPreTripRecheck(null)
      setConfirmationMessage(`已依据完整审计报告 ${report.report_id.slice(0, 8)} 确认行程。`)
      setConflict(null)
    } catch (reason) {
      const recovery = recoverableConflict(reason, { label: '审计后确认', baseRevision: revision.revision })
      if (recovery) setConflict(recovery)
      else setError(reason instanceof Error ? reason.message : '最终确认失败')
    } finally {
      setBusy(null)
    }
  }

  if (!revision || !resume) return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50 text-sm text-slate-500">
      {error ?? <><Loader2 className="mr-2 h-4 w-4 animate-spin" />正在恢复服务端工作台…</>}
    </main>
  )

  return (
    <main className="min-h-screen bg-slate-50">
      <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/95 px-4 py-3 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3">
          <button onClick={() => router.back()} className="flex items-center gap-1 text-sm text-slate-600"><ArrowLeft className="h-4 w-4" />返回</button>
          <div className="h-5 w-px bg-slate-200" />
          <div>
            <h1 className="font-semibold text-slate-900">{resume.workspace.city} 行程工作台</h1>
            <p className="text-xs text-slate-500">服务端 revision {revision.revision} · 编辑过程不调用 LLM</p>
            <p className="mt-0.5 text-[11px] text-slate-400">
              协同引用 {collaboration.connected ? '已连接' : '未连接'}
              {collaboration.refs.itineraryRevision && collaboration.refs.itineraryRevision !== revision.revision
                ? ` · 其他端已引用 revision ${collaboration.refs.itineraryRevision}，保存前会由服务端校验冲突`
                : ''}
            </p>
          </div>
          <div className="ml-auto flex gap-2">
            <button disabled={!previousRevision.current || !!busy} onClick={undo} className="flex items-center gap-1 rounded-xl border bg-white px-3 py-2 text-xs disabled:opacity-40"><RotateCcw className="h-3.5 w-3.5" />撤销</button>
            <button disabled={!!busy} onClick={runFullAudit} className="flex items-center gap-1 rounded-xl bg-slate-900 px-3 py-2 text-xs text-white disabled:opacity-40"><ShieldCheck className="h-3.5 w-3.5" />最终完整审计</button>
            {routeDelta?.async_route_refresh_required && <button disabled={!!busy} onClick={refreshChangedRouteEdges} className="flex items-center gap-1 rounded-xl border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 disabled:opacity-40"><Route className="h-3.5 w-3.5" />刷新变更路线边</button>}
            {recheckableReport && <button disabled={!!busy} onClick={runPreTripRecheck} className="flex items-center gap-1 rounded-xl border border-indigo-300 bg-indigo-50 px-3 py-2 text-xs text-indigo-800 disabled:opacity-40"><ShieldCheck className="h-3.5 w-3.5" />临行复检（本地）</button>}
            <button disabled={!!busy} onClick={confirmAfterFullAudit} className="flex items-center gap-1 rounded-xl bg-emerald-700 px-3 py-2 text-xs text-white disabled:opacity-40"><ShieldCheck className="h-3.5 w-3.5" />审计后确认</button>
          </div>
        </div>
      </header>

      <div className="mx-auto grid max-w-7xl gap-4 px-4 py-5 lg:grid-cols-[260px_minmax(0,1fr)_320px]">
        <aside className="space-y-4">
          <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
            <div className="flex items-center gap-2"><Map className="h-4 w-4 text-blue-600" /><h2 className="text-sm font-semibold">地图与路线联动</h2></div>
            <p className="mt-2 text-xs text-slate-500">时间轴和地图都只读取当前服务端 revision 的坐标投影。</p>
            {selected ? (
              <div className="mt-3 rounded-xl bg-blue-50 p-3 text-sm text-blue-900">
                <p className="font-medium">{selected.raw_name || selected.place_id}</p>
                <p className="mt-1 text-xs">第 {selected.day_index + 1} 天 · 顺序 {selected.order_index + 1}</p>
              </div>
            ) : <p className="mt-3 text-xs text-slate-400">尚未选择地点</p>}
            <WorkspaceMapProjection
              projection={mapProjection}
              selectedStopId={selectedStopId}
              onSelectStop={selectAnchor}
            />
            <div className="mt-4 space-y-2">
              {revision.days.map(day => (
                <button key={day.day_index} onClick={() => setSelectedDay(day.day_index)} className={`block w-full rounded-lg px-3 py-2 text-left text-xs ${selectedDay === day.day_index ? 'bg-slate-900 text-white' : 'bg-slate-100 text-slate-700'}`}>
                  Day {day.day_index + 1}: {day.stops.map(stop => stop.raw_name || stop.place_id).join(' → ') || '空'}
                </button>
              ))}
            </div>
            {collaboration.intents.filter(intent => intent.userId !== user?.userId).length > 0 && (
              <p className="mt-3 rounded-lg border border-blue-200 bg-blue-50 p-2 text-xs text-blue-800">
                其他成员正在提交：{collaboration.intents.filter(intent => intent.userId !== user?.userId).map(intent => `${intent.userId} · ${intent.operation}`).join('；')}。权威结果仍以服务端 revision 为准。
              </p>
            )}
          </section>
        </aside>

        <TimelineBoard revision={revision} selectedStopId={selectedStopId} onSelectStop={selectAnchor} onCommand={sendCommand} commandEnvelope={envelope} busy={!!busy} />

        <aside className="space-y-4">
          {conflict && (
            <ConflictRecoveryPanel
              conflict={conflict}
              onReload={() => void reloadAuthoritativeWorkspace()}
              disabled={!!busy}
            />
          )}
          <MemberConfirmationPanel
            workspaceId={workspaceId}
            itineraryRevision={revision.revision}
            memberConstraintRevision={resume.workspace.current_member_constraint_revision}
            currentUserId={user!.userId}
            disabled={!!busy}
            onConstraintSaved={async () => { await load() }}
          />
          <RouteDeltaPanel delta={routeDelta} />
          <HotelAreaPanel result={hotelAreas} loading={hotelAreasLoading} />
          <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
            <h2 className="text-sm font-semibold text-slate-900">增量检查</h2>
            <p className="mt-1 text-xs text-slate-500">只检查受影响依赖，不冒充完整报告。</p>
            <div className="mt-3 space-y-2">
              {incrementalFindings.length === 0 ? <p className="text-xs text-slate-400">编辑后显示受影响规则结论</p> : incrementalFindings.map(finding => (
                <div key={finding.finding_id} className="rounded-lg border border-slate-200 p-2 text-xs">
                  <p className="font-medium text-slate-800">{finding.reason_code}</p><p className="mt-1 text-slate-600">{finding.message}</p>
                </div>
              ))}
            </div>
          </section>
          <SuggestionSetPanel
            anchor={selected}
            suggestionSet={suggestions.suggestionSet}
            intents={suggestionIntents}
            pending={suggestions.pending}
            message={suggestions.message}
            onToggleIntent={toggleSuggestionIntent}
            onCreate={() => void createSuggestionSet()}
            onClose={suggestions.close}
            onAccept={candidate => void acceptSuggestion(candidate)}
          />
          {finalReport && (
            <section className="rounded-2xl border border-emerald-200 bg-emerald-50 p-4 text-xs text-emerald-900">
              <p className="font-semibold">完整审计已完成</p>
              <p className="mt-1">revision {finalReport.itinerary_revision} · {finalReport.overall_status}</p>
              {evidence?.provider_failures.length ? <p className="mt-2">外部证据不可用：{evidence.provider_failures.map(item => item.provider).join('、')}</p> : null}
            </section>
          )}
          {preTripRecheck && <PreTripRecheckDiff result={preTripRecheck} />}
          {confirmationMessage && <p className="rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-800">{confirmationMessage}</p>}
        </aside>
      </div>
      {busy && <div className="fixed bottom-4 left-1/2 flex -translate-x-1/2 items-center gap-2 rounded-full bg-slate-900 px-4 py-2 text-xs text-white shadow-lg"><Loader2 className="h-3.5 w-3.5 animate-spin" />正在保存 {busy}</div>}
      {error && <div className="fixed bottom-4 right-4 max-w-sm rounded-xl bg-rose-700 px-4 py-3 text-sm text-white shadow-lg">{error}</div>}
    </main>
  )
}


function ConflictRecoveryPanel({
  conflict,
  onReload,
  disabled,
}: {
  conflict: ConflictRecovery
  onReload: () => void
  disabled: boolean
}) {
  const isAudit = conflict.kind === 'CURRENT_AUDIT_REQUIRED'
  return (
    <section data-testid="workspace-conflict-recovery" className="rounded-2xl border border-amber-300 bg-amber-50 p-4 text-xs text-amber-950 shadow-sm">
      <h2 className="font-semibold">{isAudit ? '完整审计已不再适用于当前状态' : '检测到其他端已更新行程'}</h2>
      <p className="mt-1 leading-5">
        {isAudit
          ? '服务端拒绝用旧审计确认行程。不会替你复用或覆盖现有审计结果。'
          : '服务端拒绝了基于旧版本的编辑。不会自动合并或覆盖其他成员的变更。'}
      </p>
      <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 rounded-lg bg-white/70 p-2">
        <dt className="text-amber-800">本次操作</dt><dd className="text-right font-medium">{conflict.action.label}</dd>
        <dt className="text-amber-800">提交时版本</dt><dd className="text-right font-mono">{conflict.action.baseRevision}</dd>
        {conflict.expectedRevision !== null && <><dt className="text-amber-800">服务端期望</dt><dd className="text-right font-mono">{conflict.expectedRevision}</dd></>}
        {conflict.actualRevision !== null && <><dt className="text-amber-800">服务端当前</dt><dd className="text-right font-mono">{conflict.actualRevision}</dd></>}
        {conflict.auditReason && <><dt className="text-amber-800">审计失效原因</dt><dd className="text-right font-mono">{conflict.auditReason}</dd></>}
      </dl>
      <p className="mt-3 leading-5">
        {conflict.refreshed
          ? '已回读服务端权威版本。本次本地预览已丢弃且不会重放；请在当前时间轴核对后，使用正常编辑控件重新提交。'
          : '本地乐观预览已回滚，服务端没有写入本次操作。请先主动回读权威版本，再决定是否用正常控件重新提交。'}
      </p>
      {isAudit && conflict.refreshed && <p className="mt-2 font-medium">回读后需要重新执行“最终完整审计”，再进行确认。</p>}
      <button
        data-testid="reload-authoritative-workspace"
        disabled={disabled}
        onClick={onReload}
        className="mt-3 rounded-lg border border-amber-400 bg-white px-3 py-2 font-medium disabled:opacity-40"
      >
        {conflict.refreshed ? '再次回读服务端版本' : '回读服务端权威版本'}
      </button>
    </section>
  )
}


function RouteDeltaPanel({ delta }: { delta: RouteDelta | null }) {
  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center gap-2"><Route className="h-4 w-4 text-indigo-600" /><h2 className="text-sm font-semibold">路线变化</h2></div>
      {!delta ? <p className="mt-3 text-xs text-slate-400">编辑后显示受影响路线段</p> : (
        <div className="mt-3 text-xs">
          {delta.status === 'AVAILABLE' ? (
            <p className="rounded-lg bg-emerald-50 p-2 text-emerald-800">路线缓存可用：{delta.delta_minutes === 0 ? '通勤不变' : `${delta.delta_minutes! > 0 ? '+' : ''}${delta.delta_minutes} 分钟`}</p>
          ) : (
            <div className="rounded-lg border border-amber-200 bg-amber-50 p-2 text-amber-800">
              <p className="flex items-center gap-1 font-medium"><CircleHelp className="h-3.5 w-3.5" />路线证据{delta.status === 'PARTIAL' ? '部分' : ''}不可用</p>
              <p className="mt-1">缺少 {delta.missing_edge_ids.length} 条新路线边，不估算通勤变化。</p>
            </div>
          )}
          {(delta.day_end_times ?? []).map(item => <p key={item.day_index} className="mt-2 text-slate-600">第 {item.day_index + 1} 天结束：{item.previous_end_time ?? '?'} → {item.current_end_time ?? '?'}</p>)}
          {delta.scope === 'CURRENT_REVISION_CHANGED_EDGES_ONLY' && <p className="mt-2 text-slate-500">仅刷新本 revision 新增或替换的路线边；未改动边没有重新调用 Provider。</p>}
        </div>
      )}
    </section>
  )
}
