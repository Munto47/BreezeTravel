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

export interface ServerSentEvent<T> {
  id: string | null
  event: string
  data: T
}

function getToken(): string | null {
  if (typeof window === 'undefined') return null
  return localStorage.getItem('authToken')
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) }
  const isFormData = typeof FormData !== 'undefined' && init.body instanceof FormData
  if (!isFormData) headers['Content-Type'] = 'application/json'
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

async function streamEvents<T>(
  path: string,
  options: {
    lastEventId?: string | null
    signal?: AbortSignal
    onEvent: (event: ServerSentEvent<T>) => void
  },
): Promise<void> {
  const token = getToken()
  const headers: Record<string, string> = { Accept: 'text/event-stream' }
  if (token) headers.Authorization = `Bearer ${token}`
  if (options.lastEventId) headers['Last-Event-ID'] = options.lastEventId
  const response = await fetch(`${API_BASE}${path}`, { headers, signal: options.signal })
  if (response.status === 401) {
    if (typeof window !== 'undefined') window.location.href = '/login'
    throw new Error('Unauthorized')
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as {
      detail?: string | ApiErrorContext
    }
    const detail = body.detail
    throw new ApiRequestError(
      typeof detail === 'string' ? detail : detail?.message || detail?.code || `HTTP ${response.status}`,
      {
        status: response.status,
        context: typeof detail === 'object' && detail !== null ? detail : {},
      },
    )
  }
  if (!response.body) throw new Error('SSE response does not expose a readable body')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const dispatch = (frame: string) => {
    if (!frame || frame.startsWith(':')) return
    let id: string | null = null
    let event = 'message'
    const data: string[] = []
    for (const line of frame.split('\n')) {
      if (line.startsWith('id:')) id = line.slice(3).trim()
      else if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
    }
    if (data.length > 0) {
      options.onEvent({ id, event, data: JSON.parse(data.join('\n')) as T })
    }
  }

  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done }).replaceAll('\r\n', '\n')
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      dispatch(buffer.slice(0, boundary))
      buffer = buffer.slice(boundary + 2)
      boundary = buffer.indexOf('\n\n')
    }
    if (done) break
  }
  if (buffer.trim()) dispatch(buffer.trim())
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined }),
  postWithHeaders: <T>(path: string, body: unknown, headers: Record<string, string>) =>
    request<T>(path, { method: 'POST', headers, body: JSON.stringify(body) }),
  postFormWithHeaders: <T>(path: string, body: FormData, headers: Record<string, string>) =>
    request<T>(path, { method: 'POST', headers, body }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PUT', body: body !== undefined ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body: body !== undefined ? JSON.stringify(body) : undefined }),
  patchWithHeaders: <T>(path: string, body: unknown, headers: Record<string, string>) =>
    request<T>(path, { method: 'PATCH', headers, body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  streamEvents,

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
