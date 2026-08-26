'use client'

import { Building2, CircleHelp, Loader2 } from 'lucide-react'

import type { WorkspaceHotelAreasResponse } from '@/types/workspace'


function humanizeStatus(value: string): string {
  if (value === 'FRESH') return '证据可用'
  if (value === 'STALE') return '证据已过期'
  if (value === 'CONFLICTING') return '证据冲突'
  return '证据不可用'
}

/**
 * Hotel-area scores deliberately render absence as absence.  The only area
 * geometry available in the P5 dev route is a model-draft template zone, so
 * this is not a hotel booking recommendation and never fills unavailable
 * route costs with an estimate.
 */
export default function HotelAreaPanel({
  result,
  loading,
}: {
  result: WorkspaceHotelAreasResponse | null
  loading: boolean
}) {
  return (
    <section data-testid="workspace-hotel-areas" className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center gap-2"><Building2 className="h-4 w-4 text-violet-700" /><h2 className="text-sm font-semibold">酒店区域通勤投影</h2></div>
      <p className="mt-1 text-xs leading-5 text-slate-500">模型生成 DRAFT 区域中心，仅作本地路线投影；不是已核验酒店或住宿推荐。</p>
      {loading ? <p className="mt-3 text-xs text-slate-500"><Loader2 className="mr-1 inline h-3.5 w-3.5 animate-spin" />正在读取区域证据…</p> : !result ? (
        <p className="mt-3 rounded-lg bg-slate-50 p-3 text-xs text-slate-500">酒店区域证据当前不可用，不展示猜测通勤时长。</p>
      ) : result.areas.length === 0 ? (
        <p className="mt-3 rounded-lg bg-slate-50 p-3 text-xs text-slate-500">当前模板没有可评分的区域，不补造住宿建议。</p>
      ) : (
        <div className="mt-3 space-y-2">
          {result.areas.map(area => {
            const unavailable = area.evidence_freshness === 'UNAVAILABLE' || !area.all_days_covered || area.score_minutes === null
            return (
              <div key={area.area_id} data-testid={`hotel-area-${area.area_id}`} className={`rounded-lg border p-2.5 text-xs ${unavailable ? 'border-amber-200 bg-amber-50 text-amber-950' : 'border-violet-100 bg-violet-50 text-violet-950'}`}>
                <div className="flex items-start justify-between gap-2"><p className="font-medium">{area.area_id}</p><span className="whitespace-nowrap text-[10px]">{humanizeStatus(area.evidence_freshness)}</span></div>
                {unavailable ? (
                  <p className="mt-1.5 flex gap-1 leading-5"><CircleHelp className="mt-0.5 h-3.5 w-3.5 shrink-0" />无法覆盖全部日期或路线边缺失，通勤时长保持不可用。</p>
                ) : <p className="mt-1.5 leading-5">跨每日边界合计 {area.score_minutes} 分钟（{area.evidence_freshness}）</p>}
                {area.explanation_codes.length > 0 && <p className="mt-1 text-[10px] opacity-70">{area.explanation_codes.join(' · ')}</p>}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
