/**
 * 统一 API 客户端，自动附加 Authorization header，401 时跳转登录页。
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL || ''

export interface ApiErrorContext {
  code?: string
  message?: string
  expected_revision?: number | null
  actual_revision?: number | null
  current_revision?: number | null
  reason?: string
  report_id?: string
  report_revision?: number | null
  [key: string]: unknown
}

/** Preserve structured FastAPI domain errors for explicit recovery UI. */
export class ApiRequestError extends Error {
  readonly status: number
  readonly code?: string
  readonly context: ApiErrorContext

  constructor(message: string, options: { status: number; context?: ApiErrorContext }) {
    super(message)
    this.name = 'ApiRequestError'
    this.status = options.status
    this.context = options.context ?? {}
    this.code = this.context.code
  }
}

function getToken(): string | null {
  if (typeof window === 'undefined') return null
  return localStorage.getItem('authToken')
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init.headers as Record<string, string>),
  }
  if (token) headers['Authorization'] = `Bearer ${token}`

  const res = await fetch(`${API_BASE}${path}`, { ...init, headers })

  if (res.status === 401) {
    if (typeof window !== 'undefined') window.location.href = '/login'
    throw new Error('Unauthorized')
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({})) as {
      detail?: string | ApiErrorContext
    }
    const detail = body.detail
    const message = typeof detail === 'string'
      ? detail
      : detail?.message || detail?.code || `HTTP ${res.status}`
    throw new ApiRequestError(message, {
      status: res.status,
      context: typeof detail === 'object' && detail !== null ? detail : {},
    })
  }

  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined }),
  postWithHeaders: <T>(path: string, body: unknown, headers: Record<string, string>) =>
    request<T>(path, { method: 'POST', headers, body: JSON.stringify(body) }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PUT', body: body !== undefined ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body: body !== undefined ? JSON.stringify(body) : undefined }),
  patchWithHeaders: <T>(path: string, body: unknown, headers: Record<string, string>) =>
    request<T>(path, { method: 'PATCH', headers, body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),

  /** 下载文件（带 Auth header） */
  download: async (path: string, filename: string) => {
    const token = getToken()
    const res = await fetch(`${API_BASE}${path}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  },
}
