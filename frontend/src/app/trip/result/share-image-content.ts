import type {ActivityCardView, MapRenderView, StaySuggestionView, TripSupplementaryView, UserFacingTripResult} from '@/lib/trip-understanding-v3'
import {diningAccessLines} from './dining-access'
import {sourceMeals} from './source-meals'
import {connectorPresentation, dayRouteSummary, transportConnectorFor} from './result-presentation'

export type ShareImageContentOptions = {
  unresolvedDays?: UserFacingTripResult['days']
  sourceMealDescriptions?: string[][]
  supplementary?: TripSupplementaryView | null
  mapView?: MapRenderView | null
}

export function exportResultWithStay(result: UserFacingTripResult, latest?: StaySuggestionView | null): UserFacingTripResult {
  if (!latest) return result
  const candidates = [...latest.candidates, ...(latest.segments || []).flatMap(segment => segment.candidates)]
  const stay = ['AVAILABLE', 'LIMITED'].includes(latest.status) || candidates.some(candidate => candidate.selected)
    ? latest : result.stay
  const update = (candidate: StaySuggestionView['candidates'][number]) => {
    const detail = candidates.find(item => item.candidate_token === candidate.candidate_token)
    return detail ? {...candidate, ...detail, selected: candidate.selected} : candidate
  }
  return {...result, stay: {...stay, status: latest.status,
    candidates: stay.candidates.map(update),
    segments: stay.segments?.map(segment => ({...segment,
      status: ['NEEDS_UPDATE', 'PREPARING', 'UNAVAILABLE'].includes(latest.status) ? latest.status :
        latest.segments?.find(item => item.segment_token === segment.segment_token)?.status || latest.status,
      candidates: segment.candidates.map(update),
    })),
  }}
}

export function exportStatus(result: UserFacingTripResult, options: ShareImageContentOptions = {}) {
  const pending = Math.max(result.coverage?.unresolved_place_count || 0,
    result.days.reduce((sum, day) => sum + day.activities.filter(card => card.status !== 'READY').length, 0),
    options.unresolvedDays?.reduce((sum, day) => sum + day.activities.length, 0) || 0)
  const messages: string[] = []
  if (result.coverage?.complete === false || result.status === 'PARTIAL_RESULT' ||
    (result.coverage?.unprocessed_count || 0) > 0 || (result.coverage?.unclassified_mention_count || 0) > 0) {
    messages.push('原文尚未完整整理，请返回行程补全。')
  }
  if (pending > 0) messages.push(`待确认地点：${pending} 处，请返回行程确认。`)
  const mapStatus = options.mapView?.status || result.map.status
  if (mapStatus === 'NEEDS_UPDATE') messages.push('路线需要更新；旧路线不代表当前安排。')
  else if (mapStatus === 'PREPARING') messages.push('路线仍在准备，交通尚未完整核实。')
  else if (mapStatus === 'LIMITED') messages.push('交通仅有部分资料，尚未完整核实。')
  else if (mapStatus === 'UNAVAILABLE') messages.push('路线暂不可用，交通尚未核实。')
  if (options.supplementary?.status === 'DELETED') messages.push('补充安排已删除。')
  else if (options.supplementary?.status === 'UNAVAILABLE') messages.push('补充安排暂时无法读取，本图可能缺少原文备选或取消内容。')
  if (result.pending_lodgings?.length) messages.push(`原文住宿待确认：${result.pending_lodgings.length} 处，夜晚及地点仍需核对。`)
  return messages
}

function details(card: Pick<ActivityCardView, 'source_details'>) {
  if (!card.source_details?.length) return []
  return ['原文内部安排（地点身份与开放情况未单独核验）：',
    ...card.source_details.map((detail, index) => `${index + 1}. ${detail.name}${detail.optional ? '（备选）' : ''}`)]
}

function sourceNoteContainsSupplement(day: UserFacingTripResult['days'][number], item: TripSupplementaryView['days'][number]['items'][number]) {
  if (!item.source_note_id) return false
  return (day.source_notes || []).some(note => {
    if (note.note_id !== item.source_note_id) return false
    const label = item.role === 'EXCLUDED' ? /^已取消：/ : /^(.*? · )?(?:备选用餐|原文备选)：/
    return label.test(note.text) && note.text.replace(label, item.role === 'EXCLUDED' ? '' : '$1') === item.name
  })
}

function alternativeContainsSupplement(day: UserFacingTripResult['days'][number], item: TripSupplementaryView['days'][number]['items'][number]) {
  if (item.role !== 'OPTIONAL' || !item.source_alternative_id) return false
  const alternatives = (day.alternatives || []).filter(card => card.alternative_id === item.source_alternative_id)
  return alternatives.length === 1 && alternatives[0].name === item.name
}

export function shareDayNotes(result: UserFacingTripResult, index: number, options: ShareImageContentOptions = {}) {
  const day = result.days[index]
  const notes: string[] = []
  day.activities.forEach((card, position) => {
    const content = [card.note || '', ...details(card), ...diningAccessLines(card)].filter(Boolean)
    if (content.length) notes.push(`第 ${position + 1} 站 · ${card.name}`, ...content)
  })
  notes.push(...(day.source_notes || []).map(note => note.text))
  for (const card of day.alternatives || []) {
    const selected = day.choice_selections?.find(selection => selection.branch_token === card.branch_token &&
      selection.choice_group_token === card.choice_group_token)
    const label = selected?.status === 'MODIFIED' ? '已调整，原方案供对照' : selected ? '已选方案' : '备选'
    notes.push(`${label}${card.branch_label ? ` · ${card.branch_label}` : ''}：${card.name}` +
      `${card.replaces_name ? `（${card.replacement_condition || '条件满足时'}替换${card.replaces_name}）` : card.replacement_condition ? `（${card.replacement_condition}）` : ''}` +
      `${card.semantic_review === 'PENDING' ? ' · 安排待复核' : ''}`, ...details(card))
  }
  notes.push(...(options.sourceMealDescriptions?.[index] || sourceMeals(day)))
  const pending = new Map([...day.activities.filter(card => card.status !== 'READY'),
    ...(options.unresolvedDays?.[index]?.activities || [])].map(card => [card.activity_token, card]))
  if (pending.size) notes.push(`待确认地点：${pending.size} 处`, ...[...pending.values()]
    .filter(card => !day.activities.some(item => item.activity_token === card.activity_token))
    .map(card => `${card.name} · 地点待确认`))
  if (day.unprocessed_count) notes.push(`原文未整理：${day.unprocessed_count} 处`)
  if (options.supplementary?.status === 'AVAILABLE') {
    for (const group of options.supplementary.days.filter(group => group.day_index === index + 1))
      notes.push(...group.items.filter(item => !sourceNoteContainsSupplement(day, item) && !alternativeContainsSupplement(day, item))
        .map(item => `${item.role === 'EXCLUDED' ? '原文已取消' : '原文备选'}：${item.name}`))
  }
  if (options.mapView && day.activities.length > 1) {
    const summary = dayRouteSummary(day, options.mapView)
    if (summary) notes.push(`交通：${summary}`)
    if (['AVAILABLE', 'LIMITED'].includes(options.mapView.status)) {
      day.activities.slice(0, -1).forEach((card, position) => {
        const next = day.activities[position + 1]
        const connector = connectorPresentation(transportConnectorFor(day, card, next, options.mapView!, false))
        notes.push(`${card.name} → ${next.name}：${connector.label}${connector.warning ? ` · ${connector.warning}` : ''}`)
      })
    }
  }
  return notes
}

export function unassignedShareNotes(result: UserFacingTripResult, supplementary?: TripSupplementaryView | null) {
  if (supplementary?.status !== 'AVAILABLE') return []
  return supplementary.days.filter(group => group.day_index == null || group.day_index < 1 || group.day_index > result.days.length)
    .flatMap(group => group.items.filter(item => {
      if (group.day_index != null || item.role !== 'EXCLUDED' || !item.source_note_id) return true
      const notes = result.days.flatMap(day => day.source_notes || []).filter(note => note.note_id === item.source_note_id)
      return notes.length !== 1 || notes[0].text !== `已取消（原文未指定 Day）：${item.name}`
    }).map(item => `${item.role === 'EXCLUDED' ? '原文已取消' : '原文备选'}：${item.name}`))
}
