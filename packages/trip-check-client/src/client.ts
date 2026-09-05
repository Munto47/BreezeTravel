import type { components } from './generated/schema'
import { ensureSuccess, type JsonTransport, type TransportResponse } from './transport'
import type {
  CommandAppliedView,
  MapRenderAcceptedView,
  MapRenderView,
  MaterializedTripView,
  PublicChangeAdopted,
  PublicChangePreview,
  PublicTripChecksView,
  PlaceCandidatesView,
  StaySelectionAppliedView,
  StaySuggestionView,
  TripUnderstandingAcceptedView,
  TripUnderstandingProgressView,
  TripUnderstandingCommand,
  UserFacingTripResult,
} from './v3'

type Schemas = components['schemas']
export type WechatLoginResponse = Schemas['WechatLoginResponse']

export interface CommandOptions {
  idempotencyKey?: string
  ifMatch?: number | string
}

function commandHeaders(options: CommandOptions = {}): Record<string, string> {
  const headers: Record<string, string> = {}
  if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey
  if (options.ifMatch !== undefined) {
    const value = String(options.ifMatch)
    headers['If-Match'] = value.startsWith('"') ? value : `"${value}"`
  }
  return headers
}

export class TripCheckClient {
  constructor(private readonly transport: JsonTransport) {}

  private async json<T>(method: 'GET' | 'POST' | 'PATCH' | 'DELETE', path: string, body?: unknown, options?: CommandOptions): Promise<TransportResponse<T>> {
    return ensureSuccess(await this.transport.request<T>({ method, path, body, headers: commandHeaders(options) }))
  }

  async loginWithWechat(code: string, nickname?: string): Promise<WechatLoginResponse> {
    return (await this.json<WechatLoginResponse>('POST', '/api/auth/wechat/login', { code, nickname })).data
  }

  async createDemoTripUnderstanding(idempotencyKey: string): Promise<TripUnderstandingAcceptedView> {
    return (
      await this.json<TripUnderstandingAcceptedView>(
        'POST',
        '/api/v3/trip-understandings',
        { mode: 'DEMO' },
        { idempotencyKey },
      )
    ).data
  }

  async createFullTripUnderstanding(
    text: string,
    idempotencyKey: string,
  ): Promise<TripUnderstandingAcceptedView> {
    return (
      await this.json<TripUnderstandingAcceptedView>(
        'POST',
        '/api/v3/trip-understandings',
        { mode: 'FULL', source: { type: 'TEXT', text } },
        { idempotencyKey },
      )
    ).data
  }

  async getTripUnderstandingResult(
    publicResourceId: string,
  ): Promise<TransportResponse<TripUnderstandingProgressView | UserFacingTripResult>> {
    return this.json<TripUnderstandingProgressView | UserFacingTripResult>(
      'GET',
      `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/result`,
    )
  }

  async getTripUnderstandingMap(publicResourceId: string): Promise<MapRenderView> {
    return (
      await this.json<MapRenderView>(
        'GET',
        `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/map-renders/latest`,
      )
    ).data
  }

  async queryTripPlaceCandidates(publicResourceId: string, activityToken: string, query: string): Promise<PlaceCandidatesView> {
    return (await this.json<PlaceCandidatesView>(
      'POST', `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/place-candidates`,
      { activity_token: activityToken, query },
    )).data
  }

  async requestTripUnderstandingMap(
    publicResourceId: string,
    etag: string,
    idempotencyKey: string,
  ): Promise<MapRenderAcceptedView> {
    return (
      await this.json<MapRenderAcceptedView>(
        'POST',
        `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/map-renders`,
        undefined,
        { ifMatch: etag, idempotencyKey },
      )
    ).data
  }

  async getTripUnderstandingStaySuggestions(
    publicResourceId: string,
  ): Promise<StaySuggestionView> {
    return (
      await this.json<StaySuggestionView>(
        'GET',
        `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/stay-suggestions`,
      )
    ).data
  }

  async selectTripUnderstandingStay(
    publicResourceId: string,
    candidateToken: string,
    etag: string,
    idempotencyKey: string,
  ): Promise<TransportResponse<StaySelectionAppliedView>> {
    return this.json<StaySelectionAppliedView>(
      'POST',
      `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/stay-selection`,
      { candidate_token: candidateToken },
      { ifMatch: etag, idempotencyKey },
    )
  }

  async materializeTripUnderstanding(
    publicResourceId: string,
    etag: string,
    idempotencyKey: string,
  ): Promise<TransportResponse<MaterializedTripView>> {
    return this.json<MaterializedTripView>(
      'POST',
      `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/materialize`,
      undefined,
      { ifMatch: etag, idempotencyKey },
    )
  }

  async getTripUnderstandingChecks(
    publicResourceId: string,
  ): Promise<PublicTripChecksView> {
    return (
      await this.json<PublicTripChecksView>(
        'GET',
        `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/checks`,
      )
    ).data
  }

  async previewTripUnderstandingChange(
    publicResourceId: string,
    checkToken: string,
    idempotencyKey: string,
  ): Promise<PublicChangePreview> {
    return (
      await this.json<PublicChangePreview>(
        'POST',
        `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/changes/preview`,
        { check_token: checkToken },
        { idempotencyKey },
      )
    ).data
  }

  async adoptTripUnderstandingChange(
    publicResourceId: string,
    changeToken: string,
    etag: string,
    idempotencyKey: string,
  ): Promise<TransportResponse<PublicChangeAdopted>> {
    return this.json<PublicChangeAdopted>(
      'POST',
      `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/changes/adopt`,
      { change_token: changeToken },
      { ifMatch: etag, idempotencyKey },
    )
  }

  async applyTripUnderstandingCommand(
    publicResourceId: string,
    command: TripUnderstandingCommand,
    etag: string,
    idempotencyKey: string,
  ): Promise<TransportResponse<CommandAppliedView>> {
    return this.json<CommandAppliedView>(
      'POST',
      `/api/v3/trip-understandings/${encodeURIComponent(publicResourceId)}/commands`,
      command,
      { ifMatch: etag, idempotencyKey },
    )
  }
}
