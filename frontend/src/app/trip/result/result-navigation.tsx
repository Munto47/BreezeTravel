'use client'

import { MapPinned, Route } from 'lucide-react'

import { type ResultViewId } from './result-presentation'


const ITEMS: Array<{
  id: ResultViewId
  desktopLabel: string
  mobileLabel: string
  panelId: string
  Icon: typeof Route
}> = [
  { id: 'ITINERARY', desktopLabel: '卡片', mobileLabel: '卡片', panelId: 'itinerary-view', Icon: Route },
  { id: 'MAP_STAY', desktopLabel: '地图', mobileLabel: '地图', panelId: 'map-stay-view', Icon: MapPinned },
]


export default function ResultNavigation({
  activeView,
  onChange,
}: {
  activeView: ResultViewId
  onChange: (view: ResultViewId) => void
}) {
  return (
    <>
      <aside
        data-testid="result-desktop-nav"
        className="group fixed bottom-5 left-4 top-[5.75rem] z-30 hidden w-[4.25rem] overflow-hidden rounded-[1.4rem] border border-sky-950/10 bg-white/90 shadow-[0_24px_60px_-32px_rgba(12,120,157,0.55)] backdrop-blur-xl transition-[width] duration-200 motion-reduce:transition-none hover:w-[11.5rem] focus-within:w-[11.5rem] lg:block"
        aria-label="结果主视图"
      >
        <nav className="flex h-full flex-col gap-2 p-2.5">
          {ITEMS.map(({ id, desktopLabel, panelId, Icon }) => {
            const current = id === activeView
            return (
              <button
                key={id}
                data-testid={`desktop-nav-${id.toLowerCase()}`}
                type="button"
                aria-current={current ? 'page' : undefined}
                aria-controls={panelId}
                aria-label={desktopLabel}
                onClick={() => onChange(id)}
                className={`flex min-h-12 w-full items-center gap-3 overflow-hidden rounded-xl px-2 text-left text-sm font-semibold transition motion-reduce:transition-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] focus-visible:ring-offset-2 ${current ? 'bg-[#0c789d] text-white shadow-sm' : 'text-slate-600 hover:bg-sky-50 hover:text-sky-950'}`}
              >
                <span className="flex h-8 w-8 shrink-0 items-center justify-center">
                  <Icon className="h-5 w-5" aria-hidden="true" />
                </span>
                <span className="whitespace-nowrap opacity-0 transition-opacity motion-reduce:transition-none group-hover:opacity-100 group-focus-within:opacity-100">
                  {desktopLabel}
                </span>
              </button>
            )
          })}
        </nav>
      </aside>

      <nav
        data-testid="result-mobile-nav"
        className="fixed inset-x-3 bottom-3 z-30 grid grid-cols-2 rounded-2xl border border-sky-950/10 bg-white/[0.92] p-1.5 pb-[calc(0.375rem+env(safe-area-inset-bottom))] shadow-[0_20px_55px_-24px_rgba(12,120,157,0.55)] backdrop-blur-xl lg:hidden"
        aria-label="结果主视图"
      >
        {ITEMS.map(({ id, mobileLabel, panelId, Icon }) => {
          const current = id === activeView
          return (
            <button
              key={id}
              data-testid={`mobile-nav-${id.toLowerCase()}`}
              type="button"
              aria-current={current ? 'page' : undefined}
              aria-controls={panelId}
              onClick={() => onChange(id)}
              className={`flex min-h-12 flex-col items-center justify-center gap-0.5 rounded-xl px-1 text-[11px] font-semibold transition motion-reduce:transition-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] ${current ? 'bg-[#0c789d] text-white' : 'text-slate-600 hover:bg-sky-50 hover:text-sky-950'}`}
            >
              <Icon className="h-4 w-4" aria-hidden="true" />
              <span>{mobileLabel}</span>
            </button>
          )
        })}
      </nav>
    </>
  )
}
