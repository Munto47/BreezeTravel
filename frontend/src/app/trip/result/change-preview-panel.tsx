'use client'

import type {
  PublicChangePreview,
  UserFacingTripResult,
} from '@/lib/trip-understanding-v3'

export default function ChangePreviewPanel({
  preview,
  loading,
  stale,
  busy,
  checking,
  result,
  notice,
  onRetry,
  onAdopt,
  onCancel,
}: {
  preview: PublicChangePreview | null
  loading: boolean
  stale: boolean
  busy: boolean
  checking: boolean
  result: UserFacingTripResult
  notice: string
  onRetry: () => void
  onAdopt: () => void
  onCancel: () => void
}) {
  if (!preview)
    return (
      <div>
        <p role="status">
          {loading
            ? '正在准备调整预览…'
            : notice || '暂时没有可查看的调整预览。'}
        </p>
        <button className="e-button" onClick={onCancel}>
          返回行程
        </button>
      </div>
    )
  const changes = (preview.changes || []).filter(
    (change) => JSON.stringify(change.before) !== JSON.stringify(change.after),
  )
  const affected = result.days
    .filter((day) => preview.affected_days.includes(day.label))
    .flatMap((day) => day.activities)
  const unchanged = affected.filter(
    (card) =>
      !changes.some((change) => change.activity_token === card.activity_token),
  )
  const fields = [
    {
      key: 'start_time',
      label: '开始',
      format: (value: unknown) => value || '时间待定',
    },
    {
      key: 'end_time',
      label: '结束',
      format: (value: unknown) => value || '时间待定',
    },
    {
      key: 'visit_duration_minutes',
      label: '停留',
      format: (value: unknown) => (value == null ? '未安排' : `${value} 分钟`),
    },
    {
      key: 'locked',
      label: '时间锁定',
      format: (value: unknown) => (value ? '已锁定' : '未锁定'),
    },
  ] as const
  return (
    <div className="e-change-preview" data-testid="change-preview">
      <p className="e-preview-state" role="status">
        {stale
          ? '预览已失效 · 行程或路线依据已有变化'
          : '预览中 · 当前行程尚未改变'}
      </p>
      <h3>为什么调整</h3>
      <p>{preview.summary}</p>
      <p className="e-muted">
        影响日期：{preview.affected_days.join('、') || '以以下安排为准'}
      </p>
      {changes.length ? (
        <ol className="e-change-list">
          {changes.map((change) => (
            <li key={change.activity_token}>
              <p className="e-small e-muted">{change.day_label}</p>
              <h3>{change.name}</h3>
              <dl>
                {fields
                  .filter(
                    (field) =>
                      change.before[field.key] !== change.after[field.key],
                  )
                  .map((field) => (
                    <div className="e-change-field" key={field.key}>
                      <dt>{field.label}</dt>
                      <dd>
                        <span>
                          <small>当前</small>
                          {String(field.format(change.before[field.key]))}
                        </span>
                        <span className="e-preview-after">
                          <small>调整后</small>
                          {String(field.format(change.after[field.key]))}
                        </span>
                      </dd>
                    </div>
                  ))}
              </dl>
            </li>
          ))}
        </ol>
      ) : (
        <div className="e-preview-compare">
          <div>
            <h3>当前</h3>
            {preview.before.map((line, index) => (
              <p key={index}>{line}</p>
            ))}
          </div>
          <div>
            <h3>调整后</h3>
            {preview.after.map((line, index) => (
              <p key={index}>{line}</p>
            ))}
          </div>
        </div>
      )}
      {!!changes.length && !!unchanged.length && (
        <details className="e-disclosure">
          <summary>其余 {unchanged.length} 项不变</summary>
          <ul>
            {unchanged.map((card) => (
              <li key={card.activity_token}>{card.name}</li>
            ))}
          </ul>
        </details>
      )}
      <p className="e-notice-text">
        确认后整组应用。路线需要更新时，请主动更新，再检查交通与时间。
      </p>
      {notice && (
        <p role="status" className="e-notice-text">
          {notice}
        </p>
      )}
      <div className="e-panel-actions">
        <button
          type="button"
          className="e-button"
          disabled={busy}
          onClick={onCancel}
        >
          取消
        </button>
        {stale ? (
          <button
            type="button"
            className="e-button e-button-primary"
            disabled={busy || checking || loading}
            onClick={onRetry}
          >
            {checking || loading ? '正在重新预览…' : '重新预览'}
          </button>
        ) : (
          <button
            type="button"
            className="e-button e-button-primary"
            data-testid="adopt-change"
            disabled={busy || loading}
            onClick={onAdopt}
          >
            确认采纳
          </button>
        )}
      </div>
    </div>
  )
}
