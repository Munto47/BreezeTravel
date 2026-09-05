'use client'

import { type ReactNode, useEffect, useRef, useState } from 'react'
import { ArrowLeft } from 'lucide-react'

export type ContextMode =
  | { kind: 'timeline' }
  | { kind: 'place'; activityToken: string | null; dayIndex: number }
  | { kind: 'preview' }
  | { kind: 'issues'; allDays: boolean }
  | { kind: 'source' }
  | { kind: 'assumption'; key: 'destination' | 'calendar' | 'party_size' }
  | { kind: 'privacy'; target: 'SOURCE' | 'TRIP' }
  | { kind: 'share' }

export default function ContextPanel({
  title,
  dayLabel,
  busy,
  onClose,
  children,
}: {
  title: string
  dayLabel: string
  busy?: boolean
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
    panel.current
      ?.querySelector<HTMLButtonElement>('button')
      ?.focus({ preventScroll: true })
    return () => query.removeEventListener('change', update)
  }, [])
  useEffect(() => {
    if (!fullScreen) return
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [fullScreen])
  return (
    <section
      ref={panel}
      className="e-context-panel"
      role={fullScreen ? 'dialog' : 'region'}
      aria-modal={fullScreen || undefined}
      aria-labelledby="trip-context-title"
      onKeyDown={(event) => {
        if (event.key === 'Escape' && !busy) {
          event.preventDefault()
          close.current()
        }
        if (event.key !== 'Tab' || !fullScreen) return
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
          返回{dayLabel}
        </button>
        <h2 id="trip-context-title">{title}</h2>
      </header>
      <div className="e-context-body">{children}</div>
    </section>
  )
}
