'use client'

import {useEffect, useLayoutEffect, useRef, useState} from 'react'

let mobileLocks = 0
let originalOverflow = ''

export function useCompactScreen() {
  const [compact, setCompact] = useState(false)
  useEffect(() => {
    const query = window.matchMedia('(max-width: 1023px)')
    const update = () => setCompact(query.matches)
    update()
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])
  return compact
}

// Only an open mobile panel owns a history entry. Normal navigation is untouched.
export function useMobilePanel(open: boolean, onClose: () => void, busy = false) {
  const compact = useCompactScreen()
  const latest = useRef({onClose, busy})
  const registration = useRef<{timer: ReturnType<typeof setTimeout> | null; closing: boolean; dispose: () => void} | null>(null)
  latest.current = {onClose, busy}
  useLayoutEffect(() => {
    if (!open || !window.matchMedia('(max-width:1023px)').matches) return
    const scheduleClose = () => {
      const current = registration.current
      if (!current) return
      current.closing = true
      current.timer = setTimeout(() => {current.dispose(); if(registration.current === current) registration.current = null}, 0)
    }
    // React's development remount and responsive hydration must not add or
    // asynchronously remove a second history entry for the same panel.
    if (registration.current) {
      if(registration.current.timer)clearTimeout(registration.current.timer)
      registration.current.closing=false
      return scheduleClose
    }
    const marker = `panel-${Date.now()}-${Math.random()}`
    const previousState = history.state
    const href = location.href
    if (mobileLocks++ === 0) {originalOverflow = document.body.style.overflow; document.body.style.overflow = 'hidden'}
    history.pushState({...previousState, tripMobilePanel: marker}, '')
    const pop = () => {
      if (history.state?.tripMobilePanel === marker) return
      if (latest.current.busy) history.pushState({...previousState, tripMobilePanel: marker}, '')
      else {
        latest.current.onClose()
        // A dirty editor can ask to discard instead of closing. Keep its entry
        // while that decision is pending, without intercepting a real departure.
        setTimeout(() => {if (registration.current && !registration.current.closing && location.href === href && history.state?.tripMobilePanel !== marker) history.pushState({...history.state, tripMobilePanel: marker}, '')}, 0)
      }
    }
    window.addEventListener('popstate', pop)
    window.dispatchEvent(new Event('trip-mobile-panel-open'))
    registration.current = {timer:null, closing:false, dispose:() => {
      if (--mobileLocks === 0) document.body.style.overflow = originalOverflow
      window.removeEventListener('popstate', pop)
      if (location.href === href && history.state?.tripMobilePanel === marker) history.back()
    }}
    return scheduleClose
  }, [open, compact])
}

export function MobileViewport() {
  useEffect(() => {
    const viewport = window.visualViewport
    let frame = 0
    const update = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        // Pinch zoom remains native; don't resize the editor in response to zoom.
        if (viewport && viewport.scale !== 1) return
        document.documentElement.style.setProperty('--mobile-visible-height', `${viewport?.height || window.innerHeight}px`)
        document.documentElement.style.setProperty('--mobile-visible-top', `${viewport?.offsetTop || 0}px`)
      })
    }
    update()
    viewport?.addEventListener('resize', update)
    viewport?.addEventListener('scroll', update)
    window.addEventListener('resize', update)
    return () => {
      cancelAnimationFrame(frame)
      viewport?.removeEventListener('resize', update)
      viewport?.removeEventListener('scroll', update)
      window.removeEventListener('resize', update)
    }
  }, [])
  return null
}

export function MobileDayNavigation({count, current, active, onChange}: {
  count: number; current: number; active: boolean; onChange: (day: number) => void
}) {
  const nav = useRef<HTMLElement>(null)
  const latest = useRef(onChange)
  latest.current = onChange
  useEffect(() => {
    if (!active) return
    let frame = 0
    const update = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const boundary = nav.current?.getBoundingClientRect().bottom || 150
        const headings = [...document.querySelectorAll<HTMLElement>('.four-day-panel')]
        const target = headings.find(el => el.getBoundingClientRect().bottom > boundary + 24)
        if (target) latest.current(Number(target.dataset.dayIndex))
      })
    }
    document.addEventListener('scroll', update, true)
    return () => {cancelAnimationFrame(frame); document.removeEventListener('scroll', update, true)}
  }, [active, count])
  useEffect(() => {
    const button = nav.current?.querySelector<HTMLElement>('[aria-pressed="true"]')
    if (button && nav.current) nav.current.scrollLeft += button.getBoundingClientRect().left - nav.current.getBoundingClientRect().left - 12
  }, [current])
  return <nav ref={nav} className="mobile-day-navigation" aria-label="跳转行程日期">
    {Array.from({length: count}, (_, index) => <button key={index} type="button" aria-pressed={index === current} onClick={() => {
      onChange(index)
      document.querySelector<HTMLElement>(`[data-testid="day-lane-${index + 1}"]`)?.scrollIntoView({block: 'start'})
    }}>Day {index + 1}</button>)}
  </nav>
}
