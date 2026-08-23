'use client'

import { AlertOctagon, AlertTriangle, CheckCircle2, CircleHelp, ExternalLink } from 'lucide-react'

import type { AuditFinding, AuditReport, EvidenceSnapshot } from '@/types/workspace'


interface Props {
  report: AuditReport
  evidence: EvidenceSnapshot | null
  onProposeRepairs: () => void
  proposing: boolean
  onPreTripRecheck?: () => void
  rechecking?: boolean
}

const groups: Array<{
  title: string
  filter: (finding: AuditFinding) => boolean
  tone: string
}> = [
  {
    title: '必须修改',
    filter: finding => finding.status === 'VIOLATED' && ['BLOCKER', 'HIGH'].includes(finding.severity),
    tone: 'border-rose-200 bg-rose-50/60',
  },
  {
    title: '建议调整',
    filter: finding => finding.status === 'VIOLATED' && !['BLOCKER', 'HIGH'].includes(finding.severity),
    tone: 'border-amber-200 bg-amber-50/60',
  },
  {
    title: '待确认',
    filter: finding => finding.status === 'UNKNOWN',
    tone: 'border-sky-200 bg-sky-50/60',
  },
]


export default function AuditDrawer({
  report, evidence, onProposeRepairs, proposing, onPreTripRecheck, rechecking = false,
}: Props) {
  const facts = new Map((evidence?.facts ?? []).map(item => [item.fact_id, item]))
  const repairableHigh = report.findings.some(
    item => item.repairable && item.status === 'VIOLATED' && ['BLOCKER', 'HIGH'].includes(item.severity),
  )
  const providerFacts = (evidence?.facts ?? []).filter(item => (
    ['ROUTE_TIME', 'WEATHER', 'RISK_SOURCE'].includes(item.fact_type)
    || item.subject_type === 'ROUTE_OPTION'
  ))

  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold text-slate-900">风险报告</h2>
          <p className="mt-1 text-xs text-slate-500">revision {report.itinerary_revision} · 报告 {report.report_id.slice(0, 8)}</p>
        </div>
        <div className="flex items-center gap-2">
          <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${
            report.overall_status === 'SATISFIED'
              ? 'bg-emerald-50 text-emerald-700'
              : report.overall_status === 'UNKNOWN'
                ? 'bg-sky-50 text-sky-700'
                : 'bg-rose-50 text-rose-700'
          }`}>
            {report.overall_status}
          </span>
          {repairableHigh && (
            <button
              onClick={onProposeRepairs}
              disabled={proposing}
              className="rounded-xl bg-slate-900 px-3 py-2 text-xs font-medium text-white disabled:opacity-50"
            >
              {proposing ? '完整复验候选中…' : '生成 Repair A/B'}
            </button>
          )}
          {onPreTripRecheck && (
            <button
              onClick={onPreTripRecheck}
              disabled={proposing || rechecking}
              className="rounded-xl border border-slate-300 px-3 py-2 text-xs font-medium text-slate-700 disabled:opacity-50"
            >
              {rechecking ? '复检证据中…' : '临行复检（本地）'}
            </button>
          )}
        </div>
      </div>

      {evidence?.provider_failures.length ? (
        <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
          外部事实部分不可用：{evidence.provider_failures.map(item => `${item.provider} / ${item.error_category}`).join('；')}
        </div>
      ) : null}

      {providerFacts.length > 0 && (
        <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50 p-3">
          <h3 className="text-xs font-semibold text-slate-700">事实采集摘要</h3>
          <div className="mt-2 flex flex-wrap gap-2">
            {providerFacts.map(fact => {
              const value = typeof fact.value === 'object' && fact.value !== null
                ? fact.value as Record<string, unknown>
                : {}
              const label = fact.subject_type === 'ROUTE_OPTION'
                ? `路线 · ${String(value.mode ?? 'unknown')}`
                : fact.fact_type === 'WEATHER'
                  ? `天气 · ${String(value.date ?? fact.subject_id)}`
                  : fact.fact_type === 'RISK_SOURCE'
                    ? `风险来源 · ${String(value.source_tier ?? 'UNKNOWN')}`
                    : fact.fact_type
              return (
                <span key={fact.fact_id} className="rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px] text-slate-600">
                  {label} · {fact.provider} · {fact.freshness_status}
                </span>
              )
            })}
          </div>
        </div>
      )}

      <div className="mt-4 space-y-5">
        {groups.map(group => {
          const findings = report.findings.filter(group.filter)
          if (!findings.length) return null
          return (
            <div key={group.title}>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">{group.title} · {findings.length}</h3>
              <div className="space-y-2">
                {findings.map(finding => {
                  const Icon = finding.status === 'UNKNOWN'
                    ? CircleHelp
                    : finding.severity === 'BLOCKER'
                      ? AlertOctagon
                      : AlertTriangle
                  return (
                    <article key={finding.finding_id} className={`rounded-xl border p-4 ${group.tone}`}>
                      <div className="flex items-start gap-2">
                        <Icon className="mt-0.5 h-4 w-4 shrink-0" />
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <p className="text-sm font-semibold text-slate-900">{finding.message}</p>
                            <span className="rounded bg-white/70 px-1.5 py-0.5 text-[10px] text-slate-600">{finding.severity}</span>
                          </div>
                          <p className="mt-1 font-mono text-[10px] text-slate-500">{finding.reason_code} · {finding.rule_id}@{finding.rule_version}</p>
                          {Object.keys(finding.input_values).length > 0 && (
                            <pre className="mt-2 overflow-x-auto rounded-lg bg-white/70 p-2 text-[10px] text-slate-600">
                              {JSON.stringify(finding.input_values, null, 2)}
                            </pre>
                          )}
                          {finding.affected_member_ids.length > 0 && (
                            <p className="mt-1 text-xs text-slate-600">受影响成员：{finding.affected_member_ids.join('、')}</p>
                          )}
                          {finding.confirmation_action && <p className="mt-1 text-xs text-slate-600">{finding.confirmation_action}</p>}

                          {finding.evidence_fact_ids.length > 0 && (
                            <div className="mt-3 space-y-2">
                              {finding.evidence_fact_ids.map(factId => {
                                const fact = facts.get(factId)
                                if (!fact) return <p key={factId} className="text-xs text-rose-700">证据 {factId} 无法回读</p>
                                return (
                                  <div key={factId} className="rounded-lg border border-white/80 bg-white/70 p-2 text-[11px] text-slate-600">
                                    <div className="flex flex-wrap items-center gap-2">
                                      <span className="font-medium">{fact.fact_type}</span>
                                      <span>{fact.provider}</span>
                                      <span className="rounded bg-slate-100 px-1.5 py-0.5">{fact.freshness_status}</span>
                                      <span>{new Date(fact.observed_at).toLocaleString('zh-CN')}</span>
                                      {fact.source_url && (
                                        <a href={fact.source_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-blue-600">
                                          来源 <ExternalLink className="h-3 w-3" />
                                        </a>
                                      )}
                                    </div>
                                  </div>
                                )
                              })}
                            </div>
                          )}
                        </div>
                      </div>
                    </article>
                  )
                })}
              </div>
            </div>
          )
        })}

        {report.overall_status === 'SATISFIED' && (
          <div className="flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
            <CheckCircle2 className="h-4 w-4" /> 当前证据快照下未发现违反或未知结论。
          </div>
        )}
      </div>
    </section>
  )
}
