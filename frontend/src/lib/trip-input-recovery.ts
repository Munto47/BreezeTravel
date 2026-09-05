import { createTripRequestKey } from './trip-understanding-v3'

export const TRIP_INPUT_DRAFT_KEY = 'bt_input_draft'

export type TripInputDraft = {
  text: string
  demo: boolean
  key: string
  expires: number
  resource?: string
  failedResource?: string
}

// Call only after the server explicitly reports UNDERSTANDING_FAILED. An
// interrupted request or an unfinished result must keep its original key.
export function releaseFailedTripInput(
  reference: string,
): TripInputDraft | null {
  if (typeof window === 'undefined') return null
  try {
    const draft = JSON.parse(
      sessionStorage.getItem(TRIP_INPUT_DRAFT_KEY) || 'null',
    ) as TripInputDraft | null
    if (
      !draft ||
      typeof draft.text !== 'string' ||
      typeof draft.key !== 'string' ||
      !Number.isFinite(draft.expires) ||
      draft.expires <= Date.now()
    )
      return null
    if (!draft.resource && draft.failedResource === reference) return draft
    if (draft.resource !== reference) return null
    const recovered: TripInputDraft = {
      ...draft,
      key: createTripRequestKey(),
      resource: undefined,
      failedResource: reference,
    }
    sessionStorage.setItem(TRIP_INPUT_DRAFT_KEY, JSON.stringify(recovered))
    return recovered
  } catch {
    // Do not alter a different or unreadable recovery record.
    return null
  }
}
