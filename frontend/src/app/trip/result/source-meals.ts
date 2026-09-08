import type {UserFacingTripResult} from '@/lib/trip-understanding-v3'

export function sourceMeals(day: UserFacingTripResult['days'][number]) {
  const labels = {BREAKFAST: '早餐', LUNCH: '午餐', DINNER: '晚餐', SNACK: '加餐', UNSPECIFIED: '用餐'}
  return (day.meal_slots || []).map(slot => {
    const label = labels[slot.meal_role]
    const preference = slot.preference_text ? `；用餐意向：${slot.preference_text}` : ''
    if (slot.selection_status === 'SELECTED') {
      const selected = day.activities.find(card => card.activity_token === slot.selected_activity_token)
      return (selected
        ? `${label} · ${selected.status === 'READY' ? '已安排' : '餐厅需确认'}：「${selected.name}」`
        : `${label} · 已选餐厅需确认`) + preference
    }
    const after = day.activities.findIndex(card => card.activity_token === slot.after_activity_token)
    const before = day.activities.findIndex(card => card.activity_token === slot.before_activity_token)
    // Position describes the source arrangement, never a restaurant selection.
    const hasPosition = (after >= 0 || before >= 0) && !(after >= 0 && before >= 0 && after >= before)
    const anchor = (index: number) => `「${day.activities[index].name}」${day.activities[index].status !== 'READY' ? '（地点待确认）' : ''}`
    const relation = hasPosition ? [
      after >= 0 ? `在${anchor(after)}之后` : '',
      before >= 0 ? `在${anchor(before)}之前` : '',
    ].filter(Boolean).join('，') : ''
    const status = slot.selection_status === 'UNSELECTED' ? `${label} · 餐厅待选择` : `原文有${label}安排`
    return `${status}${relation ? `（${relation}）` : ''}${preference}`
  })
}
