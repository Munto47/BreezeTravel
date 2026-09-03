import {
  TripCheckApiError,
  TripCheckClient,
  type JsonTransport,
  type TransportRequest,
  type TransportResponse,
} from '@breezetravel/trip-check-client'

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
})
