'use client'

import { useState, useCallback, useRef } from 'react'

import type { Itinerary } from '@/types/itinerary'
import { parseItineraryFromAPI, parseSavedItinerary } from '@/types/itinerary'
import type { Place } from '@/types/place'
import { parsePlaceFromAPI } from '@/types/place'
import type { TripTaskSpec } from '@/types/taskSpec'
import { recoverExpiredLogin, runWithDeadline } from '@/lib/request-safety'

const API_BASE = process.env.NEXT_PUBLIC_API_URL || ''

interface UseOptimizeReturn {
  itinerary: Itinerary | null
  isOptimizing: boolean
  totalDistanceKm: number
  backupPool: Place[]           // 备选池（A7）
  optimize: (places: Place[], tripDays: number, startDate?: string, taskSpec?: TripTaskSpec) => Promise<boolean>
  restoreItinerary: (itinerary: unknown) => Itinerary | null
}

export function useOptimize(threadId: string, roomId?: string): UseOptimizeReturn {
  const [itinerary, setItinerary] = useState<Itinerary | null>(null)
  const [isOptimizing, setIsOptimizing] = useState(false)
  const [totalDistanceKm, setTotalDistanceKm] = useState(0)
  const [backupPool, setBackupPool] = useState<Place[]>([])
  const inFlightRef = useRef(false)
  const restoreItinerary = useCallback((saved: unknown) => {
    const restored = parseSavedItinerary(saved)
    if (!restored) return null
    setItinerary(restored)
    return restored
  }, [])

  const optimize = useCallback(
    async (places: Place[], tripDays: number, startDate?: string, parsedTaskSpec?: TripTaskSpec) => {
      if (inFlightRef.current || isOptimizing || places.length === 0) return false
      inFlightRef.current = true
      setIsOptimizing(true)

      try {
        const authToken = typeof window !== 'undefined' ? localStorage.getItem('authToken') : null
        const shouldPersistWorkspace = Boolean(roomId && startDate && authToken)
        const data = await runWithDeadline(async (signal) => {
          const response = await fetch(`${API_BASE}/api/optimize`, {
            method: 'POST',
            signal,
            headers: {
              'Content-Type': 'application/json',
              ...(authToken
                ? { Authorization: `Bearer ${authToken}` }
                : {}),
            },
            body: JSON.stringify({
              thread_id: threadId,
              room_id: roomId || null,
              places: places.map((p) => ({
                place_id: p.placeId,
                name: p.name,
                category: p.category,
                address: p.address,
                coords: p.coords,
                city: p.city,
                source: p.source,
                amap_rating: p.amapRating,
                amap_price: p.amapPrice,
                amap_photos: p.amapPhotos,
                estimated_duration: p.estimatedDuration,
                description: p.description,
                tags: p.tags,
              })),
              trip_days: tripDays,
              start_date: startDate ?? null,
              task_spec: parsedTaskSpec ?? null,
              persist_workspace: shouldPersistWorkspace,
            }),
          })
          if (response.status === 401) {
            recoverExpiredLogin()
            throw new Error('AUTH_EXPIRED')
          }
          if (!response.ok) throw new Error(`排线失败：${response.status}`)
          return await response.json()
        }, 45000)
        const parsed = parseItineraryFromAPI(data.itinerary)
        setItinerary(parsed)
        setTotalDistanceKm(data.total_distance_km ?? 0)

        // 解析备选池（A7）
        const rawBackup: unknown[] = data.backup_pool ?? []
        setBackupPool(rawBackup.map((r) => parsePlaceFromAPI(r as Record<string, unknown>)))

        if (roomId && typeof window !== 'undefined') {
          // Cache only. PostgreSQL workspace/revision/report remains authoritative.
          localStorage.setItem(`itinerary_cache_${roomId}`, JSON.stringify(parsed))
        }
        return true
      } catch (err) {
        console.error('[useOptimize]', err)
        return false
      } finally {
        inFlightRef.current = false
        setIsOptimizing(false)
      }
    },
    [threadId, roomId, isOptimizing],
  )

  return {
    itinerary,
    isOptimizing,
    totalDistanceKm,
    backupPool,
    optimize,
    restoreItinerary,
  }
}
