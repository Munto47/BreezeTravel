const AUTH_STORAGE_KEYS = ['authToken', 'authUser', 'userId', 'nickname'] as const
let loginRedirectInProgress = false

export class RequestTimeoutError extends Error {
  constructor() {
    super('REQUEST_TIMEOUT')
    this.name = 'RequestTimeoutError'
  }
}

export function currentLoginReturnPath(): string {
  if (typeof window === 'undefined') return '/'
  const candidate = `${window.location.pathname}${window.location.search}${window.location.hash}`
  if (
    !candidate.startsWith('/') ||
    candidate.startsWith('//') ||
    /[\\\u0000-\u001f]/.test(candidate) ||
    window.location.pathname === '/login'
  )
    return '/'
  return candidate
}

export function clearBrowserAuth(): void {
  if (typeof window === 'undefined') return
  for (const key of AUTH_STORAGE_KEYS) localStorage.removeItem(key)
}

export function recoverExpiredLogin(): void {
  if (typeof window === 'undefined') return
  const returnPath = currentLoginReturnPath()
  if (returnPath !== '/') sessionStorage.setItem('bt_login_return', returnPath)
  else sessionStorage.removeItem('bt_login_return')
  clearBrowserAuth()
  if (window.location.pathname !== '/login' && !loginRedirectInProgress) {
    loginRedirectInProgress = true
    window.location.assign('/login')
  }
}

export async function runWithDeadline<T>(
  operation: (signal: AbortSignal) => Promise<T>,
  timeoutMs = 15000,
  upstream?: AbortSignal | null,
): Promise<T> {
  const controller = new AbortController()
  const relayAbort = () => controller.abort(upstream?.reason)
  if (upstream?.aborted) relayAbort()
  else upstream?.addEventListener('abort', relayAbort, { once: true })
  const timer = setTimeout(() => controller.abort(new RequestTimeoutError()), timeoutMs)
  try {
    return await operation(controller.signal)
  } catch (error) {
    if (controller.signal.aborted && !upstream?.aborted)
      throw new RequestTimeoutError()
    throw error
  } finally {
    clearTimeout(timer)
    upstream?.removeEventListener('abort', relayAbort)
  }
}

export function fetchWithDeadline(
  input: RequestInfo | URL,
  init: RequestInit = {},
  timeoutMs = 15000,
): Promise<Response> {
  return runWithDeadline(
    (signal) => fetch(input, { ...init, signal }),
    timeoutMs,
    init.signal,
  )
}
