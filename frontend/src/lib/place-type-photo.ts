import type { ActivityCardView } from './trip-understanding-v3'

export type PlacePhotoType = 'restaurant' | 'hotel' | 'historic' | 'modern' | 'mountain' | 'water' | 'park' | 'museum' | 'street'
export const PLACE_PHOTO_LABELS: Record<PlacePhotoType, string> = {
  restaurant: '餐饮', hotel: '酒店', historic: '古建筑', modern: '现代建筑',
  mountain: '山岳', water: '水景', park: '园林公园', museum: '展馆', street: '街道',
}

export function placePhotoType(card: Pick<ActivityCardView, 'name' | 'category'>): PlacePhotoType | null {
  const name = card.name.replace(/\s/g, '')
  // Resolved business category takes precedence over words in business names:
  // a lakefront hotel or a restaurant called "Mountain" is not a landscape.
  if (/住宿|酒店|宾馆|旅馆|民宿/.test(card.category)) return 'hotel'
  if (/餐饮|餐厅|饭店|咖啡|小吃/.test(card.category)) return 'restaurant'
  if (/酒店|宾馆|旅馆|民宿/.test(name)) return 'hotel'
  if (/(?:路|街|巷|胡同)(?:步行街|街区|商圈)?$/.test(name)) return 'street'
  if (/故宫|天坛|雍和宫|颐和园|圆明园|长城|古建|古镇|古城|会址|祠堂|寺庙|寺$|寺院|宫殿|庙$|城墙/.test(name)) return 'historic'
  if (/博物馆|博物院|美术馆|艺术馆|纪念馆|展览馆|科技馆/.test(name)) return 'museum'
  if (/CCTV|央视|大裤衩|中国尊|大厦|大楼|摩天|商城|购物中心|金融中心|商务中心|写字楼|环球中心|上海中心|东方明珠|国贸|体育场|体育馆/i.test(name)) return 'modern'
  if (/(?:山|山脉|山峰|峰|峡谷|山风景区|山景区)$/.test(name)) return 'mountain'
  if (/滨江|滨河|滨海|湖畔|河畔|海滩|沙滩|海岸|瀑布|水库|(?:湖|江|河|海|泉)$/.test(name)) return 'water'
  if (/公园|植物园|园林|花园|豫园|绿地/.test(name)) return 'park'
  // With no reliable subtype, retain the neutral artwork. Do not turn an
  // unfamiliar building, bridge or attraction into an arbitrary landscape.
  return null
}

export function placeTypePhoto(card: Pick<ActivityCardView, 'name' | 'category'>) {
  const type = placePhotoType(card)
  return type ? { type, src: `/place-types/${type}.jpg`, label: PLACE_PHOTO_LABELS[type] } : null
}
