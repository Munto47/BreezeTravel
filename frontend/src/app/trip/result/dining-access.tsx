import type {DiningContextView} from '@/lib/trip-understanding-v3'
import './dining-access.css'

type MealRole = 'BREAKFAST' | 'LUNCH' | 'DINNER' | 'SNACK' | 'UNSPECIFIED' | null
const regularMeals = new Set<MealRole>(['BREAKFAST', 'LUNCH', 'DINNER'])

export function diningAccessLines(value: DiningContextView, showUnknown = false): string[] {
  const access = value.dining_access
  const lines: string[] = []
  if (access) {
    lines.push(access.parent_name ? `场所归属：${access.parent_name}` : '场所归属名称尚未确认。')
    lines.push(access.status === 'DURING_VISIT'
      ? `在本次${access.parent_name ? `到访「${access.parent_name}」` : '场所到访'}期间、离开前用餐。`
      : '用餐位置与本次场所到访的关联需要核对。')
    lines.push('进入条件、可用门区与营业情况尚未核实，不表示已具备入内资格。')
  } else if (showUnknown) {
    lines.push('场所归属、进入条件与营业情况尚未核实。')
  }
  if (value.meal_evidence_status === 'LIGHT_FOOD_ITEMS_ONLY') {
    lines.push('当前资料仅有茶饮点心，正餐供应尚未核实。')
  }
  return lines
}

export function diningAdoptionBlocked(value: DiningContextView, mealRole?: MealRole): boolean {
  return value.dining_access?.status === 'NEEDS_REVIEW'
    || (value.meal_evidence_status === 'LIGHT_FOOD_ITEMS_ONLY' && !!mealRole && regularMeals.has(mealRole))
}

export function diningAdoptionLabel(value: DiningContextView, fallback: string, mealRole?: MealRole): string {
  if (value.dining_access?.status === 'NEEDS_REVIEW') return '用餐位置需核对'
  if (value.meal_evidence_status === 'LIGHT_FOOD_ITEMS_ONLY' && !!mealRole && regularMeals.has(mealRole)) return '正餐供应待核实'
  if (value.dining_access?.status === 'DURING_VISIT') {
    return value.dining_access.parent_name ? `在「${value.dining_access.parent_name}」到访期间用餐` : '在本次到访期间用餐'
  }
  return fallback
}

export function diningAccessBadge(value: DiningContextView): string | null {
  if (value.dining_access?.status === 'NEEDS_REVIEW') return '用餐位置需核对'
  if (value.meal_evidence_status === 'LIGHT_FOOD_ITEMS_ONLY') return '正餐供应待核'
  return value.dining_access ? '用餐条件' : null
}

export default function DiningAccessNote({value, showUnknown = false}: {value: DiningContextView; showUnknown?: boolean}) {
  const lines = diningAccessLines(value, showUnknown)
  if (!lines.length) return null
  return <section className="dining-access-note" data-testid="dining-access-note" aria-label="餐饮访问与用途说明">
    <h4>{value.dining_access?.status === 'NEEDS_REVIEW' ? '用餐位置需核对' : '用餐前请确认'}</h4>
    {lines.map((line, index) => <p key={index}>{line}</p>)}
  </section>
}
