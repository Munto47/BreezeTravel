'use client'

import { type ReactNode, useEffect, useRef, useState } from 'react'
import { Search } from 'lucide-react'
import {
  queryTripPlaceCandidates,
  type ActivityCardView,
  type PlaceCandidateView,
  type PlaceCandidatesView,
  type TripUnderstandingCommand,
  type UserFacingTripResult,
} from '@/lib/trip-understanding-v3'

export default function PlaceEditor({
  editorMode,
  card,
  dayIndex,
  days,
  resource,
  busy,
  notice,
  onCommand,
  onApplied,
  onDirtyChange,
  onPreviewCandidate,
  candidateMap,
}: {
  editorMode: 'ADD' | 'EDIT' | 'REPLACE'
  card: ActivityCardView | null
  dayIndex: number
  days: UserFacingTripResult['days']
  resource: string
  busy: boolean
  notice: string
  candidateMap?: ReactNode
  onCommand: (command: TripUnderstandingCommand) => Promise<boolean>
  onApplied: () => void
  onDirtyChange: (dirty: boolean) => void
  onPreviewCandidate: (candidate: PlaceCandidateView | null) => void
}) {
  const originalPosition =
    days[dayIndex]?.activities.findIndex(
      (item) => item.activity_token === card?.activity_token,
    ) ?? 0
  const [name, setName] = useState(card?.name || '')
  const [query, setQuery] = useState(card?.name || '')
  const [candidates, setCandidates] = useState<PlaceCandidatesView | null>(null)
  const [candidate, setCandidate] = useState<PlaceCandidateView | null>(null)
  const [searching, setSearching] = useState(false)
  const [message, setMessage] = useState('')
  const [targetDay, setTargetDay] = useState(dayIndex)
  const [targetPosition, setTargetPosition] = useState(
    Math.max(0, originalPosition),
  )
  const [confirmDelete, setConfirmDelete] = useState(false)
  const searchController = useRef<AbortController | null>(null)
  const moveDirty =
    targetDay !== dayIndex || targetPosition !== originalPosition
  const nameDirty = Boolean(name.trim()) && name.trim() !== (card?.name || '')
  const cardWasReplaced = editorMode !== 'ADD' && !card
  const dirty =
    cardWasReplaced
      ? false
      : editorMode === 'ADD'
      ? Boolean(name.trim())
      : editorMode === 'REPLACE'
        ? nameDirty
        : nameDirty || moveDirty || Boolean(candidate)
  useEffect(() => {
    onDirtyChange(dirty)
  }, [dirty, onDirtyChange])
  useEffect(
    () => () => {
      searchController.current?.abort()
      onPreviewCandidate(null)
      onDirtyChange(false)
    },
    [onPreviewCandidate, onDirtyChange],
  )
  function selectCandidate(value: PlaceCandidateView | null) {
    setCandidate(value)
    onPreviewCandidate(value)
  }
  async function search() {
    if (!card || !query.trim()) return
    searchController.current?.abort()
    const controller = new AbortController()
    searchController.current = controller
    setSearching(true)
    setCandidates(null)
    selectCandidate(null)
    setMessage('')
    const timer = setTimeout(() => controller.abort(), 15000)
    try {
      const next = await queryTripPlaceCandidates(
        resource,
        card.activity_token,
        query.trim(),
        controller.signal,
      )
      if (
        !controller.signal.aborted &&
        searchController.current === controller
      ) {
        setCandidates(next)
        if (next.status !== 'AVAILABLE')
          setMessage(
            next.status === 'EMPTY'
              ? '没有找到合适的地点。可补充区县或完整名称，原安排会保留。'
              : '地点查询暂时不可用，可保留待确认并稍后重试。',
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
  if (cardWasReplaced)
    return (
      <div className="e-editor">
        <p className="e-notice-text" role="status">
          这张卡片已经更新。请关闭编辑，再从最新行程重新打开。
        </p>
      </div>
    )
  if (editorMode === 'ADD' || !card)
    return (
      <form
        className="e-editor"
        onSubmit={(event) => {
          event.preventDefault()
          if (name.trim())
            void onCommand({
              command_type: 'ACTIVITY_INSERT',
              day_index: dayIndex + 1,
              position: days[dayIndex]?.activities.length || 0,
              name: name.trim(),
            }).then((ok) => {
              if (ok) onApplied()
            })
        }}
      >
        <p className="e-muted">
          加入{days[dayIndex]?.label}
          。新地点先保留为待确认，确认地址后再更新路线。
        </p>
        <label className="e-field">
          地点名称
          <input
            aria-label="新增地点名称"
            data-initial-focus="true"
            data-testid="card-editor-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
            maxLength={200}
            placeholder="输入想去的地方"
            disabled={busy}
          />
        </label>
        {notice && (
          <p role="status" className="e-notice-text">
            {notice}
          </p>
        )}
        <div className="e-panel-actions">
          <button
            className="e-button e-button-primary"
            data-testid="save-card-editor"
            disabled={busy || !name.trim()}
            type="submit"
          >
            加入当天行程
          </button>
        </div>
      </form>
    )

  const textEditor = (
    <form
      className="e-form-section"
      onSubmit={(event) => {
        event.preventDefault()
        if (!nameDirty || busy) return
        const command: TripUnderstandingCommand =
          editorMode === 'REPLACE'
            ? {
                command_type: 'PLACE_REPLACE',
                activity_token: card.activity_token,
                replacement: {
                  name: name.trim(),
                  category: card.category || '地点',
                  area_or_address: '地点待确认',
                },
              }
            : {
                command_type: 'ACTIVITY_TEXT_EDIT',
                activity_token: card.activity_token,
                name: name.trim(),
              }
        void onCommand(command).then((ok) => {
          if (ok) onApplied()
        })
      }}
    >
      <p className="e-muted">
        {editorMode === 'REPLACE'
          ? '新地点会先标为待确认；核对成功前不会沿用旧地点的路线事实。'
          : '只修改卡片文字；保存后现有路线会标记为需要更新。'}
      </p>
      <label className="e-field">
        {editorMode === 'REPLACE' ? '新地点名称' : '卡片文字'}
        <input
          aria-label={editorMode === 'REPLACE' ? '替换地点名称' : '编辑卡片名称'}
          data-initial-focus="true"
          data-testid="card-editor-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          required
          maxLength={40}
          disabled={busy}
        />
      </label>
      <div className="e-panel-actions">
        <button
          className="e-button e-button-primary"
          data-testid="save-card-editor"
          disabled={busy || !nameDirty}
          type="submit"
        >
          {busy
            ? '正在保存…'
            : editorMode === 'REPLACE'
              ? '确认替换'
              : '保存文字'}
        </button>
      </div>
    </form>
  )

  if (editorMode === 'REPLACE') return textEditor

  return (
    <div className="e-editor">
      {textEditor}
      <p className="e-muted">
        {days[dayIndex]?.label} · 第 {originalPosition + 1} 站
      </p>
      <p className="e-place-address">
        {card.area_or_address || '地址尚未确认'}
      </p>
      {card.status !== 'READY' && (
        <p className="e-confirmation">地点待确认，核对地址后再使用路线。</p>
      )}
      {notice && (
        <p className="e-notice-text" role="status">
          {notice}
        </p>
      )}
      <section className="e-form-section">
        <h3>确认或更换地点</h3>
        <form
          className="e-place-search"
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
                searchController.current?.abort()
                setSearching(false)
                setQuery(event.target.value)
                setCandidates(null)
                selectCandidate(null)
                setMessage('')
              }}
              maxLength={200}
              disabled={busy}
            />
          </label>
          <button
            className="e-button"
            type="submit"
            disabled={busy || searching || !query.trim()}
          >
            <Search aria-hidden="true" />
            {searching ? '正在查询…' : '搜索地点'}
          </button>
        </form>
        {message && (
          <p className="e-notice-text" role="status">
            {message}
          </p>
        )}
        {!!candidates?.candidates.length && (
          <>
            <p className="e-small e-muted">
              先选中比较；使用这个地点后才会修改行程。
            </p>
            <ul className="e-candidates">
              {candidates.candidates.map((item, index) => (
                <li key={item.candidate_token}>
                  <button
                    type="button"
                    className={`e-candidate${candidate?.candidate_token === item.candidate_token ? ' is-selected' : ''}`}
                    disabled={busy}
                    aria-pressed={
                      candidate?.candidate_token === item.candidate_token
                    }
                    onClick={() => selectCandidate(item)}
                  >
                    <span className="e-candidate-number">候选 {index + 1}</span>
                    <strong>{item.name}</strong>
                    <span>{item.area_or_address || '地址暂未提供'}</span>
                    <span>{item.category}</span>
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
        {candidate && (
          <div className="e-candidate-confirm">
            <p>
              已选中“{candidate.name}”，行程尚未改变。
              {candidate.position
                ? '地图显示该候选位置。'
                : '暂不能在地图定位。'}
            </p>
            <button
              className="e-button e-button-primary"
              type="button"
              disabled={busy}
              onClick={() => {
                void onCommand({
                  command_type: 'PLACE_CONFIRM',
                  activity_token: card.activity_token,
                  candidate_token: candidate.candidate_token,
                }).then((ok) => {
                  if (ok) {
                    selectCandidate(null)
                    setCandidates(null)
                    setMessage('地点已更新，需要时再更新路线。')
                  }
                })
              }}
            >
              使用这个地点
            </button>
            <button
              className="e-button e-button-quiet"
              type="button"
              disabled={busy}
              onClick={() => selectCandidate(null)}
            >
              取消选择
            </button>
            {candidateMap}
          </div>
        )}
      </section>
      {!!card.knowledge_suggestions?.length && (
        <details className="e-disclosure">
          <summary>有来源的出发前建议</summary>
          {card.knowledge_suggestions.map((item, index) => (
            <div className="e-reference" key={index}>
              <p>{item.text}</p>
              {/^https?:\/\//.test(item.source_url) && (
                <a
                  href={item.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {item.source_name}
                </a>
              )}
            </div>
          ))}
        </details>
      )}
      <details className="e-disclosure">
        <summary>移动或移除这个地点</summary>
        {(candidate) && (
          <p className="e-confirmation">
            先确认或取消地点选择。
          </p>
        )}
        <div className="e-fields">
          <label className="e-field">
            移至日期
            <select
              value={targetDay}
              disabled={busy || Boolean(candidate)}
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
              disabled={busy || Boolean(candidate)}
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
          disabled={busy || Boolean(candidate) || !moveDirty}
          onClick={() => {
            void onCommand({
              command_type: 'ACTIVITY_MOVE',
              activity_token: card.activity_token,
              target_day_index: targetDay + 1,
              target_position: targetPosition,
            }).then((ok) => {
              if (ok) onApplied()
            })
          }}
        >
          移动到此位置
        </button>
        {moveDirty && (
          <button
            type="button"
            className="e-button e-button-quiet"
            disabled={busy}
            onClick={() => {
              setTargetDay(dayIndex)
              setTargetPosition(originalPosition)
            }}
          >
            还原位置选择
          </button>
        )}
        {confirmDelete ? (
          <div className="e-inline-confirm">
            <p>从执行行程中移除“{card.name}”？之后可以撤销上次修改。</p>
            <div className="e-actions">
              <button
                className="e-button"
                type="button"
                disabled={busy}
                onClick={() => setConfirmDelete(false)}
              >
                保留地点
              </button>
              <button
                className="e-button e-danger-button"
                type="button"
                disabled={busy || Boolean(candidate)}
                onClick={() => {
                  void onCommand({
                    command_type: 'ACTIVITY_DELETE',
                    activity_token: card.activity_token,
                  }).then((ok) => {
                    if (ok) onApplied()
                  })
                }}
              >
                确认移除
              </button>
            </div>
          </div>
        ) : (
          <button
            type="button"
            className="e-button e-button-quiet"
            disabled={busy || Boolean(candidate)}
            onClick={() => setConfirmDelete(true)}
          >
            从行程中移除
          </button>
        )}
      </details>
    </div>
  )
}
