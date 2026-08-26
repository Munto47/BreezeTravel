export const TERMINAL_RUN_STATUSES = new Set(['SUCCEEDED', 'FAILED', 'PRIVACY_BLOCKED', 'CANCELLED'])

export function shouldPollRun(status: string, stage: string): boolean {
  return !TERMINAL_RUN_STATUSES.has(status) && stage !== 'WAIT_ADOPTION'
}

export function pollDelay(failureCount: number): number {
  if (failureCount <= 0) return 1_000
  if (failureCount === 1) return 3_000
  return 5_000
}
