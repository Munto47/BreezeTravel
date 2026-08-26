export type ApiErrorContext = Record<string, unknown>

export class TripCheckApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly context: ApiErrorContext = {},
  ) {
    super(message)
    this.name = 'TripCheckApiError'
  }
}

export interface ApiErrorPayload {
  detail?: string | {
    code?: string
    message?: string
    context?: ApiErrorContext
  }
}

export function toApiError(status: number, payload: ApiErrorPayload | null): TripCheckApiError {
  const detail = payload?.detail
  if (typeof detail === 'string') return new TripCheckApiError(status, `HTTP_${status}`, detail)
  return new TripCheckApiError(
    status,
    detail?.code || `HTTP_${status}`,
    detail?.message || `请求失败（${status}）`,
    detail?.context || {},
  )
}
