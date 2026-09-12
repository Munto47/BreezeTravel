// Public references are only bookmarks. The HttpOnly session and server still
// decide whether a resource is readable; no source text or credential is stored.
const KEY = 'bt_browser_trip_refs_v1'
const LIMIT = 20
const validRef = (value: unknown): value is string =>
  typeof value === 'string' && /^[A-Za-z0-9_-]{20,80}$/.test(value)

export type BrowserTripReference = { resource: string }

export function readBrowserTripReferences(): BrowserTripReference[] {
  if (typeof window === 'undefined') return []
  try {
    const value = JSON.parse(localStorage.getItem(KEY) || '[]')
    if (!Array.isArray(value)) return []
    const seen = new Set<string>()
    return value.filter((row): row is BrowserTripReference => {
      if (!row || !validRef(row.resource) || seen.has(row.resource)) return false
      seen.add(row.resource)
      return true
    }).slice(0, LIMIT).map(({ resource }) => ({ resource }))
  } catch {
    return []
  }
}

export function rememberBrowserTripReference(resource: string): void {
  if (typeof window === 'undefined' || !validRef(resource)) return
  try {
    const rows = readBrowserTripReferences().filter((row) => row.resource !== resource)
    localStorage.setItem(KEY, JSON.stringify([{ resource }, ...rows].slice(0, LIMIT)))
  } catch { /* Storage denial must not interrupt a server-created job. */ }
}

export function forgetBrowserTripReference(resource: string): void {
  if (typeof window === 'undefined') return
  try {
    const rows = readBrowserTripReferences().filter((row) => row.resource !== resource)
    if (rows.length) localStorage.setItem(KEY, JSON.stringify(rows))
    else localStorage.removeItem(KEY)
  } catch { /* Server authorization remains authoritative. */ }
}
