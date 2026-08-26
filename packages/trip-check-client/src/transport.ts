import { toApiError, type ApiErrorPayload } from './errors'

export type HttpMethod = 'GET' | 'POST' | 'PATCH' | 'DELETE'

export interface TransportRequest {
  method: HttpMethod
  path: string
  body?: unknown
  headers?: Record<string, string>
}

export interface TransportResponse<T> {
  status: number
  data: T
  headers: Record<string, string>
}

export interface JsonTransport {
  request<T>(request: TransportRequest): Promise<TransportResponse<T>>
}

export interface UploadRequest {
  path: string
  filePath: string
  fieldName: string
  headers?: Record<string, string>
}

export interface UploadTransport {
  upload<T>(request: UploadRequest): Promise<TransportResponse<T>>
}

export function ensureSuccess<T>(response: TransportResponse<T>): TransportResponse<T> {
  if (response.status >= 200 && response.status < 300) return response
  throw toApiError(response.status, response.data as ApiErrorPayload)
}
