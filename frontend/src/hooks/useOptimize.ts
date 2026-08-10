'use client'

import { useState, useCallback } from 'react'

import type { Itinerary } from '@/types/itinerary'
import { parseItineraryFromAPI } from '@/types/itinerary'
import type { Place } from '@/types/place'
import { parsePlaceFromAPI } from '@/types/place'
import type { TripTaskSpec } from '@/types/taskSpec'
import type { VerificationReport } from '@/types/verification'

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export function planningFingerprint(places: Array<Place & { votedBy?: string[]; isPinned?: boolean }>): string {
  return JSON.stringify(
    places.map((place) => ({
      placeId: place.placeId,
      name: place.name,
      category: place.category,
      amapPrice: place.amapPrice ?? null,
      openingHours: (place as Place & { openingHours?: string }).openingHours ?? null,
      estimatedDuration: place.estimatedDuration ?? null,
      votedBy: [...(place.votedBy ?? [])].sort(),
      isPinned: Boolean(place.isPinned),
    })).sort((a, b) => a.placeId.localeCompare(b.placeId)),
  )
}

interface UseOptimizeReturn {
  itinerary: Itinerary | null
  isOptimizing: boolean
  totalDistanceKm: number
  backupPool: Place[]           // 备选池（A7）
  criticViolations: object[]    // Critic 违规摘要
  taskSpec: TripTaskSpec | null
  verificationReport: VerificationReport | null
  optimize: (places: Place[], tripDays: number, startDate?: string, taskSpec?: TripTaskSpec) => Promise<void>
}

export function useOptimize(threadId: string, roomId?: string): UseOptimizeReturn {
  const [itinerary, setItinerary] = useState<Itinerary | null>(null)
  const [isOptimizing, setIsOptimizing] = useState(false)
  const [totalDistanceKm, setTotalDistanceKm] = useState(0)
  const [backupPool, setBackupPool] = useState<Place[]>([])
  const [criticViolations, setCriticViolations] = useState<object[]>([])
  const [taskSpec, setTaskSpec] = useState<TripTaskSpec | null>(null)
  const [verificationReport, setVerificationReport] = useState<VerificationReport | null>(null)

  const optimize = useCallback(
    async (places: Place[], tripDays: number, startDate?: string, parsedTaskSpec?: TripTaskSpec) => {
      if (isOptimizing || places.length === 0) return
      setIsOptimizing(true)

      try {
        const response = await fetch(`${API_BASE}/api/optimize`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(typeof window !== 'undefined' && localStorage.getItem('authToken')
              ? { Authorization: `Bearer ${localStorage.getItem('authToken')}` }
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
        setTaskSpec((data.task_spec ?? parsedTaskSpec ?? null) as TripTaskSpec | null)
        setVerificationReport((data.verification_report ?? null) as VerificationReport | null)

        if (roomId && typeof window !== 'undefined') {
          localStorage.setItem(`itinerary_${roomId}`, JSON.stringify(parsed))
          if (data.task_spec ?? parsedTaskSpec) localStorage.setItem(`task_spec_${roomId}`, JSON.stringify(data.task_spec ?? parsedTaskSpec))
          if (data.verification_report) {
            localStorage.setItem(`verification_${roomId}`, JSON.stringify(data.verification_report))
            localStorage.setItem(`verification_stale_${roomId}`, 'false')
            localStorage.setItem(`planning_snapshot_${roomId}`, planningFingerprint(places))
          }
        }
      } catch (err) {
        console.error('[useOptimize]', err)
      } finally {
        setIsOptimizing(false)
      }
    },
    [threadId, roomId, isOptimizing],
  )

  return { itinerary, isOptimizing, totalDistanceKm, backupPool, criticViolations, taskSpec, verificationReport, optimize }
}
