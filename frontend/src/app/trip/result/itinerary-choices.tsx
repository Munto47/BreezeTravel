'use client'

import {useState} from 'react'
import type {TripUnderstandingCommand, UserFacingTripResult} from '@/lib/trip-understanding-v3'

type Day = UserFacingTripResult['days'][number]
type Alternative = NonNullable<Day['alternatives']>[number]
type Props = {day: Day; dayIndex: number; disabled: boolean; onApply: (command: TripUnderstandingCommand) => Promise<void>}
const action = 'min-h-11 rounded-xl px-3 text-sm font-medium text-sky-800 hover:bg-sky-50 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600'

function ChoiceGroup({day, dayIndex, disabled, onApply, token, members}: Props & {token: string; members: Alternative[]}) {
  const branches = new Map<string, Alternative[]>()
  for (const member of members) {
    if (!member.branch_token) continue
    branches.set(member.branch_token, [...(branches.get(member.branch_token) || []), member])
  }
  const suggested = members[0].insertion_position
  const validSuggestion = Number.isInteger(suggested) && suggested! >= 0 && suggested! <= day.activities.length &&
    members.every(member => member.insertion_position === suggested)
  const [position, setPosition] = useState(validSuggestion ? String(suggested) : '')
  const selection = day.choice_selections?.find(item => item.choice_group_token === token)
  const incomplete = branches.size < 2 || members.some(member => !member.branch_token)
  const selectable = members.every(member => member.choice_group_selectable === true)
  const needsConfirmation = selection?.activity_tokens.some(value => !day.activities.some(card => card.activity_token === value && card.status === 'READY'))
  return <section className="my-3 rounded-2xl border border-sky-100 bg-sky-50/40 p-3" data-testid="itinerary-choice-group">
    <h3 className="text-sm font-semibold">{incomplete ? '待补全的备选方案' : branches.size === 2 ? '二选一' : `从 ${branches.size} 个方案中选择一个`}</h3>
    {incomplete ? <p className="mt-2 text-sm text-slate-600">这组备选的信息尚未完整，请先补全。</p> :
      selection ? <p className="mt-2 text-sm text-slate-600" role="status">{selection.status === 'MODIFIED'
        ? '已调整的地点会保留为独立安排，解除关联后可重新选择方案。'
        : needsConfirmation ? '已选方案加入行程，地点仍需确认。' : '所选方案已加入行程。'}</p> : !selectable ?
        <p className="mt-2 text-sm text-slate-600">这组方案需要逐项确认，暂时不能整组加入。</p> :
        <label className="my-2 block text-sm">加入位置
          <select aria-label="方案加入位置" className="mt-1 min-h-11 w-full rounded-xl bg-white px-2" disabled={disabled}
            value={position} onChange={event => setPosition(event.target.value)}>
            <option value="" disabled>请选择加入位置</option>
            <option value="0">当天最前</option>
            {day.activities.map((card, index) => <option key={card.activity_token} value={index + 1}>{card.name}之后</option>)}
          </select>
        </label>}
    {[...branches].map(([branchToken, items]) => {
      const label = items[0].branch_label || items.map(item => item.name).join('、')
      const selected = selection?.branch_token === branchToken
      return <div key={branchToken} className="mt-2 rounded-xl bg-white p-2" data-testid="itinerary-choice-branch">
        <p className="text-sm font-medium">{label}{selected ? ' · 已选择' : ''}</p>
        <ul className="mt-1 text-sm text-slate-600">{items.map((item, index) => <li key={index}>{item.name}{item.city ? ` · ${item.city}` : ''}</li>)}</ul>
        <button className={action} disabled={disabled || incomplete || !selectable || !!selection || position === ''}
          onClick={() => void onApply({command_type: 'CHOICE_SELECT', day_index: dayIndex + 1,
            choice_group_token: token, branch_token: branchToken, position: Number(position)})}>
          {selection ? selected ? '已选择此方案' : '未选择此方案' : `选择方案：${label}`}
        </button>
        {!selectable && !selection && items.map((item, index) => <button key={index} className={action} disabled={disabled}
          onClick={() => void onApply({command_type: 'ACTIVITY_INSERT', day_index: dayIndex + 1,
            position: day.activities.length, name: item.name, category: item.category, city: item.city})}>
          加入待确认：{item.name}
        </button>)}
      </div>
    })}
    {members.filter(member => !member.branch_token).map((member, index) => <p key={index} className="mt-2 text-sm">{member.name}</p>)}
    {selection?.status === 'SELECTED' && <button className={action} disabled={disabled}
      onClick={() => void onApply({command_type: 'CHOICE_CLEAR', day_index: dayIndex + 1, choice_group_token: token})}>
      撤销方案选择
    </button>}
    {selection?.status === 'MODIFIED' && <button className={action} disabled={disabled}
      onClick={() => void onApply({command_type: 'CHOICE_CLEAR', day_index: dayIndex + 1,
        choice_group_token: token, preserve_activities: true})}>保留地点，解除方案关联</button>}
  </section>
}

export default function ItineraryChoices(props: Props) {
  const groups = new Map<string, Alternative[]>()
  const ungrouped: Alternative[] = []
  for (const item of props.day.alternatives || []) {
    if (item.choice_group_token) groups.set(item.choice_group_token, [...(groups.get(item.choice_group_token) || []), item])
    else ungrouped.push(item)
  }
  return <>
    {[...groups].map(([token, members]) => <ChoiceGroup key={token} {...props} token={token} members={members}/>)}
    {ungrouped.map((item, index) => <div key={index} className="flex items-center justify-between gap-2 py-1 text-sm">
      <span>{item.branch_label && <span className="mr-2 text-xs text-slate-500">{item.branch_label}</span>}{item.name}{item.city ? ` · ${item.city}` : ''}</span>
      <button className={action} disabled={props.disabled} onClick={() => void props.onApply({command_type: 'ACTIVITY_INSERT',
        day_index: props.dayIndex + 1, position: props.day.activities.length, name: item.name, category: item.category, city: item.city})}>加入待确认</button>
    </div>)}
  </>
}
