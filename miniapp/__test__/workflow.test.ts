import { canResumeRun, canStartTripCheck, unresolvedPlaceIds } from '@/lib/workflow'
import { pollDelay, shouldPollRun } from '@/lib/polling'

describe('miniapp workflow gates', () => {
  const readyImport = { status: 'READY', resolutions: [] } as never
  const confirmedBrief = { status: 'CONFIRMED' } as never

  test('Brief and place resolution block Run until both are authoritative', () => {
    expect(canStartTripCheck({ status: 'DRAFT' } as never, readyImport, false)).toBe(false)
    const ambiguous = {
      status: 'NEEDS_RESOLUTION',
      resolutions: [{ raw_stop_id: 'raw-1', resolution_status: 'AMBIGUOUS' }],
    } as never
    expect(unresolvedPlaceIds(ambiguous)).toEqual(['raw-1'])
    expect(canStartTripCheck(confirmedBrief, ambiguous, false)).toBe(false)
    expect(canStartTripCheck(confirmedBrief, readyImport, false)).toBe(true)
  })

  test('polling stops in adoption and terminal states, then backs off 1/3/5 seconds', () => {
    expect(shouldPollRun('RUNNING', 'AUDIT')).toBe(true)
    expect(shouldPollRun('PARTIAL', 'WAIT_ADOPTION')).toBe(false)
    expect(shouldPollRun('SUCCEEDED', 'POSTCHECK')).toBe(false)
    expect(pollDelay(0)).toBe(1000)
    expect(pollDelay(1)).toBe(3000)
    expect(pollDelay(2)).toBe(5000)
    expect(canResumeRun({ status: 'PARTIAL' } as never)).toBe(true)
  })
})
