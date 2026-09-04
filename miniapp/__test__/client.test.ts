import {
  TripCheckApiError,
  TripCheckClient,
  type JsonTransport,
  type TransportRequest,
  type TransportResponse,
} from '@breezetravel/trip-check-client'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

class RecordingTransport implements JsonTransport {
  requests: TransportRequest[] = []
  responses: Array<TransportResponse<unknown>> = []

  async request<T>(request: TransportRequest): Promise<TransportResponse<T>> {
    this.requests.push(request)
    return this.responses.shift() as TransportResponse<T>
  }
}

describe('shared TripCheckClient', () => {
  test('login and text creation use only the public v3 contracts', async () => {
    const transport = new RecordingTransport()
    transport.responses.push(
      { status: 200, data: { token: 'jwt', user_id: 'u1', nickname: '微信旅行者', is_new_user: true }, headers: {} },
      { status: 202, data: { public_resource_id: 'trip-1', status: 'PROCESSING', message: '处理中', result_url: '/result', events_url: '/events' }, headers: {} },
    )
    const client = new TripCheckClient(transport)
    expect((await client.loginWithWechat('wx-code')).token).toBe('jwt')
    await client.createFullTripUnderstanding('北京三日攻略', 'create-1')
    expect(transport.requests.map(item => item.path)).toEqual([
      '/api/auth/wechat/login',
      '/api/v3/trip-understandings',
    ])
  })

  test('mutation carries Idempotency-Key and If-Match and maps 409', async () => {
    const transport = new RecordingTransport()
    transport.responses.push({
      status: 409,
      data: { detail: { code: 'REVISION_CONFLICT', message: 'version changed' } },
      headers: {},
    })
    const client = new TripCheckClient(transport)
    await expect(client.applyTripUnderstandingCommand(
      'trip-1',
      { command_type: 'ACTIVITY_DELETE', activity_token: 'activity-1' },
      'tu3_current',
      'command-1',
    )).rejects.toEqual(
      expect.objectContaining<Partial<TripCheckApiError>>({ status: 409, code: 'REVISION_CONFLICT' }),
    )
    expect(transport.requests[0].headers).toEqual({ 'Idempotency-Key': 'command-1', 'If-Match': '"tu3_current"' })
  })

  test('shared client does not expose frozen workspace or screenshot methods', () => {
    const transport = new RecordingTransport()
    const publicClient = new TripCheckClient(transport) as unknown as Record<string, unknown>
    expect(publicClient.createWorkspace).toBeUndefined()
    expect(publicClient.createScreenshotBatch).toBeUndefined()
  })

  test('shared client carries every miniapp card editing command', async () => {
    const transport = new RecordingTransport()
    transport.responses.push(
      { status: 200, data: { status: 'APPLIED', changed_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE' }, headers: { etag: 'next-1' } },
      { status: 200, data: { status: 'APPLIED', changed_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE' }, headers: { etag: 'next-2' } },
      { status: 200, data: { status: 'APPLIED', changed_days: ['Day 1'], map_readiness: 'NEEDS_UPDATE' }, headers: { etag: 'next-3' } },
    )
    const client = new TripCheckClient(transport)

    await client.applyTripUnderstandingCommand('trip-1', {
      command_type: 'ACTIVITY_INSERT', day_index: 1, position: 0, name: '景山公园', category: '景点', area_or_address: '东城区', time_hint: '下午',
    }, 'etag-1', 'insert-1')
    await client.applyTripUnderstandingCommand('trip-1', {
      command_type: 'PLACE_REPLACE', activity_token: 'activity-1', replacement: { name: '北海公园', category: '景点', area_or_address: '西城区' },
    }, 'etag-2', 'replace-1')
    await client.applyTripUnderstandingCommand('trip-1', {
      command_type: 'ACTIVITY_MOVE', activity_token: 'activity-1', target_day_index: 2, target_position: 1,
    }, 'etag-3', 'move-1')

    expect(transport.requests.map(item => item.body)).toEqual([
      { command_type: 'ACTIVITY_INSERT', day_index: 1, position: 0, name: '景山公园', category: '景点', area_or_address: '东城区', time_hint: '下午' },
      { command_type: 'PLACE_REPLACE', activity_token: 'activity-1', replacement: { name: '北海公园', category: '景点', area_or_address: '西城区' } },
      { command_type: 'ACTIVITY_MOVE', activity_token: 'activity-1', target_day_index: 2, target_position: 1 },
    ])
    expect(transport.requests.map(item => item.headers?.['If-Match'])).toEqual(['"etag-1"', '"etag-2"', '"etag-3"'])
  })

  test('ordinary miniapp failures use safe copy and non-red feedback', () => {
    const pageSources = [
      'src/pages/home/index.tsx',
      'src/pages/login/index.tsx',
      'src/pages/trip/index.tsx',
    ].map(relativePath => readFileSync(resolve(__dirname, '..', relativePath), 'utf8'))
    const feedbackStyles = [
      'src/pages/home/index.scss',
      'src/pages/login/index.scss',
      'src/pages/trip/index.scss',
    ].map(relativePath => readFileSync(resolve(__dirname, '..', relativePath), 'utf8'))

    for (const source of pageSources) {
      expect(source).not.toMatch(/caught\s+instanceof\s+Error\s*\?\s*caught\.message/)
      expect(source).not.toMatch(/String\(caught\)/)
      expect(source).toContain("className='feedback'")
    }
    for (const stylesheet of feedbackStyles) {
      expect(stylesheet).toMatch(/\.feedback\s*\{[^}]*color:\s*#175cd3;/)
      expect(stylesheet).not.toMatch(/\.feedback\s*\{[^}]*#b42318/)
    }
  })

  test('trip enhancement failures have bounded polling and in-page retry actions', () => {
    const source = readFileSync(
      resolve(__dirname, '..', 'src/pages/trip/index.tsx'),
      'utf8',
    )

    expect(source).toContain('MAX_ENHANCEMENT_POLL_ATTEMPTS')
    expect(source).toContain('enhancementPollAttempts.current < MAX_ENHANCEMENT_POLL_ATTEMPTS')
    expect(source).toContain('路线或住宿仍在准备，可以稍后再次检查。')
    expect(source).toContain('再次检查')
    expect(source).toContain('建议暂时没有准备好，可以在这里重试。')
    expect(source).toContain('重新准备建议')
    expect(source).toContain('timer.current = setTimeout(() => void load(), 1000)')
  })
})
