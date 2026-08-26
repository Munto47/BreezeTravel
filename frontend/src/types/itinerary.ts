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

/** 将后端蛇形命名（API 响应）转换为前端驼峰命名 */
export function parseItineraryFromAPI(raw: Record<string, unknown>): Itinerary {
  const days = (raw.days as Record<string, unknown>[]).map((day) => {
    const slots = (day.slots as Record<string, unknown>[]).map((slot) => {
      const transport = slot.transport
        ? (() => {
            const t = slot.transport as Record<string, unknown>
            return {
              mode: t.mode as TransportLeg['mode'],
              durationMins: t.duration_mins as number,
              distanceKm: t.distance_km as number,
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
    itineraryId: raw.itinerary_id as string,
    threadId: raw.thread_id as string,
    city: raw.city as string,
    days,
    generatedAt: raw.generated_at as string,
    version: raw.version as number,
  }
}
