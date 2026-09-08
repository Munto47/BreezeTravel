import type { ActivityCardView } from '@/lib/trip-understanding-v3'

export default function SourceDetails({card}: {card: Pick<ActivityCardView, 'name' | 'source_details'>}) {
  if (!card.source_details?.length) return null
  return <section className="my-3 rounded-xl bg-sky-50/60 p-3" aria-label={`${card.name}的原文安排`} data-testid="source-internal-details">
    <h3 className="text-sm font-semibold text-slate-800">原文安排</h3>
    <p className="mt-1 text-xs leading-5 text-slate-600">攻略中的园内安排，地点身份与开放情况未单独核验。</p>
    <ol className="mt-3 max-h-60 list-decimal space-y-2 overflow-y-auto pl-5 text-sm leading-6">
      {card.source_details.map((detail, index) => <li key={index} className="break-words">{detail.name}{detail.optional && <span className="ml-2 text-xs text-slate-500">备选</span>}</li>)}
    </ol>
  </section>
}
