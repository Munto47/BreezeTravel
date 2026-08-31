'use client'

import { useEffect, useState } from 'react'
import { Brain, Save, Trash2 } from 'lucide-react'

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
const DEFAULT_CONSENTS: DataConsentView = {
  memory_enabled: false,
  feedback_enabled: false,
  training_eval_enabled: false,
}
const DINING = [
  ['LOCAL', '当地风味'], ['VEGETARIAN', '素食'], ['HALAL', '清真'],
  ['NO_SPICY', '少辣'], ['QUICK', '便捷就餐'],
] as const
const HOTEL = [
  ['CHAIN', '连锁酒店'], ['NEAR_TRANSIT', '靠近公交'],
  ['QUIET', '安静'], ['CENTRAL', '市中心'],
] as const

function Toggle({ checked, label, description, disabled, onChange }: {
  checked: boolean
  label: string
  description: string
  disabled: boolean
  onChange: () => void
}) {
  return (
    <div className="flex items-start gap-3 rounded-xl bg-slate-50 px-3 py-3">
      <div className="min-w-0 flex-1">
        <p className="text-xs font-semibold text-slate-800">{label}</p>
        <p className="mt-1 text-[11px] leading-5 text-slate-500">{description}</p>
      </div>
      <button type="button" aria-label={`切换${label}`} aria-pressed={checked} disabled={disabled} onClick={onChange} className={`relative mt-0.5 h-6 w-11 shrink-0 rounded-full transition disabled:opacity-50 ${checked ? 'bg-violet-600' : 'bg-slate-300'}`}>
        <span className={`absolute top-1 h-4 w-4 rounded-full bg-white transition ${checked ? 'left-6' : 'left-1'}`} />
      </button>
    </div>
  )
}

export default function MemorySettingsPanel() {
  const [consents, setConsents] = useState<DataConsentView>(DEFAULT_CONSENTS)
  const [preference, setPreference] = useState<PreferenceMemoryView>(EMPTY)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  useEffect(() => {
    Promise.all([readDataConsents(), readPreferenceMemory()])
      .then(([nextConsents, nextPreference]) => {
        setConsents(nextConsents)
        setPreference(nextPreference ?? EMPTY)
      })
      .catch(() => setMessage('偏好设置暂时无法读取。'))
      .finally(() => setLoading(false))
  }, [])

  const toggleConsent = async (purpose: 'memory' | 'feedback' | 'training-eval', enabled: boolean) => {
    if (busy) return
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
    if (busy || !consents.memory_enabled) return
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

  const toggleList = (field: 'dining_preferences' | 'hotel_preferences', value: string) => {
    setPreference((current) => {
      const selected = current[field] as string[]
      const next = selected.includes(value)
        ? selected.filter((item) => item !== value)
        : selected.length < 3 ? [...selected, value] : selected
      return { ...current, [field]: next }
    })
  }

  return (
    <section className="mt-6 overflow-hidden rounded-2xl border border-violet-100 bg-white shadow-sm" data-testid="g06-memory-settings">
      <div className="flex items-center gap-3 border-b border-gray-100 px-4 py-4">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-violet-50"><Brain className="h-4 w-4 text-violet-600" /></div>
        <div><p className="text-sm font-semibold text-gray-800">旅行偏好与数据用途</p><p className="mt-1 text-[11px] text-gray-500">全部默认关闭，三项选择互不代表。</p></div>
      </div>
      <div className="space-y-3 p-4">
        <Toggle checked={consents.memory_enabled} disabled={loading || busy} label="记住结构化偏好" description="只保存步行、出发时间、餐饮、酒店和行程强度；不保存攻略、截图或聊天。" onChange={() => void toggleConsent('memory', !consents.memory_enabled)} />
        <Toggle checked={consents.feedback_enabled} disabled={loading || busy} label="允许保存产品反馈" description="仅记录纠正、采纳或不采纳等最小事件，不含原文。" onChange={() => void toggleConsent('feedback', !consents.feedback_enabled)} />
        <Toggle checked={consents.training_eval_enabled} disabled={loading || busy} label="允许用于训练或评测" description="这是单独选择；提交产品反馈不会自动开启。" onChange={() => void toggleConsent('training-eval', !consents.training_eval_enabled)} />
      </div>
      {consents.memory_enabled ? (
        <div className="border-t border-slate-100 p-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <label className="text-xs font-medium text-slate-700">可接受单段步行（分钟）<input data-testid="walking-tolerance" type="number" min={5} max={120} value={preference.walking_tolerance_minutes ?? ''} onChange={(event) => setPreference((current) => ({ ...current, walking_tolerance_minutes: event.target.value ? Number(event.target.value) : null }))} className="mt-1.5 min-h-11 w-full rounded-xl border border-slate-200 px-3" /></label>
            <label className="text-xs font-medium text-slate-700">希望出发时间<input data-testid="preferred-start-time" type="time" value={preference.preferred_start_time ?? ''} onChange={(event) => setPreference((current) => ({ ...current, preferred_start_time: event.target.value || null }))} className="mt-1.5 min-h-11 w-full rounded-xl border border-slate-200 px-3" /></label>
            <label className="text-xs font-medium text-slate-700">行程强度<select data-testid="trip-intensity" value={preference.intensity ?? ''} onChange={(event) => setPreference((current) => ({ ...current, intensity: (event.target.value || null) as PreferenceMemoryView['intensity'] }))} className="mt-1.5 min-h-11 w-full rounded-xl border border-slate-200 px-3"><option value="">未设置</option><option value="RELAXED">轻松</option><option value="BALANCED">均衡</option><option value="FULL">充实</option></select></label>
          </div>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <fieldset><legend className="text-xs font-semibold text-slate-700">餐饮偏好（最多3项）</legend><div className="mt-2 flex flex-wrap gap-2">{DINING.map(([value, label]) => <button type="button" key={value} aria-pressed={preference.dining_preferences.includes(value)} onClick={() => toggleList('dining_preferences', value)} className={`rounded-full border px-3 py-1.5 text-xs ${preference.dining_preferences.includes(value) ? 'border-violet-500 bg-violet-50 text-violet-800' : 'border-slate-200 text-slate-600'}`}>{label}</button>)}</div></fieldset>
            <fieldset><legend className="text-xs font-semibold text-slate-700">酒店偏好（最多3项）</legend><div className="mt-2 flex flex-wrap gap-2">{HOTEL.map(([value, label]) => <button type="button" key={value} aria-pressed={preference.hotel_preferences.includes(value)} onClick={() => toggleList('hotel_preferences', value)} className={`rounded-full border px-3 py-1.5 text-xs ${preference.hotel_preferences.includes(value) ? 'border-violet-500 bg-violet-50 text-violet-800' : 'border-slate-200 text-slate-600'}`}>{label}</button>)}</div></fieldset>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <button data-testid="save-preferences" type="button" disabled={busy} onClick={() => void save()} className="inline-flex min-h-11 items-center gap-2 rounded-xl bg-violet-600 px-4 text-xs font-semibold text-white disabled:opacity-50"><Save className="h-4 w-4" />保存偏好</button>
            <button data-testid="clear-preferences" type="button" disabled={busy} onClick={() => void clear()} className="inline-flex min-h-11 items-center gap-2 rounded-xl border border-slate-200 px-4 text-xs font-semibold text-slate-600 disabled:opacity-50"><Trash2 className="h-4 w-4" />清空偏好</button>
          </div>
        </div>
      ) : null}
      {message ? <p role="status" className="border-t border-slate-100 px-4 py-3 text-xs text-slate-600">{message}</p> : null}
    </section>
  )
}
