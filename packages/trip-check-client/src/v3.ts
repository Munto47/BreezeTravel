export type DemoCreateRequest = { mode: 'DEMO' }

export interface TripUnderstandingAcceptedView {
  public_resource_id: string
  status: 'PROCESSING'
  message: string
  result_url: string
  events_url: string
}

export interface TripUnderstandingProgressView {
  status: 'PROCESSING'
  message: string
  retry_after_ms: number
}

export interface AssumptionChipView {
  key: 'destination' | 'calendar' | 'party_size'
  label: string
  value: string
  editable: boolean
}

export interface ActivityCardView {
  activity_token: string
  name: string
  category: string
  area_or_address: string
  time_hint: string | null
  status: 'READY' | 'NEEDS_CONFIRMATION'
  available_actions: Array<'VIEW_DETAILS' | 'REPLACE' | 'DELETE' | 'MOVE'>
}

export interface TripDayView {
  label: string
  activities: ActivityCardView[]
}

export interface UserFacingTripResult {
  status: 'READY' | 'PARTIAL_RESULT' | 'BASIC_ONLY' | 'LIMITED'
  assumptions: AssumptionChipView[]
  days: TripDayView[]
  map: {
    status: 'PREPARING' | 'AVAILABLE' | 'NEEDS_UPDATE' | 'LIMITED' | 'UNAVAILABLE'
    message: string
    available_actions: Array<'VIEW_MAP' | 'RENDER_MAP'>
  }
  stay: {
    status: 'PREPARING' | 'AVAILABLE' | 'NEEDS_UPDATE' | 'LIMITED' | 'UNAVAILABLE'
    message: string
    area_summary: string | null
    searched_scopes: string[]
    candidates: Array<{
      candidate_token: string
      name: string
      brand: string
      category: string
      area_or_address: string
      commute_summary: string
      max_single_leg_minutes: number
      transfer_count: number
      evidence_gap: string | null
      reason: string
      available_actions: Array<'CHOOSE_STAY'>
      selected: boolean
    }>
    available_actions: Array<'CHOOSE_STAY'>
  }
  available_actions: Array<'EDIT_ASSUMPTIONS' | 'EDIT_CARDS'>
}

export interface PublicRouteModeView {
  status: 'AVAILABLE' | 'UNAVAILABLE'
  duration_minutes: number | null
  distance_meters: number | null
  transfer_count: number | null
  geometry: Array<{ longitude: number; latitude: number }>
}

export interface MapRenderView {
  status: 'PREPARING' | 'AVAILABLE' | 'NEEDS_UPDATE' | 'LIMITED' | 'UNAVAILABLE'
  message: string
  days: Array<{
    label: string
    routes: Array<{
      from_name: string
      to_name: string
      selected_mode: 'walking' | 'transit' | null
      message: string
      walking: PublicRouteModeView
      transit: PublicRouteModeView
    }>
  }>
  available_actions: Array<'VIEW_MAP' | 'RENDER_MAP'>
}

export interface MapRenderAcceptedView {
  status: 'PREPARING' | 'AVAILABLE' | 'LIMITED' | 'UNAVAILABLE'
  message: string
}

export type StaySuggestionView = UserFacingTripResult['stay']

export interface StaySelectionAppliedView {
  status: 'APPLIED'
  selected_stay: string
  overnight_days: string[]
  map_readiness: 'NEEDS_UPDATE'
}
