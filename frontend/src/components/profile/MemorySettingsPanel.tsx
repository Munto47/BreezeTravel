'use client'

import { useEffect, useState } from 'react'
import { Save, Trash2 } from 'lucide-react'

import {
  type DataConsentView,
  type PreferenceMemoryView,
  clearPreferenceMemory,
  readDataConsents,
  readPreferenceMemory,
  savePreferenceMemory,
  setDataConsent,
} from '@/lib/trip-understanding-v3'

const EMPTY: PreferenceMemoryView = {
  walking_tolerance_minutes: null,
  preferred_start_time: null,
  dining_preferences: [],
  hotel_preferences: [],
  intensity: null,
}
const DINING = [
  ['LOCAL', '当地风味'],
  ['VEGETARIAN', '素食'],
  ['HALAL', '清真'],
  ['NO_SPICY', '少辣'],
  ['QUICK', '便捷就餐'],
] as const
const HOTEL = [
  ['CHAIN', '连锁酒店'],
  ['NEAR_TRANSIT', '靠近公交'],
  ['QUIET', '安静'],
  ['CENTRAL', '市中心'],
] as const

function Toggle({
  checked,
  label,
  description,
  disabled,
  onChange,
}: {
  checked: boolean
  label: string
  description: string
  disabled: boolean
  onChange: () => void
}) {
  return (
    <div className="profile-consent">
      <div>
        <h3>{label}</h3>
        <p className="profile-help">{description}</p>
      </div>
      <button
        type="button"
        aria-label={`切换${label}`}
        aria-pressed={checked}
        disabled={disabled}
        onClick={onChange}
        className={`profile-toggle${checked ? ' is-on' : ''}`}
      >
        <span className="profile-toggle-track" aria-hidden="true">
          <span />
        </span>
        <span className="profile-toggle-text">
          {checked ? '已开启' : '已关闭'}
        </span>
      </button>
    </div>
  )
}

export default function MemorySettingsPanel() {
  const [consents, setConsents] = useState<DataConsentView | null>(null)
  const [preference, setPreference] = useState<PreferenceMemoryView>(EMPTY)
  const [loading, setLoading] = useState(true)
  const [loadFailed, setLoadFailed] = useState(false)
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  useEffect(() => {
    let active = true
    setLoading(true)
    setLoadFailed(false)
    setMessage('')
    readDataConsents()
      .then(
        async (nextConsents) =>
          [
            nextConsents,
            nextConsents.memory_enabled ? await readPreferenceMemory() : null,
          ] as const,
      )
      .then(([nextConsents, nextPreference]) => {
        if (!active) return
        setConsents(nextConsents)
        setPreference(nextPreference ?? EMPTY)
      })
      .catch(() => {
        if (active) setLoadFailed(true)
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [loadAttempt])

  const toggleConsent = async (
    purpose: 'memory' | 'feedback' | 'training-eval',
    enabled: boolean,
  ) => {
    if (busy || loading || loadFailed || !consents) return
    setBusy(true)
    setMessage('')
    try {
      const next = await setDataConsent(purpose, enabled)
      setConsents(next)
      if (purpose === 'memory' && !enabled) setPreference(EMPTY)
      setMessage(enabled ? '已按你的选择开启。' : '已关闭，相关内容已清理。')
    } catch {
      setMessage('保存选择失败，请稍后重试。')
    } finally {
      setBusy(false)
    }
  }

  const save = async () => {
    if (busy || !consents?.memory_enabled) return
    setBusy(true)
    try {
      setPreference(await savePreferenceMemory(preference))
      setMessage('旅行偏好已更新。')
    } catch {
      setMessage('偏好保存失败，请检查后重试。')
    } finally {
      setBusy(false)
    }
  }

  const clear = async () => {
    if (busy) return
    setBusy(true)
    try {
      await clearPreferenceMemory()
      setPreference(EMPTY)
      setMessage('已清空所有结构化旅行偏好。')
    } catch {
      setMessage('清空失败，请稍后重试。')
    } finally {
      setBusy(false)
    }
  }

  const toggleList = (
    field: 'dining_preferences' | 'hotel_preferences',
    value: string,
  ) => {
    setPreference((current) => {
      const selected = current[field] as string[]
      const next = selected.includes(value)
        ? selected.filter((item) => item !== value)
        : selected.length < 3
          ? [...selected, value]
          : selected
      return { ...current, [field]: next }
    })
  }

  return (
    <section
      className="profile-section profile-memory"
      aria-labelledby="preference-settings-title"
      data-testid="g06-memory-settings"
    >
      <h2 id="preference-settings-title">旅行偏好与数据用途</h2>
      <p className="profile-help">全部默认关闭，每项选择单独生效。</p>
      {loading && (
        <p role="status" className="profile-help">
          正在读取偏好设置…
        </p>
      )}
      {loadFailed && (
        <div className="profile-notice">
          <p role="alert">
            偏好设置暂时无法读取，当前选择还未确认。请重新读取后再修改。
          </p>
          <div className="profile-button-row">
            <button
              type="button"
              className="e-button"
              onClick={() => setLoadAttempt((current) => current + 1)}
            >
              重新读取偏好设置
            </button>
          </div>
        </div>
      )}
      {!loading && !loadFailed && consents && (
        <>
          <div className="profile-consent-list">
            <Toggle
              checked={consents.memory_enabled}
              disabled={loading || busy}
              label="记住结构化偏好"
              description="只保存步行、出发时间、餐饮、酒店和行程强度；不保存攻略或聊天。"
              onChange={() =>
                void toggleConsent('memory', !consents.memory_enabled)
              }
            />
            <Toggle
              checked={consents.feedback_enabled}
              disabled={loading || busy}
              label="允许保存产品反馈"
              description="仅记录纠正、采纳或不采纳等最小事件，不含原文。"
              onChange={() =>
                void toggleConsent('feedback', !consents.feedback_enabled)
              }
            />
            <Toggle
              checked={consents.training_eval_enabled}
              disabled={loading || busy}
              label="允许用于训练或评测"
              description="这是单独选择；提交产品反馈不会自动开启。"
              onChange={() =>
                void toggleConsent(
                  'training-eval',
                  !consents.training_eval_enabled,
                )
              }
            />
          </div>
          {consents.memory_enabled && (
            <div className="profile-preference-fields">
              <div className="profile-fields profile-fields-three">
                <label>
                  可接受单段步行（分钟）
                  <input
                    data-testid="walking-tolerance"
                    type="number"
                    min={5}
                    max={120}
                    value={preference.walking_tolerance_minutes ?? ''}
                    onChange={(event) =>
                      setPreference((current) => ({
                        ...current,
                        walking_tolerance_minutes: event.target.value
                          ? Number(event.target.value)
                          : null,
                      }))
                    }
                    disabled={busy}
                  />
                </label>
                <label>
                  希望出发时间
                  <input
                    data-testid="preferred-start-time"
                    type="time"
                    value={preference.preferred_start_time ?? ''}
                    onChange={(event) =>
                      setPreference((current) => ({
                        ...current,
                        preferred_start_time: event.target.value || null,
                      }))
                    }
                    disabled={busy}
                  />
                </label>
                <label>
                  行程强度
                  <select
                    data-testid="trip-intensity"
                    value={preference.intensity ?? ''}
                    onChange={(event) =>
                      setPreference((current) => ({
                        ...current,
                        intensity: (event.target.value ||
                          null) as PreferenceMemoryView['intensity'],
                      }))
                    }
                    disabled={busy}
                  >
                    <option value="">未设置</option>
                    <option value="RELAXED">轻松</option>
                    <option value="BALANCED">均衡</option>
                    <option value="FULL">充实</option>
                  </select>
                </label>
              </div>
              <div className="profile-preference-groups">
                <fieldset>
                  <legend>
                    餐饮偏好 <span className="profile-help">最多 3 项</span>
                  </legend>
                  <div className="profile-options">
                    {DINING.map(([value, label]) => (
                      <button
                        type="button"
                        key={value}
                        disabled={busy}
                        aria-pressed={preference.dining_preferences.includes(
                          value,
                        )}
                        onClick={() => toggleList('dining_preferences', value)}
                        className="profile-option"
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </fieldset>
                <fieldset>
                  <legend>
                    酒店偏好 <span className="profile-help">最多 3 项</span>
                  </legend>
                  <div className="profile-options">
                    {HOTEL.map(([value, label]) => (
                      <button
                        type="button"
                        key={value}
                        disabled={busy}
                        aria-pressed={preference.hotel_preferences.includes(
                          value,
                        )}
                        onClick={() => toggleList('hotel_preferences', value)}
                        className="profile-option"
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </fieldset>
              </div>
              <div className="profile-button-row">
                <button
                  data-testid="save-preferences"
                  type="button"
                  disabled={busy}
                  onClick={() => void save()}
                  className="e-button e-button-primary"
                >
                  <Save aria-hidden="true" />
                  保存偏好
                </button>
                <button
                  data-testid="clear-preferences"
                  type="button"
                  disabled={busy}
                  onClick={() => void clear()}
                  className="e-button"
                >
                  <Trash2 aria-hidden="true" />
                  清空偏好
                </button>
              </div>
            </div>
          )}
        </>
      )}
      {message && (
        <p role="status" className="profile-notice">
          {message}
        </p>
      )}
    </section>
  )
}
