import type { components } from './generated/schema'
import { ensureSuccess, type JsonTransport, type TransportResponse, type UploadTransport } from './transport'
import type {
  MapRenderAcceptedView,
  MapRenderView,
  MaterializedTripView,
  PublicChangeAdopted,
  PublicChangePreview,
  PublicTripChecksView,
  StaySelectionAppliedView,
  StaySuggestionView,
  TripUnderstandingAcceptedView,
  TripUnderstandingProgressView,
  UserFacingTripResult,
} from './v3'

type Schemas = components['schemas']
export type WechatLoginResponse = Schemas['WechatLoginResponse']
export type TripWorkspaceContract = Schemas['TripWorkspace']
export type WorkspaceResumeContract = Schemas['WorkspaceResume']
export type ItineraryImportContract = Schemas['ItineraryImport']
export type TripBriefRevisionContract = Schemas['TripBriefRevision']
export type ItineraryRevisionContract = Schemas['ItineraryRevision']
export type TripCheckRunContract = Schemas['TripCheckRun']
export type AuditReportContract = Schemas['AuditReport']
export type EvidenceSnapshotContract = Schemas['EvidenceSnapshot']
export type AdviceBundleContract = Schemas['AdviceBundle']
export type RepairOptionContract = Schemas['RepairOption']
export type RepairApplyResultContract = Schemas['RepairApplyResult']
export type ScreenshotUploadBatchContract = Schemas['ScreenshotUploadBatch']
export type ScreenshotUploadBatchCommitResultContract = Schemas['ScreenshotUploadBatchCommitResult']
export type ScreenshotUploadBatchCancelResultContract = Schemas['ScreenshotUploadBatchCancelResult']
export type RunSpecContract = Schemas['RunSpec']

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
  constructor(
    private readonly transport: JsonTransport,
    private readonly uploadTransport?: UploadTransport,
  ) {}

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

  async createRoom(body: { room_id: string; thread_id: string; trip_city: string; trip_days: number; nickname?: string }): Promise<Record<string, unknown>> {
    return (await this.json<Record<string, unknown>>('POST', '/api/room', body)).data
  }

  async createWorkspace(body: Schemas['CreateWorkspaceRequest']): Promise<TripWorkspaceContract> {
    return (await this.json<TripWorkspaceContract>('POST', '/api/trip-workspaces', body)).data
  }

  async resumeWorkspace(workspaceId: string): Promise<WorkspaceResumeContract> {
    return (await this.json<WorkspaceResumeContract>('GET', `/api/trip-workspaces/${workspaceId}/resume`)).data
  }

  async createTextImport(workspaceId: string, body: Schemas['CreateImportRequest'], idempotencyKey: string): Promise<ItineraryImportContract> {
    return (await this.json<ItineraryImportContract>('POST', `/api/trip-workspaces/${workspaceId}/imports`, body, { idempotencyKey })).data
  }

  async confirmResolutions(workspaceId: string, importId: string, body: Schemas['ConfirmResolutionsRequest'], version: number): Promise<ItineraryImportContract> {
    return (await this.json<ItineraryImportContract>('PATCH', `/api/trip-workspaces/${workspaceId}/imports/${importId}/resolutions`, body, { ifMatch: version })).data
  }

  async searchCandidates(workspaceId: string, importId: string, rawStopId: string, query: string, version: number): Promise<ItineraryImportContract> {
    return (await this.json<ItineraryImportContract>('POST', `/api/trip-workspaces/${workspaceId}/imports/${importId}/raw-stops/${rawStopId}/candidates:search`, { query }, { ifMatch: version })).data
  }

  async patchBrief(workspaceId: string, revision: number, updates: Record<string, unknown>, idempotencyKey: string): Promise<TripBriefRevisionContract> {
    return (await this.json<TripBriefRevisionContract>('PATCH', `/api/trip-workspaces/${workspaceId}/trip-briefs/${revision}`, { updates }, { ifMatch: revision, idempotencyKey })).data
  }

  async confirmBrief(workspaceId: string, revision: number, idempotencyKey: string): Promise<TripBriefRevisionContract> {
    return (await this.json<TripBriefRevisionContract>('POST', `/api/trip-workspaces/${workspaceId}/trip-briefs/${revision}/confirm`, {}, { ifMatch: revision, idempotencyKey })).data
  }

  async applyImport(workspaceId: string, itineraryImport: ItineraryImportContract, idempotencyKey: string): Promise<Schemas['ImportApplyResult']> {
    return (await this.json<Schemas['ImportApplyResult']>('POST', `/api/trip-workspaces/${workspaceId}/imports/${itineraryImport.import_id}/apply`, {}, { ifMatch: itineraryImport.state_version, idempotencyKey })).data
  }

  async createRun(workspaceId: string, body: Schemas['CreateTripCheckRunRequest'], idempotencyKey: string): Promise<TripCheckRunContract> {
    return (await this.json<TripCheckRunContract>('POST', `/api/trip-workspaces/${workspaceId}/trip-check-runs`, body, { idempotencyKey })).data
  }

  async getRun(runId: string): Promise<TripCheckRunContract> {
    return (await this.json<TripCheckRunContract>('GET', `/api/trip-check-runs/${runId}`)).data
  }

  async resumeRun(run: TripCheckRunContract, idempotencyKey: string): Promise<TripCheckRunContract> {
    return (await this.json<TripCheckRunContract>('POST', `/api/trip-check-runs/${run.run_id}/resume`, { config_hash: run.config_hash }, { ifMatch: run.version, idempotencyKey })).data
  }

  async getAudit(reportId: string): Promise<AuditReportContract> {
    return (await this.json<AuditReportContract>('GET', `/api/audits/${reportId}`)).data
  }

  async getEvidence(reportId: string): Promise<EvidenceSnapshotContract> {
    return (await this.json<EvidenceSnapshotContract>('GET', `/api/audits/${reportId}/evidence`)).data
  }

  async getAdvice(workspaceId: string, reportId: string): Promise<AdviceBundleContract> {
    return (await this.json<AdviceBundleContract>('GET', `/api/trip-workspaces/${workspaceId}/reports/${reportId}/advice`)).data
  }

  async getRepairs(reportId: string): Promise<RepairOptionContract[]> {
    return (await this.json<RepairOptionContract[]>('GET', `/api/audits/${reportId}/repairs`)).data
  }

  async proposeRepairs(reportId: string, idempotencyKey: string): Promise<RepairOptionContract[]> {
    return (await this.json<RepairOptionContract[]>('POST', `/api/audits/${reportId}/repairs`, {}, { idempotencyKey })).data
  }

  async applyRepair(option: RepairOptionContract, idempotencyKey: string): Promise<RepairApplyResultContract> {
    return (await this.json<RepairApplyResultContract>('POST', `/api/audits/${option.source_report_id}/repairs/${option.repair_id}/apply`, { base_revision: option.base_itinerary_revision }, { ifMatch: option.base_itinerary_revision, idempotencyKey })).data
  }

  async rejectRepair(option: RepairOptionContract, reason: string): Promise<RepairOptionContract> {
    return (await this.json<RepairOptionContract>('POST', `/api/audits/${option.source_report_id}/repairs/${option.repair_id}/reject`, { reason })).data
  }

  async createScreenshotBatch(workspaceId: string, expectedCount: number, idempotencyKey: string): Promise<ScreenshotUploadBatchContract> {
    return (await this.json<ScreenshotUploadBatchContract>('POST', `/api/trip-workspaces/${workspaceId}/screenshot-upload-batches`, { expected_count: expectedCount }, { idempotencyKey })).data
  }

  async uploadScreenshot(workspaceId: string, batch: ScreenshotUploadBatchContract, position: number, filePath: string, idempotencyKey: string): Promise<ScreenshotUploadBatchContract> {
    if (!this.uploadTransport) throw new Error('upload transport is unavailable')
    return ensureSuccess(await this.uploadTransport.upload<ScreenshotUploadBatchContract>({
      path: `/api/trip-workspaces/${workspaceId}/screenshot-upload-batches/${batch.batch_id}/files/${position}`,
      filePath,
      fieldName: 'file',
      headers: commandHeaders({ idempotencyKey, ifMatch: batch.version }),
    })).data
  }

  async commitScreenshotBatch(workspaceId: string, batch: ScreenshotUploadBatchContract, idempotencyKey: string): Promise<ScreenshotUploadBatchCommitResultContract> {
    return (await this.json<ScreenshotUploadBatchCommitResultContract>('POST', `/api/trip-workspaces/${workspaceId}/screenshot-upload-batches/${batch.batch_id}/commit`, undefined, { idempotencyKey, ifMatch: batch.version })).data
  }

  async cancelScreenshotBatch(workspaceId: string, batch: ScreenshotUploadBatchContract, idempotencyKey: string): Promise<ScreenshotUploadBatchCancelResultContract> {
    return (await this.json<ScreenshotUploadBatchCancelResultContract>('DELETE', `/api/trip-workspaces/${workspaceId}/screenshot-upload-batches/${batch.batch_id}`, undefined, { idempotencyKey, ifMatch: batch.version })).data
  }
}

const P1_DATASET_HASH = '18322d96f3bc2e3315be6f6c0b38842d2dbc9eb81a270ea60d4a1e72824385f4'
const CONTROLLED_SNAPSHOT_HASH = '3307e65a4134b2659d79ea0b9bdea42586e93122593d16ddb73df5a2db1bcf47'

export function controlledRunSpec(commitSha = 'c6eff35'): RunSpecContract {
  return {
    schema_version: 'trip-check-run-spec-v1',
    commit_sha: commitSha,
    prompt_version: 'none-p1',
    model_version: 'none-p1',
    provider_version: 'controlled-fixture-v1',
    rule_set_version: 'audit-v1',
    execution_mode: 'fixture',
    dataset_hash: P1_DATASET_HASH,
    snapshot_hash: CONTROLLED_SNAPSHOT_HASH,
    fault_profile: 'none',
    random_seed: 7,
    budget: {
      max_tokens: 0,
      max_provider_queries: 0,
      max_retries: 1,
      timeout_seconds: 30,
      max_cost_usd: 0,
    },
  }
}
