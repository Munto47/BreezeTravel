import type { Place } from './place'
import { parsePlaceFromAPI } from './place'

export interface TransportLeg {
  mode: 'driving' | 'walking' | 'transit'
  durationMins: number
  distanceKm: number
}

export interface WeatherInfo {
  condition: string     // "晴" / "多云" / "小雨"
  tempHigh: number
  tempLow: number
  suggestion: string    // "适合户外，建议带防晒"
}

export interface TimeSlot {
  placeId: string
  place: Place
  startTime: string     // "09:00"
  endTime: string       // "11:30"
  transport?: TransportLeg  // 与下一地点的交通（最后一个为 undefined）
  tips: string[]        // 温馨提示（TipsGenerator 生成）
}

export interface DayPlan {
  dayIndex: number      // 0-based
  date?: string         // ISO 8601，可选
  clusterId: number
  slots: TimeSlot[]
  weatherSummary?: WeatherInfo
}

export interface Itinerary {
  itineraryId: string
  threadId: string
  city: string
  days: DayPlan[]
  generatedAt: string   // ISO 8601
  version: number       // 每次重新排线递增
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function boundedString(value: unknown, maximum = 500): string | null {
  return typeof value === 'string' && value.length <= maximum ? value : null
}

function stringList(value: unknown, maximum = 20): string[] {
  return Array.isArray(value)
    ? value
        .filter((item): item is string => typeof item === 'string' && item.length <= 500)
        .slice(0, maximum)
    : []
}

/**
 * Parse the collaboration itinerary persisted by the browser.
 *
 * Saved collaboration data predates the verified route contract, so transport
 * legs are deliberately discarded even when a legacy record contains minutes
 * or distance. Malformed records return null instead of reaching React.
 */
export function parseSavedItinerary(value: unknown): Itinerary | null {
  const raw = objectValue(value)
  if (!raw || !Array.isArray(raw.days) || raw.days.length > 31) return null
  const itineraryId = boundedString(raw.itineraryId, 200)
  const threadId = boundedString(raw.threadId, 200)
  const city = boundedString(raw.city, 100)
  const generatedAt = boundedString(raw.generatedAt, 100)
  const version = finiteNumber(raw.version)
  if (!itineraryId || !threadId || !city || !generatedAt || version === null) return null

  const days: DayPlan[] = []
  for (const candidateDay of raw.days) {
    const day = objectValue(candidateDay)
    if (!day || !Array.isArray(day.slots) || day.slots.length > 100) return null
    const dayIndex = finiteNumber(day.dayIndex)
    const clusterId = finiteNumber(day.clusterId)
    const date = day.date === undefined ? undefined : boundedString(day.date, 40)
    if (dayIndex === null || clusterId === null || date === null) return null
    const slots: TimeSlot[] = []
    for (const candidateSlot of day.slots) {
      const slot = objectValue(candidateSlot)
      const rawPlace = objectValue(slot?.place)
      if (!slot || !rawPlace) return null
      const placeId = boundedString(slot.placeId, 200)
      const startTime = boundedString(slot.startTime, 20)
      const endTime = boundedString(slot.endTime, 20)
      const name = boundedString(rawPlace.name, 200)
      const address = boundedString(rawPlace.address, 500)
      const placeCity = boundedString(rawPlace.city, 100)
      const coords = objectValue(rawPlace.coords)
      const lng = finiteNumber(coords?.lng)
      const lat = finiteNumber(coords?.lat)
      const category = rawPlace.category
      if (
        !placeId || !startTime || !endTime || !name || address === null ||
        !placeCity || lng === null || lat === null || Math.abs(lng) > 180 ||
        Math.abs(lat) > 90 ||
        !['attraction', 'food', 'hotel', 'transport'].includes(String(category))
      ) return null
      const source = ['amap_poi', 'rag', 'synthesized'].includes(String(rawPlace.source))
        ? rawPlace.source as Place['source']
        : 'synthesized'
      const ragMeta = objectValue(rawPlace.ragMeta)
      const place: Place = {
        placeId,
        name,
        category: category as Place['category'],
        address,
        coords: { lng, lat },
        city: placeCity,
        district: boundedString(rawPlace.district, 100) || undefined,
        source,
        amapRating: finiteNumber(rawPlace.amapRating) ?? undefined,
        amapPrice: finiteNumber(rawPlace.amapPrice) ?? undefined,
        openingHours: boundedString(rawPlace.openingHours, 200) || undefined,
        phone: boundedString(rawPlace.phone, 100) || undefined,
        amapPhotos: stringList(rawPlace.amapPhotos, 8),
        ragMeta: ragMeta
          ? {
              tipSnippets: stringList(ragMeta.tipSnippets, 3),
              sentimentScore: finiteNumber(ragMeta.sentimentScore) ?? 0,
              sourceNoteIds: stringList(ragMeta.sourceNoteIds, 20),
            }
          : undefined,
        description: boundedString(rawPlace.description, 1000) || undefined,
        tags: stringList(rawPlace.tags, 20),
        constraintEvidence: [],
        geoEvidence: [],
        confirmationActions: [],
        clusterId: finiteNumber(rawPlace.clusterId) ?? undefined,
        visitOrder: finiteNumber(rawPlace.visitOrder) ?? undefined,
        estimatedDuration: finiteNumber(rawPlace.estimatedDuration) ?? undefined,
      }
      slots.push({
        placeId,
        place,
        startTime,
        endTime,
        transport: undefined,
        tips: stringList(slot.tips, 20),
      })
    }
    const rawWeather = objectValue(day.weatherSummary)
    const weatherSummary = rawWeather
      ? {
          condition: boundedString(rawWeather.condition, 100) || '',
          tempHigh: finiteNumber(rawWeather.tempHigh) ?? 0,
          tempLow: finiteNumber(rawWeather.tempLow) ?? 0,
          suggestion: boundedString(rawWeather.suggestion, 500) || '',
        }
      : undefined
    days.push({ dayIndex, clusterId, date, slots, weatherSummary })
  }
  return { itineraryId, threadId, city, days, generatedAt, version }
}

/** 将后端蛇形命名（API 响应）转换为前端驼峰命名 */
export function parseItineraryFromAPI(raw: Record<string, unknown>): Itinerary {
  const days = (raw.days as Record<string, unknown>[]).map((day) => {
    const slots = (day.slots as Record<string, unknown>[]).map((slot) => {
      const transport = slot.transport
        ? (() => {
            const t = slot.transport as Record<string, unknown>
            // Legacy collaboration routes may contain straight-line estimates.
            // Only an explicitly verified server leg may become user-facing.
            if (t.status !== 'AVAILABLE') return undefined
            const durationMins = Number(t.duration_mins)
            const distanceKm = Number(t.distance_km)
            if (!Number.isFinite(durationMins) || !Number.isFinite(distanceKm)) return undefined
            return {
              mode: t.mode as TransportLeg['mode'],
              durationMins,
              distanceKm,
            }
          })()
        : undefined

      return {
        placeId: slot.place_id as string,
        place: parsePlaceFromAPI(slot.place as Record<string, unknown>),
        startTime: slot.start_time as string,
        endTime: slot.end_time as string,
        transport,
        tips: (slot.tips as string[] | undefined) ?? [],
      } satisfies TimeSlot
    })

    const weather = day.weather_summary
      ? (() => {
          const w = day.weather_summary as Record<string, unknown>
          return {
            condition: w.condition as string,
            tempHigh: w.temp_high as number,
            tempLow: w.temp_low as number,
            suggestion: w.suggestion as string,
          }
        })()
      : undefined

    return {
      dayIndex: day.day_index as number,
      date: day.date as string | undefined,
      clusterId: day.cluster_id as number,
      slots,
      weatherSummary: weather,
    } satisfies DayPlan
  })

  return {
    itineraryId: typeof raw.itinerary_id === 'string'
      ? raw.itinerary_id
      : 'current-collaboration-itinerary',
    threadId: typeof raw.thread_id === 'string' ? raw.thread_id : 'collaboration',
    city: raw.city as string,
    days,
    generatedAt: raw.generated_at as string,
    version: typeof raw.version === 'number' ? raw.version : 1,
  }
}
