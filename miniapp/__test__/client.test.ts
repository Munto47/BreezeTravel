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
  test('login and resume use the public contracts without browser APIs', async () => {
    const transport = new RecordingTransport()
    transport.responses.push(
      { status: 200, data: { token: 'jwt', user_id: 'u1', nickname: '微信旅行者', is_new_user: true }, headers: {} },
      { status: 200, data: { schema_version: '1.0', workspace: { workspace_id: 'w1' } }, headers: {} },
    )
    const client = new TripCheckClient(transport)
    expect((await client.loginWithWechat('wx-code')).token).toBe('jwt')
    await client.resumeWorkspace('w1')
    expect(transport.requests.map(item => item.path)).toEqual([
      '/api/auth/wechat/login',
      '/api/trip-workspaces/w1/resume',
    ])
  })

  test('mutation carries Idempotency-Key and If-Match and maps 409', async () => {
    const transport = new RecordingTransport()
    transport.responses.push({
      status: 409,
      data: { detail: { code: 'SCREENSHOT_UPLOAD_BATCH_VERSION_CONFLICT', message: 'version changed' } },
      headers: {},
    })
    const client = new TripCheckClient(transport)
    await expect(client.confirmBrief('w1', 3, 'command-1')).rejects.toEqual(
      expect.objectContaining<Partial<TripCheckApiError>>({ status: 409, code: 'SCREENSHOT_UPLOAD_BATCH_VERSION_CONFLICT' }),
    )
    expect(transport.requests[0].headers).toEqual({ 'Idempotency-Key': 'command-1', 'If-Match': '"3"' })
  })

  test('Repair adoption is followed through the postcheck report contract', async () => {
    const transport = new RecordingTransport()
    transport.responses.push(
      {
        status: 200,
        data: { new_revision: 4, postcheck_report_id: 'postcheck-1', idempotent_replay: false, repair: {} },
        headers: {},
      },
      { status: 200, data: { report_id: 'postcheck-1', overall_status: 'SATISFIED' }, headers: {} },
    )
    const client = new TripCheckClient(transport)
    const option = {
      repair_id: 'repair-1',
      source_report_id: 'report-1',
      base_itinerary_revision: 3,
    } as never
    const applied = await client.applyRepair(option, 'apply-repair-1')
    const postcheck = await client.getAudit(applied.postcheck_report_id)
    expect(postcheck.report_id).toBe('postcheck-1')
    expect(transport.requests[0]).toEqual(expect.objectContaining({
      path: '/api/audits/report-1/repairs/repair-1/apply',
      headers: { 'Idempotency-Key': 'apply-repair-1', 'If-Match': '"3"' },
    }))
    expect(transport.requests[1].path).toBe('/api/audits/postcheck-1')
  })
})
