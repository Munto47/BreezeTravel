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

export interface KnowledgeSuggestionView {
  type: 'TYPICAL_DURATION' | 'SUITABLE_TIME' | 'NIGHT_VIEW' | 'SEASON' | 'RESERVATION_ADVICE'
  text: string
  source_name: string
  source_url: string
  freshness: string
}

export interface ActivityCardView {
  activity_token: string
  name: string
  category: string
  area_or_address: string
  time_hint: string | null
  status: 'READY' | 'NEEDS_CONFIRMATION'
  available_actions: Array<'VIEW_DETAILS' | 'REPLACE' | 'DELETE' | 'MOVE'>
  knowledge_suggestions?: KnowledgeSuggestionView[]
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

export interface MaterializedTripView {
  status: 'READY'
  message: string
  calendar: string
  party_size: number
  checks_available: boolean
}

export interface PublicTripCheckItem {
  check_token: string
  label: '必须调整' | '可以更好' | '需要确认'
  title: string
  message: string
  affected_days: string[]
  can_preview: boolean
}

export interface PublicTripChecksView {
  status: 'READY' | 'STILL_NEEDS_CONFIRMATION'
  message: string
  items: PublicTripCheckItem[]
  remaining_must_adjust: number
  available_actions: Array<'PREVIEW_CHANGE'>
}

export interface PublicChangePreview {
  change_token: string
  title: string
  summary: string
  affected_days: string[]
  before: string[]
  after: string[]
  available_actions: Array<'ADOPT_CHANGE'>
}

export interface PublicChangeAdopted {
  status: 'APPLIED' | 'STILL_NEEDS_CONFIRMATION'
  message: string
  changed_days: string[]
  map_readiness: 'NEEDS_UPDATE'
  checks: PublicTripChecksView
}

export interface DataConsentView {
  memory_enabled: boolean
  feedback_enabled: boolean
  training_eval_enabled: boolean
}

export interface PreferenceMemoryView {
  walking_tolerance_minutes: number | null
  preferred_start_time: string | null
  dining_preferences: Array<'LOCAL' | 'VEGETARIAN' | 'HALAL' | 'NO_SPICY' | 'QUICK'>
  hotel_preferences: Array<'CHAIN' | 'NEAR_TRANSIT' | 'QUIET' | 'CENTRAL'>
  intensity: 'RELAXED' | 'BALANCED' | 'FULL' | null
}

export interface ShareCreatedView {
  share_url: string
  expires_at: string
}

export interface ShareListItemView {
  share_ref: string
  expires_at: string
  status: 'ACTIVE' | 'REVOKED' | 'EXPIRED'
}

export interface ShareProjectionView {
  title: string
  destination: string
  schedule: string
  party_size: string
  days: Array<{
    label: string
    activities: Array<{
      name: string
      area_or_address: string
      time_hint: string | null
      note: '可直接查看' | '地点待确认'
    }>
  }>
  accommodation: string | null
  message: string
}
