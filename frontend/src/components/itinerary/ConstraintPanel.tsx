'use client'

import { AlertCircle, CheckCircle2, HelpCircle, RefreshCw } from 'lucide-react'

import type { VerificationReport, ConstraintStatus } from '@/types/verification'

const config: Record<ConstraintStatus, { label: string; icon: typeof CheckCircle2; style: string }> = {
  SATISFIED: { label: '满足', icon: CheckCircle2, style: 'text-emerald-700 bg-emerald-50 border-emerald-100' },
  VIOLATED: { label: '违反', icon: AlertCircle, style: 'text-rose-700 bg-rose-50 border-rose-100' },
  UNKNOWN: { label: '未知', icon: HelpCircle, style: 'text-amber-700 bg-amber-50 border-amber-100' },
}

export default function ConstraintPanel({ report, stale }: { report: VerificationReport | null; stale: boolean }) {
  if (!report) return null
  if (stale) {
    return (
      <section data-testid="verification-stale" className="mb-5 rounded-2xl border border-slate-200 bg-slate-50 p-4 text-slate-600">
        <div className="flex items-center gap-2 font-semibold text-sm"><RefreshCw className="h-4 w-4" />验证结果已过期</div>
        <p className="mt-1 text-xs">协同地点、投票、锁定项或任务约束已变化；旧报告不再显示为通过。</p>
      </section>
    )
  }
  return (
    <section data-testid="constraint-panel" className="mb-5 rounded-2xl border border-gray-100 bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-bold text-gray-900">约束验证</h2>
        <span className="text-[11px] text-gray-400">修复 {report.repair_rounds}/2 轮</span>
      </div>
      <div className="mt-3 space-y-2">
        {report.checks.map((check, index) => {
          const item = config[check.status]
          const Icon = item.icon
          return (
            <div key={`${check.constraint_id}-${check.day_index ?? 'all'}-${index}`} className={`rounded-xl border px-3 py-2 ${item.style}`}>
              <div className="flex items-start gap-2">
                <Icon className="mt-0.5 h-4 w-4 shrink-0" />
                <div>
                  <p className="text-xs font-semibold">{item.label} · {check.message}</p>
                  {check.status === 'UNKNOWN' && <p className="mt-0.5 text-[10px] opacity-75">缺少证据，不会自动当作通过或触发修复。</p>}
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}
