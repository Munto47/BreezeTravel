export type ConstraintSource = 'user_explicit' | 'room_consensus' | 'memory' | 'inferred'

export interface NamedRequirement {
  kind: 'place' | 'activity' | 'category' | 'area'
  value: string
  source: ConstraintSource
}

export interface HardConstraint {
  id: string
  type: string
  operator: 'eq' | 'neq' | 'lt' | 'lte' | 'gt' | 'gte' | 'in' | 'not_in'
  value: unknown
  unit?: string
  scope?: string
  source: ConstraintSource
}

export interface TripTaskSpec {
  schema_version: '1.0'
  task_id: string
  room_id: string
  task_revision: number
  city: string
  date_range: { start?: string; days: number }
  travelers: { adults: number; children: number; seniors: number }
  budget?: {
    amount: number
    currency: string
    scope: 'total' | 'per_person' | 'per_day' | 'per_person_per_day'
    include_transport: boolean
    include_hotel: boolean
  }
  must_include: NamedRequirement[]
  exclude: NamedRequirement[]
  hard_constraints: HardConstraint[]
  soft_preferences: Array<{ id: string; type: string; value: unknown; weight: number; source: ConstraintSource }>
  assumptions: string[]
  missing_fields: string[]
  conflicts: string[]
}
