'use client'

import { useState, useCallback } from 'react'

import type { Itinerary } from '@/types/itinerary'
import { parseItineraryFromAPI } from '@/types/itinerary'
import type { Place } from '@/types/place'
import { parsePlaceFromAPI } from '@/types/place'

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

interface UseOptimizeReturn {
  itinerary: Itinerary | null
  isOptimizing: boolean
  totalDistanceKm: number
  backupPool: Place[]           // 备选池（A7）
  criticViolations: object[]    // Critic 违规摘要
  optimize: (places: Place[], tripDays: number, startDate?: string) => Promise<void>
}

export function useOptimize(threadId: string, roomId?: string): UseOptimizeReturn {
  const [itinerary, setItinerary] = useState<Itinerary | null>(null)
  const [isOptimizing, setIsOptimizing] = useState(false)
  const [totalDistanceKm, setTotalDistanceKm] = useState(0)
  const [backupPool, setBackupPool] = useState<Place[]>([])
  const [criticViolations, setCriticViolations] = useState<object[]>([])

  const optimize = useCallback(
    async (places: Place[], tripDays: number, startDate?: string) => {
      if (isOptimizing || places.length === 0) return
      setIsOptimizing(true)

      try {
        const response = await fetch(`${API_BASE}/api/optimize`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            thread_id: threadId,
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
          }),
        })

        if (!response.ok) throw new Error(`排线失败：${response.status}`)

        const data = await response.json()
        const parsed = parseItineraryFromAPI(data.itinerary)
        setItinerary(parsed)
        setTotalDistanceKm(data.total_distance_km ?? 0)

        // 解析备选池（A7）
        const rawBackup: unknown[] = data.backup_pool ?? []
        setBackupPool(rawBackup.map((r) => parsePlaceFromAPI(r as Record<string, unknown>)))

        // Critic 违规摘要
        setCriticViolations(data.critic_violations ?? [])

        if (roomId && typeof window !== 'undefined') {
          localStorage.setItem(`itinerary_${roomId}`, JSON.stringify(parsed))
        }
      } catch (err) {
        console.error('[useOptimize]', err)
      } finally {
        setIsOptimizing(false)
      }
    },
    [threadId, roomId, isOptimizing],
  )

  return { itinerary, isOptimizing, totalDistanceKm, backupPool, criticViolations, optimize }
}
