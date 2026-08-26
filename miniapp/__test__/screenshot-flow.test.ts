import { IdempotencyRegistry, type KeyValueStorage, type TripCheckClient } from '@breezetravel/trip-check-client'

import { uploadScreenshotBatch } from '@/lib/screenshot-flow'

class MemoryStorage implements KeyValueStorage {
  values = new Map<string, string>()
  get(key: string) { return this.values.get(key) || null }
  set(key: string, value: string) { this.values.set(key, value) }
  remove(key: string) { this.values.delete(key) }
}

test('screenshots upload sequentially with the latest batch version before atomic commit', async () => {
  const calls: string[] = []
  const client = {
    createScreenshotBatch: jest.fn(async () => {
      calls.push('create')
      return { batch_id: 'b1', version: 1 }
    }),
    uploadScreenshot: jest.fn(async (_workspace: string, batch: { version: number }, position: number) => {
      calls.push(`upload:${position}:v${batch.version}`)
      return { batch_id: 'b1', version: batch.version + 1 }
    }),
    commitScreenshotBatch: jest.fn(async (_workspace: string, batch: { version: number }) => {
      calls.push(`commit:v${batch.version}`)
      return { batch: { batch_id: 'b1', version: batch.version + 2 }, import_result: {} }
    }),
  } as unknown as TripCheckClient
  const registry = new IdempotencyRegistry(new MemoryStorage(), (() => {
    let id = 0
    return () => `key-${id += 1}`
  })())

  await uploadScreenshotBatch(client, registry, 'w1', [
    { path: 'a.png', size: 10 },
    { path: 'b.png', size: 20 },
  ])

  expect(calls).toEqual(['create', 'upload:0:v1', 'upload:1:v2', 'commit:v3'])
})
