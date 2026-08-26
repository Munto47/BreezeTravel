'use client'

import { CheckCircle2, CircleDot, Loader2, RotateCcw, TriangleAlert } from 'lucide-react'

import type { AdviceBundle, TripCheckRun, TripCheckRunEvent, TripCheckStage } from '@/types/workspace'


interface Props {
  run: TripCheckRun
  advice: AdviceBundle | null
  events: TripCheckRunEvent[]
  busy: boolean
  onResume: () => Promise<void>
}


const stages: Array<{ stage: TripCheckStage; label: string }> = [
  { stage: 'COLLECT_EVIDENCE', label: 'Evidence' },
  { stage: 'AUDIT', label: 'Audit' },
  { stage: 'BUILD_ADVICE', label: 'Advice' },
  { stage: 'WAIT_ADOPTION', label: '采纳确认' },
  { stage: 'POSTCHECK', label: 'Postcheck' },
]


export default function TripCheckRunPanel({ run, advice, events, busy, onResume }: Props) {
  const canResume = ['FAILED', 'PARTIAL'].includes(run.status)
    || (run.status === 'RUNNING' && Boolean(run.lease_until) && new Date(run.lease_until as string) <= new Date())

  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">行程核验 Run</h2>
          <p className="mt-1 font-mono text-[10px] text-slate-500">
            {run.run_id} · config {run.config_hash.slice(0, 12)} · v{run.version}
          </p>
        </div>
        <span className={`rounded-full px-2.5 py-1 text-[11px] font-medium ${
          run.status === 'SUCCEEDED'
            ? 'bg-emerald-100 text-emerald-800'
            : run.status === 'FAILED'
              ? 'bg-rose-100 text-rose-800'
              : 'bg-sky-100 text-sky-800'
        }`}>
          {run.status} · {run.stage}
        </span>
      </div>

      <div className="mt-4 grid gap-2 sm:grid-cols-5">
        {stages.map(item => {
          const completed = run.completed_stages.includes(item.stage)
          const active = run.stage === item.stage && !completed
          return (
            <div key={item.stage} className={`rounded-xl border px-3 py-2 text-xs ${
              completed
                ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                : active
                  ? 'border-sky-200 bg-sky-50 text-sky-800'
                  : 'border-slate-200 bg-slate-50 text-slate-500'
            }`}>
              <span className="flex items-center gap-1.5">
                {completed
                  ? <CheckCircle2 className="h-3.5 w-3.5" />
                  : active && run.status === 'RUNNING'
                    ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    : <CircleDot className="h-3.5 w-3.5" />}
                {item.label}
              </span>
            </div>
          )
        })}
      </div>

      {run.partial_failures.length > 0 && (
        <div className="mt-3 rounded-xl border border-rose-200 bg-rose-50 p-3 text-xs text-rose-800">
          <div className="flex gap-2">
            <TriangleAlert className="h-4 w-4 shrink-0" />
            <span>{run.partial_failures.map(item => `${item.stage}: ${item.category}`).join('；')}</span>
          </div>
        </div>
      )}

      {canResume && (
        <button onClick={onResume} disabled={busy} className="mt-3 inline-flex items-center gap-2 rounded-xl bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40">
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
          从持久阶段恢复
        </button>
      )}

      {events.length > 0 && (
        <div className="mt-4 rounded-xl bg-slate-950 p-3 font-mono text-[10px] leading-5 text-slate-300">
          {events.slice(-6).map(event => (
            <div key={event.event_id}>#{event.event_id} {event.event_type} · {event.stage} · run v{event.run_version}</div>
          ))}
        </div>
      )}

      {advice && (
        <div className="mt-4 space-y-3">
          <div>
            <h3 className="text-xs font-semibold text-slate-800">Advice</h3>
            <p className="mt-1 text-[11px] text-slate-500">每条建议绑定 Finding、Evidence 与预期影响；没有通过门禁的修复不会伪装成可采纳操作。</p>
          </div>
          {advice.actions.length === 0 && (
            <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-800">当前报告没有非 PASS Finding。</div>
          )}
          {advice.actions.map(action => (
            <article key={action.advice_id} className="rounded-xl border border-slate-200 p-3">
              <p className="text-sm font-medium text-slate-900">{action.action}</p>
              <p className="mt-2 text-xs text-slate-600">预期影响：{action.expected_impact}</p>
              <p className="mt-1 text-xs text-amber-700">不确定性：{action.uncertainty}</p>
              <p className="mt-2 font-mono text-[10px] text-slate-400">
                finding {action.finding_id.slice(0, 8)} · evidence {action.evidence_fact_ids.length} · receipt {action.provider_receipt_ids.length}
              </p>
            </article>
          ))}
        </div>
      )}
    </section>
  )
}
