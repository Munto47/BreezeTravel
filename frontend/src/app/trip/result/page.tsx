'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import {
  ArrowDown,
  ArrowUp,
  ArrowUpRight,
  BusFront,
  Footprints,
  Plus,
  RefreshCw,
  Undo2,
} from 'lucide-react'
import {
  createTripShare,
  deleteTripUnderstanding,
  deleteTripUnderstandingSource,
  clearTripUnderstandingSession,
  type ActivityCardView,
  type AssumptionChipView,
  type PublicTripCheckItem,
} from '@/lib/trip-understanding-v3'
import { useAuthStore } from '@/stores/authStore'
import { useTripExperience, boundedTripRequest } from './use-trip-experience'
import RouteMap from './route-map'
import PlaceEditor from './place-editor'
import ExperienceDialog from './experience-dialog'
import '../../experience.css'

export default function TripResultPage() {
  const trip = useTripExperience()
  const router = useRouter()
  const { user, hydrate, logout } = useAuthStore()
  const [dayIndex, setDayIndex] = useState(0)
  const [selected, setSelected] = useState<string | null>(null)
  const [mobile, setMobile] = useState<'ITINERARY' | 'MAP'>('ITINERARY')
  const [routeMode, setRouteMode] = useState<
    'recommended' | 'walking' | 'transit'
  >('recommended')
  const [editor, setEditor] = useState<{
    card: ActivityCardView | null
    dayIndex: number
  } | null>(null)
  const [assumption, setAssumption] = useState<AssumptionChipView | null>(null)
  const [assumptionValue, setAssumptionValue] = useState('')
  const [privacy, setPrivacy] = useState<'SOURCE' | 'TRIP' | null>(null)
  const [privacyBusy, setPrivacyBusy] = useState(false)
  const [privacyError, setPrivacyError] = useState('')
  const [sourceDeleted, setSourceDeleted] = useState(false)
  const [share, setShare] = useState('')
  const [sharing, setSharing] = useState(false)
  useEffect(() => {
    hydrate()
    setSourceDeleted(
      sessionStorage.getItem('bt_active_trip_source_deleted') === 'true',
    )
  }, [hydrate])
  const result = trip.result
  const currentDay =
    result?.days[Math.min(dayIndex, (result?.days.length || 1) - 1)]
  const selectedCard =
    currentDay?.activities.find((card) => card.activity_token === selected) ||
    currentDay?.activities[0]
  const title =
    result?.assumptions.find((item) => item.key === 'destination')?.value ||
    '我的行程'
  const accountSaved =
    result?.ownership === 'ACCOUNT' || trip.mode === 'CLAIMED'
  const demo = trip.isDemo
  const disabled = trip.locked || privacyBusy
  const dayFindings =
    trip.checks?.items.filter(
      (item) =>
        !item.affected_days.length ||
        item.affected_days.includes(currentDay?.label || ''),
    ) || []

  async function saveToAccount() {
    if (!user) {
      sessionStorage.setItem('bt_login_return', '/trip/result')
      sessionStorage.setItem('bt_claim_after_login', 'true')
      router.push('/login')
      return
    }
    if (!accountSaved) await trip.claim()
  }
  useEffect(() => {
    if (
      user &&
      result &&
      !trip.busy &&
      !trip.pending &&
      !accountSaved &&
      sessionStorage.getItem('bt_claim_after_login') === 'true'
    ) {
      sessionStorage.removeItem('bt_claim_after_login')
      void trip.claim()
    }
  }, [user, result, trip, accountSaved])
  async function confirmDelete() {
    if (disabled || !trip.resource) return
    setPrivacyBusy(true)
    setPrivacyError('')
    try {
      if (privacy === 'SOURCE') {
        await boundedTripRequest(() =>
          deleteTripUnderstandingSource(trip.resource),
        )
        sessionStorage.setItem('bt_active_trip_source_deleted', 'true')
        setSourceDeleted(true)
        setPrivacy(null)
        trip.setNotice('导入文字已删除，整理后的行程仍保留。')
      } else {
        await boundedTripRequest(() => deleteTripUnderstanding(trip.resource))
        clearTripUnderstandingSession()
        sessionStorage.removeItem('bt_pending_operation')
        router.replace('/')
      }
    } catch {
      setPrivacyError('尚未确认删除结果，请稍后重试。')
    } finally {
      setPrivacyBusy(false)
    }
  }
  async function shareTrip() {
    if (!accountSaved || sharing) return
    setSharing(true)
    try {
      const created = await boundedTripRequest(() =>
        createTripShare(trip.resource),
      )
      setShare(new URL(created.share_url, window.location.origin).toString())
    } catch {
      trip.setNotice('暂时未能创建分享链接，请稍后重试。')
    } finally {
      setSharing(false)
    }
  }
  const showFinding = (item: PublicTripCheckItem) => (
    <article
      key={item.check_token}
      className={`e-finding${item.label === '必须调整' ? ' hard' : ''}`}
    >
      <p className="e-finding-label">{item.label}</p>
      <h3>{item.title}</h3>
      <p>{item.message}</p>
      {item.can_preview ? (
        <button
          type="button"
          disabled={disabled || trip.checking}
          onClick={() => void trip.openPreview(item.check_token)}
        >
          查看怎么调整 →
        </button>
      ) : item.affected_activity_tokens?.length ? (
        <button
          type="button"
          disabled={disabled}
          onClick={() => {
            const card = currentDay?.activities.find((value) =>
              item.affected_activity_tokens?.includes(value.activity_token),
            )
            if (card) setEditor({ card, dayIndex })
          }}
        >
          查看并修改安排 →
        </button>
      ) : null}
    </article>
  )

  return (
    <main className="experience">
      <header className="e-header">
        <Link href="/" className="e-brand">
          行程查<span>TRIPCHECK</span>
        </Link>
        <div className="e-actions">
          {result && (
            <>
              <button
                type="button"
                className="e-button e-button-quiet"
                disabled={disabled || !result.can_undo}
                onClick={() => void trip.command({ command_type: 'UNDO' })}
              >
                <Undo2 aria-hidden="true" />
                撤销
              </button>
              <button
                type="button"
                className="e-button e-button-primary"
                disabled={disabled || accountSaved}
                onClick={() => void saveToAccount()}
              >
                {accountSaved ? '已保存到账号' : '保存行程'}
              </button>
            </>
          )}
          <Link
            className="e-button e-button-quiet"
            href={user ? '/profile' : '/'}
          >
            {user ? '账号' : '首页'}
          </Link>
          {user && (
            <button
              type="button"
              className="e-button e-button-quiet"
              disabled={disabled}
              onClick={() => {
                sessionStorage.setItem('bt_login_return', '/trip/result')
                logout()
              }}
            >
              退出
            </button>
          )}
        </div>
      </header>
      {demo && (
        <div className="e-demo-note">
          北京三日示例 · 固定回放，地点和路线不代表实时查询。
          <Link href="/">用自己的攻略试试 →</Link>
        </div>
      )}
      {!result ? (
        <section className="e-loading">
          <h1>
            {trip.loading ? '让每天的安排，逐渐清晰。' : '行程暂时还没打开'}
          </h1>
          <p role="status">{trip.message}</p>
          {trip.loading ? (
            <div className="e-progress" aria-hidden="true" />
          ) : (
            <div className="e-actions">
              <button
                type="button"
                className="e-button e-button-primary"
                onClick={trip.retry}
              >
                重新读取
              </button>
              <Link className="e-button" href="/">
                返回首页
              </Link>
            </div>
          )}
        </section>
      ) : (
        <>
          <section className="e-trip-head">
            <div>
              <h1>
                {title} · {result.days.length} 天
              </h1>
              <div className="e-assumptions">
                {result.assumptions.map((item) => (
                  <button
                    type="button"
                    key={item.key}
                    disabled={disabled || !item.editable}
                    aria-label={`修改${item.label}`}
                    onClick={() => {
                      setAssumption(item)
                      setAssumptionValue(item.value)
                    }}
                  >
                    {item.label} · {item.value} ↗
                  </button>
                ))}
              </div>
            </div>
            <nav className="e-days" aria-label="选择行程日期">
              {result.days.map((day, index) => (
                <button
                  key={index}
                  type="button"
                  aria-pressed={index === dayIndex}
                  onClick={() => {
                    setDayIndex(index)
                    setSelected(null)
                  }}
                >
                  {day.label}
                </button>
              ))}
            </nav>
          </section>
          <div className="e-page-message">
            {result.status !== 'READY' && (
              <p className="e-message" role="status">
                {result.status === 'BASIC_ONLY'
                  ? '已整理基础行程。部分地点和路线尚未核对，请在出发前确认。'
                  : '部分内容仍需要确认。已整理的安排可以继续查看和调整。'}
              </p>
            )}
            {trip.notice && (
              <div className="e-message" role="status">
                {trip.notice}
                {trip.pending && (
                  <button
                    type="button"
                    disabled={trip.busy}
                    onClick={() => void trip.reconcile()}
                  >
                    确认保存结果
                  </button>
                )}
              </div>
            )}
          </div>
          <nav className="e-mobile-switch" aria-label="行程和地图">
            <button
              type="button"
              aria-pressed={mobile === 'ITINERARY'}
              onClick={() => setMobile('ITINERARY')}
            >
              行程
            </button>
            <button
              type="button"
              aria-pressed={mobile === 'MAP'}
              onClick={() => setMobile('MAP')}
            >
              地图
            </button>
          </nav>
          <div className="e-workspace" data-testid="itinerary-workspace">
            <section
              className={`e-itinerary${mobile !== 'ITINERARY' ? ' e-mobile-hidden' : ''}`}
              aria-label="当天行程"
            >
              <div className="e-section-heading">
                <h2>{currentDay?.label || '每天的安排'}</h2>
                <span className="e-muted">
                  {currentDay?.activities.length || 0} 个地点
                </span>
              </div>
              <ol className="e-stops" data-testid="trip-days">
                {currentDay?.activities.map((card, index) => {
                  const next = currentDay.activities[index + 1]
                  const route =
                    trip.map?.status !== 'NEEDS_UPDATE'
                      ? trip.map?.days
                          .find((day) => day.label === currentDay.label)
                          ?.routes.find(
                            (value) =>
                              value.from_activity_token ===
                                card.activity_token &&
                              value.to_activity_token === next?.activity_token,
                          )
                      : null
                  const chosen = route?.selected_mode
                    ? route[route.selected_mode]
                    : null
                  const cardFindings = dayFindings.filter(
                    (item) =>
                      item.affected_activity_tokens?.[0] ===
                      card.activity_token,
                  )
                  return (
                    <li
                      key={card.activity_token}
                      className={`e-stop-row${selectedCard?.activity_token === card.activity_token ? ' is-selected' : ''}`}
                    >
                      <button
                        type="button"
                        className="e-stop-select"
                        aria-pressed={
                          selectedCard?.activity_token === card.activity_token
                        }
                        onClick={() => setSelected(card.activity_token)}
                      >
                        <span className="e-stop-time">
                          {card.start_time || card.time_hint || '时间待定'}
                        </span>
                        <span>
                          <h3>{card.name}</h3>
                          <span className="e-stop-sub">
                            {card.visit_duration_minutes
                              ? `停留 ${card.visit_duration_minutes} 分钟 · `
                              : ''}
                            {card.area_or_address || card.category}
                          </span>
                          {card.status !== 'READY' && (
                            <span className="e-stop-status">地点待确认</span>
                          )}
                          {card.locked && (
                            <span className="e-stop-status">固定安排</span>
                          )}
                        </span>
                        <ArrowUpRight aria-hidden="true" />
                      </button>
                      <div className="e-stop-tools">
                        <button
                          type="button"
                          disabled={disabled || index === 0}
                          aria-label={`将${card.name}上移`}
                          onClick={() =>
                            void trip.command({
                              command_type: 'ACTIVITY_MOVE',
                              activity_token: card.activity_token,
                              target_day_index: dayIndex + 1,
                              target_position: index - 1,
                            })
                          }
                        >
                          <ArrowUp aria-hidden="true" />
                        </button>
                        <button
                          type="button"
                          disabled={
                            disabled ||
                            index === currentDay.activities.length - 1
                          }
                          aria-label={`将${card.name}下移`}
                          onClick={() =>
                            void trip.command({
                              command_type: 'ACTIVITY_MOVE',
                              activity_token: card.activity_token,
                              target_day_index: dayIndex + 1,
                              target_position: index + 1,
                            })
                          }
                        >
                          <ArrowDown aria-hidden="true" />
                        </button>
                        <button
                          type="button"
                          disabled={disabled}
                          onClick={() => setEditor({ card, dayIndex })}
                        >
                          详情与编辑
                        </button>
                      </div>
                      {cardFindings.map(showFinding)}
                      {next && (
                        <div className="e-route-note">
                          {route?.selected_mode === 'transit' ? (
                            <BusFront aria-hidden="true" />
                          ) : (
                            <Footprints aria-hidden="true" />
                          )}
                          <span>
                            {trip.map?.status === 'NEEDS_UPDATE'
                              ? '行程已调整，路线需要更新'
                              : chosen?.status === 'AVAILABLE' &&
                                  chosen.duration_minutes !== null
                                ? `${route?.selected_mode === 'transit' ? '公交' : '步行'}约 ${chosen.duration_minutes} 分钟，到${next.name}`
                                : '到下一站的路线待确认'}
                          </span>
                        </div>
                      )}
                    </li>
                  )
                })}
              </ol>
              <button
                type="button"
                className="e-add"
                disabled={disabled}
                onClick={() => setEditor({ card: null, dayIndex })}
              >
                ＋ 添加想去的地方
              </button>
              {dayFindings
                .filter(
                  (item) =>
                    !item.affected_activity_tokens?.length ||
                    !currentDay?.activities.some(
                      (card) =>
                        card.activity_token ===
                        item.affected_activity_tokens?.[0],
                    ),
                )
                .map(showFinding)}
              <div className="e-muted" role="status">
                {trip.checking
                  ? '正在检查时间与路线…'
                  : trip.checksError ||
                    trip.checks?.message ||
                    '检查结果正在准备。'}
                {!trip.checking && (
                  <button
                    type="button"
                    className="e-button e-button-quiet"
                    disabled={disabled}
                    onClick={() => void trip.retryChecks()}
                  >
                    重新检查
                  </button>
                )}
              </div>
            </section>
            <section
              className={`e-map-column${mobile !== 'MAP' ? ' e-mobile-hidden' : ''}`}
              aria-label="路线地图"
            >
              <div className="e-map-head">
                <h2>这一天，怎么走</h2>
                <button
                  type="button"
                  className="e-button"
                  disabled={disabled || trip.map?.status === 'PREPARING'}
                  onClick={() => void trip.renderMap()}
                >
                  <RefreshCw aria-hidden="true" />
                  {trip.map?.status === 'PREPARING' ? '路线准备中' : '更新路线'}
                </button>
              </div>
              <div className="e-days" aria-label="路线方式">
                {(['recommended', 'walking', 'transit'] as const).map(
                  (mode) => (
                    <button
                      key={mode}
                      type="button"
                      aria-pressed={routeMode === mode}
                      onClick={() => setRouteMode(mode)}
                    >
                      {mode === 'recommended'
                        ? '推荐方式'
                        : mode === 'walking'
                          ? '步行'
                          : '公交'}
                    </button>
                  ),
                )}
              </div>
              <RouteMap
                view={trip.map}
                day={currentDay}
                selected={selectedCard?.activity_token || null}
                onSelect={setSelected}
                mode={routeMode}
                visible={mobile === 'MAP'}
                focusSelected={Boolean(selected)}
              />
              <p className="e-map-note" role="status">
                {trip.map?.message || result.map.message}
              </p>
              {trip.map?.status === 'UNAVAILABLE' && (
                <button
                  type="button"
                  className="e-button e-button-quiet"
                  onClick={() => void trip.retryMap()}
                >
                  重新读取路线
                </button>
              )}
              {selectedCard && (
                <div className="e-map-detail">
                  <div>
                    <h3>{selectedCard.name}</h3>
                    <p className="e-muted">
                      {selectedCard.start_time ||
                        selectedCard.time_hint ||
                        '时间待安排'}
                      {selectedCard.end_time ? `–${selectedCard.end_time}` : ''}
                    </p>
                  </div>
                  <button
                    type="button"
                    className="e-button e-button-quiet"
                    disabled={disabled}
                    onClick={() => setEditor({ card: selectedCard, dayIndex })}
                  >
                    查看详情 ↗
                  </button>
                </div>
              )}
            </section>
          </div>
          <details className="e-secondary">
            <summary>住宿与出发前建议</summary>
            <p className="e-muted">
              {trip.stay?.message || result.stay.message}
            </p>
            <div className="e-stay-list">
              {(trip.stay?.candidates || result.stay.candidates).map(
                (candidate) => (
                  <article key={candidate.candidate_token}>
                    <h3>{candidate.name}</h3>
                    <p>{candidate.area_or_address}</p>
                    <p>{candidate.commute_summary}</p>
                    <p>{candidate.reason}</p>
                    <button
                      className="e-button"
                      disabled={disabled || candidate.selected}
                      type="button"
                      onClick={() =>
                        void trip.selectStay(candidate.candidate_token)
                      }
                    >
                      {candidate.selected ? '已选择' : '选择这家住宿'}
                    </button>
                  </article>
                ),
              )}
            </div>
          </details>
          <footer className="e-footer">
            <span>
              {accountSaved
                ? '行程已保存到账号，默认保留 30 天。'
                : '匿名行程保留 24 小时，登录可保存 30 天。'}
              {trip.busy ? ' 正在处理…' : ''}
            </span>
            <div className="e-actions">
              {accountSaved && (
                <button
                  type="button"
                  className="e-button e-button-quiet"
                  disabled={disabled || sharing}
                  onClick={() => void shareTrip()}
                >
                  分享行程
                </button>
              )}
              <button
                type="button"
                className="e-button e-button-quiet"
                disabled={disabled || sourceDeleted}
                onClick={() => {
                  setPrivacy('SOURCE')
                  setPrivacyError('')
                }}
              >
                {sourceDeleted ? '导入文字已删除' : '删除导入文字'}
              </button>
              <button
                type="button"
                className="e-button e-button-quiet"
                disabled={disabled}
                onClick={() => {
                  setPrivacy('TRIP')
                  setPrivacyError('')
                }}
              >
                删除行程
              </button>
            </div>
          </footer>
        </>
      )}
      {editor && result && (
        <PlaceEditor
          key={editor.card?.activity_token || 'new'}
          card={editor.card}
          dayIndex={editor.dayIndex}
          days={result.days}
          resource={trip.resource}
          busy={disabled}
          saving={trip.busy}
          notice={trip.notice}
          onClose={() => setEditor(null)}
          onCommand={trip.command}
        />
      )}
      {assumption && (
        <ExperienceDialog
          title={`修改${assumption.label}`}
          busy={disabled}
          onClose={() => setAssumption(null)}
        >
          <form
            onSubmit={(event) => {
              event.preventDefault()
              void trip
                .command({
                  command_type: 'ASSUMPTION_SET',
                  key: assumption.key,
                  value: assumptionValue,
                })
                .then((ok) => {
                  if (ok) setAssumption(null)
                })
            }}
          >
            <label className="e-field">
              {assumption.label}
              <input
                value={assumptionValue}
                onChange={(event) => setAssumptionValue(event.target.value)}
                required
                maxLength={100}
              />
            </label>
            <p className="e-notice-text">未明确提供的信息可以在这里调整。</p>
            <button
              className="e-button e-button-primary"
              type="submit"
              disabled={disabled}
            >
              保存修改
            </button>
          </form>
        </ExperienceDialog>
      )}
      {trip.preview && (
        <ExperienceDialog
          title={trip.preview.title}
          busy={disabled}
          onClose={trip.closePreview}
        >
          <p>{trip.preview.summary}</p>
          <div className="e-preview-compare">
            <div>
              <h3>现在的安排</h3>
              {trip.preview.before.map((line, index) => (
                <p key={index}>{line}</p>
              ))}
            </div>
            <div className="e-preview-after">
              <h3>调整后</h3>
              {trip.preview.after.map((line, index) => (
                <p key={index}>{line}</p>
              ))}
            </div>
          </div>
          {trip.preview.changes?.map((change, index) => (
            <div className="e-form-section" key={index}>
              <h3>{change.name}</h3>
              <p>
                {change.before.start_time || '未设置'} →{' '}
                {change.after.start_time || '未设置'}
                {change.after.visit_duration_minutes != null
                  ? `，停留 ${change.after.visit_duration_minutes} 分钟`
                  : ''}
              </p>
            </div>
          ))}
          <p className="e-notice-text">
            预览不会修改行程。采纳后保留原路线，需要时点击“更新路线”重新检查。
          </p>
          <button
            type="button"
            className="e-button e-button-primary"
            disabled={disabled}
            onClick={() => void trip.adopt()}
          >
            采纳这次调整
          </button>
        </ExperienceDialog>
      )}
      {privacy && (
        <ExperienceDialog
          title={privacy === 'SOURCE' ? '删除导入文字' : '删除这份行程'}
          busy={privacyBusy}
          onClose={() => setPrivacy(null)}
        >
          <p>
            {privacy === 'SOURCE'
              ? '导入的攻略文字将永久删除，整理后的行程仍保留。'
              : '这份行程及关联数据将永久删除，无法恢复。'}
          </p>
          {privacyError && (
            <p className="e-message" role="alert">
              {privacyError}
            </p>
          )}
          <div className="e-actions" style={{ marginTop: 25 }}>
            <button
              className="e-button"
              type="button"
              disabled={privacyBusy}
              onClick={() => setPrivacy(null)}
            >
              取消
            </button>
            <button
              className="e-button e-button-primary"
              type="button"
              disabled={privacyBusy}
              onClick={() => void confirmDelete()}
            >
              {privacyBusy ? '正在删除…' : '确认永久删除'}
            </button>
          </div>
        </ExperienceDialog>
      )}
      {share && (
        <ExperienceDialog title="分享这份行程" onClose={() => setShare('')}>
          <p className="e-notice-text">
            拥有链接的人可查看行程，链接默认 7 天后失效。可在账号中撤销。
          </p>
          <label className="e-field">
            分享链接
            <input
              readOnly
              value={share}
              onFocus={(event) => event.target.select()}
            />
          </label>
          <button
            className="e-button"
            type="button"
            onClick={() => {
              void navigator.clipboard
                .writeText(share)
                .then(() => trip.setNotice('分享链接已复制。'))
                .catch(() => trip.setNotice('请选中链接后复制。'))
            }}
          >
            复制链接
          </button>
        </ExperienceDialog>
      )}
    </main>
  )
}
