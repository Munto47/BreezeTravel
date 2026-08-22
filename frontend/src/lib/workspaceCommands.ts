import type { EditOperation, ItineraryRevision, RevisionStop, WorkspaceEditRequest } from '@/types/workspace'


type CommandEnvelope = Pick<WorkspaceEditRequest, 'command_id' | 'base_revision' | 'client_timestamp'>


function sourceOf(revision: ItineraryRevision, stopId: string): { dayIndex: number; orderIndex: number } {
  for (const day of revision.days) {
    const orderIndex = day.stops.findIndex(stop => stop.stop_id === stopId)
    if (orderIndex >= 0) return { dayIndex: day.day_index, orderIndex }
  }
  throw new Error('地点已不在当前版本中，请刷新后重试')
}


export function moveStopCommand(
  revision: ItineraryRevision,
  stopId: string,
  targetDayIndex: number,
  targetOrderIndex: number,
  envelope: CommandEnvelope,
): WorkspaceEditRequest {
  const source = sourceOf(revision, stopId)
  const operation: EditOperation = source.dayIndex === targetDayIndex ? 'REORDER_STOP' : 'MOVE_TO_DAY'
  return {
    ...envelope,
    operation,
    payload: { stop_id: stopId, target_day_index: targetDayIndex, target_order_index: targetOrderIndex },
  }
}


export function stopCommand(
  operation: Extract<EditOperation, 'LOCK_STOP' | 'UNLOCK_STOP' | 'REMOVE_STOP'>,
  stopId: string,
  envelope: CommandEnvelope,
): WorkspaceEditRequest {
  return { ...envelope, operation, payload: { stop_id: stopId } }
}


export function adjustTimeCommand(
  stopId: string,
  startTime: string | null,
  endTime: string | null,
  envelope: CommandEnvelope,
): WorkspaceEditRequest {
  return {
    ...envelope,
    operation: 'ADJUST_TIME',
    payload: { stop_id: stopId, start_time: startTime, end_time: endTime },
  }
}


export function addStopCommand(
  stop: RevisionStop,
  envelope: CommandEnvelope,
): WorkspaceEditRequest {
  return { ...envelope, operation: 'ADD_STOP', payload: { stop } }
}


export function replaceStopCommand(
  stopId: string,
  candidate: { place_id: string; name: string; category: string },
  envelope: CommandEnvelope,
): WorkspaceEditRequest {
  return {
    ...envelope,
    operation: 'REPLACE_STOP',
    payload: {
      stop_id: stopId,
      new_place_id: candidate.place_id,
      raw_name: candidate.name,
      category: candidate.category,
    },
  }
}


export function applyOptimisticCommand(
  revision: ItineraryRevision,
  command: WorkspaceEditRequest,
): ItineraryRevision {
  const days = revision.days.map(day => ({ ...day, stops: day.stops.map(stop => ({ ...stop })) }))
  const normalize = (dayIndex: number) => {
    days[dayIndex].stops = days[dayIndex].stops.map((stop, orderIndex) => ({
      ...stop,
      day_index: dayIndex,
      order_index: orderIndex,
      // Edge evidence belongs to both endpoints. Hide it optimistically until
      // the server restores only endpoint-identical cached edges.
      transport_to_next: null,
    }))
  }
  const locate = (stopId: string) => {
    for (const day of days) {
      const index = day.stops.findIndex(stop => stop.stop_id === stopId)
      if (index >= 0) return { dayIndex: day.day_index, index }
    }
    return null
  }
  const stopId = typeof command.payload.stop_id === 'string' ? command.payload.stop_id : null

  if (['MOVE_STOP', 'MOVE_TO_DAY', 'REORDER_STOP'].includes(command.operation) && stopId) {
    const source = locate(stopId)
    const targetDay = Number(command.payload.target_day_index)
    let targetOrder = Number(command.payload.target_order_index)
    if (source && Number.isInteger(targetDay) && Number.isInteger(targetOrder) && days[targetDay]) {
      const [moved] = days[source.dayIndex].stops.splice(source.index, 1)
      if (source.dayIndex === targetDay && targetOrder > source.index) targetOrder -= 1
      days[targetDay].stops.splice(Math.max(0, Math.min(targetOrder, days[targetDay].stops.length)), 0, moved)
      normalize(source.dayIndex)
      if (source.dayIndex !== targetDay) normalize(targetDay)
    }
  } else if (command.operation === 'REMOVE_STOP' && stopId) {
    const source = locate(stopId)
    if (source) {
      days[source.dayIndex].stops.splice(source.index, 1)
      normalize(source.dayIndex)
    }
  } else if (['LOCK_STOP', 'UNLOCK_STOP'].includes(command.operation) && stopId) {
    const source = locate(stopId)
    if (source) days[source.dayIndex].stops[source.index].locked = command.operation === 'LOCK_STOP'
  } else if (command.operation === 'ADJUST_TIME' && stopId) {
    const source = locate(stopId)
    if (source) {
      const stop = days[source.dayIndex].stops[source.index]
      if ('start_time' in command.payload) stop.start_time = command.payload.start_time as string | null
      if ('end_time' in command.payload) stop.end_time = command.payload.end_time as string | null
    }
  } else if (command.operation === 'REPLACE_STOP' && stopId) {
    const source = locate(stopId)
    if (source) {
      const stop = days[source.dayIndex].stops[source.index]
      stop.place_id = String(command.payload.new_place_id)
      stop.raw_name = typeof command.payload.raw_name === 'string' ? command.payload.raw_name : null
      stop.category = typeof command.payload.category === 'string' ? command.payload.category : stop.category
      normalize(source.dayIndex)
    }
  } else if (command.operation === 'ADD_STOP') {
    const stop = command.payload.stop as RevisionStop | undefined
    if (stop && days[stop.day_index]) {
      days[stop.day_index].stops.splice(Math.min(stop.order_index, days[stop.day_index].stops.length), 0, stop)
      normalize(stop.day_index)
    }
  }
  return { ...revision, days }
}
