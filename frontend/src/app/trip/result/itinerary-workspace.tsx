'use client'

import {
  type DragEvent,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import {
  ArrowLeftRight,
  ArrowRight,
  BedDouble,
  BusFront,
  CalendarDays,
  ChevronRight,
  Clock3,
  ExternalLink,
  Footprints,
  GripVertical,
  List,
  MapPin,
  Pencil,
  Plus,
  Replace,
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
import AccessibleDialog from './accessible-dialog'
import { DAY_ACCENTS, DAY_COLORS, transportConnectorFor, type TransportConnector } from './result-presentation'


type DayView = UserFacingTripResult['days'][number]

type CardLocation = {
  card: ActivityCardView
  dayIndex: number
  position: number
}

type DialogState =
  | { kind: 'DETAIL'; item: CardLocation }
  | { kind: 'MOVE'; item: CardLocation }
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
  onCommand: (command: TripUnderstandingCommand) => Promise<WorkspaceCommandResult>
  onAdd: (dayIndex: number, position: number) => void
  onEdit: (item: CardLocation) => void
  onReplace: (item: CardLocation) => void
}


const KNOWLEDGE_LABELS: Record<NonNullable<ActivityCardView['knowledge_suggestions']>[number]['type'], string> = {
  TYPICAL_DURATION: '游览时长',
  SUITABLE_TIME: '适合时段',
  NIGHT_VIEW: '夜景建议',
  SEASON: '季节提示',
  RESERVATION_ADVICE: '预约建议',
}

export default function ItineraryWorkspace({
  days,
  toolbar,
  disabled,
  routesPending,
  mapView,
  onCommand,
  onAdd,
  onEdit,
  onReplace,
}: ItineraryWorkspaceProps) {
  const reduceMotion = useReducedMotion()
  const [localDays, setLocalDays] = useState(days)
  const [dragged, setDragged] = useState<DraggedCard | null>(null)
  const touchTarget = useRef<DropTarget | 'TRASH' | null>(null)
  const pointerPosition = useRef<{x:number;y:number} | null>(null)
  const [touchPoint, setTouchPoint] = useState<{x:number;y:number} | null>(null)
  const [dropTarget, setDropTarget] = useState<DropTarget | null>(null)
  const [dialog, setDialog] = useState<DialogState | null>(null)
  const [moveDay, setMoveDay] = useState(1)
  const [movePosition, setMovePosition] = useState(0)
  const [operationPending, setOperationPending] = useState(false)
  const [layoutMode, setLayoutMode] = useState<'CHAIN' | 'LIST'>('CHAIN')
  const [announcement, setAnnouncement] = useState('行程卡片已加载，可以拖拽或使用移动按钮调整。')
  const lastTriggerRef = useRef<HTMLElement | null>(null)
  const operationLockRef = useRef(false)
  const dragCompletedRef = useRef(false)
  const laneScrollers = useRef(new Map<number, HTMLDivElement>())
  const synchronizingScroll = useRef(false)
  const dragGeometry = useRef(new Map<number, number[]>())
  const pointerDropTarget = useRef<DropTarget | null>(null)
  const captureDropGeometry = () => {
    pointerDropTarget.current = null
    dragGeometry.current.clear()
    laneScrollers.current.forEach((lane, dayIndex) => {
      const left = lane.getBoundingClientRect().left
      dragGeometry.current.set(dayIndex, Array.from(lane.querySelectorAll('[data-testid="activity-card"]')).map(card => {
        const box = card.getBoundingClientRect()
        return box.left - left + lane.scrollLeft + box.width / 2
      }))
    })
  }
  const targetAt = (x: number, y: number): DropTarget | null => {
    for (const [dayIndex, centers] of dragGeometry.current) {
      const lane = laneScrollers.current.get(dayIndex)
      if (!lane) continue
      const box = lane.getBoundingClientRect()
      const dayBox = lane.closest('[data-day-index]')?.getBoundingClientRect() || box
      if(x >= dayBox.left && x <= dayBox.right && y >= dayBox.top && y <= dayBox.bottom) {
        return {dayIndex,position:centers.filter(center => center < x-box.left+lane.scrollLeft).length}
      }
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
      const target=touchTarget.current
      if(target && target !== 'TRASH') {
        const lane=laneScrollers.current.get(target.dayIndex)
        if(lane) {
          const box=lane.getBoundingClientRect()
          if(touchPoint.x>box.right-48) lane.scrollLeft+=12
          else if(touchPoint.x<box.left+48) lane.scrollLeft-=12
        }
      }
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

  const synchronizeLaneScroll = (sourceDay: number, scrollLeft: number) => {
    if (synchronizingScroll.current) return
    synchronizingScroll.current = true
    laneScrollers.current.forEach((scroller, day) => {
      if (day !== sourceDay) scroller.scrollLeft = scrollLeft
    })
    window.requestAnimationFrame(() => {
      synchronizingScroll.current = false
    })
  }

  const closeDialog = (restoreFocus = true) => {
    setDialog(null)
    if (restoreFocus) {
      window.setTimeout(() => lastTriggerRef.current?.focus(), 0)
    }
  }

  const openDetails = (item: CardLocation, element: HTMLElement) => {
    rememberTrigger(element)
    setDialog({ kind: 'DETAIL', item })
  }

  const openMove = (item: CardLocation, element?: HTMLElement) => {
    if (element) rememberTrigger(element)
    const anotherDay = localDays.findIndex((_, index) => index + 1 !== item.dayIndex)
    const initialDay = anotherDay >= 0 ? anotherDay + 1 : item.dayIndex
    const initialLength = localDays[initialDay - 1]?.activities.length ?? 0
    setMoveDay(initialDay)
    setMovePosition(initialDay === item.dayIndex ? Math.max(0, initialLength - 1) : initialLength)
    setDialog({ kind: 'MOVE', item })
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

  const confirmMove = async () => {
    if (!dialog || dialog.kind !== 'MOVE') return
    const targetDay = localDays[moveDay - 1]
    if (!targetDay) return
    await applyMove(
      dialog.item,
      moveDay,
      movePosition,
      `${dialog.item.card.name} 已移动并自动保存。路线需要手动更新。`,
    )
  }

  const currentMoveSlots = dialog?.kind === 'MOVE'
    ? moveSlots(localDays, dialog.item, moveDay)
    : []
  const moveIsNoop = dialog?.kind === 'MOVE'
    && dialog.item.dayIndex === moveDay
    && dialog.item.position === movePosition

  return (
    <div
      data-testid="itinerary-workspace"
      data-reduced-motion={reduceMotion ? 'true' : 'false'}
      className="soft-workspace mt-2"
    >
      <div className="min-w-0">
        <div className="mb-4 flex items-center justify-end gap-2">
          <button type="button" className="e-button" aria-label={layoutMode === 'CHAIN' ? '切换为列表' : '切换为横链'}
            title={layoutMode === 'CHAIN' ? '列表' : '横链'} aria-pressed={layoutMode === 'LIST'}
            onClick={() => setLayoutMode(layoutMode === 'CHAIN' ? 'LIST' : 'CHAIN')}>
            <List aria-hidden="true" />
          </button>
          {toolbar}
        </div>

        <div data-testid="trip-days" className="space-y-4" aria-label="按天排列的游览顺序">
          {localDays.map((day, dayOffset) => {
            const dayIndex = dayOffset + 1
            const accent = DAY_ACCENTS[dayOffset % DAY_ACCENTS.length]
            return (
              <section
                key={`${day.label}-${dayIndex}`}
                data-testid={`day-lane-${dayIndex}`}
                data-day-index={dayOffset}
                className="overflow-hidden rounded-[1.75rem] border border-emerald-950/10 bg-white shadow-[0_18px_45px_-32px_rgba(15,23,42,0.45)]"
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
                      <span className="rounded-full bg-white/75 px-3 py-1.5 text-xs font-semibold text-slate-600 md:mt-4 md:inline-block">
                        {day.activities.length} 个地点
                      </span>
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
                                <span className="text-xs text-slate-500">{activity.time_hint || '时间待定'} · {activity.status === 'READY' ? '已确认' : '待确认'}</span>
                              </button>
                              <button type="button" disabled={locked} onClick={(event) => openMove(item, event.currentTarget)} className="min-h-11 rounded-xl border border-slate-200 px-3 text-sm font-semibold text-[#0c789d] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0c789d] disabled:opacity-50">移动</button>
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
                        onScroll={(event) => synchronizeLaneScroll(dayIndex, event.currentTarget.scrollLeft)}
                        onDragOver={(event) => {
                          if (!dragged || (!event.clientX && !event.clientY)) return
                          event.preventDefault()
                          const target = targetAt(event.clientX,event.clientY)
                          pointerDropTarget.current = target
                          setDropTarget(target)
                          const box = event.currentTarget.getBoundingClientRect()
                          if(event.clientX > box.right-35) event.currentTarget.scrollLeft += 12
                          if(event.clientX < box.left+35) event.currentTarget.scrollLeft -= 12
                        }}
                        className="relative flex min-h-[15.5rem] snap-x items-start overflow-x-auto pb-2 pt-5 [scrollbar-width:thin]"
                      >
                        {day.activities.map((activity, position) => {
                          const item = { card: activity, dayIndex, position }
                          return (
                            <div key={activity.activity_token} className="flex shrink-0 items-start">
                              <DropSlot
                                dayIndex={dayIndex}
                                rawPosition={position}
                                active={dropTarget?.dayIndex === dayIndex && dropTarget.position === position}
                                dragging={dragged !== null}
                          previewCard={dragged?.card}
                                onDragEnter={() => setDropTarget({ dayIndex, position })}
                                onDrop={() => void handleDrop(dayIndex, position)}
                              />
                              <motion.article
                                layout
                                data-testid="activity-card"
                                data-activity-name={activity.name}
                                data-drop-day={dayIndex}
                                data-drop-position={position}
                                onDragOver={(event) => {
                                  if (!dragged) return
                                  event.preventDefault()
                                  const box = event.currentTarget.getBoundingClientRect()
                                  setDropTarget({ dayIndex, position: position + (event.clientX > box.left + box.width / 2 ? 1 : 0) })
                                }}
                                onDrop={(event) => { event.preventDefault(); if (dropTarget) void handleDrop(dropTarget.dayIndex, dropTarget.position) }}
                                style={{ opacity: dragged?.card.activity_token === activity.activity_token ? 0.2 : 1 }}
                                animate={{ rotate: dragged && dropTarget?.dayIndex === dayIndex && position >= dropTarget.position ? 1.4 : 0, scale: dragged && dragged.card.activity_token !== activity.activity_token ? 0.985 : 1 }}
                                className="soft-activity-card w-[13.5rem] snap-start overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-[0_12px_28px_-20px_rgba(15,23,42,0.6)]"
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
                                      <span className="mt-1 flex items-center gap-1 text-xs text-slate-500">
                                        <Clock3 className="h-3.5 w-3.5" aria-hidden="true" />
                                        {activity.time_hint || '时间待定'}
                                      </span>
                                    </span>
                                    <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-slate-300" aria-hidden="true" />
                                  </span>
                                  <span className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px]">
                                    <span className="rounded-full bg-slate-100 px-2 py-1 text-slate-600">{activity.category}</span>
                                    <span className={activity.status === 'READY'
                                      ? 'rounded-full bg-emerald-50 px-2 py-1 text-emerald-700'
                                      : 'rounded-full bg-amber-50 px-2 py-1 text-amber-800'}
                                    >
                                      {activity.status === 'READY' ? '已确认' : '待确认'}
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
                              {position < day.activities.length - 1 && (
                                <TransportConnectorView
                                  connector={transportConnectorFor(
                                    day,
                                    activity,
                                    day.activities[position + 1],
                                    mapView,
                                    operationPending ||
                                      routesPending,
                                  )}
                                />
                              )}
                            </div>
                          )
                        })}

                        <DropSlot
                          dayIndex={dayIndex}
                          rawPosition={day.activities.length}
                          active={dropTarget?.dayIndex === dayIndex && dropTarget.position === day.activities.length}
                          dragging={dragged !== null}
                          previewCard={dragged?.card}
                          onDragEnter={() => setDropTarget({ dayIndex, position: day.activities.length })}
                          onDrop={() => void handleDrop(dayIndex, day.activities.length)}
                        />

                        <button
                          type="button"
                          data-testid={`day-${dayIndex}-add`}
                          data-day-add={dayIndex}
                          aria-label={`新增地点到 ${day.label}`}
                          disabled={locked}
                          onClick={() => onAdd(dayIndex, day.activities.length)}
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
        {dialog?.kind === 'DETAIL' && (
          <AccessibleDialog key="activity-detail" titleId="activity-detail-title" onClose={() => closeDialog()} returnFocusRef={lastTriggerRef}>
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs font-semibold text-emerald-700">{dialog.item.card.category}</p>
                <h2 id="activity-detail-title" className="mt-1 text-2xl font-semibold text-slate-900">{dialog.item.card.name}</h2>
              </div>
              <DialogCloseButton onClick={() => closeDialog()} label="关闭地点详情" />
            </div>
            <div className="mt-5 space-y-3 rounded-2xl bg-slate-50 p-4 text-sm text-slate-600">
              <p className="flex items-start gap-2"><MapPin className="mt-0.5 h-4 w-4 shrink-0 text-emerald-700" aria-hidden="true" />{dialog.item.card.area_or_address}</p>
              <p className="flex items-center gap-2"><CalendarDays className="h-4 w-4 text-emerald-700" aria-hidden="true" />{dialog.item.card.time_hint || '时间待定'}</p>
            </div>
            {(dialog.item.card.knowledge_suggestions?.length || 0) > 0 && (
              <section className="mt-5" aria-labelledby="knowledge-suggestions-title" data-testid="knowledge-suggestions">
                <div className="flex items-center gap-2">
                  <Sparkles className="h-4 w-4 text-sky-700" aria-hidden="true" />
                  <h3 id="knowledge-suggestions-title" className="text-sm font-semibold text-slate-800">出行建议</h3>
                </div>
                <ul className="mt-3 space-y-3">
                  {dialog.item.card.knowledge_suggestions?.map((suggestion) => {
                    const sourceUrl = safeExternalUrl(suggestion.source_url)
                    return (
                    <li key={`${suggestion.type}-${suggestion.source_url}-${suggestion.text}`} className="rounded-2xl border border-sky-100 bg-sky-50/65 p-4">
                      <p className="text-xs font-semibold text-sky-800">{KNOWLEDGE_LABELS[suggestion.type]}</p>
                      <p className="mt-1 text-sm leading-6 text-slate-700">{suggestion.text}</p>
                      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
                        {sourceUrl ? (
                          <a
                            href={sourceUrl}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex min-h-10 items-center gap-1 rounded-lg px-1 font-medium text-sky-800 underline decoration-sky-300 underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-700"
                          >
                            {suggestion.source_name}
                            <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                          </a>
                        ) : <span>{suggestion.source_name}</span>}
                        <span>{suggestion.freshness}</span>
                      </div>
                    </li>
                    )
                  })}
                </ul>
              </section>
            )}
            <div className="mt-5 grid grid-cols-2 gap-2">
              <DialogAction onClick={() => { closeDialog(false); onEdit(dialog.item) }} icon={<Pencil className="h-4 w-4" />}>编辑文字</DialogAction>
              <DialogAction onClick={() => { closeDialog(false); onReplace(dialog.item) }} icon={<Replace className="h-4 w-4" />}>替换地点</DialogAction>
              <DialogAction onClick={() => openMove(dialog.item)} icon={<ArrowLeftRight className="h-4 w-4" />}>移动位置</DialogAction>
              {dialog.item.dayIndex < localDays.length && (
                <DialogAction
                  onClick={() => {
                    const item = dialog.item
                    const targetDay = localDays[item.dayIndex]
                    closeDialog(false)
                    void applyMove(
                      item,
                      item.dayIndex + 1,
                      targetDay.activities.length,
                      `${item.card.name} 已移到 ${targetDay.label}。路线需要手动更新。`,
                    )
                  }}
                  icon={<ArrowRight className="h-4 w-4" />}
                >
                  移到后一天
                </DialogAction>
              )}
            </div>

          </AccessibleDialog>
        )}

        {dialog?.kind === 'MOVE' && (
          <AccessibleDialog key="move-activity" titleId="move-activity-title" onClose={() => closeDialog()} returnFocusRef={lastTriggerRef} dismissDisabled={locked}>
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs font-semibold text-emerald-700">移动地点</p>
                <h2 id="move-activity-title" className="mt-1 text-xl font-semibold">把“{dialog.item.card.name}”移到哪里？</h2>
              </div>
              <DialogCloseButton onClick={() => closeDialog()} label="关闭移动面板" />
            </div>
            <div className="mt-5 space-y-4">
              <label className="block text-sm font-medium text-slate-700">
                目标日期
                <select
                  data-testid="move-target-day"
                  value={moveDay}
                  onChange={(event) => {
                    const nextDay = Number(event.target.value)
                    const nextSlots = moveSlots(localDays, dialog.item, nextDay)
                    setMoveDay(nextDay)
                    setMovePosition(nextSlots[nextSlots.length - 1] ?? 0)
                  }}
                  className="mt-2 min-h-12 w-full rounded-xl border border-slate-300 bg-white px-3 outline-none focus:border-emerald-700 focus:ring-2 focus:ring-emerald-700/20"
                >
                  {localDays.map((day, index) => <option key={day.label} value={index + 1}>{day.label}</option>)}
                </select>
              </label>
              <label className="block text-sm font-medium text-slate-700">
                目标位置
                <select
                  data-testid="move-target-position"
                  value={movePosition}
                  onChange={(event) => setMovePosition(Number(event.target.value))}
                  className="mt-2 min-h-12 w-full rounded-xl border border-slate-300 bg-white px-3 outline-none focus:border-emerald-700 focus:ring-2 focus:ring-emerald-700/20"
                >
                  {currentMoveSlots.map((position) => (
                    <option key={position} value={position}>{position === currentMoveSlots[currentMoveSlots.length - 1] ? `末尾（第 ${position + 1} 站）` : `第 ${position + 1} 站`}</option>
                  ))}
                </select>
              </label>
              {moveIsNoop && <p className="rounded-xl bg-amber-50 px-3 py-2 text-sm text-amber-800">当前已在这个位置，请选择其他位置。</p>}
            </div>
            <div className="mt-6 grid grid-cols-2 gap-3">
              <button type="button" onClick={() => closeDialog()} className="min-h-12 rounded-xl border border-slate-300 px-4 text-sm font-semibold text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700">取消</button>
              <button data-testid="confirm-move" type="button" disabled={locked || moveIsNoop} onClick={() => void confirmMove()} className="min-h-12 rounded-xl bg-emerald-700 px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2 disabled:cursor-wait disabled:opacity-50">{locked ? '正在保存…' : '确认移动'}</button>
            </div>
          </AccessibleDialog>
        )}

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


function moveSlots(days: DayView[], item: CardLocation, targetDayIndex: number): number[] {
  const target = days[targetDayIndex - 1]
  if (!target) return []
  const remainingCount = target.activities.length - (targetDayIndex === item.dayIndex ? 1 : 0)
  return Array.from({ length: remainingCount + 1 }, (_, index) => index)
}


function safeExternalUrl(value: string): string | null {
  try {
    const parsed = new URL(value)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.toString() : null
  } catch {
    return null
  }
}


function TransportConnectorView({ connector }: { connector: TransportConnector }) {
  const available = connector.status === 'AVAILABLE'
  const needsUpdate = connector.status === 'NEEDS_UPDATE'
  const Icon = available ? (connector.mode === 'transit' ? BusFront : Footprints) : ArrowRight
  const label = available
    ? `${connector.mode === 'walking' ? '步行' : '公交'} · ${connector.durationMinutes} 分钟`
    : needsUpdate
      ? '路线需要更新'
      : '路线待确认'
  return (
    <div
      data-testid="transport-connector"
      data-connector-status={connector.status}
      className="soft-connector mx-1 mt-[4.25rem] flex w-[7.25rem] shrink-0 flex-col items-center text-center"
      aria-label={label}
    >
      <div className="relative h-16 w-full text-slate-400" aria-hidden="true">
        <svg viewBox="0 0 116 64" className="absolute inset-0 h-full w-full" fill="none">
          <path data-testid="sagging-chain" d="M 0 4 C 26 4 22 54 58 54 C 94 54 90 4 116 4" stroke="currentColor" strokeWidth="1.5" />
        </svg>
        <span className="absolute left-1/2 top-9 flex h-8 w-8 -translate-x-1/2 items-center justify-center rounded-full bg-white shadow-sm"><Icon className="h-4 w-4" /></span>
      </div>
      <span className={`mt-1 rounded-full px-2 py-1 text-[10px] font-semibold ${available ? 'bg-emerald-50 text-emerald-800' : 'bg-slate-100 text-slate-600'}`}>
        {label}
      </span>
    </div>
  )
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
      className={`soft-drop-slot mx-1 flex h-[13rem] shrink-0 items-center justify-center rounded-3xl transition-all motion-reduce:transition-none ${active ? 'bg-sky-100/70 ring-2 ring-inset ring-sky-300' : 'bg-transparent'}`}
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


function DialogAction({ onClick, icon, children }: { onClick: () => void; icon: ReactNode; children: ReactNode }) {
  return (
    <button type="button" onClick={onClick} className="inline-flex min-h-12 items-center justify-center gap-2 rounded-xl bg-slate-100 px-3 text-sm font-medium text-slate-700 transition motion-reduce:transition-none hover:bg-emerald-50 hover:text-emerald-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700">
      <span aria-hidden="true">{icon}</span>{children}
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
