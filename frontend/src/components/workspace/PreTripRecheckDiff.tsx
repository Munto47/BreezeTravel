'use client'

import type { PreTripRecheckResult } from '@/types/workspace'

interface Props {
  result: PreTripRecheckResult
}

const evidenceLabels: Record<string, string> = {
  ADDED: '新增', REMOVED: '移除', VALUE_CHANGED: '事实变化',
  FRESHNESS_CHANGED: '新鲜度变化', VALIDITY_CHANGED: '有效期刷新', PROVIDER_CHANGED: '来源变化',
}

const recheckWindowLabels = {
  EARLY: '尚早',
  RECOMMENDED_24_48H: '建议复检窗口',
  LATE: '已接近或进入行程',
} as const

export default function PreTripRecheckDiff({ result }: Props) {
  const hasChanges = result.evidence_changes.length > 0
    || result.finding_changes.length > 0
    || result.provider_failure_changes.length > 0
  return (
    <section className="rounded-2xl border border-indigo-200 bg-indigo-50/40 p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="font-semibold text-slate-900">临行复检差异（本地）</h2>
          <p className="mt-1 text-xs text-slate-600">对比报告 {result.source_report_id.slice(0, 8)} 与新快照；不会代表真人或公网验收已完成。</p>
        </div>
        <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${result.degraded ? 'bg-amber-100 text-amber-800' : 'bg-emerald-100 text-emerald-800'}`}>
          {result.degraded ? '部分 Provider 降级' : '复检完成'}
        </span>
      </div>

      <div className="mt-4 rounded-xl border border-indigo-200 bg-white/80 p-3 text-xs text-slate-700">
        <p className="font-semibold">复检时间窗口：{recheckWindowLabels[result.recheck_window_state]}</p>
        <p className="mt-1 text-slate-600">{result.recheck_window_reason}</p>
        <p className="mt-1 text-slate-500">
          以行程首日零点（{new Date(result.trip_start_reference_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}）为保守参考；当前相差 {result.hours_until_trip_start} 小时。
        </p>
      </div>

      {result.provider_failures.length > 0 && (
        <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
          <p className="font-semibold">未完成的外部事实刷新</p>
          <ul className="mt-1 list-disc space-y-1 pl-4">
            {result.provider_failures.map((item, index) => <li key={`${item.provider}-${index}`}>{item.provider} / {item.error_category}{item.detail ? `：${item.detail}` : ''}</li>)}
          </ul>
        </div>
      )}

      {result.provider_failure_changes.length > 0 && (
        <div className="mt-4">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-600">Provider 失败状态变化</h3>
          <ul className="mt-2 space-y-2">
            {result.provider_failure_changes.map((item, index) => (
              <li key={`${item.change_type}-${item.failure.provider}-${item.failure.error_category}-${index}`} className="rounded-xl border border-white/80 bg-white/80 p-3 text-xs text-slate-700">
                <p><span className="font-semibold">{item.change_type === 'ADDED' ? '新增失败' : '失败已消失'}</span> · {item.failure.provider} / {item.failure.error_category}</p>
                <p className="mt-1 text-slate-500">为什么：{item.reason}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {result.provider_receipts?.length > 0 && (
        <div className="mt-4 rounded-xl border border-slate-200 bg-white/80 p-3 text-xs text-slate-700">
          <h3 className="font-semibold">Provider 调用收据</h3>
          <ul className="mt-1 space-y-1 text-slate-600">
            {result.provider_receipts.map((item, index) => (
              <li key={`${item.provider}-${item.subject_id ?? 'workspace'}-${index}`}>
                {item.provider_call_attempted ? '已尝试调用' : '未调用实时 Provider'} · {item.provider} · {item.execution_mode} / {item.status}{item.detail ? `：${item.detail}` : ''}
              </li>
            ))}
          </ul>
        </div>
      )}

      {!hasChanges && result.provider_failures.length === 0 && (
        <p className="mt-4 text-sm text-slate-700">新快照下没有可见的事实或审计结论变化。</p>
      )}

      {result.evidence_changes.length > 0 && (
        <div className="mt-4">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-600">证据发生了什么变化</h3>
          <ul className="mt-2 space-y-2">
            {result.evidence_changes.map((item, index) => (
              <li key={`${item.subject_type}-${item.subject_id}-${item.fact_type}-${index}`} className="rounded-xl border border-white/80 bg-white/80 p-3 text-xs text-slate-700">
                <p><span className="font-semibold">{evidenceLabels[item.change_type] ?? item.change_type}</span> · {item.subject_id} · {item.fact_type} · {item.provider}</p>
                <p className="mt-1 text-slate-500">为什么：{item.reason}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {result.finding_changes.length > 0 && (
        <div className="mt-4">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-600">审计结论发生了什么变化</h3>
          <ul className="mt-2 space-y-2">
            {result.finding_changes.map((item, index) => (
              <li key={`${item.rule_id}-${item.reason_code}-${index}`} className="rounded-xl border border-white/80 bg-white/80 p-3 text-xs text-slate-700">
                <p><span className="font-semibold">{item.change_type}</span> · {item.reason_code}</p>
                <p className="mt-1 text-slate-500">为什么：{item.reason}</p>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
