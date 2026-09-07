'use client'

import { useEffect, useRef, useState } from 'react'
import { BedDouble } from 'lucide-react'
import type { ActivityCardView, TripUnderstandingCommand } from '@/lib/trip-understanding-v3'
import type { WorkspaceCommandResult } from './itinerary-workspace'
import PendingPlaceDropdown from './pending-place-dropdown'

export default function SourceLodging({ cards, resource, disabled, onCommand }: {
  cards: ActivityCardView[]
  resource: string
  disabled: boolean
  onCommand: (command: TripUnderstandingCommand) => Promise<WorkspaceCommandResult>
}) {
  const [editing, setEditing] = useState<string | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const saveLock = useRef(false)
  const heading = useRef<HTMLHeadingElement>(null)
  const buttons = useRef(new Map<string, HTMLButtonElement>())
  useEffect(() => {
    if (editing && !cards.some(card => card.activity_token === editing)) setEditing(null)
    if (removing && !cards.some(card => card.activity_token === removing)) setRemoving(null)
  }, [cards, editing, removing])
  const locked = disabled || saving
  function closeEditor(token: string) {
    setEditing(null)
    requestAnimationFrame(() => (buttons.current.get(token) || heading.current)?.focus({ preventScroll: true }))
  }
  async function remove(card: ActivityCardView) {
    if (locked || saveLock.current) return
    saveLock.current = true
    setSaving(true)
    setMessage('')
    try {
      const outcome = await onCommand({ command_type: 'ACTIVITY_DELETE', activity_token: card.activity_token })
      if (outcome.status === 'APPLIED') {
        setRemoving(null)
        requestAnimationFrame(() => {
          if (heading.current) heading.current.focus({ preventScroll: true })
          else document.querySelector<HTMLButtonElement>('[data-testid="undo-trip-command"]')?.focus({ preventScroll: true })
        })
      } else setMessage(outcome.status === 'SYNCED' ? '行程已变化，请核对最新住宿。' : '正在确认移除结果，请稍候。')
    } catch { setMessage('未能确认移除结果，请稍后重试。') }
    finally { saveLock.current = false; setSaving(false) }
  }
  if (!cards.length) return null
  return <section data-testid="source-lodging" aria-labelledby="source-lodging-heading" className="my-3 rounded-2xl border border-sky-100 bg-white p-4 text-slate-700">
    <h2 id="source-lodging-heading" ref={heading} tabIndex={-1} className="flex items-center gap-2 text-sm font-semibold text-sky-900"><BedDouble aria-hidden="true" className="h-4 w-4" />全程住宿</h2>
    <p className="mt-1 text-xs leading-6 text-slate-500">保留原文住宿安排，按实际过夜日期连接返店和次日出发路线。</p>
    {cards.map(card => <article key={card.activity_token} data-testid="source-lodging-card" className="relative mt-3 rounded-xl border border-slate-100 p-3">
      <h3 className="text-sm font-semibold">{card.name}</h3>
      {!!card.lodging_excluded_nights?.length && <p className="mt-1 text-xs leading-6 text-slate-600">第{[...new Set(card.lodging_excluded_nights)].sort((a, b) => a - b).join('、')}晚原文安排另住，不返回这家酒店。</p>}
      <details className="mt-1 text-sm">
        <summary className="min-h-11 cursor-pointer py-3 text-sky-800">查看住宿</summary>
        <p className="pb-2 leading-6">{card.city ? `${card.city} · ` : ''}{card.area_or_address || '地址暂缺'}</p>
      </details>
      <div className="flex flex-wrap gap-2">
        {card.available_actions.includes('REPLACE') && <button type="button" className="e-button" disabled={locked}
          ref={button => { if (button) buttons.current.set(card.activity_token, button); else buttons.current.delete(card.activity_token) }}
          aria-label={`修改全程住宿 ${card.name}`} aria-expanded={editing === card.activity_token}
          onClick={() => { setEditing(editing === card.activity_token ? null : card.activity_token); setRemoving(null); setMessage('') }}>修改住宿</button>}
        {card.available_actions.includes('DELETE') && <button type="button" className="e-button e-button-quiet" disabled={locked || editing === card.activity_token}
          aria-label={`移除全程住宿 ${card.name}`} onClick={() => { setRemoving(card.activity_token); setMessage('') }}>移除住宿</button>}
      </div>
      {editing === card.activity_token && <PendingPlaceDropdown card={card} resource={resource} disabled={locked} onCommand={onCommand} onClose={() => closeEditor(card.activity_token)} />}
      {removing === card.activity_token && <div className="mt-3 rounded-xl bg-sky-50 p-3 text-sm" role="group" aria-label="确认移除全程住宿">
        <p>移除“{card.name}”的全程住宿安排？之后可以撤销。</p>
        <div className="mt-2 flex flex-wrap gap-2">
          <button type="button" className="e-button" disabled={locked} onClick={() => setRemoving(null)}>保留住宿</button>
          <button type="button" className="e-button" disabled={locked} onClick={() => void remove(card)}>{saving ? '正在移除…' : '确认移除住宿'}</button>
        </div>
      </div>}
    </article>)}
    {message && <p role="status" className="mt-2 text-sm">{message}</p>}
  </section>
}
