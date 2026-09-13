import { parseItineraryFromAPI, parseSavedItinerary, type Itinerary } from '@/types/itinerary'

export interface CurrentRoomRoute {
  roomId: string
  version: number
  itinerary: Itinerary | null
  selectionSnapshot: { trip_days: number; place_ids: string[] } | null
}

/** Only this member-authorized server response can identify the shared route. */
export function parseCurrentRoomRoute(value: unknown, roomId: string): CurrentRoomRoute {
  if (!value || typeof value !== 'object') throw new Error('INVALID_ROOM_ROUTE')
  const raw = value as Record<string, unknown>
  if (raw.room_id !== roomId || !Number.isSafeInteger(raw.version) || Number(raw.version) < 0)
    throw new Error('INVALID_ROOM_ROUTE')
  const version = Number(raw.version)
  if (version === 0 && raw.itinerary_data === null)
    return { roomId, version, itinerary: null, selectionSnapshot: null }
  if (version === 0 || !raw.itinerary_data || typeof raw.itinerary_data !== 'object')
    throw new Error('INVALID_ROOM_ROUTE')
  const itinerary = parseItineraryFromAPI(raw.itinerary_data as Record<string, unknown>)
  // Reuse the persisted shape checks without granting legacy transport authority.
  if (!parseSavedItinerary(itinerary)) throw new Error('INVALID_ROOM_ROUTE')
  const selection = raw.selection_snapshot as Record<string, unknown> | undefined
  if (!selection || !Number.isInteger(selection.trip_days) || Number(selection.trip_days) < 1 ||
      !Array.isArray(selection.place_ids) || selection.place_ids.some(id => typeof id !== 'string'))
    throw new Error('INVALID_ROOM_ROUTE_SELECTION')
  return { roomId, version, itinerary,
    selectionSnapshot: { trip_days: Number(selection.trip_days), place_ids: [...selection.place_ids] } }
}

export function roomRouteSelectionChanged(route: CurrentRoomRoute | null, placeIds: string[], tripDays: number): boolean {
  const selection = route?.selectionSnapshot
  if (!selection) return false
  return selection.trip_days !== tripDays ||
    JSON.stringify([...selection.place_ids].sort()) !== JSON.stringify([...placeIds].sort())
}
