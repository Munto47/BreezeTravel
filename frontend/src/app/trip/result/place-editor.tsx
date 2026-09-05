'use client'

import { useEffect, useRef, useState } from 'react'
import { Search } from 'lucide-react'
import {
  queryTripPlaceCandidates,
  type ActivityCardView,
  type PlaceCandidatesView,
  type TripUnderstandingCommand,
  type UserFacingTripResult,
} from '@/lib/trip-understanding-v3'
import ExperienceDialog from './experience-dialog'

export default function PlaceEditor({
  card,
  dayIndex,
  days,
  resource,
  busy,
  saving,
  notice,
  onClose,
  onCommand,
}: {
  card: ActivityCardView | null
  dayIndex: number
  days: UserFacingTripResult['days']
  resource: string
  busy: boolean
  saving: boolean
  notice: string
  onClose: () => void
  onCommand: (command: TripUnderstandingCommand) => Promise<boolean>
}) {
  const [name, setName] = useState(card?.name || '')
  const [start, setStart] = useState(card?.start_time || '')
  const [end, setEnd] = useState(card?.end_time || '')
  const [duration, setDuration] = useState(
    card?.visit_duration_minutes?.toString() || '',
  )
  const [locked, setLocked] = useState(card?.locked || false)
  const [query, setQuery] = useState(card?.name || '')
  const [candidates, setCandidates] = useState<PlaceCandidatesView | null>(null)
  const [searching, setSearching] = useState(false)
  const [message, setMessage] = useState('')
  const [targetDay, setTargetDay] = useState(dayIndex)
  const [targetPosition, setTargetPosition] = useState(0)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const searchController = useRef<AbortController | null>(null)
  useEffect(() => () => searchController.current?.abort(), [])
  async function apply(command: TripUnderstandingCommand) {
    if (await onCommand(command)) onClose()
  }
  async function search() {
    if (!card || !query.trim()) return
    searchController.current?.abort()
    const controller = new AbortController()
    searchController.current = controller
    setSearching(true)
    setCandidates(null)
    setMessage('')
    const timer = setTimeout(() => controller.abort(), 15000)
    try {
      const next = await queryTripPlaceCandidates(
        resource,
        card.activity_token,
        query.trim(),
        controller.signal,
      )
      if (!controller.signal.aborted) {
        setCandidates(next)
        if (next.status !== 'AVAILABLE')
          setMessage(
            next.status === 'EMPTY'
              ? '没有找到合适的地点。试着加上区县或完整名称。'
              : '地点查询暂时不可用，可以稍后重试。',
          )
      }
    } catch {
      if (searchController.current === controller)
        setMessage('暂时未能查询地点，请稍后重试。')
    } finally {
      clearTimeout(timer)
      if (searchController.current === controller) setSearching(false)
    }
  }
  return (
    <ExperienceDialog
      title={card ? card.name : '新增地点'}
      onClose={onClose}
      busy={saving}
    >
      {notice && (
        <p className="e-message" role="status">
          {notice}
        </p>
      )}
      {card ? (
        <>
          <p className="e-muted">
            {card.area_or_address || '地址待确认'}
            {card.status !== 'READY' && ' · 地点待确认'}
          </p>
          <form
            onSubmit={(event) => {
              event.preventDefault()
              void apply({
                command_type: 'ACTIVITY_TIME_SET',
                activity_token: card.activity_token,
                start_time: start || null,
                end_time: end || null,
                visit_duration_minutes:
                  duration === '' ? null : Number(duration),
                locked,
              })
            }}
          >
            <div className="e-fields">
              <label className="e-field">
                开始时间
                <input
                  aria-label="开始时间"
                  type="time"
                  value={start}
                  onChange={(event) => setStart(event.target.value)}
                />
              </label>
              <label className="e-field">
                结束时间
                <input
                  aria-label="结束时间"
                  type="time"
                  value={end}
                  onChange={(event) => setEnd(event.target.value)}
                />
              </label>
            </div>
            <label className="e-field">
              预计停留（分钟）
              <input
                aria-label="预计停留分钟"
                type="number"
                min={0}
                max={1440}
                value={duration}
                placeholder="未安排"
                onChange={(event) => setDuration(event.target.value)}
              />
            </label>
            <label className="e-field-check">
              <input
                type="checkbox"
                checked={locked}
                onChange={(event) => setLocked(event.target.checked)}
              />
              <span>有预约或固定安排，建议不要自动移动时间</span>
            </label>
            <button
              className="e-button e-button-primary"
              type="submit"
              disabled={busy}
            >
              保存时间安排
            </button>
          </form>
          <section className="e-form-section" style={{ marginTop: 25 }}>
            <h3>确认或更换地点</h3>
            <form
              onSubmit={(event) => {
                event.preventDefault()
                void search()
              }}
            >
              <label className="e-field">
                地点名称
                <input
                  aria-label="搜索地点名称"
                  value={query}
                  onChange={(event) => {
                    setQuery(event.target.value)
                    setCandidates(null)
                  }}
                  maxLength={200}
                />
              </label>
              <button
                className="e-button"
                type="submit"
                disabled={busy || searching || !query.trim()}
              >
                <Search aria-hidden="true" />
                {searching ? '正在查询…' : '搜索真实地点'}
              </button>
            </form>
            {message && (
              <p className="e-notice-text" role="status">
                {message}
              </p>
            )}
            <ul className="e-candidates">
              {candidates?.candidates.map((candidate) => (
                <li key={candidate.candidate_token}>
                  <button
                    type="button"
                    className="e-candidate"
                    disabled={busy}
                    onClick={() =>
                      void apply({
                        command_type: 'PLACE_CONFIRM',
                        activity_token: card.activity_token,
                        candidate_token: candidate.candidate_token,
                      })
                    }
                  >
                    <strong>{candidate.name}</strong>
                    <span>
                      {candidate.area_or_address} · {candidate.category}
                    </span>
                    <span>选择这个地点 →</span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
          <section className="e-form-section">
            <h3>移动到其他位置</h3>
            <div className="e-fields">
              <label className="e-field">
                日期
                <select
                  value={targetDay}
                  onChange={(event) => {
                    setTargetDay(Number(event.target.value))
                    setTargetPosition(0)
                  }}
                >
                  {days.map((day, index) => (
                    <option value={index} key={index}>
                      {day.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="e-field">
                位置
                <select
                  value={targetPosition}
                  onChange={(event) =>
                    setTargetPosition(Number(event.target.value))
                  }
                >
                  {Array.from(
                    {
                      length:
                        (days[targetDay]?.activities.filter(
                          (item) => item.activity_token !== card.activity_token,
                        ).length || 0) + 1,
                    },
                    (_, index) => (
                      <option key={index} value={index}>
                        第 {index + 1} 站
                      </option>
                    ),
                  )}
                </select>
              </label>
            </div>
            <button
              className="e-button"
              type="button"
              disabled={busy}
              onClick={() =>
                void apply({
                  command_type: 'ACTIVITY_MOVE',
                  activity_token: card.activity_token,
                  target_day_index: targetDay + 1,
                  target_position: targetPosition,
                })
              }
            >
              移动地点
            </button>
          </section>
          {card.knowledge_suggestions?.length ? (
            <section className="e-form-section">
              <h3>出发前参考</h3>
              {card.knowledge_suggestions.map((item, index) => (
                <p className="e-notice-text" key={index}>
                  {item.text}{' '}
                  {/^https?:\/\//.test(item.source_url) && (
                    <a
                      href={item.source_url}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {item.source_name}
                    </a>
                  )}
                </p>
              ))}
            </section>
          ) : null}
          <section className="e-form-section">
            {confirmDelete ? (
              <>
                <p className="e-notice-text">
                  从行程中移除“{card.name}”？之后可以撤销上次修改。
                </p>
                <div className="e-actions">
                  <button
                    className="e-button"
                    type="button"
                    disabled={busy}
                    onClick={() => setConfirmDelete(false)}
                  >
                    保留
                  </button>
                  <button
                    className="e-button e-danger-button"
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void apply({
                        command_type: 'ACTIVITY_DELETE',
                        activity_token: card.activity_token,
                      })
                    }
                  >
                    确认移除
                  </button>
                </div>
              </>
            ) : (
              <button
                type="button"
                className="e-button e-danger-button"
                disabled={busy}
                onClick={() => setConfirmDelete(true)}
              >
                从行程中移除
              </button>
            )}
          </section>
        </>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault()
            if (name.trim())
              void apply({
                command_type: 'ACTIVITY_INSERT',
                day_index: dayIndex + 1,
                position: days[dayIndex]?.activities.length || 0,
                name: name.trim(),
              })
          }}
        >
          <label className="e-field">
            地点名称
            <input
              aria-label="新增地点名称"
              value={name}
              onChange={(event) => setName(event.target.value)}
              required
              maxLength={200}
              placeholder="输入想去的地方"
            />
          </label>
          <p className="e-notice-text">
            新增后可以搜索真实地点、确认地址，再补充时间。
          </p>
          <button
            className="e-button e-button-primary"
            disabled={busy || !name.trim()}
            type="submit"
          >
            加入当天行程
          </button>
        </form>
      )}
    </ExperienceDialog>
  )
}
