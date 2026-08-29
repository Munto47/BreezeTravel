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

export interface UserFacingTripResult {
  status: 'READY' | 'PARTIAL_RESULT' | 'BASIC_ONLY' | 'LIMITED'
  assumptions: AssumptionChipView[]
  days: Array<{ label: string; activities: ActivityCardView[] }>
  map: {
    status: 'PREPARING' | 'AVAILABLE' | 'NEEDS_UPDATE' | 'LIMITED' | 'UNAVAILABLE'
    message: string
    available_actions: Array<'VIEW_MAP' | 'RENDER_MAP'>
  }
  stay: {
    status: 'AVAILABLE' | 'LIMITED' | 'UNAVAILABLE'
    message: string
    candidates: unknown[]
    available_actions: Array<'CHOOSE_STAY'>
  }
  available_actions: Array<'EDIT_ASSUMPTIONS' | 'EDIT_CARDS'>
}

export type TripUnderstandingCommand =
  | {
      command_type: 'ACTIVITY_INSERT'
      day_index: number
      position: number
      name: string
      category?: string
      area_or_address?: string
      time_hint?: string | null
    }
  | { command_type: 'ACTIVITY_DELETE'; activity_token: string }
  | {
      command_type: 'ACTIVITY_MOVE'
      activity_token: string
      target_day_index: number
      target_position: number
    }
  | {
      command_type: 'ACTIVITY_TEXT_EDIT'
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
  map_readiness: 'NEEDS_UPDATE'
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

function requestKey(): string {
  const values = new Uint32Array(4)
  crypto.getRandomValues(values)
  return Array.from(values, (value) => value.toString(16).padStart(8, '0')).join('')
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

export function clearTripUnderstandingSession(): void {
  if (typeof window === 'undefined') return
  for (const key of [
    'bt_active_trip_ref',
    'bt_active_trip_mode',
    'bt_active_trip_event_cursor',
    'bt_active_trip_etag',
    'bt_active_trip_source_deleted',
  ]) sessionStorage.removeItem(key)
  for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
    const key = sessionStorage.key(index)
    if (key?.startsWith('bt_v3_operation_')) sessionStorage.removeItem(key)
  }
}

export async function createDemoTripUnderstanding(): Promise<TripUnderstandingAcceptedView> {
  const response = await fetch('/api/v3/trip-understandings', {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': requestKey(),
    },
    body: JSON.stringify({ mode: 'DEMO' }),
  })
  if (!response.ok) throw new Error('DEMO_CREATE_FAILED')
  return response.json() as Promise<TripUnderstandingAcceptedView>
}

export async function createFullTripUnderstanding(text: string): Promise<TripUnderstandingAcceptedView> {
  const response = await fetch('/api/v3/trip-understandings', {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': requestKey(),
      ...authorizationHeaders(),
    },
    body: JSON.stringify({ mode: 'FULL', source: { type: 'TEXT', text } }),
  })
  if (!response.ok) {
    if (response.status === 401) throw new Error('LOGIN_REQUIRED')
    if (response.status === 429) throw new Error('ACTIVE_LIMIT_REACHED')
    throw new Error('FULL_CREATE_FAILED')
  }
  return response.json() as Promise<TripUnderstandingAcceptedView>
}

export async function readTripUnderstandingResult(publicResourceId: string): Promise<{
  status: number
  body: TripUnderstandingProgressView | UserFacingTripResult
  etag: string | null
}> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/result`,
    {
      credentials: 'include',
      cache: 'no-store',
      headers: authorizationHeaders(),
    },
  )
  if (!response.ok) throw new Error('TRIP_RESULT_UNAVAILABLE')
  return {
    status: response.status,
    body: (await response.json()) as TripUnderstandingProgressView | UserFacingTripResult,
    etag: response.headers.get('etag'),
  }
}

export async function applyTripUnderstandingCommand(
  publicResourceId: string,
  etag: string,
  command: TripUnderstandingCommand,
): Promise<{ body: CommandAppliedView; etag: string }> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/commands`,
    {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': requestKey(),
        'If-Match': etag,
        ...authorizationHeaders(),
      },
      body: JSON.stringify(command),
    },
  )
  if (!response.ok) {
    if (response.status === 409) throw new Error('REVISION_CONFLICT')
    if (response.status === 428) throw new Error('IF_MATCH_REQUIRED')
    throw new Error('COMMAND_FAILED')
  }
  const nextEtag = response.headers.get('etag')
  if (!nextEtag) throw new Error('COMMAND_ETAG_MISSING')
  return {
    body: (await response.json()) as CommandAppliedView,
    etag: nextEtag,
  }
}

export async function claimTripUnderstanding(
  publicResourceId: string,
): Promise<{ body: ClaimedTripView; etag: string }> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/claim`,
    {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Idempotency-Key': operationRequestKey(`claim:${publicResourceId}`),
        ...authorizationHeaders(),
      },
    },
  )
  if (!response.ok) {
    if (response.status === 401) throw new Error('LOGIN_REQUIRED')
    if (response.status === 410) throw new Error('TRIP_ALREADY_GONE')
    throw new Error('CLAIM_FAILED')
  }
  const etag = response.headers.get('etag')
  if (!etag) throw new Error('CLAIM_ETAG_MISSING')
  return { body: (await response.json()) as ClaimedTripView, etag }
}

export async function deleteTripUnderstandingSource(publicResourceId: string): Promise<void> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/source`,
    {
      method: 'DELETE',
      credentials: 'include',
      headers: {
        'Idempotency-Key': operationRequestKey(`delete-source:${publicResourceId}`),
        ...authorizationHeaders(),
      },
    },
  )
  if (!response.ok) {
    if (response.status === 401) throw new Error('LOGIN_REQUIRED')
    throw new Error('SOURCE_DELETE_FAILED')
  }
}

export async function deleteTripUnderstanding(publicResourceId: string): Promise<void> {
  const response = await fetch(
    `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}`,
    {
      method: 'DELETE',
      credentials: 'include',
      headers: {
        'Idempotency-Key': operationRequestKey(`delete-trip:${publicResourceId}`),
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
      headers: authorizationHeaders(),
    },
  )
  if (readback.status !== 410) throw new Error('TRIP_DELETE_READBACK_FAILED')
}

export async function deleteAllTravelData(): Promise<TravelDataDeletionStatusView> {
  const response = await fetch('/api/v3/me/travel-data', {
    method: 'DELETE',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': operationRequestKey('delete-account-travel-data'),
      ...authorizationHeaders(),
    },
    body: JSON.stringify({ confirmation: 'DELETE_ALL_TRAVEL_DATA' }),
  })
  if (!response.ok) {
    if (response.status === 401) throw new Error('RECENT_LOGIN_REQUIRED')
    throw new Error('ACCOUNT_TRAVEL_DELETE_FAILED')
  }
  return response.json() as Promise<TravelDataDeletionStatusView>
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

export function tripUnderstandingEventsUrl(publicResourceId: string): string {
  return `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/events`
}

export interface TripUnderstandingPublicEvent {
  id: number
  type: 'progress' | 'result_available'
  message: string
}

export async function streamTripUnderstandingEvents(
  publicResourceId: string,
  onEvent: (event: TripUnderstandingPublicEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const cursor = typeof window === 'undefined' ? null : sessionStorage.getItem('bt_active_trip_event_cursor')
  const response = await fetch(tripUnderstandingEventsUrl(publicResourceId), {
    credentials: 'include',
    cache: 'no-store',
    headers: {
      Accept: 'text/event-stream',
      ...(cursor ? { 'Last-Event-ID': cursor } : {}),
      ...authorizationHeaders(),
    },
    signal,
  })
  if (!response.ok || !response.body) throw new Error('TRIP_EVENTS_UNAVAILABLE')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (!signal.aborted) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const fields = Object.fromEntries(
        block
          .split('\n')
          .filter((line) => line.includes(':') && !line.startsWith(':'))
          .map((line) => {
            const separator = line.indexOf(':')
            return [line.slice(0, separator), line.slice(separator + 1).trimStart()]
          }),
      )
      if (fields.id && fields.event && fields.data) {
        const id = Number(fields.id)
        const payload = JSON.parse(fields.data) as { message?: string }
        if (
          Number.isSafeInteger(id)
          && id > 0
          && (fields.event === 'progress' || fields.event === 'result_available')
          && typeof payload.message === 'string'
        ) {
          sessionStorage.setItem('bt_active_trip_event_cursor', String(id))
          onEvent({ id, type: fields.event, message: payload.message })
        }
      }
      boundary = buffer.indexOf('\n\n')
    }
    if (done) return
  }
}
