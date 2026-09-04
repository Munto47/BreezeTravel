'use client'

import {
  type KeyboardEvent,
  type MutableRefObject,
  type ReactNode,
  useEffect,
  useRef,
} from 'react'
import { motion, useReducedMotion } from 'framer-motion'


const FOCUSABLE_SELECTOR = [
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'a[href]',
  '[tabindex]:not([tabindex="-1"])',
].join(', ')


export default function AccessibleDialog({
  titleId,
  descriptionId,
  onClose,
  children,
  returnFocusRef,
  panelRef,
  dismissDisabled = false,
  size = 'md',
}: {
  titleId: string
  descriptionId?: string
  onClose: () => void
  children: ReactNode
  returnFocusRef?: MutableRefObject<HTMLElement | null>
  panelRef?: MutableRefObject<HTMLDivElement | null>
  dismissDisabled?: boolean
  size?: 'md' | 'lg'
}) {
  const internalPanelRef = useRef<HTMLDivElement | null>(null)
  const openerRef = useRef<HTMLElement | null>(null)
  const reduceMotion = useReducedMotion()

  useEffect(() => {
    if (!openerRef.current) {
      openerRef.current = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null
    }
    const panel = internalPanelRef.current
    const initial = panel?.querySelector<HTMLElement>('[data-dialog-initial-focus]')
      || panel?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR)
    initial?.focus()
    return () => {
      const target = returnFocusRef ? returnFocusRef.current : openerRef.current
      window.requestAnimationFrame(() => {
        const active = document.activeElement
        if (active instanceof HTMLElement && active.closest('[role="dialog"]')) return
        if (target?.isConnected) target.focus()
      })
    }
  }, [returnFocusRef])

  const setPanel = (node: HTMLDivElement | null) => {
    internalPanelRef.current = node
    if (panelRef) panelRef.current = node
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape' && !dismissDisabled) {
      event.preventDefault()
      onClose()
      return
    }
    if (event.key !== 'Tab') return
    const focusable = Array.from(
      internalPanelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR) || [],
    ).filter((element) => element.getClientRects().length > 0)
    if (focusable.length === 0) {
      event.preventDefault()
      internalPanelRef.current?.focus()
      return
    }
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && (document.activeElement === first || document.activeElement === internalPanelRef.current)) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  return (
    <motion.div
      className="fixed inset-0 z-50 flex items-end justify-center bg-slate-950/35 p-4 backdrop-blur-sm sm:items-center"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: reduceMotion ? 0 : 0.16 }}
      onMouseDown={(event) => {
        if (!dismissDisabled && event.target === event.currentTarget) onClose()
      }}
    >
      <motion.div
        ref={setPanel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
        initial={reduceMotion ? { opacity: 1 } : { opacity: 0, y: 18, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={reduceMotion ? { opacity: 0 } : { opacity: 0, y: 12, scale: 0.98 }}
        transition={{ duration: reduceMotion ? 0 : 0.18 }}
        className={`max-h-[calc(100dvh-2rem)] w-full overflow-y-auto rounded-[1.75rem] bg-white p-6 shadow-2xl outline-none ${size === 'lg' ? 'max-w-lg' : 'max-w-md'}`}
      >
        {children}
      </motion.div>
    </motion.div>
  )
}
