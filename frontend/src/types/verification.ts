export type ConstraintStatus = 'SATISFIED' | 'VIOLATED' | 'UNKNOWN'

export interface ConstraintCheck {
  constraint_id: string
  status: ConstraintStatus
  reason_code: string
  message: string
  day_index?: number
  place_id?: string
  evidence_refs: string[]
  observed_at: string
  repairable: boolean
}

export interface VerificationReport {
  report_id: string
  task_id: string
  task_revision: number
  itinerary_id: string
  itinerary_version: number
  planning_input_hash: string
  overall_status: ConstraintStatus
  checks: ConstraintCheck[]
  verified_at: string
  repair_rounds: number
  unresolved_reasons: string[]
}
