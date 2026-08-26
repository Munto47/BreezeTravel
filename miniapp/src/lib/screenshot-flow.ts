import {
  type IdempotencyRegistry,
  type ScreenshotUploadBatchCommitResultContract,
  type TripCheckClient,
} from '@breezetravel/trip-check-client'

export interface LocalScreenshot {
  path: string
  size: number
}

export async function uploadScreenshotBatch(
  client: TripCheckClient,
  registry: IdempotencyRegistry,
  workspaceId: string,
  files: LocalScreenshot[],
): Promise<ScreenshotUploadBatchCommitResultContract> {
  const createScope = `create-screenshot-batch:${workspaceId}`
  let batch = await client.createScreenshotBatch(
    workspaceId,
    files.length,
    registry.acquire(createScope, { count: files.length }),
  )
  registry.complete(createScope)
  for (let position = 0; position < files.length; position += 1) {
    const scope = `upload-screenshot:${batch.batch_id}:${position}`
    batch = await client.uploadScreenshot(
      workspaceId,
      batch,
      position,
      files[position].path,
      registry.acquire(scope, { path: files[position].path, size: files[position].size }),
    )
    registry.complete(scope)
  }
  const scope = `commit-screenshot:${batch.batch_id}`
  const result = await client.commitScreenshotBatch(
    workspaceId,
    batch,
    registry.acquire(scope, { version: batch.version }),
  )
  registry.complete(scope)
  return result
}
