'use client'

import { ArrowRight, CheckCircle2, XCircle } from 'lucide-react'

import type { RepairOption } from '@/types/workspace'


interface Props {
  options: RepairOption[]
  busyRepairId: string | null
  onApply: (option: RepairOption) => void
  onReject: (option: RepairOption, reason: string) => void
}


export default function RepairCompare({ options, busyRepairId, onApply, onReject }: Props) {
  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div>
        <h2 className="font-semibold text-slate-900">Repair A/B</h2>
        <p className="mt-1 text-xs text-slate-500">只展示已保存 postcheck report 的候选；应用会创建新 revision，不覆盖原行程。</p>
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        {options.map((option, index) => (
          <article key={option.repair_id} className="rounded-2xl border border-slate-200 p-4">
            <div className="flex items-center justify-between">
              <h3 className="font-semibold text-slate-900">方案 {String.fromCharCode(65 + index)}</h3>
              <span className={`rounded-full px-2 py-1 text-[10px] ${
                option.status === 'APPLIED'
                  ? 'bg-emerald-50 text-emerald-700'
                  : option.status === 'REJECTED'
                    ? 'bg-rose-50 text-rose-700'
                    : option.status === 'STALE'
                      ? 'bg-slate-100 text-slate-500'
                      : 'bg-sky-50 text-sky-700'
              }`}>
                {option.status === 'PROPOSED' ? 'POSTCHECKED' : option.status}
              </span>
            </div>
            <div className="mt-3 space-y-2">
              {option.operations.map((operation, operationIndex) => (
                <div key={`${operation.operation}-${operationIndex}`} className="rounded-xl bg-slate-50 p-3">
                  <p className="text-xs font-semibold text-slate-700">{operation.operation}</p>
                  <p className="mt-1 text-xs text-slate-600">{operation.rationale}</p>
                </div>
              ))}
            </div>
            <dl className="mt-3 grid grid-cols-2 gap-2 text-xs">
              <div className="rounded-lg border border-slate-100 p-2"><dt className="text-slate-400">edit cost</dt><dd className="font-semibold">{option.edit_cost}</dd></div>
              <div className="rounded-lg border border-slate-100 p-2"><dt className="text-slate-400">risk cost</dt><dd className="font-semibold">{option.risk_cost}</dd></div>
              <div className="rounded-lg border border-slate-100 p-2"><dt className="text-slate-400">route delta</dt><dd className="font-semibold">{option.route_cost_delta ?? '待确认'}</dd></div>
              <div className="rounded-lg border border-slate-100 p-2"><dt className="text-slate-400">新增 UNKNOWN</dt><dd className="font-semibold">{option.new_unknown_count}</dd></div>
            </dl>
            {option.tradeoffs.length > 0 && (
              <div className="mt-3 text-xs text-amber-800">
                {option.tradeoffs.map(item => <p key={item}>• {item}</p>)}
              </div>
            )}
            <p className="mt-3 break-all font-mono text-[10px] text-slate-400">postcheck {option.postcheck_report_id}</p>
            <div className="mt-4 flex gap-2">
              <button
                onClick={() => onApply(option)}
                disabled={busyRepairId !== null || option.status !== 'PROPOSED'}
                className="inline-flex flex-1 items-center justify-center gap-1 rounded-xl bg-slate-900 px-3 py-2 text-xs font-medium text-white disabled:opacity-40"
              >
                <CheckCircle2 className="h-3.5 w-3.5" />
                {busyRepairId === option.repair_id ? '应用中…' : '预览确认并应用'}
                <ArrowRight className="h-3.5 w-3.5" />
              </button>
              <button
                onClick={() => {
                  const reason = window.prompt('请记录不采用该方案的原因')
                  if (reason?.trim()) onReject(option, reason.trim())
                }}
                disabled={busyRepairId !== null || option.status !== 'PROPOSED'}
                className="inline-flex items-center justify-center gap-1 rounded-xl border border-slate-200 px-3 py-2 text-xs text-slate-600 disabled:opacity-40"
              >
                <XCircle className="h-3.5 w-3.5" /> 拒绝
              </button>
            </div>
          </article>
        ))}
      </div>
    </section>
  )
}
