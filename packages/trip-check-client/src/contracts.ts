export type TripCheckRunStatus =
  | 'WAITING'
  | 'RUNNING'
  | 'PARTIAL'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'PRIVACY_BLOCKED'
  | 'CANCELLED'

export type TripCheckStage =
  | 'PARSE'
  | 'WAIT_BRIEF_CONFIRMATION'
  | 'RESOLVE_PLACES'
  | 'COLLECT_EVIDENCE'
  | 'AUDIT'
  | 'BUILD_ADVICE'
  | 'WAIT_ADOPTION'
  | 'POSTCHECK'

export interface AuthSession {
  token: string
  user_id: string
  nickname: string
  is_new_user: boolean
}

export interface TripWorkspace {
  workspace_id: string
  room_id: string
  city: string
  trip_date_range: { start: string; end: string }
  current_itinerary_revision: number | null
  current_import_id?: string | null
  current_trip_brief_revision?: number | null
  current_trip_check_run_id?: string | null
}

export interface ItineraryImport {
  import_id: string
  workspace_id: string
  source_type: 'AI_TEXT' | 'MANUAL_TEXT' | 'SCREENSHOT_OCR'
  raw_text: string
  status: string
  state_version: number
  resolutions: Array<{
    raw_stop_id: string
    raw_name: string
    resolution_status: string
    candidates: Array<{ place_id: string; name: string; address?: string | null }>
  }>
}

export interface TripBriefRevision {
  brief_id: string
  workspace_id: string
  revision: number
  status: 'DRAFT' | 'CONFIRMED'
  city: string
  traveler_count: number
  date_range: { start: string; end: string }
  transport_modes: string[]
  daily_pace: string
  activity_intensity: string
  arrival: { location?: string | null }
  departure: { location?: string | null }
  accommodation: { hotel_name?: string | null; area?: string | null }
  field_provenance: Record<string, { origin: string; confirmation: string }>
}

export interface ItineraryRevision {
  workspace_id: string
  itinerary_id: string
  revision: number
  content_hash: string
  days: Array<{ day_index: number; stops: Array<Record<string, unknown>> }>
}

export interface TripCheckRun {
  run_id: string
  workspace_id: string
  itinerary_revision: number
  brief_revision: number
  status: TripCheckRunStatus
  stage: TripCheckStage
  version: number
  config_hash: string
  completed_stages: TripCheckStage[]
  partial_failures: Array<{ stage: string; category: string }>
  report_id?: string | null
  evidence_snapshot_id?: string | null
  advice_bundle_id?: string | null
  lease_until?: string | null
}

export interface AuditReport {
  report_id: string
  workspace_id: string
  itinerary_revision: number
  overall_status: string
  findings: Array<{
    finding_id: string
    severity: string
    category: string
    message: string
    status?: string
  }>
}

export interface AdviceBundle {
  advice_bundle_id: string
  workspace_id: string
  run_id: string
  report_id: string
  actions: Array<{
    advice_id: string
    finding_id: string
    action: string
    expected_impact: string
    uncertainty: string
  }>
}

export interface RepairOption {
  repair_id: string
  source_report_id: string
  base_itinerary_revision: number
  status: string
  tradeoffs: string[]
  result_preview: Record<string, unknown>
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
  current_evidence: Record<string, unknown> | null
  proposed_repairs: RepairOption[]
}

export interface ScreenshotUploadBatch {
  batch_id: string
  workspace_id: string
  expected_count: number
  uploaded_positions: number[]
  status: 'PENDING' | 'PROCESSING' | 'SUCCEEDED' | 'FAILED' | 'PRIVACY_BLOCKED' | 'CANCELLED' | 'EXPIRED'
  version: number
  expires_at: string
}
