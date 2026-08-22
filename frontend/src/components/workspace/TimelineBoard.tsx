'use client'

import { Clock3, GripVertical, Lock, LockOpen, Trash2, ChevronDown, ChevronUp } from 'lucide-react'

import type { ItineraryRevision, RevisionStop, WorkspaceEditRequest } from '@/types/workspace'
import { adjustTimeCommand, moveStopCommand, stopCommand } from '@/lib/workspaceCommands'


interface Props {
  revision: ItineraryRevision
  selectedStopId: string | null
  onSelectStop: (stopId: string) => void
  onCommand: (command: WorkspaceEditRequest) => void
  commandEnvelope: () => Pick<WorkspaceEditRequest, 'command_id' | 'base_revision' | 'client_timestamp'>
  busy: boolean
}


export default function TimelineBoard({
  revision,
  selectedStopId,
  onSelectStop,
  onCommand,
  commandEnvelope,
  busy,
}: Props) {
  const move = (stopId: string, dayIndex: number, orderIndex: number) => {
    onCommand(moveStopCommand(revision, stopId, dayIndex, orderIndex, commandEnvelope()))
  }

  return (
    <section aria-label="每日时间轴" className="space-y-4">
      {revision.days.map(day => (
        <article
          key={day.day_index}
          className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm"
          onDragOver={event => event.preventDefault()}
          onDrop={event => {
            const stopId = event.dataTransfer.getData('text/breezetravel-stop')
            if (stopId) move(stopId, day.day_index, day.stops.length)
          }}
        >
          <div className="mb-3 flex items-center justify-between">
            <div>
              <h2 className="font-semibold text-slate-900">第 {day.day_index + 1} 天</h2>
              <p className="text-xs text-slate-500">{day.date ?? '日期待定'} · {day.stops.length} 个地点</p>
            </div>
            <span className="rounded-full bg-slate-100 px-2 py-1 text-xs text-slate-600">可拖入此日</span>
          </div>

          <div className="space-y-2">
            {day.stops.length === 0 && (
              <div className="rounded-xl border border-dashed border-slate-300 px-3 py-8 text-center text-sm text-slate-400">
                将地点拖到这里，或从候选区加入
              </div>
            )}
            {day.stops.map((stop, index) => (
              <StopCard
                key={stop.stop_id}
                stop={stop}
                selected={selectedStopId === stop.stop_id}
                busy={busy}
                onSelect={() => onSelectStop(stop.stop_id)}
                onDragStart={event => event.dataTransfer.setData('text/breezetravel-stop', stop.stop_id)}
                onMoveUp={() => move(stop.stop_id, day.day_index, Math.max(0, index - 1))}
                onMoveDown={() => move(stop.stop_id, day.day_index, Math.min(day.stops.length, index + 2))}
                onMoveDay={target => move(stop.stop_id, target, revision.days[target].stops.length)}
                dayCount={revision.days.length}
                onLock={() => onCommand(stopCommand(
                  stop.locked ? 'UNLOCK_STOP' : 'LOCK_STOP', stop.stop_id, commandEnvelope(),
                ))}
                onAdjustTime={() => {
                  const start = window.prompt('开始时间（HH:MM）', stop.start_time ?? '')
                  if (start === null) return
                  const end = window.prompt('结束时间（HH:MM）', stop.end_time ?? '')
                  if (end === null) return
                  onCommand(adjustTimeCommand(stop.stop_id, start || null, end || null, commandEnvelope()))
                }}
                onRemove={() => onCommand(stopCommand('REMOVE_STOP', stop.stop_id, commandEnvelope()))}
              />
            ))}
          </div>
        </article>
      ))}
    </section>
  )
}


function StopCard({
  stop,
  selected,
  busy,
  onSelect,
  onDragStart,
  onMoveUp,
  onMoveDown,
  onMoveDay,
  dayCount,
  onLock,
  onAdjustTime,
  onRemove,
}: {
  stop: RevisionStop
  selected: boolean
  busy: boolean
  onSelect: () => void
  onDragStart: (event: React.DragEvent) => void
  onMoveUp: () => void
  onMoveDown: () => void
  onMoveDay: (dayIndex: number) => void
  dayCount: number
  onLock: () => void
  onAdjustTime: () => void
  onRemove: () => void
}) {
  return (
    <div
      draggable={!busy && !stop.locked && !stop.fixed_commitment}
      onDragStart={onDragStart}
      onClick={onSelect}
      className={`rounded-xl border p-3 transition ${selected ? 'border-blue-400 bg-blue-50/50' : 'border-slate-200 bg-white'}`}
    >
      <div className="flex items-start gap-2">
        <GripVertical aria-hidden className="mt-0.5 hidden h-4 w-4 text-slate-300 md:block" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="truncate text-sm font-medium text-slate-900">{stop.raw_name || stop.place_id}</h3>
            {(stop.locked || stop.fixed_commitment) && <Lock className="h-3.5 w-3.5 text-amber-600" />}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <span className="flex items-center gap-1"><Clock3 className="h-3 w-3" />{stop.start_time ?? '?'}–{stop.end_time ?? '?'}</span>
            <span>{stop.category}</span>
          </div>
          {stop.transport_to_next && (
            <p className="mt-2 text-xs text-slate-500">下一段缓存路线：{stop.transport_to_next.duration_minutes ?? '?'} 分钟</p>
          )}
        </div>
      </div>

      <div className="mt-3 flex flex-wrap gap-1.5" onClick={event => event.stopPropagation()}>
        <button aria-label="上移" disabled={busy || stop.locked} onClick={onMoveUp} className="rounded-lg border px-2 py-1.5 text-xs disabled:opacity-40"><ChevronUp className="h-3.5 w-3.5" /></button>
        <button aria-label="下移" disabled={busy || stop.locked} onClick={onMoveDown} className="rounded-lg border px-2 py-1.5 text-xs disabled:opacity-40"><ChevronDown className="h-3.5 w-3.5" /></button>
        <select
          aria-label="移动到另一天"
          disabled={busy || stop.locked}
          value={stop.day_index}
          onChange={event => onMoveDay(Number(event.target.value))}
          className="rounded-lg border bg-white px-2 py-1.5 text-xs disabled:opacity-40"
        >
          {Array.from({ length: dayCount }, (_, index) => <option key={index} value={index}>移至第 {index + 1} 天</option>)}
        </select>
        <button disabled={busy || stop.fixed_commitment} onClick={onLock} className="flex items-center gap-1 rounded-lg border px-2 py-1.5 text-xs disabled:opacity-40">
          {stop.locked ? <LockOpen className="h-3.5 w-3.5" /> : <Lock className="h-3.5 w-3.5" />}{stop.locked ? '解锁' : '锁定'}
        </button>
        <button disabled={busy || stop.locked || stop.fixed_commitment} onClick={onAdjustTime} className="flex items-center gap-1 rounded-lg border px-2 py-1.5 text-xs disabled:opacity-40"><Clock3 className="h-3.5 w-3.5" />调整时间</button>
        <button disabled={busy || stop.locked || stop.fixed_commitment} onClick={onRemove} className="flex items-center gap-1 rounded-lg border border-rose-200 px-2 py-1.5 text-xs text-rose-700 disabled:opacity-40">
          <Trash2 className="h-3.5 w-3.5" />删除
        </button>
      </div>
    </div>
  )
}
