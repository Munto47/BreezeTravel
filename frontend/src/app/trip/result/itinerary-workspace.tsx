'use client'

import {
  type DragEvent,
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import {
  ArrowRight,
  BedDouble,
  BusFront,
  ChevronRight,
  Footprints,
  GripVertical,
  List,
  MapPin,
  Plus,
  Sparkles,
  Trash2,
  UtensilsCrossed,
  X,
} from 'lucide-react'

import {
  type ActivityCardView,
  type MapRenderView,
  type TripUnderstandingCommand,
  type UserFacingTripResult,
} from '@/lib/trip-understanding-v3'
import { serpentineLayout, serpentineEdge } from './serpentine-layout'
import PendingPlaceDropdown from './pending-place-dropdown'
import AccessibleDialog from './accessible-dialog'
import { DAY_ACCENTS, DAY_COLORS, transportConnectorFor, distanceLabel } from './result-presentation'


type DayView = UserFacingTripResult['days'][number]

type CardLocation = {
  card: ActivityCardView
  dayIndex: number
  position: number
}

type DialogState =
  | { kind: 'DELETE'; item: CardLocation }


type DraggedCard = CardLocation

type DropTarget = {
  dayIndex: number
  position: number
}

export type WorkspaceCommandResult =
  | { status: 'APPLIED' | 'SYNCED'; days?: DayView[] }
  | { status: 'RECONCILING' }

type ItineraryWorkspaceProps = {
  toolbar?: ReactNode
  days: UserFacingTripResult['days']
  disabled: boolean
  routesPending: boolean
  mapView: MapRenderView
  checkStatus: string
  resource: string
  onRender: () => void
  onCommand: (command: TripUnderstandingCommand) => Promise<WorkspaceCommandResult>
  onAdd: (dayIndex: number, position: number) => void
  onAlternatives?: (dayIndex: number, trigger: HTMLButtonElement) => void
}



export default function ItineraryWorkspace({
  days,
  toolbar,
  resource,
  onRender,
  disabled,
  routesPending,
  mapView,
  onCommand,
  onAdd,
  onAlternatives,
}: ItineraryWorkspaceProps) {
  const reduceMotion = useReducedMotion()
  const [localDays, setLocalDays] = useState(days)
  const [dragged, setDragged] = useState<DraggedCard | null>(null)
  const touchTarget = useRef<DropTarget | 'TRASH' | null>(null)
  const pointerPosition = useRef<{x:number;y:number} | null>(null)
  const [touchPoint, setTouchPoint] = useState<{x:number;y:number} | null>(null)
  const [dropTarget, setDropTarget] = useState<DropTarget | null>(null)
  const [pendingPlace, setPendingPlace] = useState<CardLocation | null>(null)
  const [dialog, setDialog] = useState<DialogState | null>(null)
  const [keyboardMove, setKeyboardMove] = useState<{item:CardLocation;dayIndex:number;position:number}|null>(null)
  const [operationPending, setOperationPending] = useState(false)
  const [layoutMode, setLayoutMode] = useState<'CHAIN' | 'LIST'>('CHAIN')
  const [announcement, setAnnouncement] = useState('行程卡片已加载，可以拖拽调整。键盘在把手按 Enter 提起，左右调顺序，上下换日期，再按 Enter 放下，Escape 取消。')
  const lastTriggerRef = useRef<HTMLElement | null>(null)
  const operationLockRef = useRef(false)
  const dragCompletedRef = useRef(false)
  const laneScrollers = useRef(new Map<number, HTMLDivElement>())
  const [laneWidths, setLaneWidths] = useState<Record<number, number>>({})
  const dragGeometry = useRef(new Map<number, ReturnType<typeof serpentineLayout>>())
  const pointerDropTarget = useRef<DropTarget | null>(null)
  useLayoutEffect(() => {
    const measure = () => {
      const widths: Record<number, number> = {}
      laneScrollers.current.forEach((lane, day) => { widths[day] = lane.clientWidth })
      setLaneWidths(widths)
    }
    measure()
    const observer = new ResizeObserver(measure)
    laneScrollers.current.forEach(lane => observer.observe(lane))
    return () => observer.disconnect()
  }, [localDays.length, layoutMode])
  const captureDropGeometry = () => {
    pointerDropTarget.current = null
    dragGeometry.current.clear()
    laneScrollers.current.forEach((lane, dayIndex) => {
      dragGeometry.current.set(dayIndex, serpentineLayout(lane.clientWidth, localDays[dayIndex-1].activities.length))
    })
  }
  const targetAt = (x: number, y: number): DropTarget | null => {
    for (const [dayIndex, layout] of dragGeometry.current) {
      const lane = laneScrollers.current.get(dayIndex)
      if (!lane) continue
      const box = lane.getBoundingClientRect()
      const dayBox = lane.closest('[data-day-index]')?.getBoundingClientRect() || box
      if (x < dayBox.left || x > dayBox.right || y < dayBox.top || y > dayBox.bottom) continue
      const count = localDays[dayIndex-1].activities.length
      const row = layout.rowAt(y-box.top,count)
      const first = row*layout.columns, last = Math.min(count, first+layout.columns)
      const reverse = row%2 === 1
      let position=first
      for(let index=first;index<last;index++) {
        const center=layout.point(index).x+layout.cardWidth/2
        if(reverse ? x-box.left < center : x-box.left > center) position++
      }
      return {dayIndex,position:Math.min(position,count)}
    }
    return null
  }
  useEffect(() => {
    if (!dragged) return
    const cancel = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setDragged(null); setDropTarget(null); setTouchPoint(null); touchTarget.current=null; pointerPosition.current=null
    }
    window.addEventListener('keydown',cancel)
    return () => window.removeEventListener('keydown',cancel)
  }, [dragged])

  useEffect(() => {
    if (!dragged || !touchPoint) return
    let frame=0
    const scroll = () => {
      const touchPoint = pointerPosition.current
      if (!touchPoint) return
      if(touchPoint.y>window.innerHeight-90) window.scrollBy(0,10)
      else if(touchPoint.y<90) window.scrollBy(0,-10)
      if(!document.elementFromPoint(touchPoint.x,touchPoint.y)?.closest('[data-trash]')) {
        const updated=targetAt(touchPoint.x,touchPoint.y)
        touchTarget.current=updated
        setDropTarget(current => current?.dayIndex===updated?.dayIndex && current?.position===updated?.position ? current : updated)
      }
      frame=requestAnimationFrame(scroll)
    }
    frame=requestAnimationFrame(scroll)
    return () => cancelAnimationFrame(frame)
  },[dragged,touchPoint])

  const locked = disabled || operationPending

  useEffect(() => {
    setLocalDays(days)
  }, [days])

  const rememberTrigger = (element: HTMLElement) => {
    lastTriggerRef.current = element
  }

  const closeDialog = (restoreFocus = true) => {
    setDialog(null)
    if (restoreFocus) {
      window.setTimeout(() => lastTriggerRef.current?.focus(), 0)
    }
  }

  const openDetails = (item: CardLocation, element: HTMLElement) => {
    rememberTrigger(element)
    setPendingPlace(current=>current?.card.activity_token===item.card.activity_token?null:item)
  }

  const closePendingPlace = () => {
    setPendingPlace(null)
    requestAnimationFrame(()=>{
      if(lastTriggerRef.current?.isConnected)lastTriggerRef.current.focus({preventScroll:true})
      else document.querySelector<HTMLElement>(`[data-day-heading="${pendingPlace?.dayIndex||1}"]`)?.focus({preventScroll:true})
    })
  }
  useEffect(()=>{if(pendingPlace && !days.some(day=>day.activities.some(card=>card.activity_token===pendingPlace.card.activity_token)))setPendingPlace(null)},[days,pendingPlace])

  const openMove = (item: CardLocation, element?: HTMLElement) => {
    if(element)rememberTrigger(element)
    setPendingPlace(null)
    setKeyboardMove({item,dayIndex:item.dayIndex,position:item.position})
    setDragged(item);setDropTarget({dayIndex:item.dayIndex,position:item.position})
    setAnnouncement('已提起。左右调整顺序，上下切换日期，Enter 放下，Escape 取消。')
  }

  const openDelete = (item: CardLocation, element?: HTMLElement) => {
    if (element) rememberTrigger(element)
    setDialog({ kind: 'DELETE', item })
  }

  const finishOperation = (message: string, focusDayIndex: number) => {
    setAnnouncement(message)
    setDialog(null)
    window.setTimeout(() => {
      document.querySelector<HTMLElement>(`[data-day-heading="${focusDayIndex}"]`)?.focus()
    }, 0)
  }

  const restoreOperationFocus = (
    message: string,
    focusDayIndex: number,
    replacementLabel?: string,
  ) => {
    const trigger = lastTriggerRef.current
    setAnnouncement(message)
    setDialog(null)
    window.setTimeout(() => {
      if (trigger?.isConnected) {
        trigger.focus()
        return
      }
      const replacement = replacementLabel
        ? Array.from(document.querySelectorAll<HTMLElement>('button')).find(
          (element) => element.getAttribute('aria-label') === replacementLabel,
        )
        : null
      if (replacement) {
        replacement.focus()
        return
      }
      document.querySelector<HTMLElement>(`[data-day-heading="${focusDayIndex}"]`)?.focus()
    }, 0)
  }

  const applyMove = async (
    item: CardLocation,
    targetDayIndex: number,
    targetPosition: number,
    successMessage: string,
  ) => {
    if (operationLockRef.current || disabled || !localDays[targetDayIndex - 1]) return false
    if (item.dayIndex === targetDayIndex && item.position === targetPosition) {
      setAnnouncement(`${item.card.name} 已在这个位置，没有发送保存请求。`)
      return false
    }

    const before = localDays
    const optimistic = moveCard(before, item, targetDayIndex, targetPosition)
    if (!optimistic) return false

    operationLockRef.current = true
    setOperationPending(true)
    setLocalDays(optimistic)
    setAnnouncement(`正在保存 ${item.card.name} 的新位置…`)
    try {
      const outcome = await onCommand({
        command_type: 'ACTIVITY_MOVE',
        activity_token: item.card.activity_token,
        target_day_index: targetDayIndex,
        target_position: targetPosition,
      })
      if (outcome.status === 'APPLIED') {
        if (outcome.days) setLocalDays(outcome.days)
        finishOperation(successMessage, targetDayIndex)
      } else if (outcome.status === 'SYNCED') {
        setLocalDays(outcome.days || before)
        finishOperation(`${item.card.name} 的调整未能确认，已读取服务端最新行程。`, targetDayIndex)
      } else {
        finishOperation(`${item.card.name} 的调整已提交，正在确认服务端保存结果。`, targetDayIndex)
      }
      return outcome.status
    } catch {
      setLocalDays(before)
      finishOperation(`${item.card.name} 的调整尚未保存，已恢复原顺序。`, item.dayIndex)
      return false
    } finally {
      operationLockRef.current = false
      setOperationPending(false)
    }
  }

  const applyDelete = async (item: CardLocation) => {
    if (operationLockRef.current || disabled) return
    operationLockRef.current = true
    setOperationPending(true)
    const before = localDays
    setLocalDays((current) => removeCard(current, item.card.activity_token))
    setAnnouncement(`正在删除 ${item.card.name}…`)
    try {
      const outcome = await onCommand({
        command_type: 'ACTIVITY_DELETE',
        activity_token: item.card.activity_token,
      })
      if (outcome.status === 'APPLIED') {
        if (outcome.days) setLocalDays(outcome.days)
        finishOperation(`${item.card.name} 已删除，${localDays[item.dayIndex - 1]?.label || '当天'}仍然保留。`, item.dayIndex)
      } else if (outcome.status === 'SYNCED') {
        setLocalDays(outcome.days || before)
        restoreOperationFocus(
          `${item.card.name} 的删除未能确认，已读取服务端最新行程。`,
          item.dayIndex,
          `拖动 ${item.card.name}`,
        )
      } else {
        finishOperation(`${item.card.name} 的删除已提交，正在确认服务端保存结果。`, item.dayIndex)
      }
    } catch {
      setLocalDays(before)
      restoreOperationFocus(`${item.card.name} 尚未删除，已恢复原卡片。`, item.dayIndex)
    } finally {
      operationLockRef.current = false
      setOperationPending(false)
    }
  }

  const handleDrop = async (targetDayIndex: number, rawPosition: number) => {
    if (!dragged || locked) return
    dragCompletedRef.current = true
    const actualTarget = pointerDropTarget.current
    if (actualTarget) { targetDayIndex = actualTarget.dayIndex; rawPosition = actualTarget.position }
    pointerDropTarget.current = null
    const targetPosition = normalizeDropPosition(dragged, targetDayIndex, rawPosition)
    setDragged(null)
    setDropTarget(null)
    if (targetPosition === null) {
      setAnnouncement(`${dragged.card.name} 仍在原位，没有发送保存请求。`)
      return
    }
    const preview = moveCard(localDays, dragged, targetDayIndex, targetPosition)
    if (!preview) return
    await applyMove(dragged, targetDayIndex, targetPosition, `${dragged.card.name} 已移动。路线需要更新。`)
  }

  useEffect(()=>{
    if(!keyboardMove)return
    const key=(event:KeyboardEvent)=>{
      if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Enter',' ','Escape'].includes(event.key)||locked)return
      event.preventDefault();event.stopPropagation()
      const {item}=keyboardMove
      if(event.key==='Escape'){
        setKeyboardMove(null);setDragged(null);setDropTarget(null)
        requestAnimationFrame(()=>lastTriggerRef.current?.focus({preventScroll:true}))
        setAnnouncement('已取消，没有保存。');return
      }
      if(event.key==='Enter'||event.key===' '){
        setKeyboardMove(null);setDragged(null);setDropTarget(null)
        if(item.dayIndex===keyboardMove.dayIndex&&item.position===keyboardMove.position)requestAnimationFrame(()=>lastTriggerRef.current?.focus({preventScroll:true}))
        void applyMove(item,keyboardMove.dayIndex,keyboardMove.position,'地点已移动。路线需要更新。')
        return
      }
      let dayIndex=keyboardMove.dayIndex,position=keyboardMove.position
      if(event.key==='ArrowUp'||event.key==='ArrowDown')dayIndex=Math.max(1,Math.min(localDays.length,dayIndex+(event.key==='ArrowDown'?1:-1)))
      else position+=event.key==='ArrowRight'?1:-1
      position=Math.max(0,Math.min(localDays[dayIndex-1].activities.length-(dayIndex===item.dayIndex?1:0),position))
      setKeyboardMove({item,dayIndex,position})
      setDropTarget({dayIndex,position:position+(dayIndex===item.dayIndex&&position>item.position?1:0)})
      setAnnouncement(`${localDays[dayIndex-1].label}，第 ${position+1} 站。Enter 放下。`)
    }
    document.addEventListener('keydown',key,true)
    return ()=>document.removeEventListener('keydown',key,true)
  },[keyboardMove,locked,localDays])


  return (
    <div
      data-testid="itinerary-workspace"
      data-reduced-motion={reduceMotion ? 'true' : 'false'}
      className="soft-workspace serpentine-workspace mt-2"
    >
      <div className="min-w-0">
        <div className="mb-4 flex flex-wrap items-center justify-end gap-2">
          <button type="button" className="e-button" aria-label={layoutMode === 'CHAIN' ? '切换为列表' : '切换为横链'}
            title={layoutMode === 'CHAIN' ? '列表' : '横链'} aria-pressed={layoutMode === 'LIST'}
            onClick={() => setLayoutMode(layoutMode === 'CHAIN' ? 'LIST' : 'CHAIN')}>
            <List aria-hidden="true" />
          </button>
          {mapView.status === 'NEEDS_UPDATE' && mapView.available_actions.includes('RENDER_MAP') && <button type="button" className="e-button" data-testid="update-card-routes" disabled={disabled} onClick={onRender}>更新步行路线</button>}
          {toolbar}
        </div>

        <div data-testid="trip-days" className="space-y-4" aria-label="按天排列的游览顺序">
          {localDays.map((day, dayOffset) => {
            const dayIndex = dayOffset + 1
            const accent = DAY_ACCENTS[dayOffset % DAY_ACCENTS.length]
            const sourceIndex = dragged && dropTarget ? day.activities.findIndex(card => card.activity_token === dragged.card.activity_token) : -1
            const rawInsertion = dragged && dropTarget?.dayIndex === dayIndex ? dropTarget.position : null
            const insertion = rawInsertion === null ? null : rawInsertion - (sourceIndex >= 0 && sourceIndex < rawInsertion ? 1 : 0)
            const originalLayout = serpentineLayout(laneWidths[dayIndex] || 900, day.activities.length)
            const layout = serpentineLayout(laneWidths[dayIndex] || 900, day.activities.length - (sourceIndex >= 0 ? 1 : 0) + (insertion !== null ? 1 : 0))
            // A dragged card vacates its slot; retain the day height so later dates do not jump.
            if (dragged) layout.height = Math.max(layout.height, originalLayout.height)
            const previewPosition = (position: number) => {
              const compactPosition = position - (sourceIndex >= 0 && sourceIndex < position ? 1 : 0)
              return compactPosition + (insertion !== null && compactPosition >= insertion ? 1 : 0)
            }
            const placeStyle = (position: number) => ({left:layout.point(position).x, top:layout.point(position).y, width:layout.cardWidth, height:layout.cardHeight})
            return (
              <section
                key={`${day.label}-${dayIndex}`}
                data-testid={`day-lane-${dayIndex}`}
                data-day-index={dayOffset}
                style={{position:'relative',zIndex:pendingPlace?.dayIndex===dayIndex?30:undefined}}
                className="overflow-visible rounded-[1.75rem] border border-emerald-950/10 bg-white shadow-[0_18px_45px_-32px_rgba(15,23,42,0.45)]"
                aria-labelledby={`day-heading-${dayIndex}`}
              >
                <div className="grid min-w-0 md:grid-cols-[9.5rem_minmax(0,1fr)]">
                  <div className={`bg-gradient-to-br ${accent[0]} ${accent[1]} px-5 py-5 md:min-h-[18rem] md:border-r md:border-emerald-950/10`}>
                    <div className="flex items-center justify-between md:block">
                      <div>
                        <p className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.18em] text-slate-600">
                          <span data-testid="itinerary-day-color" className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: DAY_COLORS[dayOffset % DAY_COLORS.length] }} aria-hidden="true" />
                          行程日
                        </p>
                        <h2
                          id={`day-heading-${dayIndex}`}
                          data-day-heading={dayIndex}
                          tabIndex={-1}
                          className={`mt-1 text-2xl font-semibold outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 ${accent[2]}`}
                        >
                          {day.label}
                        </h2>
                      </div>
                      <div className="flex flex-wrap items-center justify-end gap-2 md:mt-4 md:justify-start">
                      <span className="rounded-full bg-white/75 px-3 py-1.5 text-xs font-semibold text-slate-600">
                        {day.activities.length} 个地点
                      </span>
                      {onAlternatives && !!day.alternatives?.length && <button
                        type="button" className="min-h-11 whitespace-nowrap rounded-full bg-white/80 px-3 text-xs font-semibold text-sky-800 hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600"
                        data-testid={`day-alternatives-${dayIndex}`}
                        aria-label={`${day.label}的备选地点`}
                        aria-controls="journey-suggestions"
                        onClick={event => onAlternatives(dayIndex, event.currentTarget)}
                      >备选 · {day.alternatives.length}</button>}
                      </div>
                    </div>

                  </div>

                  <div className="min-w-0 px-4 py-4 sm:px-5">


                    {layoutMode === 'LIST' ? (
                      <ol className="mt-3 grid gap-3" aria-label={`${day.label} 地点列表`}>
                        {day.activities.map((activity, position) => {
                          const item = { card: activity, dayIndex, position }
                          return (
                            <li key={activity.activity_token} className="grid min-h-16 grid-cols-[2rem_minmax(0,1fr)_auto] items-center gap-3 rounded-2xl border border-slate-200 bg-white p-3">
                              <span className="flex h-8 w-8 items-center justify-center rounded-full bg-[#0c789d] text-xs font-bold text-white">{position + 1}</span>
                              <button type="button" onClick={(event) => openDetails(item, event.currentTarget)} className="min-h-11 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d]">
                                <strong className="block text-sm text-slate-900">{activity.name}</strong>
                                <span className="text-xs text-slate-500">{activity.status === 'READY' ? '已匹配' : '待确认'}</span>
                              </button>
                              {pendingPlace?.card.activity_token===activity.activity_token && <div className="col-span-full"><PendingPlaceDropdown card={activity} resource={resource} disabled={locked} onCommand={onCommand} onClose={closePendingPlace}/></div>}
                            </li>
                          )
                        })}
                        <li>
                          <button type="button" disabled={locked} onClick={() => onAdd(dayIndex, day.activities.length)} className="min-h-11 w-full rounded-xl border border-dashed border-[#0c789d]/30 bg-sky-50/50 text-sm font-semibold text-[#0c789d] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] disabled:opacity-50">添加地点</button>
                        </li>
                      </ol>
                    ) : (
                    <div className="relative mt-2">
                      <div
                        ref={(node) => {
                          if (node) laneScrollers.current.set(dayIndex, node)
                          else laneScrollers.current.delete(dayIndex)
                        }}

                        onDragOver={(event) => {
                          if (!dragged || (!event.clientX && !event.clientY)) return
                          event.preventDefault()
                          const target = targetAt(event.clientX,event.clientY)
                          pointerDropTarget.current = target
                          setDropTarget(target)

                        }}
                        className="serpentine-canvas"
                        data-testid={`serpentine-canvas-${dayIndex}`} data-columns={layout.columns}
                        style={{height:layout.height}}
                      >
                        {!dragged && <SerpentineConnectors day={day} layout={layout} mapView={mapView} pending={operationPending || routesPending} />}
                        {insertion !== null && <div className="serpentine-placeholder" style={placeStyle(insertion)} data-testid="drop-preview"><strong>{dragged?.card.name}</strong><span>放在这里</span></div>}

                        {day.activities.map((activity, position) => {
                          const item = { card: activity, dayIndex, position }
                          return (
                            <motion.div key={activity.activity_token} className="serpentine-cell" initial={false} data-reverse={layout.point(previewPosition(position)).reverse} style={{...placeStyle(position),zIndex:pendingPlace?.card.activity_token===activity.activity_token?40:undefined,visibility:position === sourceIndex ? 'hidden' : 'visible'}} animate={{x:layout.point(previewPosition(position)).x-layout.point(position).x,y:layout.point(previewPosition(position)).y-layout.point(position).y}} transition={reduceMotion ? {duration:0} : {type:"spring",stiffness:390,damping:32}}>
                              <DropSlot
                                dayIndex={dayIndex}
                                rawPosition={position}
                                active={dropTarget?.dayIndex === dayIndex && dropTarget.position === position}
                                dragging={dragged !== null}
                          previewCard={undefined}
                                onDragEnter={() => setDropTarget({ dayIndex, position })}
                                onDrop={() => void handleDrop(dayIndex, position)}
                              />
                              <motion.article
                                data-testid="activity-card"
                                data-activity-name={activity.name}
                                data-drop-day={dayIndex}
                                data-drop-position={position}
                                onDragOver={(event) => {
                                  if (!dragged) return
                                  event.preventDefault()
                                  setDropTarget(targetAt(event.clientX,event.clientY))
                                }}
                                onDrop={(event) => { event.preventDefault(); if (dropTarget) void handleDrop(dropTarget.dayIndex, dropTarget.position) }}
                                style={{ opacity: dragged?.card.activity_token === activity.activity_token ? 0.2 : 1 }}
                                animate={{ rotate: dragged && dropTarget?.dayIndex === dayIndex && position >= dropTarget.position ? 1.4 : 0, scale: dragged && dragged.card.activity_token !== activity.activity_token ? 0.985 : 1 }}
                                className="soft-activity-card overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-[0_12px_28px_-20px_rgba(15,23,42,0.6)]"
                                transition={reduceMotion ? { duration: 0 } : { type: 'spring', stiffness: 420, damping: 36 }}
                              >
                                <div data-testid={`card-grab-${dayIndex}-${position}`} className={`fluid-card-grab relative h-20 overflow-hidden bg-gradient-to-br ${accent[0]} ${accent[1]}`}
                                    onPointerDown={(event) => {
                                      if (locked || event.button !== 0) return
                                      event.preventDefault()
                                      // Native HTML drag takes over touch and cancels pointer capture.
                                      event.currentTarget.draggable = false
                                      rememberTrigger(event.currentTarget.querySelector<HTMLElement>('button') || event.currentTarget)
                                      event.currentTarget.setPointerCapture(event.pointerId)
                                      touchTarget.current = null
                                      captureDropGeometry()
                                      setDragged(item)
                                      pointerPosition.current = {x:event.clientX,y:event.clientY}
                                      setTouchPoint(pointerPosition.current)
                                    }}
                                    onPointerMove={(event) => {
                                      if (!dragged || !event.currentTarget.hasPointerCapture(event.pointerId)) return
                                      pointerPosition.current = {x:event.clientX,y:event.clientY}
                                      setTouchPoint(pointerPosition.current)
                                      const element = document.elementFromPoint(event.clientX,event.clientY)
                                      if (element?.closest('[data-trash]')) {
                                        touchTarget.current = 'TRASH'
                                        setDropTarget(null)
                                        return
                                      }
                                      const target = targetAt(event.clientX,event.clientY)
                                      touchTarget.current = target
                                      setDropTarget(target)
                                      if(event.clientY > window.innerHeight-80) window.scrollBy(0,18)
                                      if(event.clientY < 100) window.scrollBy(0,-18)
                                    }}
                                    onPointerUp={(event) => {
                                      if (!event.currentTarget.hasPointerCapture(event.pointerId)) return
                                      event.currentTarget.releasePointerCapture(event.pointerId)
                                      event.currentTarget.draggable = false
                                      pointerPosition.current = null
                                      if (!dragged) return
                                      const target = document.elementFromPoint(event.clientX,event.clientY)?.closest('[data-trash]')
                                        ? 'TRASH' : targetAt(event.clientX,event.clientY)
                                      setTouchPoint(null)
                                      if(target === 'TRASH') { setDragged(null); setDropTarget(null); void applyDelete(item) }
                                      else if(target) void handleDrop(target.dayIndex,target.position)
                                      else { setDragged(null); setDropTarget(null) }
                                      touchTarget.current = null
                                    }}
                                    onPointerCancel={(event) => { event.currentTarget.draggable = false; setDragged(null); setDropTarget(null); setTouchPoint(null); touchTarget.current=null }}
>
                                  <CategoryArtwork category={activity.category} />
                                  <PlacePhoto card={activity} />
                                  <span style={{ backgroundColor: DAY_COLORS[dayOffset % DAY_COLORS.length] }} className="absolute left-3 top-3 flex h-7 min-w-7 items-center justify-center rounded-full bg-emerald-700 px-2 text-xs font-bold text-white shadow-sm">
                                    {position + 1}
                                  </span>
                                  <button
                                    type="button"
                                    draggable={!locked}
                                    data-testid={`drag-handle-${dayIndex}-${position}`}
                                    onDragStart={(event) => {
                                      rememberTrigger(event.currentTarget)
                                      captureDropGeometry()
                                      event.dataTransfer.effectAllowed = 'move'
                                      event.dataTransfer.setData('text/plain', activity.name)
                                      dragCompletedRef.current = false
                                      setDragged(item)
                                      setDropTarget(null)
                                      setAnnouncement(`正在拖动 ${activity.name}，请选择同一天或其他天的插入位置。`)
                                    }}
                                    onDragEnd={() => {
                                      if (!dragCompletedRef.current) {
                                        setAnnouncement(`${activity.name} 的拖动已取消，没有移动，也没有发送保存请求。`)
                                      }
                                      dragCompletedRef.current = false
                                      setDragged(null)
                                      setDropTarget(null)
                                    }}
                                    onKeyDown={(event) => {
                                      if (event.key === 'Delete' || event.key === 'Backspace') {
                                        event.preventDefault()
                                        openDelete(item, event.currentTarget)
                                      }
                                      if (event.key === 'Enter' || event.key === ' ') {
                                        event.preventDefault()
                                        openMove(item, event.currentTarget)
                                      }
                                    }}
                                    disabled={locked}
                                    className="absolute right-2 top-2 inline-flex min-h-12 min-w-12 touch-none cursor-grab items-center justify-center rounded-xl bg-white/90 text-slate-600 shadow-sm backdrop-blur transition motion-reduce:transition-none hover:bg-white hover:text-emerald-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 disabled:cursor-wait disabled:opacity-50"
                                    aria-label={`拖动 ${activity.name}`}
                                  >
                                    <GripVertical className="h-5 w-5" aria-hidden="true" />
                                  </button>
                                </div>

                                <button
                                  type="button"
                                  onClick={(event) => openDetails(item, event.currentTarget)}
                                  className="block w-full px-4 py-3 text-left outline-none transition motion-reduce:transition-none hover:bg-emerald-50/40 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-emerald-700"
                                >
                                  <span className="flex items-start justify-between gap-2">
                                    <span className="min-w-0">
                                      <h3 className="truncate text-sm font-semibold text-slate-800">{activity.name}</h3>

                                    </span>
                                    <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-slate-300" aria-hidden="true" />
                                  </span>
                                  <span className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px]">
                                    <span className="rounded-full bg-slate-100 px-2 py-1 text-slate-600">{activity.category}</span>
                                    <span className={activity.status === 'READY'
                                      ? 'rounded-full bg-emerald-50 px-2 py-1 text-emerald-700'
                                      : 'rounded-full bg-amber-50 px-2 py-1 text-amber-800'}
                                    >
                                      {activity.status === 'READY' ? '已匹配' : '待确认'}
                                    </span>
                                    {(activity.knowledge_suggestions?.length || 0) > 0 && (
                                      <span className="rounded-full bg-sky-50 px-2 py-1 text-sky-700">
                                        有来源建议 {activity.knowledge_suggestions?.length}
                                      </span>
                                    )}
                                  </span>
                                  <span className="sr-only">，查看详情</span>
                                </button>


                              </motion.article>
                              {pendingPlace?.card.activity_token===activity.activity_token && <div style={{position:'absolute',top:layout.cardHeight+8,...(layout.point(position).x+290>layout.width?{right:0}:{left:0})}}><PendingPlaceDropdown card={activity} resource={resource} disabled={locked} onCommand={onCommand} onClose={closePendingPlace}/></div>}
                            </motion.div>
                          )
                        })}

                        <div className="serpentine-end-slot" style={{...placeStyle(Math.max(0,day.activities.length-1)),height:day.activities.length ? layout.cardHeight : 48,left:day.activities.length ? layout.point(day.activities.length-1).x + (layout.point(day.activities.length-1).reverse ? -14 : layout.cardWidth+4) : layout.point(0).x}}><DropSlot
                          dayIndex={dayIndex}
                          rawPosition={day.activities.length}
                          active={dropTarget?.dayIndex === dayIndex && dropTarget.position === day.activities.length}
                          dragging={dragged !== null}
                          previewCard={undefined}
                          onDragEnter={() => setDropTarget({ dayIndex, position: day.activities.length })}
                          onDrop={() => void handleDrop(dayIndex, day.activities.length)}
                        /></div>

                        <button
                          type="button"
                          data-testid={`day-${dayIndex}-add`}
                          data-day-add={dayIndex}
                          aria-label={`新增地点到 ${day.label}`}
                          disabled={locked}
                          onClick={() => onAdd(dayIndex, day.activities.length)}
                          style={{right:8,bottom:4,position:"absolute"}}
                          className="soft-add-card flex min-h-[13rem] w-[4rem] shrink-0 snap-start flex-col items-center justify-center rounded-2xl border border-dashed border-emerald-900/20 bg-emerald-50/30 px-4 text-center text-sm text-emerald-800 transition motion-reduce:transition-none hover:border-emerald-600 hover:bg-emerald-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2 disabled:cursor-wait disabled:opacity-50"
                        >
                          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-white shadow-sm">
                            <Plus className="h-5 w-5" aria-hidden="true" />
                          </span>
                          <span className="sr-only">添加地点</span>

                        </button>
                      </div>
                    </div>
                    )}
                  </div>
                </div>
              </section>
            )
          })}
        </div>
      </div>


      {dragged && touchPoint && <div className="soft-touch-ghost fluid-card-ghost" style={{left:touchPoint.x,top:touchPoint.y}} aria-hidden="true"><div className="fluid-drop-art"><PlacePhoto card={dragged.card} /><MapPin /></div><strong>{dragged.card.name}</strong></div>}
      {dragged && (
        <div data-testid="drag-trash" className="soft-drag-trash" data-trash="true"
          onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = 'move' }}
          onDrop={(event) => {
            event.preventDefault()
            dragCompletedRef.current = true
            const item = dragged
            setDragged(null); setDropTarget(null)
            void applyDelete(item)
          }}>
          <Trash2 aria-hidden="true" /> 松开删除
        </div>
      )}


      <p className="sr-only" aria-live="polite" aria-atomic="true" data-testid="itinerary-live-status">{announcement}</p>

      <AnimatePresence>
        {dialog?.kind === 'DELETE' && (
          <AccessibleDialog key="delete-activity" titleId="delete-activity-title" descriptionId="delete-activity-description" onClose={() => closeDialog()} returnFocusRef={lastTriggerRef} dismissDisabled={locked}>
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs font-semibold text-amber-700">删除地点</p>
                <h2 id="delete-activity-title" className="mt-1 text-xl font-semibold">删除“{dialog.item.card.name}”？</h2>
              </div>
              <DialogCloseButton onClick={() => closeDialog()} label="关闭删除确认" />
            </div>
            <p id="delete-activity-description" className="mt-4 text-sm leading-6 text-slate-600">只会删除这张地点卡片。即使它是当天最后一个地点，{localDays[dialog.item.dayIndex - 1]?.label || '当天'}也会保留。</p>
            <div className="mt-6 grid grid-cols-2 gap-3">
              <button data-dialog-initial-focus type="button" onClick={() => closeDialog()} className="min-h-12 rounded-xl border border-slate-300 px-4 text-sm font-semibold text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700">取消</button>
              <button data-testid="confirm-delete" type="button" disabled={locked} onClick={() => void applyDelete(dialog.item)} className="min-h-12 rounded-xl bg-slate-900 px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-900 focus-visible:ring-offset-2 disabled:cursor-wait disabled:opacity-50">{locked ? '正在删除…' : '确认删除'}</button>
            </div>
          </AccessibleDialog>
        )}
      </AnimatePresence>
    </div>
  )
}


function normalizeDropPosition(item: DraggedCard, targetDayIndex: number, rawPosition: number): number | null {
  let targetPosition = rawPosition
  if (item.dayIndex === targetDayIndex && rawPosition > item.position) targetPosition -= 1
  if (item.dayIndex === targetDayIndex && targetPosition === item.position) return null
  return targetPosition
}


function moveCard(
  days: DayView[],
  item: CardLocation,
  targetDayIndex: number,
  targetPosition: number,
): DayView[] | null {
  if (!days[targetDayIndex - 1]) return null
  const next = days.map((day) => ({ ...day, activities: [...day.activities] }))
  let moving: ActivityCardView | undefined
  for (const day of next) {
    const sourceIndex = day.activities.findIndex((activity) => activity.activity_token === item.card.activity_token)
    if (sourceIndex >= 0) {
      moving = day.activities.splice(sourceIndex, 1)[0]
      break
    }
  }
  if (!moving) return null
  const target = next[targetDayIndex - 1].activities
  target.splice(Math.max(0, Math.min(targetPosition, target.length)), 0, moving)
  return next
}


function removeCard(days: DayView[], activityToken: string): DayView[] {
  return days.map((day) => ({
    ...day,
    activities: day.activities.filter((activity) => activity.activity_token !== activityToken),
  }))
}


function SerpentineConnectors({day,layout,mapView,pending}: {day:DayView;layout:ReturnType<typeof serpentineLayout>;mapView:MapRenderView;pending:boolean}) {
  return <div className="serpentine-connections">
    {day.activities.slice(0,-1).map((card,index)=>{
      const edge=serpentineEdge(layout,index)
      const connector=transportConnectorFor(day,card,day.activities[index+1],mapView,pending)
      const available=connector.status==='AVAILABLE'
      const Icon=available ? connector.mode==='walking' ? Footprints : BusFront : ArrowRight
      const label=available ? `${connector.mode==='walking'?'步行':'公交'} · ${connector.durationMinutes} 分钟${connector.distanceMeters===null?'':` · ${distanceLabel(connector.distanceMeters)}`}` : connector.status==='NEEDS_UPDATE' ? '路线需要更新' : '路线待确认'
      return <div key={index} data-testid="transport-connector" data-connector-status={connector.status} data-turn={edge.turn} aria-label={label}>
        <svg className="serpentine-edge" width={layout.width} height={layout.height} aria-hidden="true">
          <path data-testid="order-arc" d={edge.path} fill="none" stroke="currentColor" strokeWidth="1.4"/>
          <path d={`M ${edge.arrowX-3} ${edge.arrowY-edge.arrowDirection*7} l 3 ${edge.arrowDirection*5} l 3 ${-edge.arrowDirection*5}`} fill="none" stroke="currentColor" strokeWidth="1.4"/>
        </svg>
        <span className={`serpentine-route-label ${available?'is-available':''}`} style={{left:edge.x,top:edge.y}}><Icon aria-hidden="true"/>{label}</span>
      </div>
    })}
  </div>
}


function CategoryArtwork({ category }: { category: string }) {
  const normalized = category.toLowerCase()
  const Icon = normalized.includes('餐') || normalized.includes('food')
    ? UtensilsCrossed
    : normalized.includes('住') || normalized.includes('酒店')
      ? BedDouble
      : normalized.includes('交通') || normalized.includes('车站')
        ? ArrowRight
        : normalized.includes('公园') || normalized.includes('自然')
          ? Sparkles
          : MapPin
  return (
    <div className="absolute inset-0 flex items-center justify-center" aria-hidden="true">
      <div className="absolute -bottom-7 left-4 h-16 w-28 rounded-[50%] bg-white/55" />
      <div className="absolute -bottom-8 right-0 h-20 w-32 rounded-[50%] bg-emerald-900/10" />
      <Icon className="relative h-8 w-8 text-emerald-800/55" />
    </div>
  )
}


function DropSlot({
  dayIndex,
  rawPosition,
  active,
  dragging,
  previewCard,
  onDragEnter,
  onDrop,
}: {
  dayIndex: number
  rawPosition: number
  active: boolean
  dragging: boolean
  previewCard?: ActivityCardView
  onDragEnter: () => void
  onDrop: () => void
}) {
  return (
    <div
      data-testid={`drop-slot-${dayIndex}-${rawPosition}`}
      data-drop-day={dayIndex}
      data-drop-position={rawPosition}
      style={{ width: dragging && active ? 216 : 16 }}
      className={`soft-drop-slot mx-1 flex h-[13rem] shrink-0 items-center justify-center rounded-3xl transition-all motion-reduce:transition-none bg-transparent`}
      onDragEnter={(event) => {
        event.preventDefault()
        onDragEnter()
      }}
      onDragOver={(event) => {
        event.preventDefault()
        event.dataTransfer.dropEffect = 'move'
      }}
      onDrop={(event: DragEvent<HTMLDivElement>) => {
        event.preventDefault()
        onDrop()
      }}
      aria-hidden="true"
    >
      {active && dragging && previewCard ? <div className="fluid-drop-preview" data-testid="drop-preview"><span className="fluid-drop-art"><PlacePhoto card={previewCard} /><MapPin aria-hidden="true" /></span><strong>{previewCard.name}</strong><span>放在这里</span></div> : <span className="sr-only">落点</span>}
    </div>
  )
}


function DialogCloseButton({ onClick, label }: { onClick: () => void; label: string }) {
  return (
    <button type="button" onClick={onClick} className="inline-flex min-h-12 min-w-12 items-center justify-center rounded-xl bg-slate-100 text-slate-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700" aria-label={label}>
      <X className="h-5 w-5" aria-hidden="true" />
    </button>
  )
}


function PlacePhoto({card}: {card: ActivityCardView}) {
  const [failed, setFailed] = useState(false)
  useEffect(() => setFailed(false), [card.photo_url])
  if (card.status !== 'READY' || !card.photo_url || failed) return null
  return (
    // Photo belongs to the resolved POI; never search for a replacement in the browser.
    // eslint-disable-next-line @next/next/no-img-element
    <img src={card.photo_url} alt="" loading="lazy" referrerPolicy="no-referrer"
      onError={() => setFailed(true)} className="absolute inset-0 h-full w-full object-cover" />
  )
}
