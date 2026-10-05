type WaitClock = { startedAt: number; fromSubmission: boolean }

export function tripWaitClock(resource: string, submittedAt?: number): WaitClock {
  const fallback = {startedAt: submittedAt ?? Date.now(), fromSubmission: submittedAt !== undefined}
  if (!resource || typeof window === 'undefined') return fallback
  try {
    const key = `bt_trip_wait:${resource}`
    const saved = JSON.parse(sessionStorage.getItem(key) || 'null') as WaitClock | null
    if (saved && Number.isFinite(saved.startedAt) && saved.startedAt <= Date.now() &&
      Date.now() - saved.startedAt < 24 * 60 * 60 * 1000 && typeof saved.fromSubmission === 'boolean') return saved
    sessionStorage.setItem(key, JSON.stringify(fallback))
  } catch { /* Waiting feedback remains available when browser storage is disabled. */ }
  return fallback
}
