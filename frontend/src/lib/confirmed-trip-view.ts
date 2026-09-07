import type { TripUnderstandingCommand, UserFacingTripResult } from './trip-understanding-v3'

// Presentation only. Keep the authoritative result, source, unresolved records
// and status intact for editing, undo, diagnostics and honest route coverage.
export function confirmedDays(days: UserFacingTripResult['days']) {
  return days.map(day => ({ ...day,
    activities: day.activities.filter(card => card.status === 'READY'),
    // These are source suggestions, not searched/verified places.
    alternatives: [],
  }))
}

export function confirmedTripView(result: UserFacingTripResult | null) {
  return result ? { ...result, days: confirmedDays(result.days) } : null
}

// The server still owns all original positions. Translate a visible insertion
// boundary by token after removing the dragged card, never by hidden count.
export function storedPositionCommand(command: TripUnderstandingCommand, result: UserFacingTripResult | null): TripUnderstandingCommand {
  if (!result || !['ACTIVITY_MOVE', 'ACTIVITY_INSERT'].includes(command.command_type)) return command
  if (command.command_type === 'ACTIVITY_MOVE') {
    const cards = (result.days[command.target_day_index - 1]?.activities || [])
      .filter(card => card.activity_token !== command.activity_token)
    return { ...command, target_position: storedPosition(cards, command.target_position) }
  }
  if (command.command_type === 'ACTIVITY_INSERT') {
    return { ...command, position: storedPosition(result.days[command.day_index - 1]?.activities || [], command.position) }
  }
  return command
}

function storedPosition(cards: UserFacingTripResult['days'][number]['activities'], position: number) {
  const next = cards.filter(card => card.status === 'READY')[position]
  return next ? cards.findIndex(card => card.activity_token === next.activity_token) : cards.length
}
