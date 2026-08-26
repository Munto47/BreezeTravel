import { nanoid } from 'nanoid/non-secure'

import { IdempotencyRegistry } from '@breezetravel/trip-check-client'

import { taroStorage } from './storage'

export const commandRegistry = new IdempotencyRegistry(
  taroStorage,
  () => `${Date.now()}-${nanoid(16)}`,
  'breezetravel:miniapp-command',
)
