'use client'

import { type ReactNode, useEffect, useRef, useState } from 'react'
import { ArrowLeft } from 'lucide-react'

export type ContextMode =
  | { kind: 'timeline' }
  | {
      kind: 'place'
      activityToken: string | null
      dayIndex: number
      editorMode: 'ADD' | 'EDIT' | 'REPLACE'
    }
  | { kind: 'preview' }
  | { kind: 'issues'; allDays: boolean }
  | { kind: 'assumption'; key: 'destination' | 'calendar' | 'party_size' }
  | { kind: 'privacy'; target: 'SOURCE' | 'TRIP' }
  | { kind: 'share' }

export default function ContextPanel({
  title,
  dayLabel,
  busy,
  modal = false,
  closeLabel,
  onClose,
  children,
}: {
  title: string
  dayLabel: string
  busy?: boolean
  modal?: boolean
  closeLabel?: string
  onClose: () => void
  children: ReactNode
}) {
  const panel = useRef<HTMLElement>(null)
  const [fullScreen, setFullScreen] = useState(false)
  const close = useRef(onClose)
  close.current = onClose
  useEffect(() => {
    const query = window.matchMedia('(max-width: 1023px)')
    const update = () => setFullScreen(query.matches)
    update()
    query.addEventListener('change', update)
    const initialFocus =
      panel.current?.querySelector<HTMLElement>('[data-initial-focus="true"]') ||
      panel.current?.querySelector<HTMLButtonElement>('button')
    initialFocus?.focus({ preventScroll: true })
    return () => query.removeEventListener('change', update)
  }, [])
  useEffect(() => {
    if (!fullScreen && !modal) return
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [fullScreen, modal])
  return (
    <section
      ref={panel}
      className="e-context-panel"
      role={fullScreen || modal ? 'dialog' : 'region'}
      aria-modal={fullScreen || modal || undefined}
      aria-labelledby="trip-context-title"
      onKeyDown={(event) => {
        if (event.key === 'Escape' && !busy) {
          event.preventDefault()
          close.current()
        }
        if (event.key !== 'Tab' || (!fullScreen && !modal)) return
        const elements = Array.from(
          panel.current?.querySelectorAll<HTMLElement>(
            'button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),a[href],summary',
          ) || [],
        ).filter((node) => node.getClientRects().length > 0)
        if (!elements.length) return
        if (event.shiftKey && document.activeElement === elements[0]) {
          event.preventDefault()
          elements.at(-1)?.focus()
        }
        if (!event.shiftKey && document.activeElement === elements.at(-1)) {
          event.preventDefault()
          elements[0].focus()
        }
      }}
    >
      <header className="e-context-head">
        <button
          type="button"
          className="e-button e-button-quiet"
          disabled={busy}
          onClick={onClose}
        >
          <ArrowLeft aria-hidden="true" />
          {closeLabel || `返回${dayLabel}`}
        </button>
        <h2 id="trip-context-title">{title}</h2>
      </header>
      <div className="e-context-body">{children}</div>
    </section>
  )
}
