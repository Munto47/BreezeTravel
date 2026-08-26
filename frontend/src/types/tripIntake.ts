import type { ItineraryImport, TripBriefRevision, TripWorkspace } from './workspace'

export type IntakeStatus = 'NEEDS_CONFIRMATION' | 'READY' | 'EXTRACTION_FAILED'
export type LocationStatus = 'EXACT' | 'MULTIPLE' | 'UNCERTAIN' | 'MISSING'
export type QuantityQuantifier = 'EXACT' | 'RANGE' | 'APPROXIMATE' | 'AT_LEAST' | 'AT_MOST' | 'UNKNOWN'

export interface EvidenceSpan {
  source_id: string
  start: number
  end: number
  quote: string
}

export interface IntakeSource {
  source_id: string
  source_type: 'AI_TEXT' | 'MANUAL_TEXT' | 'SCREENSHOT_OCR'
  text: string
  text_sha256: string
  metadata: Record<string, unknown>
}

export interface LocationMention {
  mention_id: string
  raw_text: string
  normalized_name: string | null
  country_code: string | null
  entity_type: string
  role: string
  confidence: number
  evidence: EvidenceSpan[]
}

export interface QuantifiedValue {
  min: number | null
  max: number | null
  quantifier: QuantityQuantifier
  derivation: string
  evidence: EvidenceSpan[]
}

export interface TripIntakeExtraction {
  locations: {
    mentions: LocationMention[]
    primary_mention_id: string | null
    status: LocationStatus
  }
  party_size: { total: QuantifiedValue }
  temporal: {
    days: QuantifiedValue
    nights: QuantifiedValue
    date_range: {
      raw_text: string
      start: { year: number | null; month: number; day: number }
      end: { year: number | null; month: number; day: number }
      evidence: EvidenceSpan[]
    } | null
  }
  preferences: {
    status: 'UNSPECIFIED' | 'SPECIFIED' | 'NO_PREFERENCE'
    items: Array<{
      item_id: string
      category: string
      label: string
      polarity: string
      evidence: EvidenceSpan[]
    }>
    pace: { value: string; evidence: EvidenceSpan[] }
  }
  issues: Array<{
    code: string
    field_path: string
    message: string
    blocking: boolean
    evidence: EvidenceSpan[]
  }>
  readiness: 'NEEDS_CONFIRMATION' | 'READY'
}

export interface TripIntakeRevision {
  intake_id: string
  room_id: string
  revision: number
  parent_revision: number | null
  content_hash: string
  source_type: IntakeSource['source_type']
  raw_text: string
  raw_text_sha256: string
  sources: IntakeSource[]
  extraction: TripIntakeExtraction
  confirmed_fields: string[]
  status: IntakeStatus
  created_at: string
  confirmed_by: string | null
  confirmed_at: string | null
}

export interface IntakeMaterializationResult {
  materialization: {
    materialization_id: string
    intake_id: string
    intake_revision: number
    workspace: TripWorkspace
    brief: TripBriefRevision
    itinerary_import: ItineraryImport
    created_at: string
  }
  idempotent_replay: boolean
  resolution_dispatch: 'NOT_CONFIGURED' | 'SUCCEEDED' | 'FAILED_RETRYABLE'
}

/** Backend offsets are Unicode code points; JS String.slice uses UTF-16 code units. */
export function codePointSlice(source: string, start: number, end: number): string {
  return Array.from(source).slice(start, end).join('')
}

export function evidenceMatchesSource(span: EvidenceSpan, source: IntakeSource): boolean {
  return codePointSlice(source.text, span.start, span.end) === span.quote
}
