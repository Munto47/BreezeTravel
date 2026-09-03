import Taro from '@tarojs/taro'

import type { KeyValueStorage, WechatLoginResponse } from '@breezetravel/trip-check-client'

const SESSION_KEY = 'breezetravel:wechat-session'
const TRIP_RESOURCE_KEY = 'breezetravel:last-trip-resource-id'

export const taroStorage: KeyValueStorage = {
  get(key) {
    const value = Taro.getStorageSync<string>(key)
    return typeof value === 'string' && value ? value : null
  },
  set(key, value) {
    Taro.setStorageSync(key, value)
  },
  remove(key) {
    Taro.removeStorageSync(key)
  },
}

export function readSession(): WechatLoginResponse | null {
  const raw = taroStorage.get(SESSION_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as WechatLoginResponse
  } catch {
    taroStorage.remove(SESSION_KEY)
    return null
  }
}

export function saveSession(session: WechatLoginResponse): void {
  taroStorage.set(SESSION_KEY, JSON.stringify(session))
}

export function clearSession(): void {
  taroStorage.remove(SESSION_KEY)
}

export function readLastTripResourceId(): string | null {
  return taroStorage.get(TRIP_RESOURCE_KEY)
}

export function saveLastTripResourceId(publicResourceId: string): void {
  taroStorage.set(TRIP_RESOURCE_KEY, publicResourceId)
}
