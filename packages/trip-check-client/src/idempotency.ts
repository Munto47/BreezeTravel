export interface KeyValueStorage {
  get(key: string): string | null
  set(key: string, value: string): void
  remove(key: string): void
}

type StoredCommand = { fingerprint: string; key: string }

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`)
      .join(',')}}`
  }
  return JSON.stringify(value)
}

export class IdempotencyRegistry {
  constructor(
    private readonly storage: KeyValueStorage,
    private readonly createKey: () => string,
    private readonly namespace = 'trip-check-command',
  ) {}

  acquire(scope: string, payload: unknown): string {
    const storageKey = `${this.namespace}:${scope}`
    const fingerprint = canonical(payload)
    const raw = this.storage.get(storageKey)
    if (raw) {
      const existing = JSON.parse(raw) as StoredCommand
      if (existing.fingerprint === fingerprint) return existing.key
    }
    const next = { fingerprint, key: this.createKey() }
    this.storage.set(storageKey, JSON.stringify(next))
    return next.key
  }

  complete(scope: string): void {
    this.storage.remove(`${this.namespace}:${scope}`)
  }
}
