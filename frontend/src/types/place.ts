export type PlaceSource = 'amap_poi' | 'rag' | 'synthesized'
export type PlaceCategory = 'attraction' | 'food' | 'hotel' | 'transport'

export interface Coordinates {
  lng: number
  lat: number
}

export interface PlaceRAGMeta {
  tipSnippets: string[]       // 从游记提取的避坑/推荐语，最多3条
  sentimentScore: number      // -1 ~ 1
  sourceNoteIds: string[]     // 支撑该内容的游记文档 ID（可溯源）
}

/** Phase B：替代方案 */
export interface PlaceAlternative {
  placeId: string
  name: string
  whyAlternative: string      // "比 A 更便宜 / 排队少 / 更适合带娃"
}

/** Phase B：结构化推荐（地点卡背面展示） */
export interface PlaceRecommendation {
  placeId: string
  name: string
  categoryL1: string
  categoryL2: string
  reason: string              // 推荐理由（引自游记，可为空）
  suitableFor: string[]       // 适合人群
  avoidTips: string[]         // 避坑提示
  sourceChunkIds: string[]    // 游记 chunk 来源
  alternatives: PlaceAlternative[]
  confidence: 'high' | 'medium' | 'low'
}

export interface Place {
  placeId: string
  name: string
  category: PlaceCategory
  address: string
  coords: Coordinates
  city: string
  district?: string
  source: PlaceSource

  // 高德客观数据
  amapRating?: number
  amapPrice?: number
  openingHours?: string
  phone?: string
  amapPhotos: string[]

  // RAG 主观数据
  ragMeta?: PlaceRAGMeta

  // AI 生成
  description?: string
  tags: string[]

  // Optimizer
  clusterId?: number
  visitOrder?: number
  estimatedDuration?: number

  // Phase B：结构化推荐（可选，由 Synthesizer v2 填入）
  recommendation?: PlaceRecommendation
}

/** 将后端蛇形命名转换为前端驼峰命名 */
export function parsePlaceFromAPI(raw: Record<string, unknown>): Place {
  const rec = raw.recommendation as Record<string, unknown> | undefined
  return {
    placeId: raw.place_id as string,
    name: raw.name as string,
    category: raw.category as PlaceCategory,
    address: raw.address as string,
    coords: raw.coords as Coordinates,
    city: raw.city as string,
    district: raw.district as string | undefined,
    source: (raw.source as PlaceSource) || 'synthesized',
    amapRating: raw.amap_rating as number | undefined,
    amapPrice: raw.amap_price as number | undefined,
    openingHours: raw.opening_hours as string | undefined,
    phone: raw.phone as string | undefined,
    amapPhotos: (raw.amap_photos as string[]) || [],
    ragMeta: raw.rag_meta
      ? {
          tipSnippets: (raw.rag_meta as Record<string, unknown>).tip_snippets as string[],
          sentimentScore: (raw.rag_meta as Record<string, unknown>).sentiment_score as number,
          sourceNoteIds: (raw.rag_meta as Record<string, unknown>).source_note_ids as string[],
        }
      : undefined,
    description: raw.description as string | undefined,
    tags: (raw.tags as string[]) || [],
    clusterId: raw.cluster_id as number | undefined,
    visitOrder: raw.visit_order as number | undefined,
    estimatedDuration: raw.estimated_duration as number | undefined,
    recommendation: rec
      ? {
          placeId: rec.place_id as string,
          name: rec.name as string,
          categoryL1: (rec.category_l1 as string) || '',
          categoryL2: (rec.category_l2 as string) || '',
          reason: (rec.reason as string) || '',
          suitableFor: (rec.suitable_for as string[]) || [],
          avoidTips: (rec.avoid_tips as string[]) || [],
          sourceChunkIds: (rec.source_chunk_ids as string[]) || [],
          alternatives: ((rec.alternatives as Record<string, unknown>[]) || []).map((a) => ({
            placeId: a.place_id as string,
            name: a.name as string,
            whyAlternative: (a.why_alternative as string) || '',
          })),
          confidence: (rec.confidence as 'high' | 'medium' | 'low') || 'low',
        }
      : undefined,
  }
}
