import type { ActivityCardView, TripUnderstandingCommand, UserFacingTripResult } from './trip-understanding-v3'

export function isWholeTripLodging(card: ActivityCardView) {
  return card.category === '住宿' && card.lodging_event === 'OVERNIGHT' && card.lodging_scope === 'WHOLE_TRIP'
}

export type SourceLodgingCard = ActivityCardView & {scope?:'WHOLE_TRIP'|'NIGHTS';overnight_days?:number[]}

export function confirmedSourceLodgings(result: UserFacingTripResult | null):SourceLodgingCard[] {
  if(!result)return []
  return [...result.days.flatMap(day => day.activities.filter(card => card.status === 'READY' && isWholeTripLodging(card))),
    ...(result.lodging_constraints||[]).filter(card=>card.status==='READY')]
}

function isConfirmedVisit(card: ActivityCardView) {
  return card.status === 'READY' && !isWholeTripLodging(card)
}

// Only system suggestions are filtered. Original text, source details and
// stored timing fields remain intact, including on historical readback.
export function relativeKnowledgeSuggestions(card: Pick<ActivityCardView, 'knowledge_suggestions'>) {
  return (card.knowledge_suggestions || []).filter(item => {
    if (item.type === 'TYPICAL_DURATION' || item.type === 'SUITABLE_TIME') return false
    if (item.type !== 'RESERVATION_ADVICE') return true
    return !/(?:\d{1,2}[:：]\d{2}|[零〇一二三四五六七八九十两\d]+\s*(?:点|时|小时|分钟|天|月|号)|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})/.test(item.text)
  })
}

// Presentation only. Keep the authoritative result, source, unresolved records
// and status intact for editing, undo, diagnostics and honest route coverage.
export function confirmedDays(days: UserFacingTripResult['days']) {
  return days.map(day => ({ ...day,
    activities: day.activities.filter(isConfirmedVisit).map(card => ({...card,
      knowledge_suggestions: relativeKnowledgeSuggestions(card),
    })),
    // Source alternatives stay in their separate, explicitly unconfirmed panel.
    alternatives: day.alternatives || [],
  }))
}

export function confirmedTripView(result: UserFacingTripResult | null) {
  return result ? { ...result, days: confirmedDays(result.days) } : null
}

// The server still owns all original positions. Translate a visible insertion
// boundary by token after removing the dragged card, never by hidden count.
export function storedPositionCommand(command: TripUnderstandingCommand, result: UserFacingTripResult | null): TripUnderstandingCommand {
  if (!result || !['ACTIVITY_MOVE', 'ACTIVITY_INSERT', 'ALTERNATIVE_INSERT', 'CHOICE_SELECT'].includes(command.command_type)) return command
  if (command.command_type === 'ACTIVITY_MOVE') {
    const cards = (result.days[command.target_day_index - 1]?.activities || [])
      .filter(card => card.activity_token !== command.activity_token)
    return { ...command, target_position: storedPosition(cards, command.target_position) }
  }
  if (command.command_type === 'ACTIVITY_INSERT' || command.command_type === 'ALTERNATIVE_INSERT' || command.command_type === 'CHOICE_SELECT') {
    // An omitted choice position means the server's exact source boundary,
    // including hidden visits. Only an explicit user selection is translated.
    if (command.position === undefined) return command
    return { ...command, position: storedPosition(result.days[command.day_index - 1]?.activities || [], command.position) }
  }
  return command
}

function storedPosition(cards: UserFacingTripResult['days'][number]['activities'], position: number) {
  const next = cards.filter(isConfirmedVisit)[position]
  return next ? cards.findIndex(card => card.activity_token === next.activity_token) : cards.length
}
