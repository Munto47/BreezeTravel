'use client'

import { type ReactNode, useEffect, useRef } from 'react'
import { X } from 'lucide-react'

export default function ExperienceDialog({
  title,
  onClose,
  busy = false,
  children,
}: {
  title: string
  onClose: () => void
  busy?: boolean
  children: ReactNode
}) {
  const dialog = useRef<HTMLDivElement>(null)
  const close = useRef(onClose)
  close.current = onClose
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    dialog.current?.querySelector<HTMLElement>('button, input, select')?.focus()
    return () => {
      document.body.style.overflow = previous
      if (opener?.isConnected) opener.focus()
    }
  }, [])
  return (
    <div
      className="e-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) close.current()
      }}
    >
      <div
        ref={dialog}
        className="e-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="experience-dialog-title"
        onKeyDown={(event) => {
          if (event.key === 'Escape' && !busy) close.current()
          if (event.key !== 'Tab') return
          const items = Array.from(
            dialog.current?.querySelectorAll<HTMLElement>(
              'button:not([disabled]),input:not([disabled]),select:not([disabled]),a[href]',
            ) || [],
          ).filter((item) => item.getClientRects().length > 0)
          if (!items.length) {
            event.preventDefault()
            return
          }
          if (event.shiftKey && document.activeElement === items[0]) {
            event.preventDefault()
            items.at(-1)?.focus()
          }
          if (!event.shiftKey && document.activeElement === items.at(-1)) {
            event.preventDefault()
            items[0].focus()
          }
        }}
      >
        <div className="e-dialog-head">
          <h2 id="experience-dialog-title">{title}</h2>
          <button
            type="button"
            className="e-button e-button-quiet"
            aria-label="关闭详情"
            disabled={busy}
            onClick={onClose}
          >
            <X aria-hidden="true" />
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}
