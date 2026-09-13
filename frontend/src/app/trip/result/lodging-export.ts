import type {SourceLodgingCard} from '@/lib/confirmed-trip-view'
import type {StayCandidateView, StaySuggestionView, UserFacingTripResult} from '@/lib/trip-understanding-v3'

type ExportLodging = {
  name: string; city: string; address: string; nights: number[]; unknownNights: boolean
  origins: string[]; notes: string[]
}

function validNights(nights: number[]) {
  // Persisted public constraints allow 1..13 even when a later itinerary day
  // is absent. Keep that explicit night; do not invent its next-day boundary.
  return [...new Set(nights.filter(night => Number.isInteger(night) && night > 0 && night <= 13))].sort((a, b) => a - b)
}

function segmentNights(labels: string[], days: UserFacingTripResult['days']) {
  const values = labels.map(label => {
    const matches = days.flatMap((day, index) => day.label === label ? [index + 1] : [])
    if (matches.length === 1) return matches[0]
    const relative = /^Day\s*(\d+)$/i.exec(label.trim())
    return relative ? Number(relative[1]) : NaN
  })
  const nights = validNights(values)
  return {nights, unknownNights: !values.length || values.some(value => !nights.includes(value))}
}

function commuteNote(candidate: StayCandidateView, status: StaySuggestionView['status']) {
  if (status === 'NEEDS_UPDATE') return '住宿建议需要更新；已选酒店保留，通勤尚需核对。'
  if (status === 'PREPARING') return '住宿建议仍在准备，通勤尚需核对。'
  if (status === 'LIMITED') return '住宿通勤仅有部分资料，尚未完整核实。'
  if (status === 'UNAVAILABLE') return '住宿通勤资料暂不可用。'
  if (candidate.max_single_leg_minutes == null || !Number.isFinite(candidate.max_single_leg_minutes)) return '住宿通勤时间尚未完整核实。'
  return candidate.commute_summary || '住宿通勤时间尚未完整核实。'
}

/** Display only: selection is not a new mainline visit or a booking command. */
export function lodgingExportLines(result: UserFacingTripResult, sourceLodgings: SourceLodgingCard[] = []) {
  const entries: ExportLodging[] = []
  const source = [...new Map([...sourceLodgings, ...(result.lodging_constraints || [])]
    .map(card => [card.activity_token, card])).values()]
  for (const card of source) {
    const declared = card.overnight_days?.length ? card.overnight_days :
      card.scope === 'WHOLE_TRIP' || card.lodging_scope === 'WHOLE_TRIP'
        ? Array.from({length: Math.max(0, result.days.length - 1)}, (_, index) => index + 1) : []
    const nights = validNights(declared)
    entries.push({name: card.name, city: card.city || '', address: card.area_or_address || '', nights,
      unknownNights: !declared.length || declared.some(night => !nights.includes(night)),
      origins: ['原文住宿'], notes: [card.status === 'READY' ? '原文地点已确认。' : '原文住宿地点待确认。',
        ...(card.source_details || []).map(detail => `${detail.name}${detail.optional ? '（备选）' : ''}`)]})
  }

  const selected = new Map<string, ExportLodging>()
  const segments = result.stay.segments?.length ? result.stay.segments : [{
    city: null, overnight_days: [], status: result.stay.status, candidates: result.stay.candidates,
  }]
  for (const segment of segments) {
    for (const candidate of segment.candidates.filter(candidate => candidate.selected)) {
      const scope = segmentNights(segment.overnight_days, result.days)
      const status = result.stay.status === 'NEEDS_UPDATE' ? 'NEEDS_UPDATE' : segment.status
      const entry = selected.get(candidate.candidate_token)
      if (entry) {
        entry.nights = [...new Set([...entry.nights, ...scope.nights])].sort((a, b) => a - b)
        entry.unknownNights ||= scope.unknownNights
        entry.notes = [...new Set([...entry.notes, commuteNote(candidate, status)])]
      } else selected.set(candidate.candidate_token, {name: candidate.name, city: segment.city || '',
        address: candidate.area_or_address || '', ...scope, origins: ['建议已选择'], notes: [commuteNote(candidate, status)]})
    }
  }
  for (const entry of selected.values()) {
    // This only combines identical display records. It does not establish a
    // shared physical identity or resolve conflicting addresses/night scopes.
    const same = entries.find(source => source.name === entry.name && source.city && source.city === entry.city
      && source.address && source.address === entry.address && source.unknownNights === entry.unknownNights
      && source.nights.join(',') === entry.nights.join(','))
    if (same) {
      same.origins.push(...entry.origins)
      same.notes = [...new Set([...same.notes, ...entry.notes])]
    } else entries.push(entry)
  }
  return entries.flatMap(entry => {
    const nights = entry.nights.map(night => night < result.days.length ? `第 ${night} 晚（Day ${night} → Day ${night + 1}）`
      : night === result.days.length ? `第 ${night} 晚（Day ${night} 之后；次日行程未提供）` : `第 ${night} 晚（相邻日行程未提供）`)
    if (entry.unknownNights) nights.push(entry.nights.length ? '另有夜晚所属日期待确认' : '具体夜晚未指定')
    return [{text: `${entry.name}${entry.city ? ` · ${entry.city}` : ''} · ${nights.join('、')}`, heading: true},
      {text: entry.origins.join(' / '), heading: false},
      ...(entry.address ? [{text: entry.address, heading: false}] : []),
      ...entry.notes.map(text => ({text, heading: false}))]
  })
}
