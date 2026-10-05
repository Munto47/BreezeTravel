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
  phase: 'RECEIVED' | 'CARDS_AVAILABLE' | 'CHECKING_PLACES'
  event_cursor: number
  progress: TripUnderstandingProgressMetrics
  snapshot: UserFacingTripResult | null
}

export interface TripUnderstandingProgressMetrics {
  semantic_complete?: boolean | null
  places_total_final?: boolean | null
  day_count: number
  card_count: number
  places_checked: number
  places_total: number
}

export interface TripUnderstandingCancelView {
  status: 'STOPPED_WITH_DRAFT' | 'STOPPED_EMPTY' | 'ALREADY_FINISHED'
  message: string
  has_editable_result: boolean
}

export interface AssumptionChipView {
  key: 'destination' | 'calendar' | 'party_size'
  label: string
  value: string
  editable: boolean
}

export interface KnowledgeSuggestionView {
  type:
    | 'TYPICAL_DURATION'
    | 'SUITABLE_TIME'
    | 'NIGHT_VIEW'
    | 'SEASON'
    | 'RESERVATION_ADVICE'
  text: string
  source_name: string
  source_url: string
  freshness: string
}

export interface DiningAccessView {
  status: 'DURING_VISIT' | 'NEEDS_REVIEW'
  parent_name: string | null
}

export interface DiningContextView {
  dining_access?: DiningAccessView | null
  meal_evidence_status?: 'LIGHT_FOOD_ITEMS_ONLY' | 'UNSPECIFIED'
}

export interface ActivityCardView extends DiningContextView {
  semantic_review?: 'CONFIRMED' | 'PENDING'
  verification_pending?: boolean
  source_fragment_id?:string|null
  note?: string
  visit_id?: string
  source_details?: Array<{name: string; optional: boolean}>
  lodging_event?: 'OVERNIGHT' | 'CHECK_OUT' | 'DEPARTURE' | 'LUGGAGE_PICKUP' | 'VISIT_ONLY' | null
  lodging_scope?: 'WHOLE_TRIP' | 'DAY' | null
  lodging_role_uncertain?: boolean
  lodging_excluded_nights?: number[]
  meal_role?: 'BREAKFAST' | 'LUNCH' | 'DINNER' | 'SNACK' | null
  city?: string | null
  photo_url?: string | null
  activity_token: string
  name: string
  category: string
  area_or_address: string
  time_hint: string | null
  status: 'READY' | 'NEEDS_CONFIRMATION'
  available_actions: Array<'VIEW_DETAILS' | 'REPLACE' | 'DELETE' | 'MOVE'>
  knowledge_suggestions?: KnowledgeSuggestionView[]
  start_time?: string | null
  end_time?: string | null
  visit_duration_minutes?: number | null
  timing_source?: 'TEXT' | 'USER' | 'SUGGESTED' | 'UNSPECIFIED'
  locked?: boolean
  fixed_commitment?: boolean
}

export interface PlacePosition {
  longitude: number
  latitude: number
  coordinate_system: 'GCJ02'
}
export interface PlaceCandidateView extends DiningContextView {
  candidate_token: string
  name: string
  category: string
  area_or_address: string
  position: PlacePosition
}
export interface PlaceCandidatesView {
  status: 'AVAILABLE' | 'EMPTY' | 'UNAVAILABLE'
  candidates: PlaceCandidateView[]
}

export interface PublicRouteModeView {
  status: 'AVAILABLE' | 'UNAVAILABLE'
  connection_status?: 'VERIFIED' | 'UNVERIFIED'
  geometry_break_indices?: number[] | null
  duration_minutes: number | null
  distance_meters: number | null
  transfer_count: number | null
  geometry: Array<{ longitude: number; latitude: number }>
}

export interface MapRenderView {
  status: 'PREPARING' | 'AVAILABLE' | 'NEEDS_UPDATE' | 'LIMITED' | 'UNAVAILABLE'
  message: string
  lodging_points?: Array<{
    point_token: string
    day_label: string
    name: string
    position: PlacePosition
  }>
  points?: Array<{
    activity_token: string
    day_label: string
    sequence_index: number
    name: string
    position: PlacePosition | null
  }>
  days: Array<{
    day_index?: number
    label: string
    routes: Array<{
      from_activity_token?: string
      to_activity_token?: string
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

export interface StayCandidateView {
  candidate_token: string
  name: string
  brand: string
  category: string
  area_or_address: string
  commute_summary: string
  max_single_leg_minutes: number | null
  transfer_count: number
  reason: string
  available_actions: Array<'CHOOSE_STAY'>
  selected: boolean
  brand_group?: string | null
  brand_note?: string | null
}

export interface StaySegmentView {
  segment_token: string; city?: string | null; overnight_days: string[]
  status: StaySuggestionView['status']; message: string
  candidates: StayCandidateView[]; preserved_hotels?: string[]
  expected_boundary_count?: number; missing_boundary_count?: number
}

export interface StaySuggestionView {
  status: 'PREPARING' | 'AVAILABLE' | 'NEEDS_UPDATE' | 'LIMITED' | 'UNAVAILABLE'
  message: string
  area_summary: string | null
  searched_scopes: string[]
  candidates: StayCandidateView[]
  available_actions: Array<'CHOOSE_STAY'>
  segments?: StaySegmentView[]
}

export interface UserFacingTripResult {
  correction_suggestions?: Array<{suggestion_id:string;target_visit_id:string;source_name:string}>
  issue_dispositions?: Record<string,{status:string;title:string;message:string;input_fingerprint:string}>
  pending_lodgings?: PendingLodgingView[]
  lodging_constraints?: LodgingConstraintView[]
  coverage?: {recognized_place_count: number; confirmed_place_count: number; unresolved_place_count: number; unclassified_mention_count: number; unprocessed_count: number; complete: boolean} | null
  can_undo?: boolean
  can_redo?: boolean
  ownership?: 'ANONYMOUS' | 'ACCOUNT'
  expires_at?: string | null
  updated_at?: string | null
  is_demo?: boolean
  status: 'READY' | 'PARTIAL_RESULT' | 'BASIC_ONLY' | 'LIMITED'
  assumptions: AssumptionChipView[]
  days: Array<{ label: string; day_id?: string; source_notes?: Array<{note_id:string; text:string; position:number}>; activities: ActivityCardView[]; unprocessed_count?: number;
    meal_slots?: Array<{meal_role:'BREAKFAST'|'LUNCH'|'DINNER'|'SNACK'|'UNSPECIFIED';after_activity_token?:string|null;before_activity_token?:string|null;
      preference_text?:string|null;
      selection_status?:'UNKNOWN'|'UNSELECTED'|'SELECTED';selected_activity_token?:string|null}>;
    choice_selections?: Array<{choice_group_token:string;branch_token:string;activity_tokens:string[];status:'SELECTED'|'MODIFIED'}>;
    alternatives?: Array<{semantic_review?: 'CONFIRMED' | 'PENDING'; name: string; category: string; city?: string | null; replaces_visit_id?: string | null; replaces_name?: string | null; replacement_condition?: string | null; branch_label?: string | null; branch_token?: string | null; choice_group_token?: string | null; choice_group_selectable?: boolean; activity_token?: string | null; insertion_position?:number|null; source_details?: ActivityCardView['source_details']}> }>
  map: {
    status:
      | 'PREPARING'
      | 'AVAILABLE'
      | 'NEEDS_UPDATE'
      | 'LIMITED'
      | 'UNAVAILABLE'
    message: string
    available_actions: Array<'VIEW_MAP' | 'RENDER_MAP'>
  }
  stay: {
    segments?: StaySegmentView[]
    status: StaySuggestionView['status']
    message: string
    area_summary: string | null
    searched_scopes: string[]
    candidates: StayCandidateView[]
    available_actions: Array<'CHOOSE_STAY'>
  }
  available_actions: Array<'EDIT_ASSUMPTIONS' | 'EDIT_CARDS'>
}

export type TripUnderstandingCommand =
  | {command_type:'ISSUE_DISPOSITION';issue_id:string;disposition:'IGNORED'|'OPEN'}
  | { command_type: 'ALTERNATIVE_INSERT'; day_index: number; alternative_token: string; position: number }
  | { command_type: 'CHOICE_CLEAR'; day_index: number; choice_group_token: string; preserve_activities?: boolean }
  | { command_type: 'CHOICE_SELECT'; day_index: number; choice_group_token: string; branch_token: string; position?: number }
  | { command_type: 'LODGING_RECOVER'; pending_token: string; candidate_token: string; intent: LodgingRecoveryIntent }
  | { command_type: 'DINING_INSERT'; after_activity_token: string; candidate_token: string; insert_before?:boolean; meal_role?:'BREAKFAST'|'LUNCH'|'DINNER'|'SNACK'; meal_slot?:SourceMealRef }
  | {
      command_type: 'ACTIVITY_TIMES_APPLY'
      changes: Array<{
        activity_token: string
        start_time: string
        end_time: string | null
      }>
    }
  | { command_type: 'UNDO'; change_etag?:string }
  | { command_type: 'REDO' }
  | {
      command_type: 'PLACE_CONFIRM'
      activity_token: string
      candidate_token: string
    }
  | {
      command_type: 'ACTIVITY_TIME_SET'
      activity_token: string
      start_time?: string | null
      end_time?: string | null
      visit_duration_minutes?: number | null
      locked?: boolean
    }
  | {
      command_type: 'ACTIVITY_INSERT'
      city?: string | null
      day_index: number
      position: number
      name: string
      category?: string
      area_or_address?: string
      time_hint?: string | null
    }
  | { command_type: 'ACTIVITY_DELETE'; activity_token: string }
  | { command_type:'SOURCE_RESTORE'; source_token:string; day_index:number; position:number; kind:'NOTE'|'PLACE'; name?:string }
  | {
      command_type: 'ACTIVITY_MOVE'
      activity_token: string
      target_day_index: number
      target_position: number
    }
  | {
      command_type: 'ACTIVITY_TEXT_EDIT'
      note?: string
      activity_token: string
      name?: string
      time_hint?: string | null
    }
  | {
      command_type: 'PLACE_REPLACE'
      activity_token: string
      replacement: { name: string; category: string; area_or_address: string }
    }
  | {
      command_type: 'ASSUMPTION_SET'
      key: 'destination' | 'calendar' | 'party_size'
      value: string
    }

export interface CommandAppliedView {
  status: 'APPLIED'
  changed_days: string[]
  map_readiness: 'NEEDS_UPDATE'|'PREPARING'
}

export interface ClaimedTripView {
  status: 'CLAIMED'
  public_resource_id: string
}

export interface TravelDataDeletionStatusView {
  status: 'IN_PROGRESS' | 'COMPLETED' | 'RETRY_REQUIRED'
  message: string
  next_action: 'NONE' | 'RETRY'
}

export interface DataConsentView {
  memory_enabled: boolean
  feedback_enabled: boolean
  training_eval_enabled: boolean
}

export interface PreferenceMemoryView {
  walking_tolerance_minutes: number | null
  preferred_start_time: string | null
  dining_preferences: Array<
    'LOCAL' | 'VEGETARIAN' | 'HALAL' | 'NO_SPICY' | 'QUICK'
  >
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

export interface SharedTripActivity {
  name: string
  area_or_address: string
  time_hint: string | null
  note: '可直接查看' | '地点待确认' | '安排待复核'
  details?: Array<{name: string; optional: boolean}>
}

export interface ShareProjectionView {
  title: string
  destination: string
  schedule: string
  party_size: string
  days: Array<{
    label: string
    activities: SharedTripActivity[]
    pending_activities?: SharedTripActivity[]
    alternatives?: Array<{name: string; category: string; branch_label: string | null; state: string; details?: Array<{name: string; optional: boolean}>}>
    meal_arrangements?: string[]
    notes?: string[]
    pending_count?: number
    unprocessed_count?: number
  }>
  accommodation: string | null
  message: string
  warnings?: string[]
  unassigned_alternatives?: string[]
  lodging_arrangements?: string[]
}

export interface MaterializedTripView {
  status: 'READY'
  message: string
  calendar: string
  party_size: number
  checks_available: boolean
}

export interface PublicTripCheckItem {
  input_fingerprint?: string
  issue_id?: string
  issue_type?: string
  target_visit_ids?: string[]
  target_day_ids?: string[]
  check_token: string
  label: '必须调整' | '可以更好' | '需要确认'
  title: string
  message: string
  affected_days: string[]
  can_preview: boolean
  affected_activity_tokens?: string[]
  depends_on_routes?: boolean
  basis_status?: 'CURRENT' | 'NEEDS_RECHECK'
}

export interface MyTripListItem {
  public_resource_id: string
  title: string
  city: string
  day_count: number
  updated_at: string
  expires_at: string
  is_demo: boolean
  state?: 'PROCESSING' | 'READY' | 'PARTIAL' | 'FAILED' | 'CANCELLED'
  has_result?: boolean
  source_status?: 'AVAILABLE' | 'DELETED' | 'UNAVAILABLE'
}

export interface MyTripListView {
  items: MyTripListItem[]
  next_cursor: string | null
}

export interface TripSourceView {
  fragments?:Array<{fragment_id:string;source_version:string;start:number;end:number;day_index:number|null;text:string;source_token:string;restored:boolean}>
  status: 'AVAILABLE' | 'DELETED' | 'UNAVAILABLE'
  text: string | null
  activities: Array<{ activity_token: string; name: string; quote: string }>
}

export interface TripSupplementaryView {
  pending_lodgings?: Array<{
    pending_token: string
    name: string
    city: string | null
    status: 'NEEDS_CONFIRMATION'
  }>
  status: 'AVAILABLE' | 'DELETED' | 'UNAVAILABLE'
  days: Array<{
    day_index: number | null
    day_label: string
    items: Array<{
      name: string
      time_hint: string | null
      role: 'OPTIONAL' | 'EXCLUDED'
    }>
  }>
}

export interface PendingLodgingView {
  pending_token: string
  status: 'NEEDS_CONFIRMATION'
  unprocessed_count: number
}

export interface LodgingRecoveryIntent {
  kind: 'WHOLE_TRIP' | 'NIGHTS' | 'VISIT_ONLY'
  overnight_days?: number[]
  day_index?: number | null
  before_activity_token?: string | null
}

export interface LodgingConstraintView extends ActivityCardView {
  scope: 'WHOLE_TRIP' | 'NIGHTS'
  overnight_days: number[]
}

export interface PublicTripChecksView {
  all_items?: PublicTripCheckItem[]
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
  changes?: Array<{
    activity_token: string
    day_label: string
    name: string
    before: {
      start_time: string | null
      end_time: string | null
      visit_duration_minutes: number | null
      locked: boolean
    }
    after: {
      start_time: string | null
      end_time: string | null
      visit_duration_minutes: number | null
      locked: boolean
    }
  }>
}

export interface PublicChangeAdopted {
  status: 'APPLIED' | 'STILL_NEEDS_CONFIRMATION'
  message: string
  changed_days: string[]
  map_readiness: 'NEEDS_UPDATE'
  checks: PublicTripChecksView
}

export interface PublicComparedRouteEdge {
  from_name:string; to_name:string; mode:'walking'|'transit'
  duration_minutes:number; distance_meters:number
}
export interface PublicRelativeRoutePreview {
  kind:'RELATIVE_ORDER'; change_token:string; title:string; summary:string; day_index:number
  before:string[]; after:string[]; routes_before:PublicComparedRouteEdge[]; routes_after:PublicComparedRouteEdge[]
  duration_minutes_before:number; duration_minutes_after:number; minutes_saved:number
  distance_meters_before:number; distance_meters_after:number; comparison_scope:'CHANGED_EDGES_ONLY'
  route_coverage_scope?: 'REQUESTED_POINTS' | 'RETURNED_SEGMENTS' | null
}
export interface PublicRelativeRouteOptions {
  kind:'RELATIVE_ORDER'; status:'AVAILABLE'|'NO_IMPROVEMENT'|'NEEDS_CONFIRMATION'|'NEEDS_UPDATE'|'UNAVAILABLE'
  message:string; day_index:number; options:PublicRelativeRoutePreview[]
}

function requestKey(): string {
  const values = new Uint32Array(4)
  crypto.getRandomValues(values)
  return Array.from(values, (value) =>
    value.toString(16).padStart(8, '0'),
  ).join('')
}

export function createTripRequestKey(): string {
  return requestKey()
}

function authorizationHeaders(): Record<string, string> {
  if (typeof window === 'undefined') return {}
  const token = localStorage.getItem('authToken')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

function operationRequestKey(scope: string): string {
  if (typeof window === 'undefined') return requestKey()
  const storageKey = `bt_v3_operation_${scope}`
  const existing = sessionStorage.getItem(storageKey)
  if (existing) return existing
  const created = requestKey()
  sessionStorage.setItem(storageKey, created)
  return created
}

function rotateOperationRequestKey(scope: string): void {
  if (typeof window === 'undefined') return
  sessionStorage.removeItem(`bt_v3_operation_${scope}`)
}

export function clearTripUnderstandingInputDraft(
  publicResourceId: string,
): void {
  if (typeof window === 'undefined') return
  try {
    const draft = JSON.parse(sessionStorage.getItem('bt_input_draft') || 'null')
    if (draft?.resource === publicResourceId)
      sessionStorage.removeItem('bt_input_draft')
  } catch {
    // A response for one trip must not erase unrelated or unsubmitted input.
  }
}

export function clearTripUnderstandingSession(): void {
  if (typeof window === 'undefined') return
  for (const key of [
    'bt_active_trip_ref',
    'bt_active_trip_mode',
    'bt_active_trip_is_demo',
    'bt_active_trip_event_cursor',
    'bt_active_trip_etag',
    'bt_active_trip_source_deleted',
  ])
    sessionStorage.removeItem(key)
  for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
    const key = sessionStorage.key(index)
    if (
      key?.startsWith('bt_v3_operation_') ||
      key?.startsWith('bt_trip_event_cursor:')
    )
      sessionStorage.removeItem(key)
  }
}

export async function createDemoTripUnderstanding(
  signal?: AbortSignal,
  idempotencyKey = requestKey(),
): Promise<TripUnderstandingAcceptedView> {
  const response = await fetch('/api/v3/trip-understandings', {
    method: 'POST',
    credentials: 'include',
    signal,
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': idempotencyKey,
    },
    body: JSON.stringify({ mode: 'DEMO' }),
  })
  if (!response.ok) {
    if ([502, 503, 504].includes(response.status))
      throw new Error('CREATE_SERVICE_UNAVAILABLE')
    throw new Error('DEMO_CREATE_FAILED')
  }
  return response.json() as Promise<TripUnderstandingAcceptedView>
}

export async function createFullTripUnderstanding(
  text: string,
  idempotencyKey = requestKey(),
  signal?: AbortSignal,
  exampleReference?: {id: string; version: number},
): Promise<TripUnderstandingAcceptedView> {
  const response = await fetch('/api/v3/trip-understandings', {
    method: 'POST',
    credentials: 'include',
    signal,
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': idempotencyKey,
      ...authorizationHeaders(),
    },
    body: JSON.stringify({ mode: 'FULL', source: { type: 'TEXT', text }, ...(exampleReference ? {example_reference: exampleReference} : {}) }),
  })
  if (!response.ok) {
    if (response.status === 401) throw new Error('LOGIN_REQUIRED')
    if (response.status === 429) throw new Error('ACTIVE_LIMIT_REACHED')
    if ([502, 503, 504].includes(response.status))
      throw new Error('CREATE_SERVICE_UNAVAILABLE')
    throw new Error('FULL_CREATE_FAILED')
  }
  return response.json() as Promise<TripUnderstandingAcceptedView>
}

export async function readTripUnderstandingResult(
  publicResourceId: string,
  signal?: AbortSignal,
): Promise<{
  status: number
  body: TripUnderstandingProgressView | UserFacingTripResult
  etag: string | null
}> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/result`,
    {
      credentials: 'include',
      cache: 'no-store',
      signal,
      headers: authorizationHeaders(),
    },
  )
  if (!response.ok) {
    if (response.status === 410) {
      clearTripUnderstandingInputDraft(publicResourceId)
      throw new Error('TRIP_GONE')
    }
    if (response.status === 404) throw new Error('TRIP_NOT_AVAILABLE')
    if (response.status === 401) throw new Error('LOGIN_REQUIRED')
    if (response.status === 409) {
      const failure = (await response.json().catch(() => null)) as {
        detail?: { code?: string }
      } | null
      if (failure?.detail?.code === 'UNDERSTANDING_FAILED')
        throw new Error('UNDERSTANDING_FAILED')
      if (failure?.detail?.code === 'INPUT_CAPACITY_EXCEEDED')
        throw new Error('INPUT_CAPACITY_EXCEEDED')
      if (failure?.detail?.code === 'INPUT_DAY_CAPACITY_EXCEEDED')
        throw new Error('INPUT_DAY_CAPACITY_EXCEEDED')
      if (failure?.detail?.code === 'UNDERSTANDING_CANCELLED')
        throw new Error('UNDERSTANDING_CANCELLED')
    }
    throw new Error('TRIP_RESULT_UNAVAILABLE')
  }
  const payload = (await response.json()) as
    | Partial<TripUnderstandingProgressView>
    | UserFacingTripResult
  if (response.status === 202) {
    const pending = payload as Partial<TripUnderstandingProgressView>
    const metric = pending.progress || ({} as TripUnderstandingProgressMetrics)
    const numberOrZero = (value: unknown) =>
      typeof value === 'number' && Number.isFinite(value) && value >= 0
        ? value
        : 0
    const allowedPhases = new Set<TripUnderstandingProgressView['phase']>([
      'RECEIVED',
      'CARDS_AVAILABLE',
      'CHECKING_PLACES',
    ])
    const body: TripUnderstandingProgressView = {
      status: 'PROCESSING',
      message:
        typeof pending.message === 'string' && pending.message.trim()
          ? pending.message
          : '正在整理每天的安排…',
      retry_after_ms:
        typeof pending.retry_after_ms === 'number' &&
        Number.isFinite(pending.retry_after_ms) &&
        pending.retry_after_ms > 0
          ? pending.retry_after_ms
          : 500,
      phase: allowedPhases.has(
        pending.phase as TripUnderstandingProgressView['phase'],
      )
        ? (pending.phase as TripUnderstandingProgressView['phase'])
        : 'RECEIVED',
      event_cursor: Number.isSafeInteger(pending.event_cursor) &&
        Number(pending.event_cursor) >= 0
        ? Number(pending.event_cursor)
        : 0,
      progress: {
        semantic_complete: typeof metric.semantic_complete === 'boolean' ? metric.semantic_complete : null,
        places_total_final: typeof metric.places_total_final === 'boolean' ? metric.places_total_final : null,
        day_count: numberOrZero(metric.day_count),
        card_count: numberOrZero(metric.card_count),
        places_checked: numberOrZero(metric.places_checked),
        places_total: numberOrZero(metric.places_total),
      },
      snapshot:
        pending.snapshot &&
        typeof pending.snapshot === 'object' &&
        Array.isArray(pending.snapshot.days)
          ? pending.snapshot
          : null,
    }
    return {
      status: response.status,
      body,
      etag: response.headers.get('etag'),
    }
  }
  return {
    status: response.status,
    body: payload as UserFacingTripResult,
    etag: response.headers.get('etag'),
  }
}

export async function cancelTripUnderstanding(
  publicResourceId: string,
  signal?: AbortSignal,
  idempotencyKey = operationRequestKey(`cancel:${publicResourceId}`),
): Promise<{
  body: TripUnderstandingCancelView
  etag: string | null
  replayed: boolean
}> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/cancel`,
    {
      method: 'POST',
      credentials: 'include',
      cache: 'no-store',
      signal,
      headers: {
        'Idempotency-Key': idempotencyKey,
        ...authorizationHeaders(),
      },
    },
  )
  if (!response.ok) {
    if (response.status === 401) throw new Error('LOGIN_REQUIRED')
    if (response.status === 404) throw new Error('TRIP_NOT_AVAILABLE')
    if (response.status === 410) throw new Error('TRIP_GONE')
    if (response.status === 409) throw new Error('CANCEL_CONFLICT')
    throw new Error('CANCEL_UNAVAILABLE')
  }
  const result = {
    body: (await response.json()) as TripUnderstandingCancelView,
    etag: response.headers.get('etag'),
    replayed: response.headers.get('Idempotency-Replayed') === 'true',
  }
  rotateOperationRequestKey(`cancel:${publicResourceId}`)
  return result
}

export async function applyTripUnderstandingCommand(
  publicResourceId: string,
  etag: string,
  command: TripUnderstandingCommand,
  idempotencyKey = requestKey(),
  signal?: AbortSignal,
): Promise<{ body: CommandAppliedView; etag: string }> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/commands`,
    {
      method: 'POST',
      credentials: 'include',
      signal,
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': idempotencyKey,
        'If-Match': etag,
        ...authorizationHeaders(),
      },
      body: JSON.stringify(command),
    },
  )
  if (!response.ok) {
    if (response.status === 409) {
      const failure = (await response.json().catch(() => null)) as {
        detail?: { code?: string }
      } | null
      if (failure?.detail?.code === 'REQUEST_IN_PROGRESS')
        throw new Error('OPERATION_PENDING')
      throw new Error('REVISION_CONFLICT')
    }
    if (response.status === 428) throw new Error('IF_MATCH_REQUIRED')
    if (response.status === 422 || response.status === 400)
      throw new Error('COMMAND_REJECTED')
    throw new Error('COMMAND_FAILED')
  }
  const nextEtag = response.headers.get('etag')
  if (!nextEtag) throw new Error('COMMAND_ETAG_MISSING')
  return {
    body: (await response.json()) as CommandAppliedView,
    etag: nextEtag,
  }
}

export async function readTripUnderstandingMap(
  publicResourceId: string,
  signal?: AbortSignal,
): Promise<MapRenderView> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/map-renders/latest`,
    {
      credentials: 'include',
      cache: 'no-store',
      signal,
      headers: authorizationHeaders(),
    },
  )
  if (!response.ok) throw new Error('MAP_UNAVAILABLE')
  return response.json() as Promise<MapRenderView>
}

async function readPrivateTripJson<T>(
  path: string,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(path, {
    credentials: 'include',
    cache: 'no-store',
    signal,
    headers: authorizationHeaders(),
  })
  if (!response.ok) {
    if (response.status === 401) throw new Error('LOGIN_REQUIRED')
    if (response.status === 410) throw new Error('TRIP_GONE')
    if (response.status === 404) throw new Error('TRIP_NOT_AVAILABLE')
    if (response.status === 400) throw new Error('LIST_CURSOR_CHANGED')
    throw new Error('READ_UNAVAILABLE')
  }
  return response.json() as Promise<T>
}

export function listMyTrips(
  cursor?: string | null,
  signal?: AbortSignal,
): Promise<MyTripListView> {
  const query = new URLSearchParams({ limit: '20' })
  if (cursor) query.set('cursor', cursor)
  return readPrivateTripJson<MyTripListView>(
    `/api/v3/me/trips?${query}`,
    signal,
  )
}

export function readTripSource(
  publicResourceId: string,
  signal?: AbortSignal,
): Promise<TripSourceView> {
  return readPrivateTripJson<TripSourceView>(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/source`,
    signal,
  )
}

export interface TripSupplementState {
  status: 'AVAILABLE'|'UNAVAILABLE'|'QUEUED'|'RUNNING'|'APPLIED'|'NO_CHANGES'|'FAILED'|'CANCELLED'
  message: string
  job_id?: string|null
  base_etag?: string|null
  result_etag?: string|null
  added_count: number
  rejected: Array<{operation_id:string;reason:string}>
  available_actions: Array<'REQUEST'|'RETRY'|'CANCEL'>
  reason?: string|null
}

export function readTripSupplement(resource: string, signal?: AbortSignal): Promise<TripSupplementState> {
  return readPrivateTripJson(`/api/v3/trip-understandings/${encodeURIComponent(resource)}/supplements`, signal)
}

export async function requestTripSupplement(resource:string, etag:string, retry:boolean, key:string):Promise<TripSupplementState> {
  const response = await fetch(`/api/v3/trip-understandings/${encodeURIComponent(resource)}/supplements`, {
    method:'POST', credentials:'include', cache:'no-store',
    headers:{...authorizationHeaders(),'Content-Type':'application/json','If-Match':etag,'Idempotency-Key':key},
    body:JSON.stringify({retry}),
  })
  const body = await response.json()
  if(!response.ok) throw new Error(body.detail?.message || '补全请求尚未确认，请稍后查看')
  return body
}

export async function cancelTripSupplement(resource:string, jobId:string):Promise<TripSupplementState> {
  const response = await fetch(`/api/v3/trip-understandings/${encodeURIComponent(resource)}/supplements/${encodeURIComponent(jobId)}/cancel`, {
    method:'POST', credentials:'include', cache:'no-store', headers:authorizationHeaders(),
  })
  if(!response.ok) throw new Error('停止结果尚未确认，请稍后查看')
  return response.json()
}

export function readTripSupplementary(
  publicResourceId: string,
  signal?: AbortSignal,
  includePendingLodgings = false,
): Promise<TripSupplementaryView> {
  return readPrivateTripJson<TripSupplementaryView>(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/supplementary${includePendingLodgings ? '?include_pending_lodgings=true' : ''}`,
    signal,
  )
}

export async function queryPendingLodgingCandidates(
  publicResourceId: string,
  pendingToken: string,
  query: string,
  intent: LodgingRecoveryIntent,
  etag: string,
  signal?: AbortSignal,
  city?: string,
): Promise<PlaceCandidatesView> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/place-candidates`,
    {
      method: 'POST',
      credentials: 'include',
      cache: 'no-store',
      signal,
      headers: {
        'Content-Type': 'application/json',
        'If-Match': etag,
        ...authorizationHeaders(),
      },
      body: JSON.stringify({ pending_token: pendingToken, query, intent, ...(city ? { city } : {}) }),
    },
  )
  if (!response.ok) {
    if (response.status === 401) throw new Error('LOGIN_REQUIRED')
    if (response.status === 410) throw new Error('TRIP_GONE')
    if (response.status === 409) throw new Error('REVISION_CONFLICT')
    if (response.status === 428) throw new Error('IF_MATCH_REQUIRED')
    if (response.status === 422) {
      const failure = (await response.json().catch(() => null)) as { detail?: { code?: string } } | null
      if (failure?.detail?.code === 'CITY_REQUIRED') throw new Error('CITY_REQUIRED')
    }
    throw new Error('PLACE_SEARCH_UNAVAILABLE')
  }
  return response.json() as Promise<PlaceCandidatesView>
}

export async function queryTripPlaceCandidates(
  publicResourceId: string,
  activityToken: string,
  query: string,
  signal?: AbortSignal,
  city?: string,
): Promise<PlaceCandidatesView> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/place-candidates`,
    {
      method: 'POST',
      credentials: 'include',
      signal,
      headers: {
        'Content-Type': 'application/json',
        ...authorizationHeaders(),
      },
      body: JSON.stringify({ activity_token: activityToken, query, ...(city ? { city } : {}) }),
    },
  )
  if (!response.ok) throw new Error('PLACE_SEARCH_UNAVAILABLE')
  return response.json() as Promise<PlaceCandidatesView>
}

export interface DiningCandidatesView {
  status: 'AVAILABLE' | 'EMPTY' | 'UNAVAILABLE' | 'NEEDS_CONFIRMATION'
  message: string
  candidates: Array<PlaceCandidateView & { reason: string }>
}

export interface SourceMealRef {day_index: number; slot_index: number}
export interface DiningPOIInfo {
  photo_url?: string | null; cuisine?: string | null; tags: string[]
  rating?: number | null; cost?: number | null; source: 'AMAP_POI_V2'; observed_at: string
}
export interface SourceMealCandidatesView {
  status: 'AVAILABLE'|'EMPTY'|'UNAVAILABLE'|'NEEDS_CONFIRMATION'|'POSITION_REQUIRED'|'EXISTING'|'NEEDS_REVIEW'
  message: string; meal_slot: SourceMealRef; preference_text?: string | null
  meal_role?: 'BREAKFAST'|'LUNCH'|'DINNER'|'SNACK'|null
  after_activity_token?:string|null; insert_before:boolean; anchor_name?:string|null
  pending_activity_tokens:string[]
  candidates:Array<PlaceCandidateView & {reason:string;dining_info?:DiningPOIInfo|null}>
}
export async function querySourceMealCandidates(resource: string, etag: string,
  body: {meal_slot:SourceMealRef; query:string;position?:{activity_token:string;insert_before:boolean}}, signal:AbortSignal,
): Promise<SourceMealCandidatesView> {
  const response = await fetch(`/api/v3/trip-understandings/${encodeURIComponent(resource)}/source-meal-candidates`, {
    method:'POST', credentials:'include', cache:'no-store', signal,
    headers:{...authorizationHeaders(),'Content-Type':'application/json','If-Match':etag}, body:JSON.stringify(body),
  })
  if (response.status === 409) throw new Error('SOURCE_MEAL_VERSION_CHANGED')
  if (response.status === 401) throw new Error('LOGIN_REQUIRED')
  if (response.status === 404 || response.status === 410) throw new Error('TRIP_GONE')
  if (!response.ok) throw new Error('SOURCE_MEAL_UNAVAILABLE')
  if (response.headers.get('ETag') !== etag) throw new Error('SOURCE_MEAL_VERSION_CHANGED')
  return response.json() as Promise<SourceMealCandidatesView>
}

export interface DailyMealView {
  day_index: number; label: string; status: 'AVAILABLE' | 'EMPTY' | 'UNAVAILABLE' | 'EXISTING' | 'NEEDS_CONFIRMATION'
  message: string; after_activity_token?: string | null; next_name?: string | null
  insert_before?: boolean; meal_role?: 'LUNCH' | null
  existing_activity_token?: string | null; area?: string | null
  area_relation?: 'PROVIDER_AREA' | 'NEARBY' | null; area_distance_m?: number | null
  candidates: Array<DiningContextView & {candidate_token: string; name: string; area_or_address: string; business_area?: string | null; reason: string; extra_minutes?: number | null; recommended: boolean; route_coverage_scope?: 'REQUESTED_POINTS' | 'RETURNED_SEGMENTS' | null}>
}
export interface DailyDiningView {
  status: 'PREPARING' | 'AVAILABLE' | 'NEEDS_UPDATE' | 'UNAVAILABLE'
  message: string; days: DailyMealView[]
}
export async function readDailyDining(resource: string, signal?: AbortSignal, refresh?: {etag: string; key: string}): Promise<{body: DailyDiningView; etag: string}> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  if (signal?.aborted) controller.abort()
  else signal?.addEventListener('abort', abort, {once:true})
  const timer = setTimeout(abort, 15000)
  try {
    const response = await fetch(`/api/v3/trip-understandings/${encodeURIComponent(resource)}/daily-dining`, {
      credentials: 'include', cache:'no-store', signal:controller.signal, method: refresh ? 'POST' : 'GET',
      headers: {...authorizationHeaders(), ...(refresh ? {'If-Match':refresh.etag, 'Idempotency-Key':refresh.key} : {})},
    })
    if (!response.ok) throw new Error('DAILY_DINING_UNAVAILABLE')
    return {body: await response.json(), etag: response.headers.get('ETag') || ''}
  } finally {clearTimeout(timer); signal?.removeEventListener('abort',abort)}
}

export async function queryTripDiningCandidates(resource: string, activity: string, signal?: AbortSignal): Promise<{body: DiningCandidatesView; etag: string}> {
  const response = await fetch(`/api/v3/trip-understandings/${encodeURIComponent(resource)}/dining-candidates`, {
    method: 'POST', credentials: 'include', signal,
    headers: {'Content-Type': 'application/json', ...authorizationHeaders()},
    body: JSON.stringify({activity_token: activity}),
  })
  if (!response.ok) throw new Error('DINING_SEARCH_UNAVAILABLE')
  return {body: await response.json(), etag: response.headers.get('ETag') || ''}
}

export async function requestTripUnderstandingMap(
  publicResourceId: string,
  etag: string,
  idempotencyKey = requestKey(),
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/map-renders`,
    {
      method: 'POST',
      credentials: 'include',
      signal,
      headers: {
        'Idempotency-Key': idempotencyKey,
        'If-Match': etag,
        ...authorizationHeaders(),
      },
    },
  )
  if (!response.ok) {
    if (response.status === 409) {
      const failure = (await response.json().catch(() => null)) as {
        detail?: { code?: string }
      } | null
      if (failure?.detail?.code === 'REQUEST_IN_PROGRESS')
        throw new Error('OPERATION_PENDING')
      throw new Error('REVISION_CONFLICT')
    }
    throw new Error('MAP_RENDER_FAILED')
  }
}

export async function readTripUnderstandingStay(
  publicResourceId: string,
  signal?: AbortSignal,
): Promise<StaySuggestionView> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/stay-suggestions`,
    {
      credentials: 'include',
      cache: 'no-store',
      signal,
      headers: authorizationHeaders(),
    },
  )
  if (!response.ok) throw new Error('STAY_UNAVAILABLE')
  return response.json() as Promise<StaySuggestionView>
}

export async function refreshTripUnderstandingStay(publicResourceId: string, etag: string, key: string, signal?: AbortSignal): Promise<StaySuggestionView> {
  const response = await fetch(`/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/stay-suggestions`, {
    method:'POST', credentials:'include', cache:'no-store', signal,
    headers:{...authorizationHeaders(), 'If-Match':etag, 'Idempotency-Key':key},
  })
  if (!response.ok) throw new Error(response.status === 409 ? 'REVISION_CONFLICT' : 'STAY_UNAVAILABLE')
  return response.json() as Promise<StaySuggestionView>
}

export async function selectTripUnderstandingStay(
  publicResourceId: string,
  candidateToken: string,
  etag: string,
  idempotencyKey = requestKey(),
  signal?: AbortSignal,
): Promise<{ selected_stay: string; etag: string }> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/stay-selection`,
    {
      method: 'POST',
      credentials: 'include',
      signal,
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': idempotencyKey,
        'If-Match': etag,
        ...authorizationHeaders(),
      },
      body: JSON.stringify({ candidate_token: candidateToken }),
    },
  )
  if (!response.ok) {
    if (response.status === 409) {
      const failure = (await response.json().catch(() => null)) as {
        detail?: { code?: string }
      } | null
      if (failure?.detail?.code === 'REQUEST_IN_PROGRESS')
        throw new Error('OPERATION_PENDING')
      throw new Error('REVISION_CONFLICT')
    }
    throw new Error('STAY_SELECTION_FAILED')
  }
  const nextEtag = response.headers.get('etag')
  if (!nextEtag) throw new Error('STAY_SELECTION_ETAG_MISSING')
  const body = (await response.json()) as { selected_stay: string }
  return { selected_stay: body.selected_stay, etag: nextEtag }
}

export async function materializeTripUnderstanding(
  publicResourceId: string,
  etag: string,
  signal?: AbortSignal,
  idempotencyKey?: string,
): Promise<{ body: MaterializedTripView; etag: string }> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/materialize`,
    {
      method: 'POST',
      credentials: 'include',
      signal,
      headers: {
        'Idempotency-Key':
          idempotencyKey ||
          operationRequestKey(`materialize:${publicResourceId}:${etag}`),
        'If-Match': etag,
        ...authorizationHeaders(),
      },
    },
  )
  if (!response.ok) {
    if (response.status === 409) throw new Error('TRIP_UPDATED')
    throw new Error('MATERIALIZE_FAILED')
  }
  const currentEtag = response.headers.get('etag')
  if (!currentEtag) throw new Error('MATERIALIZE_ETAG_MISSING')
  return {
    body: (await response.json()) as MaterializedTripView,
    etag: currentEtag,
  }
}

export interface InspectorIssue {
  issue_id:string; kind:'PLACE'|'SOURCE'|'CHECK'|'MEAL'|'ALTERNATIVE'|'LODGING'
  category:'DECISION'|'OPTIONAL'; severity:'INFO'|'WARNING'|'ERROR'; disposition:'OPEN'|'IGNORED'
  title:string; message:string; day_index:number|null; target_day_ids:string[]; target_visit_ids:string[]
  actions:string[]; dependencies:string[]; input_version:string; input_fingerprint:string; check:PublicTripCheckItem|null
}
export async function readTripInspector(resource:string, signal?:AbortSignal):Promise<{input_version:string;issues:InspectorIssue[];changes:Array<{change_etag:string;title:string}>}> {
  const response=await fetch(`/api/v3/trip-understandings/${encodeURIComponent(resource)}/inspector`,{
    credentials:'include',cache:'no-store',signal,headers:authorizationHeaders(),
  })
  if(!response.ok)throw new Error('INSPECTOR_UNAVAILABLE')
  return response.json()
}

export async function readTripUnderstandingChecks(
  publicResourceId: string,
  signal?: AbortSignal,
): Promise<PublicTripChecksView> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/checks`,
    {
      credentials: 'include',
      cache: 'no-store',
      signal,
      headers: authorizationHeaders(),
    },
  )
  if (!response.ok) throw new Error('CHECKS_UNAVAILABLE')
  return response.json() as Promise<PublicTripChecksView>
}

export async function previewTripUnderstandingChange(
  publicResourceId: string,
  checkToken: string,
  signal?: AbortSignal,
): Promise<PublicChangePreview> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/changes/preview`,
    {
      method: 'POST',
      credentials: 'include',
      signal,
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': operationRequestKey(`change-preview:${checkToken}`),
        ...authorizationHeaders(),
      },
      body: JSON.stringify({ check_token: checkToken }),
    },
  )
  if (!response.ok) {
    if (response.status === 409) throw new Error('CHECK_CHANGED')
    throw new Error('CHANGE_PREVIEW_FAILED')
  }
  return response.json() as Promise<PublicChangePreview>
}

export async function compareRelativeTripRoutes(resource:string, etag:string, dayIndex:number, key:string, signal:AbortSignal):Promise<PublicRelativeRouteOptions> {
  const response = await fetch(`/api/v3/trip-understandings/${encodeURIComponent(resource)}/changes/preview`, {
    method:'POST', credentials:'include', cache:'no-store', signal,
    headers:{...authorizationHeaders(),'Content-Type':'application/json','If-Match':etag,'Idempotency-Key':key},
    body:JSON.stringify({day_index:dayIndex}),
  })
  if(response.status===409) {
    const error=await response.json().catch(()=>null) as {detail?:{code?:string}}|null
    throw new Error(error?.detail?.code==='REQUEST_IN_PROGRESS'?'ROUTE_COMPARISON_PENDING':'ROUTE_COMPARISON_STALE')
  }
  if(response.status===401)throw new Error('LOGIN_REQUIRED')
  if(response.status===404||response.status===410)throw new Error('TRIP_GONE')
  if(!response.ok)throw new Error('ROUTE_COMPARISON_UNAVAILABLE')
  const body=await response.json() as PublicRelativeRouteOptions
  if(response.headers.get('ETag')!==etag||body.kind!=='RELATIVE_ORDER'||body.day_index!==dayIndex
    ||!Array.isArray(body.options)||body.options.length>3||body.options.some(option=>option.kind!=='RELATIVE_ORDER'||option.day_index!==dayIndex||option.comparison_scope!=='CHANGED_EDGES_ONLY'))throw new Error('ROUTE_COMPARISON_STALE')
  return body
}

export async function adoptTripUnderstandingChange(
  publicResourceId: string,
  changeToken: string,
  etag: string,
  idempotencyKey = operationRequestKey(`change-adopt:${changeToken}`),
  signal?: AbortSignal,
): Promise<{ body: PublicChangeAdopted; etag: string }> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/changes/adopt`,
    {
      method: 'POST',
      credentials: 'include',
      signal,
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': idempotencyKey,
        'If-Match': etag,
        ...authorizationHeaders(),
      },
      body: JSON.stringify({ change_token: changeToken }),
    },
  )
  if (!response.ok) {
    if (response.status === 409) {
      const failure = (await response.json().catch(() => null)) as {
        detail?: { code?: string }
      } | null
      if (failure?.detail?.code === 'REQUEST_IN_PROGRESS')
        throw new Error('OPERATION_PENDING')
      throw new Error('PREVIEW_STALE')
    }
    throw new Error('CHANGE_ADOPT_FAILED')
  }
  const nextEtag = response.headers.get('etag')
  if (!nextEtag) throw new Error('CHANGE_ADOPT_ETAG_MISSING')
  return {
    body: (await response.json()) as PublicChangeAdopted,
    etag: nextEtag,
  }
}

export async function claimTripUnderstanding(
  publicResourceId: string,
  idempotencyKey = operationRequestKey(`claim:${publicResourceId}`),
  signal?: AbortSignal,
): Promise<{ body: ClaimedTripView; etag: string }> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/claim`,
    {
      method: 'POST',
      credentials: 'include',
      signal,
      headers: {
        'Idempotency-Key': idempotencyKey,
        ...authorizationHeaders(),
      },
    },
  )
  if (!response.ok) {
    if (response.status === 401) throw new Error('LOGIN_REQUIRED')
    if (response.status === 409) {
      const failure = (await response.json().catch(() => null)) as {
        detail?: { code?: string }
      } | null
      if (failure?.detail?.code === 'REQUEST_IN_PROGRESS')
        throw new Error('OPERATION_PENDING')
      throw new Error('CLAIM_FAILED')
    }
    if (response.status === 410) {
      clearTripUnderstandingInputDraft(publicResourceId)
      throw new Error('TRIP_ALREADY_GONE')
    }
    throw new Error('CLAIM_FAILED')
  }
  const etag = response.headers.get('etag')
  if (!etag) throw new Error('CLAIM_ETAG_MISSING')
  return { body: (await response.json()) as ClaimedTripView, etag }
}

export async function deleteTripUnderstandingSource(
  publicResourceId: string,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/source`,
    {
      method: 'DELETE',
      credentials: 'include',
      signal,
      headers: {
        'Idempotency-Key': operationRequestKey(
          `delete-source:${publicResourceId}`,
        ),
        ...authorizationHeaders(),
      },
    },
  )
  if (!response.ok) {
    if (response.status === 401) throw new Error('LOGIN_REQUIRED')
    throw new Error('SOURCE_DELETE_FAILED')
  }
  clearTripUnderstandingInputDraft(publicResourceId)
}

export async function deleteTripUnderstanding(
  publicResourceId: string,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}`,
    {
      method: 'DELETE',
      credentials: 'include',
      signal,
      headers: {
        'Idempotency-Key': operationRequestKey(
          `delete-trip:${publicResourceId}`,
        ),
        ...authorizationHeaders(),
      },
    },
  )
  if (!response.ok) throw new Error('TRIP_DELETE_FAILED')
  const readback = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/result`,
    {
      credentials: 'include',
      cache: 'no-store',
      signal,
      headers: authorizationHeaders(),
    },
  )
  if (readback.status !== 410) throw new Error('TRIP_DELETE_READBACK_FAILED')
  clearTripUnderstandingInputDraft(publicResourceId)
}

export async function deleteAllTravelData(): Promise<TravelDataDeletionStatusView> {
  const operationScope = 'delete-account-travel-data'
  const response = await fetch('/api/v3/me/travel-data', {
    method: 'DELETE',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': operationRequestKey(operationScope),
      ...authorizationHeaders(),
    },
    body: JSON.stringify({ confirmation: 'DELETE_ALL_TRAVEL_DATA' }),
  })
  if (!response.ok) {
    if (response.status === 401) throw new Error('RECENT_LOGIN_REQUIRED')
    throw new Error('ACCOUNT_TRAVEL_DELETE_FAILED')
  }
  const outcome = (await response.json()) as TravelDataDeletionStatusView
  if (outcome.status === 'RETRY_REQUIRED')
    rotateOperationRequestKey(operationScope)
  return outcome
}

export async function readTravelDataDeletionStatus(): Promise<TravelDataDeletionStatusView> {
  const response = await fetch('/api/v3/me/travel-data-deletion', {
    credentials: 'include',
    cache: 'no-store',
    headers: authorizationHeaders(),
  })
  if (!response.ok) throw new Error('ACCOUNT_TRAVEL_DELETE_STATUS_FAILED')
  return response.json() as Promise<TravelDataDeletionStatusView>
}

export async function readDataConsents(): Promise<DataConsentView> {
  const response = await fetch('/api/v3/me/data-consents', {
    credentials: 'include',
    cache: 'no-store',
    headers: authorizationHeaders(),
  })
  if (!response.ok) throw new Error('CONSENT_READ_FAILED')
  return response.json() as Promise<DataConsentView>
}

export async function setDataConsent(
  purpose: 'memory' | 'feedback' | 'training-eval',
  enabled: boolean,
): Promise<DataConsentView> {
  const response = await fetch(`/api/v3/me/data-consents/${purpose}`, {
    method: 'PUT',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...authorizationHeaders() },
    body: JSON.stringify({ enabled }),
  })
  if (!response.ok) throw new Error('CONSENT_UPDATE_FAILED')
  return response.json() as Promise<DataConsentView>
}

export async function readPreferenceMemory(): Promise<PreferenceMemoryView | null> {
  const response = await fetch('/api/v3/me/travel-preferences', {
    credentials: 'include',
    cache: 'no-store',
    headers: authorizationHeaders(),
  })
  if (!response.ok) throw new Error('PREFERENCE_READ_FAILED')
  return response.json() as Promise<PreferenceMemoryView | null>
}

export async function savePreferenceMemory(
  value: PreferenceMemoryView,
): Promise<PreferenceMemoryView> {
  const response = await fetch('/api/v3/me/travel-preferences', {
    method: 'PUT',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...authorizationHeaders() },
    body: JSON.stringify(value),
  })
  if (!response.ok) throw new Error('PREFERENCE_SAVE_FAILED')
  return response.json() as Promise<PreferenceMemoryView>
}

export async function clearPreferenceMemory(): Promise<void> {
  const response = await fetch('/api/v3/me/travel-preferences', {
    method: 'DELETE',
    credentials: 'include',
    headers: authorizationHeaders(),
  })
  if (!response.ok) throw new Error('PREFERENCE_CLEAR_FAILED')
}

export async function submitTripFeedback(
  publicResourceId: string,
  eventType: 'CORRECTION' | 'ADOPTED' | 'REJECTED' | 'VOLUNTARY',
): Promise<void> {
  const scope = `feedback:${publicResourceId}:${eventType}`
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/feedback`,
    {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': operationRequestKey(scope),
        ...authorizationHeaders(),
      },
      body: JSON.stringify({ event_type: eventType, subject_type: 'TRIP' }),
    },
  )
  if (!response.ok)
    throw new Error(
      response.status === 409 ? 'FEEDBACK_NOT_ENABLED' : 'FEEDBACK_FAILED',
    )
  rotateOperationRequestKey(scope)
}

export async function createTripShare(
  publicResourceId: string,
  expiresInDays = 7,
): Promise<ShareCreatedView> {
  const scope = `create-share:${publicResourceId}`
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/shares`,
    {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': operationRequestKey(scope),
        ...authorizationHeaders(),
      },
      body: JSON.stringify({ expires_in_days: expiresInDays }),
    },
  )
  if (!response.ok) throw new Error('SHARE_CREATE_FAILED')
  const view = (await response.json()) as ShareCreatedView
  rotateOperationRequestKey(scope)
  return view
}

export async function readMyShares(): Promise<ShareListItemView[]> {
  const response = await fetch('/api/v3/me/shares', {
    credentials: 'include',
    cache: 'no-store',
    headers: authorizationHeaders(),
  })
  if (!response.ok) throw new Error('SHARE_LIST_FAILED')
  return response.json() as Promise<ShareListItemView[]>
}

export async function revokeShare(shareRef: string): Promise<void> {
  const response = await fetch(
    `/api/v3/me/shares/${encodeURIComponent(shareRef)}`,
    {
      method: 'DELETE',
      credentials: 'include',
      headers: authorizationHeaders(),
    },
  )
  if (!response.ok) throw new Error('SHARE_REVOKE_FAILED')
}

export function tripUnderstandingEventsUrl(publicResourceId: string): string {
  return `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/events`
}

export interface TripUnderstandingPublicEvent {
  id: number
  type: 'progress' | 'result_available'
  message: string
  status: 'PROCESSING' | 'READY' | 'PARTIAL' | 'CANCELLED' | 'FAILED'
  phase: 'RECEIVED' | 'CARDS_AVAILABLE' | 'CHECKING_PLACES' | null
  progress: TripUnderstandingProgressMetrics
  snapshot: UserFacingTripResult | null
}

export async function streamTripUnderstandingEvents(
  publicResourceId: string,
  onEvent: (event: TripUnderstandingPublicEvent) => void,
  signal: AbortSignal,
  lastEventId = 0,
  onConnected?: () => void,
): Promise<void> {
  const response = await fetch(tripUnderstandingEventsUrl(publicResourceId), {
    credentials: 'include',
    cache: 'no-store',
    headers: {
      Accept: 'text/event-stream',
      ...(lastEventId > 0 ? { 'Last-Event-ID': String(lastEventId) } : {}),
      ...authorizationHeaders(),
    },
    signal,
  })
  if (!response.ok || !response.body) throw new Error('TRIP_EVENTS_UNAVAILABLE')
  onConnected?.()

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const dispatch = (block: string) => {
    let idValue = ''
    let eventValue = ''
    const data: string[] = []
    for (const line of block.split('\n')) {
      if (!line || line.startsWith(':')) continue
      const separator = line.indexOf(':')
      const field = separator < 0 ? line : line.slice(0, separator)
      const value =
        separator < 0 ? '' : line.slice(separator + 1).trimStart()
      if (field === 'id') idValue = value
      if (field === 'event') eventValue = value
      if (field === 'data') data.push(value)
    }
    if (!idValue || !eventValue || !data.length) return
    const id = Number(idValue)
    let payload: Partial<Omit<TripUnderstandingPublicEvent, 'id' | 'type'>>
    try {
      payload = JSON.parse(data.join('\n')) as typeof payload
    } catch {
      return
    }
    if (
      !Number.isSafeInteger(id) ||
      id <= 0 ||
      (eventValue !== 'progress' && eventValue !== 'result_available') ||
      typeof payload.message !== 'string' ||
      !payload.status
    )
      return
    onEvent({
      id,
      type: eventValue,
      message: payload.message,
      status: payload.status,
      phase: payload.phase || null,
      progress: payload.progress || {
        day_count: 0,
        card_count: 0,
        places_checked: 0,
        places_total: 0,
      },
      snapshot: payload.snapshot || null,
    })
  }
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read()
      buffer += decoder
        .decode(value, { stream: !done })
        .replace(/\r\n/g, '\n')
      if (done && buffer && !buffer.endsWith('\n\n')) buffer += '\n\n'
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        dispatch(buffer.slice(0, boundary))
        buffer = buffer.slice(boundary + 2)
        boundary = buffer.indexOf('\n\n')
      }
      if (done) return
    }
  } finally {
    await reader.cancel().catch(() => undefined)
  }
}
