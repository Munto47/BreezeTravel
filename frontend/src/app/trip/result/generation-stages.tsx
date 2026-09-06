'use client'

import { Check, LoaderCircle, Circle } from 'lucide-react'
import type { TripUnderstandingProgressMetrics } from '@/lib/trip-understanding-v3'

/** Only authoritative progress advances a step; animation never fabricates progress. */
export default function GenerationStages({ phase, progress, complete = false }: {
  phase: 'RECEIVED' | 'CARDS_AVAILABLE' | 'CHECKING_PLACES'
  progress: TripUnderstandingProgressMetrics
  complete?: boolean
}) {
  const current = complete ? 4 : phase === 'RECEIVED' ? 1 : phase === 'CARDS_AVAILABLE' ? 2
    : progress.places_total > 0 && progress.places_checked >= progress.places_total ? 3 : 2
  const labels = ['收到攻略', '整理每天安排', '核对地点', '生成完毕']
  return (
    <ol className="soft-generation-stages" aria-label="整理进度" aria-live="polite" data-testid="generation-stages">
      {labels.map((label, index) => {
        const done = index < current
        const active = index === current
        const Icon = done ? Check : active ? LoaderCircle : Circle
        return <li key={label} data-state={done ? 'done' : active ? 'active' : 'pending'} aria-current={active ? 'step' : undefined}>
          <span className="soft-stage-dot"><Icon aria-hidden="true" className={active ? 'animate-spin motion-reduce:animate-none' : ''} /></span>
          <span>{index === 3 && active ? '收尾中' : label}</span>
          {index === 2 && active && progress.places_total > 0 && <small>{progress.places_checked}/{progress.places_total}</small>}
        </li>
      })}
    </ol>
  )
}
