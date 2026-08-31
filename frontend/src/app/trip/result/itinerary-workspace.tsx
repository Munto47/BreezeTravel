'use client'

import {
  type DragEvent,
  type KeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import {
  ArrowDown,
  ArrowLeftRight,
  ArrowRight,
  ArrowUp,
  BedDouble,
  CalendarDays,
  Check,
  ChevronRight,
  Circle,
  Clock3,
  Compass,
  ExternalLink,
  GripVertical,
  MapPin,
  MoveHorizontal,
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
  type TripUnderstandingCommand,
  type UserFacingTripResult,
} from '@/lib/trip-understanding-v3'


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

type WorkspaceCommandResult =
  | { status: 'APPLIED' | 'SYNCED'; days: DayView[] }
  | { status: 'RECONCILING' }

type ItineraryWorkspaceProps = {
  days: UserFacingTripResult['days']
  disabled: boolean
  mapStatus: UserFacingTripResult['map']['status']
  checkStatus: string
  onCommand: (command: TripUnderstandingCommand) => Promise<WorkspaceCommandResult>
  onAdd: (dayIndex: number, position: number) => void
  onEdit: (item: CardLocation) => void
  onReplace: (item: CardLocation) => void
}


const MAP_LABELS: Record<UserFacingTripResult['map']['status'], string> = {
  PREPARING: '路线准备中',
  AVAILABLE: '路线已准备',
  NEEDS_UPDATE: '需要手动更新',
  LIMITED: '部分路线可用',
  UNAVAILABLE: '暂时不可用',
}

const KNOWLEDGE_LABELS: Record<NonNullable<ActivityCardView['knowledge_suggestions']>[number]['type'], string> = {
  TYPICAL_DURATION: '游览时长',
  SUITABLE_TIME: '适合时段',
  NIGHT_VIEW: '夜景建议',
  SEASON: '季节提示',
  RESERVATION_ADVICE: '预约建议',
}

const DAY_ACCENTS = [
  ['from-amber-50', 'to-emerald-50', 'text-emerald-800'],
  ['from-sky-50', 'to-teal-50', 'text-teal-800'],
  ['from-violet-50', 'to-amber-50', 'text-violet-800'],
  ['from-rose-50', 'to-orange-50', 'text-orange-800'],
]


export default function ItineraryWorkspace({
  days,
  disabled,
  mapStatus,
  checkStatus,
  onCommand,
  onAdd,
  onEdit,
  onReplace,
}: ItineraryWorkspaceProps) {
  const reduceMotion = useReducedMotion()
  const [localDays, setLocalDays] = useState(days)
  const [dragged, setDragged] = useState<DraggedCard | null>(null)
  const [dropTarget, setDropTarget] = useState<DropTarget | null>(null)
  const [dialog, setDialog] = useState<DialogState | null>(null)
  const [moveDay, setMoveDay] = useState(1)
  const [movePosition, setMovePosition] = useState(0)
  const [operationPending, setOperationPending] = useState(false)
  const [announcement, setAnnouncement] = useState('行程卡片已加载，可以拖拽或使用移动按钮调整。')
  const lastTriggerRef = useRef<HTMLElement | null>(null)
  const operationLockRef = useRef(false)
  const dragCompletedRef = useRef(false)
  const locked = disabled || operationPending

  useEffect(() => {
    setLocalDays(days)
  }, [days])

  const totalPlaces = useMemo(
    () => localDays.reduce((total, day) => total + day.activities.length, 0),
    [localDays],
  )
  const pendingPlaces = useMemo(
    () => localDays.reduce(
      (total, day) => total + day.activities.filter((activity) => activity.status === 'NEEDS_CONFIRMATION').length,
      0,
    ),
    [localDays],
  )

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
    const outcome = await onCommand({
      command_type: 'ACTIVITY_MOVE',
      activity_token: item.card.activity_token,
      target_day_index: targetDayIndex,
      target_position: targetPosition,
    })
    if (outcome.status === 'APPLIED') {
      setLocalDays(outcome.days)
      finishOperation(successMessage, targetDayIndex)
    } else if (outcome.status === 'SYNCED') {
      setLocalDays(outcome.days)
      finishOperation(`${item.card.name} 的调整未能确认，已读取服务端最新行程。`, targetDayIndex)
    } else {
      finishOperation(`${item.card.name} 的调整已提交，正在确认服务端保存结果。`, targetDayIndex)
    }
    operationLockRef.current = false
    setOperationPending(false)
    return outcome.status
  }

  const applyDelete = async (item: CardLocation) => {
    if (operationLockRef.current || disabled) return
    operationLockRef.current = true
    setOperationPending(true)
    setLocalDays((current) => removeCard(current, item.card.activity_token))
    setAnnouncement(`正在删除 ${item.card.name}…`)
    const outcome = await onCommand({
      command_type: 'ACTIVITY_DELETE',
      activity_token: item.card.activity_token,
    })
    if (outcome.status === 'APPLIED') {
      setLocalDays(outcome.days)
      finishOperation(`${item.card.name} 已删除，${localDays[item.dayIndex - 1]?.label || '当天'}仍然保留。`, item.dayIndex)
    } else if (outcome.status === 'SYNCED') {
      setLocalDays(outcome.days)
      restoreOperationFocus(
        `${item.card.name} 的删除未能确认，已读取服务端最新行程。`,
        item.dayIndex,
        `删除 ${item.card.name}`,
      )
    } else {
      finishOperation(`${item.card.name} 的删除已提交，正在确认服务端保存结果。`, item.dayIndex)
    }
    operationLockRef.current = false
    setOperationPending(false)
  }

  const handleDrop = async (targetDayIndex: number, rawPosition: number) => {
    if (!dragged || locked) return
    dragCompletedRef.current = true
    const targetPosition = normalizeDropPosition(dragged, targetDayIndex, rawPosition)
    setDragged(null)
    setDropTarget(null)
    if (targetPosition === null) {
      setAnnouncement(`${dragged.card.name} 仍在原位，没有发送保存请求。`)
      return
    }
    await applyMove(
      dragged,
      targetDayIndex,
      targetPosition,
      `${dragged.card.name} 已移到 ${localDays[targetDayIndex - 1].label} 第 ${targetPosition + 1} 站。路线需要手动更新。`,
    )
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
      className="mt-8 grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_17rem]"
    >
      <div className="min-w-0">
        <div className="mb-4 flex flex-col gap-3 rounded-2xl border border-emerald-900/10 bg-white/80 px-4 py-3 shadow-sm sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-3">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-emerald-50 text-emerald-700">
              <MoveHorizontal className="h-5 w-5" aria-hidden="true" />
            </span>
            <div>
              <p className="text-sm font-semibold text-slate-800">拖动手柄调整游览顺序</p>
              <p className="text-xs leading-5 text-slate-500">也可用卡片下方的移动按钮；保存后路线只会标记为需要更新。</p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => {
              const firstDay = localDays[0]
              if (firstDay) onAdd(1, firstDay.activities.length)
            }}
            disabled={locked || localDays.length === 0}
            className="inline-flex min-h-12 items-center justify-center gap-2 rounded-xl bg-emerald-700 px-4 text-sm font-semibold text-white shadow-sm transition motion-reduce:transition-none hover:bg-emerald-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2 disabled:cursor-wait disabled:opacity-50"
          >
            <Plus className="h-4 w-4" aria-hidden="true" />
            新增地点
          </button>
        </div>

        <div data-testid="trip-days" className="space-y-4" aria-label="按天排列的游览顺序">
          {localDays.map((day, dayOffset) => {
            const dayIndex = dayOffset + 1
            const accent = DAY_ACCENTS[dayOffset % DAY_ACCENTS.length]
            return (
              <section
                key={`${day.label}-${dayIndex}`}
                data-testid={`day-lane-${dayIndex}`}
                className="overflow-hidden rounded-[1.75rem] border border-emerald-950/10 bg-white shadow-[0_18px_45px_-32px_rgba(15,23,42,0.45)]"
                aria-labelledby={`day-heading-${dayIndex}`}
              >
                <div className="grid min-w-0 md:grid-cols-[9.5rem_minmax(0,1fr)]">
                  <div className={`bg-gradient-to-br ${accent[0]} ${accent[1]} px-5 py-5 md:min-h-[18rem] md:border-r md:border-emerald-950/10`}>
                    <div className="flex items-center justify-between md:block">
                      <div>
                        <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-600">行程日</p>
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
                    <p className="mt-3 text-sm font-medium text-slate-600">{dayTheme(day)}</p>
                    <p className="mt-2 hidden text-xs leading-5 text-slate-500 md:block">当天即使没有地点也会保留，方便继续补充。</p>
                  </div>

                  <div className="min-w-0 px-4 py-4 sm:px-5">
                    <div className="flex items-center justify-between gap-4 px-1 text-[11px] text-slate-600">
                      <span className="inline-flex items-center gap-1.5"><Circle className="h-3 w-3 fill-emerald-600 text-emerald-600" aria-hidden="true" />起点</span>
                      <span className="text-center">游览顺序 · 不代表实时路线</span>
                      <span className="inline-flex items-center gap-1.5">终点<Circle className="h-3 w-3 fill-emerald-600 text-emerald-600" aria-hidden="true" /></span>
                    </div>

                    <div className="relative mt-2">
                      <div className="pointer-events-none absolute left-3 right-3 top-3 h-px bg-gradient-to-r from-emerald-600/70 via-emerald-600/25 to-emerald-600/70" />
                      <div className="relative flex min-h-[15.5rem] snap-x items-start overflow-x-auto pb-2 pt-5 [scrollbar-width:thin]">
                        {day.activities.map((activity, position) => {
                          const item = { card: activity, dayIndex, position }
                          return (
                            <div key={activity.activity_token} className="flex shrink-0 items-start">
                              <DropSlot
                                dayIndex={dayIndex}
                                rawPosition={position}
                                active={dropTarget?.dayIndex === dayIndex && dropTarget.position === position}
                                dragging={dragged !== null}
                                onDragEnter={() => setDropTarget({ dayIndex, position })}
                                onDrop={() => void handleDrop(dayIndex, position)}
                              />
                              <motion.article
                                layout
                                data-testid="activity-card"
                                data-activity-name={activity.name}
                                className="w-[13.5rem] snap-start overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-[0_12px_28px_-20px_rgba(15,23,42,0.6)]"
                                transition={reduceMotion ? { duration: 0 } : { type: 'spring', stiffness: 420, damping: 36 }}
                              >
                                <div className={`relative h-20 overflow-hidden bg-gradient-to-br ${accent[0]} ${accent[1]}`}>
                                  <CategoryArtwork category={activity.category} />
                                  <span className="absolute left-3 top-3 flex h-7 min-w-7 items-center justify-center rounded-full bg-emerald-700 px-2 text-xs font-bold text-white shadow-sm">
                                    {position + 1}
                                  </span>
                                  <button
                                    type="button"
                                    draggable={!locked}
                                    data-testid={`drag-handle-${dayIndex}-${position}`}
                                    onDragStart={(event) => {
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
                                      if (event.key === 'Enter' || event.key === ' ') {
                                        event.preventDefault()
                                        openMove(item, event.currentTarget)
                                      }
                                    }}
                                    disabled={locked}
                                    className="absolute right-2 top-2 hidden min-h-12 min-w-12 cursor-grab items-center justify-center rounded-xl bg-white/90 text-slate-600 shadow-sm backdrop-blur transition motion-reduce:transition-none hover:bg-white hover:text-emerald-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 disabled:cursor-wait disabled:opacity-50 md:inline-flex"
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

                                <div className="grid grid-cols-4 border-t border-slate-100 bg-[#fcfbf8] p-1.5">
                                  <CardAction
                                    label={`上移 ${activity.name}`}
                                    disabled={locked || position === 0}
                                    onClick={() => void applyMove(item, dayIndex, position - 1, `${activity.name} 已上移一站。路线需要手动更新。`)}
                                  >
                                    <ArrowUp className="h-4 w-4" aria-hidden="true" />
                                  </CardAction>
                                  <CardAction
                                    label={`下移 ${activity.name}`}
                                    disabled={locked || position === day.activities.length - 1}
                                    onClick={() => void applyMove(item, dayIndex, position + 1, `${activity.name} 已下移一站。路线需要手动更新。`)}
                                  >
                                    <ArrowDown className="h-4 w-4" aria-hidden="true" />
                                  </CardAction>
                                  <CardAction
                                    label={`移动 ${activity.name} 到其他天或位置`}
                                    disabled={locked}
                                    onClick={(event) => openMove(item, event.currentTarget)}
                                  >
                                    <ArrowLeftRight className="h-4 w-4" aria-hidden="true" />
                                  </CardAction>
                                  <CardAction
                                    label={`删除 ${activity.name}`}
                                    disabled={locked}
                                    onClick={(event) => openDelete(item, event.currentTarget)}
                                  >
                                    <Trash2 className="h-4 w-4" aria-hidden="true" />
                                  </CardAction>
                                </div>
                              </motion.article>
                            </div>
                          )
                        })}

                        <DropSlot
                          dayIndex={dayIndex}
                          rawPosition={day.activities.length}
                          active={dropTarget?.dayIndex === dayIndex && dropTarget.position === day.activities.length}
                          dragging={dragged !== null}
                          onDragEnter={() => setDropTarget({ dayIndex, position: day.activities.length })}
                          onDrop={() => void handleDrop(dayIndex, day.activities.length)}
                        />

                        <button
                          type="button"
                          data-testid={`day-${dayIndex}-add`}
                          data-day-add={dayIndex}
                          disabled={locked}
                          onClick={() => onAdd(dayIndex, day.activities.length)}
                          className="flex min-h-[13rem] w-[10rem] shrink-0 snap-start flex-col items-center justify-center rounded-2xl border border-dashed border-emerald-900/20 bg-emerald-50/30 px-4 text-center text-sm text-emerald-800 transition motion-reduce:transition-none hover:border-emerald-600 hover:bg-emerald-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2 disabled:cursor-wait disabled:opacity-50"
                        >
                          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-white shadow-sm">
                            <Plus className="h-5 w-5" aria-hidden="true" />
                          </span>
                          <span className="mt-3 font-semibold">添加地点</span>
                          <span className="mt-1 text-xs text-slate-500">{day.activities.length === 0 ? '从这里开始安排' : '添加到当天末尾'}</span>
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              </section>
            )
          })}
        </div>
      </div>

      <aside className="space-y-4 xl:sticky xl:top-24" aria-label="行程概览">
        <div className="overflow-hidden rounded-[1.75rem] border border-emerald-950/10 bg-white shadow-[0_18px_45px_-32px_rgba(15,23,42,0.5)]">
          <div className="relative overflow-hidden bg-gradient-to-br from-[#f7f4e9] via-emerald-50 to-[#eef5ed] px-5 py-5">
            <Compass className="absolute -bottom-5 -right-3 h-24 w-24 text-emerald-800/10" aria-hidden="true" />
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-emerald-700">行程概览</p>
            <h2 className="mt-2 text-lg font-semibold text-slate-800">每天都能继续调整</h2>
          </div>
          <dl className="grid grid-cols-3 border-y border-slate-100 px-3 py-5 text-center">
            <OverviewNumber value={localDays.length} label="共天数" />
            <OverviewNumber value={totalPlaces} label="地点总数" />
            <OverviewNumber value={pendingPlaces} label="待确认" />
          </dl>
          <div className="space-y-3 px-5 py-5 text-sm">
            <OverviewStatus icon={<ArrowRight className="h-4 w-4" />} label="地图状态" value={operationPending ? '需要手动更新' : MAP_LABELS[mapStatus]} />
            <OverviewStatus icon={<Sparkles className="h-4 w-4" />} label="检查状态" value={checkStatus} />
            <OverviewStatus icon={<Check className="h-4 w-4" />} label="自动保存" value={locked ? '正在保存' : '已保存'} />
          </div>
        </div>
        <div className="rounded-2xl border border-emerald-900/10 bg-emerald-50/65 p-4 text-xs leading-5 text-emerald-900">
          <p className="font-semibold">调整后会发生什么？</p>
          <p className="mt-1 text-emerald-950">卡片顺序会自动保存；现有路线不会自动重算，需在地图区域手动更新。</p>
        </div>
      </aside>

      <p className="sr-only" aria-live="polite" aria-atomic="true" data-testid="itinerary-live-status">{announcement}</p>

      <AnimatePresence>
        {dialog?.kind === 'DETAIL' && (
          <AccessibleDialog key="activity-detail" titleId="activity-detail-title" onClose={() => closeDialog()} reduceMotion={reduceMotion}>
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
                  {dialog.item.card.knowledge_suggestions?.map((suggestion) => (
                    <li key={`${suggestion.type}-${suggestion.source_url}-${suggestion.text}`} className="rounded-2xl border border-sky-100 bg-sky-50/65 p-4">
                      <p className="text-xs font-semibold text-sky-800">{KNOWLEDGE_LABELS[suggestion.type]}</p>
                      <p className="mt-1 text-sm leading-6 text-slate-700">{suggestion.text}</p>
                      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
                        <a
                          href={suggestion.source_url}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex min-h-10 items-center gap-1 rounded-lg px-1 font-medium text-sky-800 underline decoration-sky-300 underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-700"
                        >
                          {suggestion.source_name}
                          <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                        </a>
                        <span>{suggestion.freshness}</span>
                      </div>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            <div className="mt-5 grid grid-cols-2 gap-2">
              <DialogAction onClick={() => { closeDialog(false); onEdit(dialog.item) }} icon={<Pencil className="h-4 w-4" />}>编辑文字</DialogAction>
              <DialogAction onClick={() => { closeDialog(false); onReplace(dialog.item) }} icon={<Replace className="h-4 w-4" />}>替换地点</DialogAction>
              <DialogAction onClick={() => openMove(dialog.item)} icon={<ArrowLeftRight className="h-4 w-4" />}>移动位置</DialogAction>
              <DialogAction onClick={() => openDelete(dialog.item)} icon={<Trash2 className="h-4 w-4" />}>删除地点</DialogAction>
            </div>
            <p className="mt-4 text-xs leading-5 text-slate-500">卡片调整会自动保存，路线需要时再手动更新。</p>
          </AccessibleDialog>
        )}

        {dialog?.kind === 'MOVE' && (
          <AccessibleDialog key="move-activity" titleId="move-activity-title" onClose={() => closeDialog()} reduceMotion={reduceMotion}>
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
          <AccessibleDialog key="delete-activity" titleId="delete-activity-title" onClose={() => closeDialog()} reduceMotion={reduceMotion}>
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs font-semibold text-amber-700">删除地点</p>
                <h2 id="delete-activity-title" className="mt-1 text-xl font-semibold">删除“{dialog.item.card.name}”？</h2>
              </div>
              <DialogCloseButton onClick={() => closeDialog()} label="关闭删除确认" />
            </div>
            <p className="mt-4 text-sm leading-6 text-slate-600">只会删除这张地点卡片。即使它是当天最后一个地点，{localDays[dialog.item.dayIndex - 1]?.label || '当天'}也会保留。</p>
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


function dayTheme(day: DayView): string {
  if (day.activities.length === 0) return '留白待安排'
  const categories = Array.from(new Set(day.activities.map((activity) => activity.category))).slice(0, 2)
  return `${categories.join('与')}之旅`
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
  onDragEnter,
  onDrop,
}: {
  dayIndex: number
  rawPosition: number
  active: boolean
  dragging: boolean
  onDragEnter: () => void
  onDrop: () => void
}) {
  return (
    <div
      data-testid={`drop-slot-${dayIndex}-${rawPosition}`}
      className={`mx-1 flex h-[13rem] w-4 shrink-0 items-center justify-center rounded-full transition-colors motion-reduce:transition-none ${active ? 'bg-emerald-100' : 'bg-transparent'}`}
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
      <span className={`h-full w-1 rounded-full transition motion-reduce:transition-none ${dragging ? (active ? 'bg-emerald-600' : 'bg-emerald-200') : 'bg-emerald-900/10'}`} />
    </div>
  )
}


function CardAction({
  label,
  disabled,
  onClick,
  children,
}: {
  label: string
  disabled: boolean
  onClick: (event: ReactMouseEvent<HTMLButtonElement>) => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="inline-flex min-h-12 min-w-12 items-center justify-center rounded-xl text-slate-500 transition motion-reduce:transition-none hover:bg-white hover:text-emerald-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 disabled:cursor-not-allowed disabled:opacity-30"
      aria-label={label}
      title={label}
    >
      {children}
    </button>
  )
}


function OverviewNumber({ value, label }: { value: number; label: string }) {
  return (
    <div>
      <dt className="text-[11px] text-slate-500">{label}</dt>
      <dd className="mb-1 text-2xl font-semibold text-emerald-800">{value}</dd>
    </div>
  )
}


function OverviewStatus({ icon, label, value }: { icon: ReactNode; label: string; value: string }) {
  return (
    <div className="flex items-center gap-3">
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-emerald-50 text-emerald-700" aria-hidden="true">{icon}</span>
      <span className="min-w-0 flex-1">
        <span className="block text-xs text-slate-600">{label}</span>
        <span className="block truncate font-medium text-slate-700">{value}</span>
      </span>
    </div>
  )
}


function AccessibleDialog({
  titleId,
  onClose,
  reduceMotion,
  children,
}: {
  titleId: string
  onClose: () => void
  reduceMotion: boolean | null
  children: ReactNode
}) {
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const panel = panelRef.current
    if (!panel) return
    const initial = panel.querySelector<HTMLElement>('[data-dialog-initial-focus]')
      || panel.querySelector<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled)')
    initial?.focus()
  }, [])

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      onClose()
      return
    }
    if (event.key !== 'Tab') return
    const focusable = Array.from(panelRef.current?.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
    ) || []).filter((element) => element.offsetParent !== null)
    if (focusable.length === 0) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) {
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
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <motion.div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={handleKeyDown}
        initial={reduceMotion ? { opacity: 1 } : { opacity: 0, y: 18, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={reduceMotion ? { opacity: 0 } : { opacity: 0, y: 12, scale: 0.98 }}
        transition={{ duration: reduceMotion ? 0 : 0.18 }}
        className="w-full max-w-md rounded-[1.75rem] bg-white p-6 shadow-2xl outline-none"
      >
        {children}
      </motion.div>
    </motion.div>
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
