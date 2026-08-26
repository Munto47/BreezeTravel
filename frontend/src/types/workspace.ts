export type ResolutionStatus = 'AUTO_MATCHED' | 'USER_CONFIRMED' | 'AMBIGUOUS' | 'NOT_FOUND'
export type AuditStatus = 'SATISFIED' | 'VIOLATED' | 'UNKNOWN'
export type AuditSeverity = 'BLOCKER' | 'HIGH' | 'MEDIUM' | 'LOW' | 'INFO'
export type EvidenceFreshness = 'FRESH' | 'STALE' | 'UNAVAILABLE' | 'CONFLICTING'

export interface TripWorkspace {
  workspace_id: string
  room_id: string
  city: string
  trip_date_range: { start: string; end: string }
  current_itinerary_revision: number | null
  current_import_id: string | null
  current_brief_id?: string | null
  current_trip_brief_revision?: number | null
  current_trip_check_run_id?: string | null
  current_report_id: string | null
  current_member_constraint_revision: number | null
  status: 'DRAFT' | 'AUDITING' | 'NEEDS_CONFIRMATION' | 'CONFIRMED'
}

export interface PlaceCandidate {
  place_id: string
  name: string
  city: string
  district: string | null
  address: string | null
  category: string | null
  retrieval_provider: string | null
  execution_mode: string | null
  score: number
  reasons: string[]
}

export interface RawImportStop {
  raw_stop_id: string
  day_index: number | null
  raw_name: string
  raw_time: string | null
  source_span: { start: number; end: number }
  source_sentence: string
  fixed_commitment: boolean
}

export interface ResolvedImportStop {
  raw_stop_id: string
  canonical_place_id: string | null
  candidates: PlaceCandidate[]
  confidence: number
  resolution_status: ResolutionStatus
  resolution_version: number
}

export interface ItineraryImport {
  import_id: string
  workspace_id: string
  source_type: 'AI_TEXT' | 'MANUAL_TEXT'
  raw_text: string
  parse_version: string
  status: 'PARSED' | 'NEEDS_RESOLUTION' | 'READY' | 'APPLIED' | 'FAILED'
  raw_stops: RawImportStop[]
  resolutions: ResolvedImportStop[]
  member_summary: string[]
  parse_errors: string[]
  state_version: number
}

export interface ScreenshotOcrLine {
  text: string
  confidence: number
  box: { x_min: number; y_min: number; x_max: number; y_max: number }
  requires_confirmation: boolean
}

export interface ScreenshotOcrReceipt {
  asset_id: string
  asset_hash: string
  media_type: string
  byte_size: number
  engine: string
  engine_version: string
  observed_at: string
  lines: ScreenshotOcrLine[]
}

export interface ScreenshotCleanupReceipt {
  receipt_id: string
  asset_id: string
  terminal_reason: string
  cleanup_status: string
  asset_hash: string
  cleanup_attempted_at: string
  cleanup_error_category: string | null
}

export interface ScreenshotImportResult {
  itinerary_import: ItineraryImport
  ocr_receipts: ScreenshotOcrReceipt[]
  cleanup_receipts: ScreenshotCleanupReceipt[]
}

export interface RevisionStop {
  stop_id: string
  place_id: string
  day_index: number
  order_index: number
  start_time: string | null
  end_time: string | null
  visit_duration_minutes: number | null
  transport_to_next: {
    mode: string
    duration_minutes: number | null
    distance_meters: number | null
  } | null
  raw_name: string | null
  fixed_commitment: boolean
  locked: boolean
  category: string
  notes: string
}

export interface ItineraryRevision {
  itinerary_id: string
  workspace_id: string
  revision: number
  content_hash: string
  days: Array<{ day_index: number; date: string | null; stops: RevisionStop[] }>
}

export interface WorkspaceMapStopProjection {
  stop_id: string
  place_id: string
  name: string
  day_index: number
  order_index: number
  coords: { lng: number; lat: number }
  coordinate_role: string
  provenance: string
  projection_revision: number
}

export interface WorkspaceMapCoordinateLink {
  day_index: number
  from_stop_id: string
  to_stop_id: string
  kind: 'CANONICAL_COORDINATE_LINK'
}

export interface WorkspaceMapProjection {
  workspace_id: string
  revision: number
  city: string
  stops: WorkspaceMapStopProjection[]
  coordinate_links: WorkspaceMapCoordinateLink[]
  missing_stop_ids: string[]
  status: 'AVAILABLE' | 'PARTIAL' | 'UNAVAILABLE'
  unavailable_reason: string | null
}

export type EditOperation =
  | 'ADD_STOP'
  | 'MOVE_STOP'
  | 'MOVE_TO_DAY'
  | 'REORDER_STOP'
  | 'ADJUST_TIME'
  | 'REPLACE_STOP'
  | 'REMOVE_STOP'
  | 'LOCK_STOP'
  | 'UNLOCK_STOP'
  | 'UNDO'

export interface WorkspaceEditRequest {
  command_id: string
  base_revision: number
  operation: EditOperation
  payload: Record<string, unknown>
  client_timestamp: string
}

export interface RouteEdgeDelta {
  edge_id: string
  previous_minutes: number | null
  current_minutes: number | null
  freshness: EvidenceFreshness
  source: string
  reason_code: string | null
}

export interface RouteDelta {
  status: 'AVAILABLE' | 'PARTIAL' | 'UNAVAILABLE'
  previous_minutes: number | null
  current_minutes: number | null
  delta_minutes: number | null
  changed_edges: RouteEdgeDelta[]
  removed_edge_ids?: string[]
  missing_edge_ids: string[]
  day_end_times?: Array<{
    day_index: number
    previous_end_time: string | null
    current_end_time: string | null
  }>
  async_route_refresh_required: boolean
  scope?: 'CURRENT_REVISION_CHANGED_EDGES_ONLY'
}

export interface ChangedRouteEdgeRefreshResult {
  workspace_id: string
  itinerary_revision: number
  source_revision: number | null
  report: AuditReport
  evidence_snapshot: EvidenceSnapshot
  route_delta: RouteDelta
  provider_failures: Array<{ provider: string; error_category: string; retryable: boolean; detail: string | null }>
  idempotent_replay: boolean
}

export interface ItineraryPatchResult {
  accepted: boolean
  command_id: string
  new_revision: number | null
  changed_days: number[]
  changed_route_edges: string[]
  route_delta: RouteDelta | null
  incremental_findings: AuditFinding[]
  affected_rule_ids: string[]
  audit_mode: 'NONE' | 'INCREMENTAL_REVISION_ONLY'
  llm_calls: number
  report_stale: boolean
  idempotent_replay: boolean
}

export interface CandidateSuggestion {
  candidate: {
    place_id: string
    name: string
    category: string
    address?: string | null
  }
  tier: 'ON_THE_WAY' | 'ACCEPTABLE' | 'ANOTHER_DAY' | 'NOT_FEASIBLE'
  delta_route_minutes: number | null
  evidence_freshness: EvidenceFreshness
  hard_gate_passed: boolean
  explanation: string
}

export interface WorkspaceCandidatesResponse {
  workspace_id: string
  revision: number
  day: number
  candidates: CandidateSuggestion[]
  route_context_status: string
}

export type SuggestionIntent = 'NEARBY' | 'POPULAR' | 'FUN' | 'FOOD'
export type SuggestionClassification =
  | 'ON_ROUTE'
  | 'ACCEPTABLE_DETOUR'
  | 'DEFER_TO_OTHER_DAY'
  | 'INFEASIBLE'
export type SuggestionFreshnessStatus = 'FRESH' | 'STALE' | 'UNKNOWN'

export interface SuggestionRouteDelta {
  status: 'AVAILABLE' | 'UNAVAILABLE' | 'UNKNOWN'
  delta_route_minutes: number | null
  previous_to_candidate_minutes: number | null
  candidate_to_next_minutes: number | null
  previous_to_next_minutes: number | null
  reason_code: string | null
}

export interface SuggestionEvidenceFreshness {
  status: SuggestionFreshnessStatus
  observed_at: string | null
  max_age_seconds: number | null
  reason_code: string | null
}

export interface FrozenSuggestionPlace {
  place_id: string
  name: string
  city: string
  district: string | null
  address: string | null
  category: string
  coords: { lng: number; lat: number }
}

/**
 * Frozen server candidate.  `canonical_place` and provider receipts are
 * display-only: the accept API is deliberately identified only by the set and
 * candidate IDs, so none of these facts can become client-side authority.
 */
export interface SuggestionCandidateV1 {
  suggestion_set_id: string
  candidate_id: string
  canonical_place: FrozenSuggestionPlace
  provider_receipt_id: string
  rank_position: number
  classification: SuggestionClassification
  source_prior_refs: string[]
  score_components: Record<string, number>
  total_score: number
  hard_gate: { passed: boolean; reason_codes: string[] }
  route_delta: SuggestionRouteDelta
  evidence_freshness: SuggestionEvidenceFreshness
  explanation_codes: string[]
}

export interface SuggestionSetV1 {
  suggestion_set_id: string
  workspace_id: string
  base_revision: number
  day_index: number
  insert_after_stop_id: string | null
  insert_before_stop_id: string | null
  intents: SuggestionIntent[]
  context_hash: string
  policy_version: string
  provider_snapshot_id: string
  expires_at: string
  session_id: string
  candidates: SuggestionCandidateV1[]
  created_by: string
  created_at: string
}

export interface CreateSuggestionSetRequest {
  base_revision: number
  day_index: number
  insert_after_stop_id: string | null
  insert_before_stop_id: string | null
  intents: SuggestionIntent[]
  session_id: string
}

export interface AcceptSuggestionResult {
  accepted: boolean
  suggestion_set_id: string
  candidate_id: string
  new_revision: number
  stop_id: string
  revision: ItineraryRevision
  idempotent_replay: boolean
}

export interface RecommendationEventCommandResult {
  event: {
    event_id: string
    event_type: 'candidate_previewed' | 'candidate_dismissed' | 'line_completed'
  }
  idempotent_replay: boolean
}

/**
 * A route skeleton is a planning aid, not a published recommendation.  In
 * particular MODEL_GENERATED/DRAFT must remain visible to callers instead of
 * being collapsed into an optimistic "recommended" badge.
 */
export interface CityRouteTemplate {
  template_id: string
  city: string
  name: string
  template_version: number
  suitable_days: number[]
  suitable_groups: string[]
  budget_level: string
  intensity: string
  route_zones: Array<{
    zone_id: string
    district: string
    preferred_transport: string
  }>
  anchor_slots: Array<{
    slot_id: string
    day_offset: number
    time_window: string
    zone_id: string
    slot_type: string
    optional: boolean
  }>
  status: 'DRAFT' | 'REVIEWED' | 'RETIRED'
  provenance: 'MODEL_GENERATED' | 'HUMAN_CURATED'
  last_verified_at: string | null
}

export interface TemplateApplyResponse {
  workspace_id: string
  template_id: string
  template_version: number
  revision: ItineraryRevision
  workspace: TripWorkspace
  template_provenance: string
  human_review_evidence: boolean
}

export interface HotelAreaScore {
  area_id: string
  score_minutes: number | null
  all_days_covered: boolean
  evidence_freshness: EvidenceFreshness
  explanation_codes: string[]
}

export interface WorkspaceHotelAreasResponse {
  workspace_id: string
  revision: number
  areas: HotelAreaScore[]
  route_context_status: string
}

export interface AuditFinding {
  finding_id: string
  rule_id: string
  rule_version: string
  status: AuditStatus
  severity: AuditSeverity
  reason_code: string
  message: string
  input_values: Record<string, unknown>
  affected_days: number[]
  affected_stop_ids: string[]
  affected_member_ids: string[]
  evidence_fact_ids: string[]
  repairable: boolean
  confirmation_action: string | null
}

export interface AuditReport {
  report_id: string
  workspace_id: string
  itinerary_revision: number
  evidence_snapshot_id: string
  overall_status: AuditStatus
  findings: AuditFinding[]
  created_at: string
}

export interface EvidenceFact {
  fact_id: string
  subject_type: string
  subject_id: string
  fact_type: string
  value: unknown
  provider: string
  source_url: string | null
  observed_at: string
  valid_until: string | null
  confidence: number
  freshness_status: EvidenceFreshness
}

export interface EvidenceSnapshot {
  snapshot_id: string
  itinerary_revision: number
  provider_set: string[]
  facts: EvidenceFact[]
  provider_failures: Array<{ provider: string; error_category: string; retryable: boolean; detail: string | null }>
}

export interface EvidenceFactChange {
  change_type: 'ADDED' | 'REMOVED' | 'VALUE_CHANGED' | 'FRESHNESS_CHANGED' | 'VALIDITY_CHANGED' | 'PROVIDER_CHANGED'
  subject_type: string
  subject_id: string
  fact_type: string
  provider: string
  before: EvidenceFact | null
  after: EvidenceFact | null
  reason: string
}

export interface AuditFindingChange {
  change_type: 'ADDED' | 'RESOLVED' | 'CHANGED'
  rule_id: string
  reason_code: string
  affected_days: number[]
  affected_stop_ids: string[]
  before: AuditFinding | null
  after: AuditFinding | null
  reason: string
}

export interface PreTripRecheckResult {
  source_report_id: string
  source_snapshot_id: string
  report: AuditReport
  evidence_snapshot: EvidenceSnapshot
  evidence_changes: EvidenceFactChange[]
  finding_changes: AuditFindingChange[]
  provider_failure_changes: Array<{
    change_type: 'ADDED' | 'RESOLVED'
    failure: { provider: string; error_category: string; retryable: boolean; detail: string | null }
    reason: string
  }>
  provider_failures: Array<{ provider: string; error_category: string; retryable: boolean; detail: string | null }>
  provider_receipts: Array<{
    provider: string
    provider_call_attempted: boolean
    execution_mode: string
    status: string
    subject_id: string | null
    query: string | null
    response_hash: string | null
    observed_at: string
    result_count: number | null
    detail: string | null
  }>
  degraded: boolean
  recheck_window_state: 'EARLY' | 'RECOMMENDED_24_48H' | 'LATE'
  trip_start_reference_at: string
  hours_until_trip_start: number
  recheck_window_reason: string
}

export interface RepairOperation {
  operation: string
  payload: Record<string, unknown>
  rationale: string
}

export interface RepairOption {
  repair_id: string
  source_report_id: string
  base_itinerary_revision: number
  operations: RepairOperation[]
  targeted_finding_ids: string[]
  edit_cost: number
  risk_cost: number
  route_cost_delta: number | null
  new_unknown_count: number
  tradeoffs: string[]
  affected_member_ids: string[]
  result_preview: ItineraryRevision
  postcheck_report_id: string
  status: 'PROPOSED' | 'APPLIED' | 'REJECTED' | 'STALE'
  decision_reason: string | null
}

export interface RepairApplyResult {
  repair: RepairOption
  new_revision: number
  postcheck_report_id: string
  idempotent_replay: boolean
}

export interface FinalTipsArtifact {
  report_id: string
  workspace_id: string
  itinerary_revision: number
  basis_content_hash: string
  artifact_hash: string
  itinerary: {
    version: number
    days: Array<{
      day_index: number
      slots: Array<{
        place_id: string
        place: { name?: string }
        tips: string[]
      }>
    }>
  }
  created_at: string
}

export type TipsState = 'NOT_APPLICABLE' | 'INELIGIBLE' | 'NOT_GENERATED' | 'READY'

export type TripBriefStatus = 'DRAFT' | 'NEEDS_CONFIRMATION' | 'CONFIRMED'

export interface BriefFieldProvenance {
  source_spans: Array<{ source_id: string; start: number; end: number }>
  confidence: number
  origin: 'USER_TEXT' | 'PARSER' | 'USER_CONFIRMED' | 'INFERRED' | 'DEFAULT_NO_PREFERENCE'
  confirmation: 'UNCONFIRMED' | 'CONFIRMED'
  hardness: 'HARD' | 'SOFT' | 'NO_PREFERENCE'
}

export interface TripBriefRevision {
  brief_id: string
  workspace_id: string
  revision: number
  parent_revision: number | null
  content_hash: string
  city: string
  date_range: { start: string; end: string }
  traveler_count: number
  arrival: { location: string | null; at: string | null; notes: string | null }
  departure: { location: string | null; at: string | null; notes: string | null }
  accommodation: { hotel_name: string | null; area: string | null }
  transport_modes: Array<'WALKING' | 'TRANSIT' | 'BICYCLING' | 'DRIVING'>
  transport_restrictions: string[] | string
  budget: Record<string, unknown> | string
  dining_style: string[] | string
  lodging_style: string[] | string
  dietary_restrictions: string[] | string
  daily_pace: string
  activity_intensity: string
  field_provenance: Record<string, BriefFieldProvenance>
  status: TripBriefStatus
  confirmed_by: string | null
  confirmed_at: string | null
}

export interface RunSpec {
  schema_version: 'trip-check-run-spec-v1'
  commit_sha: string
  prompt_version: string
  model_version: string
  provider_version: string
  rule_set_version: string
  execution_mode: string
  dataset_hash: string
  snapshot_hash: string
  fault_profile: string
  random_seed: number
  budget: {
    max_tokens: number
    max_provider_queries: number
    max_retries: number
    timeout_seconds: number
    max_cost_usd: number
  }
}

export type TripCheckStage =
  | 'PARSE'
  | 'WAIT_BRIEF_CONFIRMATION'
  | 'RESOLVE_PLACES'
  | 'COLLECT_EVIDENCE'
  | 'AUDIT'
  | 'BUILD_ADVICE'
  | 'WAIT_ADOPTION'
  | 'POSTCHECK'

export type TripCheckRunStatus =
  | 'WAITING'
  | 'RUNNING'
  | 'PARTIAL'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'PRIVACY_BLOCKED'
  | 'CANCELLED'

export interface TripCheckRun {
  run_id: string
  workspace_id: string
  itinerary_revision: number
  brief_id: string
  brief_revision: number
  stage: TripCheckStage
  stage_attempt: number
  lease_owner: string | null
  lease_until: string | null
  run_spec: RunSpec
  config_hash: string
  completed_stages: TripCheckStage[]
  partial_failures: Array<{
    stage: TripCheckStage
    category: string
    affected_fields: string[]
    retryable: boolean
  }>
  status: TripCheckRunStatus
  evidence_snapshot_id: string | null
  report_id: string | null
  advice_bundle_id: string | null
  version: number
  created_at: string
  updated_at: string
}

export interface AdviceAction {
  advice_id: string
  finding_id: string
  action: string
  expected_impact: string
  uncertainty: string
  candidate_set_id: string | null
  evidence_fact_ids: string[]
  provider_receipt_ids: string[]
  route_delta: Record<string, unknown> | null
  repair_id: string | null
  tradeoffs: string[]
}

export interface AdviceBundle {
  advice_bundle_id: string
  workspace_id: string
  run_id: string
  report_id: string
  itinerary_revision: number
  brief_revision: number
  evidence_snapshot_id: string
  actions: AdviceAction[]
  created_at: string
}

export interface TripCheckRunEvent {
  event_id: number
  run_id: string
  event_type: string
  stage: TripCheckStage
  run_version: number
  payload: Record<string, unknown>
  created_at: string
}

export interface WorkspaceResume {
  schema_version: '1.0'
  workspace: TripWorkspace
  current_revision: ItineraryRevision | null
  current_import: ItineraryImport | null
  current_brief: TripBriefRevision | null
  current_trip_check_run: TripCheckRun | null
  current_advice: AdviceBundle | null
  current_report: AuditReport | null
  current_evidence: EvidenceSnapshot | null
  proposed_repairs: RepairOption[]
  applied_repair: RepairOption | null
  current_tips: FinalTipsArtifact | null
  tips_state: TipsState
  write_etags: {
    itinerary: string | null
    import: string | null
  }
}

// P7 member state is fetched from the service.  It is intentionally separate
// from Yjs awareness: awareness may show who is online, but it cannot prove a
// constraint, acknowledgement, or share capability was persisted.
export type MemberConstraintHardness = 'HARD' | 'SOFT'
export type MemberConstraintSource = 'MEMBER_EXPLICIT' | 'ORGANIZER' | 'ROOM_CONSENSUS' | 'MEMORY' | 'INFERRED'
export type MemberConstraintConfirmation = 'PENDING' | 'CONFIRMED' | 'REJECTED'

export interface MemberConstraintView {
  constraint_id: string
  owner_member_id: string
  type: string
  operator: string
  value: unknown
  hardness: MemberConstraintHardness
  priority: number
  source: MemberConstraintSource
  confirmation_status: MemberConstraintConfirmation
  waivable_by: string[]
  workspace_id: string
  revision: number
}

export interface TravelerProfileView {
  workspace_id: string
  member_id: string
  display_name: string
  age_group: string
  child_age: number | null
  child_height_cm: number | null
  walking_limit_minutes: number | null
  requires_nap: boolean
  wheelchair_or_stroller: boolean
  dietary_restrictions: string[]
  medication_times: string[]
  latest_return_time: string | null
  confirmed_revision: number | null
}

export interface WorkspaceMemberView {
  member_id: string
  profile: TravelerProfileView | null
  constraints: MemberConstraintView[]
  confirmed_itinerary_revision: number | null
}

export interface MemberConstraintWriteResult {
  constraint: MemberConstraintView
  previous_workspace_revision: number
  current_workspace_revision: number
  stale_report_id: string | null
}

export type ShareScope = 'REPORT_READ' | 'CONSTRAINT_WRITE' | 'ACKNOWLEDGE'

export interface ShareLinkView {
  share_link_id: string
  workspace_id: string
  itinerary_revision: number
  report_id: string | null
  scopes: ShareScope[]
  recipient_member_id: string | null
  created_by: string
  expires_at: string
  revoked_at: string | null
  created_at: string
}

export interface IssuedShareLink {
  link: ShareLinkView
  token: string
}

// The recipient projection is deliberately redacted.  It is not a workspace
// resume response and never contains a room ID, workspace ID, owner, or edit
// capability.
export interface SharedItineraryStop {
  stop_id: string
  place_id: string
  day_index: number
  order_index: number
  start_time: string | null
  end_time: string | null
  visit_duration_minutes: number | null
  raw_name: string | null
  fixed_commitment: boolean
  locked: boolean
  category: string
  notes: string
}

export interface SharedItineraryView {
  revision: number
  content_hash: string
  city: string
  trip_start_date: string
  trip_end_date: string
  days: Array<{ day_index: number; date: string | null; stops: SharedItineraryStop[] }>
}

export interface SharedAuditFindingView {
  finding_id: string
  rule_id: string
  status: AuditStatus
  severity: AuditSeverity
  reason_code: string
  message: string
  affected_days: number[]
  affected_stop_ids: string[]
  repairable: boolean
  confirmation_action: string | null
}

export interface SharedAuditReportView {
  report_id: string
  itinerary_revision: number
  audit_rule_set_version: string
  overall_status: AuditStatus
  created_at: string
  findings: SharedAuditFindingView[]
}

export interface SharedWorkspaceView {
  itinerary: SharedItineraryView
  report: SharedAuditReportView | null
  scopes: ShareScope[]
  recipient_bound: boolean
  acknowledgement: {
    required: boolean
    acknowledged: boolean
    acknowledged_at: string | null
  }
  constraint_write_context: { expected_base_revision: number } | null
}
