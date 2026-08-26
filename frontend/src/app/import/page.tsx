'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { ArrowLeft, FileImage, FileSearch, Loader2, ShieldCheck, Trash2 } from 'lucide-react'

import AuditDrawer from '@/components/workspace/AuditDrawer'
import ImportResolutionPanel from '@/components/workspace/ImportResolutionPanel'
import PreTripRecheckDiff from '@/components/workspace/PreTripRecheckDiff'
import RepairCompare from '@/components/workspace/RepairCompare'
import TripBriefConfirmationPanel from '@/components/workspace/TripBriefConfirmationPanel'
import TripCheckRunPanel from '@/components/workspace/TripCheckRunPanel'
import { api } from '@/lib/api'
import { useAuthStore } from '@/stores/authStore'
import type {
  AuditReport,
  AdviceBundle,
  EvidenceSnapshot,
  FinalTipsArtifact,
  ItineraryImport,
  ItineraryRevision,
  RepairApplyResult,
  RepairOption,
  RunSpec,
  ScreenshotImportResult,
  PreTripRecheckResult,
  TripBriefRevision,
  TripCheckRun,
  TripCheckRunEvent,
  TripWorkspace,
  WorkspaceResume,
} from '@/types/workspace'


const P1_DATASET_HASH = '18322d96f3bc2e3315be6f6c0b38842d2dbc9eb81a270ea60d4a1e72824385f4'
const CONTROLLED_SNAPSHOT_HASH = '3307e65a4134b2659d79ea0b9bdea42586e93122593d16ddb73df5a2db1bcf47'


function controlledRunSpec(): RunSpec {
  return {
    schema_version: 'trip-check-run-spec-v1',
    commit_sha: process.env.NEXT_PUBLIC_TRIP_CHECK_COMMIT_SHA || 'eb65f7a',
    prompt_version: 'none-p1',
    model_version: 'none-p1',
    provider_version: 'controlled-fixture-v1',
    rule_set_version: 'audit-v1',
    execution_mode: 'fixture',
    dataset_hash: process.env.NEXT_PUBLIC_TRIP_CHECK_DATASET_HASH || P1_DATASET_HASH,
    snapshot_hash: process.env.NEXT_PUBLIC_TRIP_CHECK_SNAPSHOT_HASH || CONTROLLED_SNAPSHOT_HASH,
    fault_profile: 'none',
    random_seed: 7,
    budget: {
      max_tokens: 0,
      max_provider_queries: 0,
      max_retries: 1,
      timeout_seconds: 30,
      max_cost_usd: 0,
    },
  }
}


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
  const [inputMode, setInputMode] = useState<'TEXT' | 'SCREENSHOT'>('TEXT')
  const [screenshots, setScreenshots] = useState<File[]>([])
  const [screenshotResult, setScreenshotResult] = useState<ScreenshotImportResult | null>(null)
  const [workspace, setWorkspace] = useState<TripWorkspace | null>(null)
  const [itineraryImport, setItineraryImport] = useState<ItineraryImport | null>(null)
  const [brief, setBrief] = useState<TripBriefRevision | null>(null)
  const [revision, setRevision] = useState<ItineraryRevision | null>(null)
  const [tripCheckRun, setTripCheckRun] = useState<TripCheckRun | null>(null)
  const [runEvents, setRunEvents] = useState<TripCheckRunEvent[]>([])
  const [advice, setAdvice] = useState<AdviceBundle | null>(null)
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
  const lastRunEventId = useRef(0)

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

  const applyResumeState = (resumed: WorkspaceResume) => {
    const restoredWorkspace = resumed.workspace
    setWorkspace(restoredWorkspace)
    setRoomId(restoredWorkspace.room_id)
    setCity(restoredWorkspace.city)
    setStartDate(restoredWorkspace.trip_date_range.start)
    const start = new Date(`${restoredWorkspace.trip_date_range.start}T00:00:00`)
    const end = new Date(`${restoredWorkspace.trip_date_range.end}T00:00:00`)
    setDays(Math.round((end.getTime() - start.getTime()) / 86_400_000) + 1)
    setItineraryImport(resumed.current_import)
    if (resumed.current_import) setRawText(resumed.current_import.raw_text)
    setBrief(resumed.current_brief)
    setRevision(resumed.current_revision)
    setTripCheckRun(resumed.current_trip_check_run)
    setAdvice(resumed.current_advice)
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
  }

  const loadRunArtifacts = async (current: TripCheckRun) => {
    setTripCheckRun(current)
    if (!current.report_id) return
    const [auditReport, snapshot] = await Promise.all([
      api.get<AuditReport>(`/api/audits/${current.report_id}`),
      api.get<EvidenceSnapshot>(`/api/audits/${current.report_id}/evidence`),
    ])
    setReport(auditReport)
    setEvidence(snapshot)
    // A completed Run points at the postcheck report while its immutable
    // Advice bundle remains bound to the source report through lineage.
    // Resume state already returns that source Advice and the applied Repair;
    // do not query either resource with the postcheck report id.
    if (current.stage === 'POSTCHECK') return
    const options = await api.get<RepairOption[]>(`/api/audits/${current.report_id}/repairs`)
    setRepairOptions(options)
    if (current.advice_bundle_id) {
      const bundle = await api.get<AdviceBundle>(
        `/api/trip-workspaces/${current.workspace_id}/reports/${current.report_id}/advice`,
      )
      setAdvice(bundle)
    }
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
        applyResumeState(resumed)

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

  useEffect(() => {
    const runId = tripCheckRun?.run_id
    if (!runId) return
    const cursorKey = `trip-check-last-event:${runId}`
    const storedCursor = Number(sessionStorage.getItem(cursorKey) ?? '0')
    if (Number.isInteger(storedCursor) && storedCursor > lastRunEventId.current) {
      lastRunEventId.current = storedCursor
    }
    const controller = new AbortController()
    let stopped = false

    const watch = async () => {
      while (!stopped) {
        try {
          await api.streamEvents<TripCheckRunEvent>(
            `/api/trip-check-runs/${runId}/events`,
            {
              lastEventId: lastRunEventId.current ? String(lastRunEventId.current) : null,
              signal: controller.signal,
              onEvent: message => {
                const event = message.data
                if (event.event_id <= lastRunEventId.current) return
                lastRunEventId.current = event.event_id
                sessionStorage.setItem(cursorKey, String(event.event_id))
                setRunEvents(current => (
                  current.some(item => item.event_id === event.event_id)
                    ? current
                    : [...current, event].slice(-20)
                ))
              },
            },
          )
          if (stopped) return
          const current = await api.get<TripCheckRun>(`/api/trip-check-runs/${runId}`)
          await loadRunArtifacts(current)
          const terminal = ['SUCCEEDED', 'FAILED', 'PRIVACY_BLOCKED', 'CANCELLED'].includes(current.status)
          const waitingForAdoption = current.status === 'WAITING' && current.stage === 'WAIT_ADOPTION'
          if (terminal || waitingForAdoption) return
        } catch (caught) {
          if (controller.signal.aborted) return
          setError(`Run 事件流暂时中断，正在从事件 ${lastRunEventId.current} 重连：${caught instanceof Error ? caught.message : String(caught)}`)
        }
        await new Promise(resolve => window.setTimeout(resolve, 600))
      }
    }
    void watch()
    return () => {
      stopped = true
      controller.abort()
    }
    // Reconnect only when the authoritative run identity changes. Run state is
    // refreshed inside the stream loop to keep Last-Event-ID monotonic.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tripCheckRun?.run_id])

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
      setSelections({})
      updateResumeUrl({ workspaceId: target.workspace_id, importId: created.import_id })
      const resumed = await api.get<WorkspaceResume>(
        `/api/trip-workspaces/${target.workspace_id}/resume`,
      )
      applyResumeState(resumed)
      return created
    })
  }

  const createScreenshotImport = async () => {
    if (screenshots.length < 1 || screenshots.length > 6) {
      setError('请选择 1～6 张截图')
      return
    }
    const oversized = screenshots.find(file => file.size > 10 * 1024 * 1024)
    if (oversized) {
      setError(`${oversized.name} 超过 10MB，未上传`)
      return
    }
    await run('screenshot-import', async () => {
      const target = await ensureWorkspace()
      const form = new FormData()
      screenshots.forEach(file => form.append('screenshots', file, file.name))
      const fingerprint = screenshots.map(file => `${file.name}:${file.size}:${file.lastModified}`)
      const scope = `create-screenshot-import:${target.workspace_id}`
      const created = await api.postFormWithHeaders<ScreenshotImportResult>(
        `/api/trip-workspaces/${target.workspace_id}/imports/screenshots`,
        form,
        { 'Idempotency-Key': commandKey(scope, fingerprint) },
      )
      completeCommand(scope)
      setScreenshotResult(created)
      setItineraryImport(created.itinerary_import)
      setRawText(created.itinerary_import.raw_text)
      setSelections({})
      updateResumeUrl({ workspaceId: target.workspace_id, importId: created.itinerary_import.import_id })
      const resumed = await api.get<WorkspaceResume>(
        `/api/trip-workspaces/${target.workspace_id}/resume`,
      )
      applyResumeState(resumed)
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

  const patchBrief = async (updates: Record<string, unknown>) => {
    if (!workspace || !brief || Object.keys(updates).length === 0) return
    await run('brief-patch', async () => {
      const scope = `patch-brief:${brief.brief_id}:${brief.revision}`
      const body = { updates }
      const updated = await api.patchWithHeaders<TripBriefRevision>(
        `/api/trip-workspaces/${workspace.workspace_id}/trip-briefs/${brief.revision}`,
        body,
        {
          'If-Match': `"${brief.revision}"`,
          'Idempotency-Key': commandKey(scope, body),
        },
      )
      completeCommand(scope)
      setBrief(updated)
      return updated
    })
  }

  const confirmBrief = async () => {
    if (!workspace || !brief) return
    await run('brief-confirm', async () => {
      const scope = `confirm-brief:${brief.brief_id}:${brief.revision}`
      const confirmed = await api.postWithHeaders<TripBriefRevision>(
        `/api/trip-workspaces/${workspace.workspace_id}/trip-briefs/${brief.revision}/confirm`,
        {},
        {
          'If-Match': `"${brief.revision}"`,
          'Idempotency-Key': commandKey(scope, {}),
        },
      )
      completeCommand(scope)
      setBrief(confirmed)
      return confirmed
    })
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

  const createTripCheckRun = async (
    targetRevision: ItineraryRevision,
    confirmedBrief: TripBriefRevision,
  ) => {
    if (!workspace) return null
    const runSpec = controlledRunSpec()
    const body = {
      itinerary_revision: targetRevision.revision,
      brief_revision: confirmedBrief.revision,
      run_spec: runSpec,
    }
    const scope = `create-trip-check:${workspace.workspace_id}:${targetRevision.revision}:${confirmedBrief.revision}`
    const created = await api.postWithHeaders<TripCheckRun>(
      `/api/trip-workspaces/${workspace.workspace_id}/trip-check-runs`,
      body,
      { 'Idempotency-Key': commandKey(scope, body) },
    )
    completeCommand(scope)
    setTripCheckRun(created)
    setRunEvents([])
    setAdvice(null)
    setReport(null)
    setEvidence(null)
    setRepairOptions([])
    return created
  }

  const applyImportAndRun = async () => {
    if (!workspace || !itineraryImport || !brief) return
    if (brief.status !== 'CONFIRMED') {
      setError('TripBrief 尚未确认，不能进入权威核验')
      return
    }
    await run('trip-check', async () => {
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
      return createTripCheckRun(applied.revision, brief)
    })
  }

  const startTripCheckForCurrentRevision = async () => {
    if (!revision || !brief || brief.status !== 'CONFIRMED') return
    await run('trip-check', () => createTripCheckRun(revision, brief))
  }

  const resumeTripCheck = async () => {
    if (!tripCheckRun) return
    await run('resume-run', async () => {
      const body = { config_hash: tripCheckRun.config_hash }
      const scope = `resume-trip-check:${tripCheckRun.run_id}:${tripCheckRun.version}`
      const resumed = await api.postWithHeaders<TripCheckRun>(
        `/api/trip-check-runs/${tripCheckRun.run_id}/resume`,
        body,
        {
          'If-Match': `"${tripCheckRun.version}"`,
          'Idempotency-Key': commandKey(scope, body),
        },
      )
      completeCommand(scope)
      setTripCheckRun(resumed)
      return resumed
    })
  }

  const proposeRepairs = async () => {
    if (!report) return
    await run('propose', async () => {
      if (advice) {
        const existing = await api.get<RepairOption[]>(`/api/audits/${report.report_id}/repairs`)
        setRepairOptions(existing)
        return existing
      }
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
      setReport(null)
      setEvidence(null)
      const postcheck = await api.get<AuditReport>(`/api/audits/${result.postcheck_report_id}`)
      await loadAudit(postcheck)
      if (tripCheckRun) {
        const completedRun = await api.get<TripCheckRun>(`/api/trip-check-runs/${tripCheckRun.run_id}`)
        setTripCheckRun(completedRun)
      }
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
          <h1 className="text-sm font-semibold text-slate-900">文本 → Brief → Evidence → Audit → Repair → Postcheck</h1>
          {workspace && <span className="ml-auto font-mono text-[10px] text-slate-400">workspace {workspace.workspace_id.slice(0, 8)}</span>}
        </div>
      </header>

      <div className="mx-auto max-w-5xl space-y-5 px-4 py-6">
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex items-start gap-3">
            <div className="rounded-xl bg-coral-50 p-2 text-coral-600"><ShieldCheck className="h-5 w-5" /></div>
            <div>
              <h2 className="font-semibold text-slate-900">把已有行程变成可验证 revision</h2>
              <p className="mt-1 text-xs text-slate-500">当前范围仅北京、上海、杭州，2–5 天。文本和 OCR 结果都只是不可信输入，必须确认后才能进入权威行程。</p>
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
          {!itineraryImport && (
            <div className="mt-4 inline-flex rounded-xl bg-slate-100 p-1 text-xs font-medium">
              <button type="button" onClick={() => setInputMode('TEXT')} className={`rounded-lg px-3 py-2 ${inputMode === 'TEXT' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500'}`}>粘贴文本</button>
              <button type="button" onClick={() => setInputMode('SCREENSHOT')} className={`rounded-lg px-3 py-2 ${inputMode === 'SCREENSHOT' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500'}`}>上传截图</button>
            </div>
          )}
          {inputMode === 'TEXT' || itineraryImport ? (
            <>
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
            </>
          ) : (
            <div className="mt-3 rounded-2xl border border-dashed border-slate-300 bg-slate-50 p-4">
              <label className="flex cursor-pointer items-center gap-3 text-sm font-medium text-slate-700">
                <FileImage className="h-5 w-5 text-coral-500" />
                选择 PNG、JPEG 或 WebP（最多 6 张，每张不超过 10MB）
                <input
                  aria-label="选择行程截图"
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  multiple
                  className="sr-only"
                  onChange={event => setScreenshots(Array.from(event.target.files ?? []).slice(0, 6))}
                />
              </label>
              {screenshots.length > 0 && (
                <div className="mt-3 space-y-2">
                  {screenshots.map(file => (
                    <div key={`${file.name}-${file.lastModified}`} className="flex items-center gap-2 rounded-lg bg-white px-3 py-2 text-xs text-slate-600">
                      <span className="min-w-0 flex-1 truncate">{file.name} · {(file.size / 1024).toFixed(1)}KB</span>
                      <button type="button" aria-label={`移除 ${file.name}`} onClick={() => setScreenshots(current => current.filter(item => item !== file))}><Trash2 className="h-4 w-4" /></button>
                    </div>
                  ))}
                </div>
              )}
              <button onClick={createScreenshotImport} disabled={busy !== null || screenshots.length === 0} className="mt-3 inline-flex items-center gap-2 rounded-xl bg-coral-500 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50">
                {busy === 'screenshot-import' && <Loader2 className="h-4 w-4 animate-spin" />}
                OCR 识别并生成待确认草稿
              </button>
              <p className="mt-2 text-xs text-slate-500">服务端仅暂存原图用于 OCR；成功、失败或超时后都会删除原图，只保留 hash、文本框、置信度和清理回执。</p>
            </div>
          )}
          {itineraryImport?.status === 'FAILED' && (
            <p className="mt-2 text-xs text-amber-700">失败草稿和原文已保存；重新解析会创建新的导入记录，不覆盖旧记录。</p>
          )}
        </section>

        {screenshotResult && (
          <section aria-label="OCR 与隐私回执" className="rounded-2xl border border-emerald-200 bg-emerald-50 p-5 text-xs text-emerald-950">
            <h2 className="text-sm font-semibold">OCR 与隐私回执</h2>
            <p className="mt-1">识别 {screenshotResult.ocr_receipts.length} 张截图；原图清理 {screenshotResult.cleanup_receipts.filter(item => item.cleanup_status === 'DELETED').length}/{screenshotResult.cleanup_receipts.length}。</p>
            <div className="mt-3 space-y-2">
              {screenshotResult.ocr_receipts.map((receipt, index) => (
                <div key={receipt.asset_id} className="rounded-xl bg-white/80 p-3">
                  <p>截图 {index + 1} · {receipt.engine} {receipt.engine_version} · hash {receipt.asset_hash.slice(0, 12)}…</p>
                  <p className="mt-1 text-amber-800">{receipt.lines.some(line => line.requires_confirmation) ? '含低置信度字段，必须人工确认' : 'OCR 字段仍需在 Brief 和地点步骤确认'}</p>
                </div>
              ))}
            </div>
          </section>
        )}

        {error && <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div>}

        {brief && (
          <TripBriefConfirmationPanel
            brief={brief}
            busy={busy === 'brief-patch' || busy === 'brief-confirm'}
            onPatch={patchBrief}
            onConfirm={confirmBrief}
          />
        )}

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
              <button onClick={applyImportAndRun} disabled={busy !== null || brief?.status !== 'CONFIRMED'} className="rounded-xl bg-slate-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50">
                {busy === 'trip-check' ? '创建 revision 并启动核验…' : '应用为 revision 1 并启动行程核验'}
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
        {revision && brief?.status === 'CONFIRMED' && (
          !tripCheckRun
          || tripCheckRun.itinerary_revision !== revision.revision
          || tripCheckRun.brief_revision !== brief.revision
        ) && (
          <button onClick={startTripCheckForCurrentRevision} disabled={busy !== null} className="rounded-xl bg-slate-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50">
            {busy === 'trip-check' ? '启动核验中…' : `为 revision ${revision.revision} 启动行程核验`}
          </button>
        )}
        {tripCheckRun && (
          <TripCheckRunPanel
            run={tripCheckRun}
            advice={advice}
            events={runEvents}
            busy={busy === 'resume-run'}
            onResume={resumeTripCheck}
          />
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
