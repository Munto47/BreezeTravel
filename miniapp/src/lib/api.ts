import Taro from '@tarojs/taro'

import {
  TripCheckClient,
  type JsonTransport,
  type TransportRequest,
  type TransportResponse,
} from '@breezetravel/trip-check-client'

import { readSession } from './storage'

const API_BASE = process.env.TARO_APP_API_URL || 'http://127.0.0.1:8000'

function authHeaders(headers: Record<string, string> = {}): Record<string, string> {
  const token = readSession()?.token
  return token ? { ...headers, Authorization: `Bearer ${token}` } : headers
}

function normalizeHeaders(headers: Record<string, unknown>): Record<string, string> {
  return Object.fromEntries(Object.entries(headers).map(([key, value]) => [key.toLowerCase(), String(value)]))
}

export const taroJsonTransport: JsonTransport = {
  async request<T>(request: TransportRequest): Promise<TransportResponse<T>> {
    const { method, path, body, headers = {} } = request
    const response = await Taro.request<T>({
      url: `${API_BASE}${path}`,
      method,
      data: body,
      header: authHeaders({ 'Content-Type': 'application/json', ...headers }),
      timeout: 30_000,
    })
    return {
      status: response.statusCode,
      data: response.data,
      headers: normalizeHeaders(response.header as Record<string, unknown>),
    }
  },
}

export const tripCheckClient = new TripCheckClient(taroJsonTransport)
