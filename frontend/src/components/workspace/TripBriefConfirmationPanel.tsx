'use client'

import { useEffect, useMemo, useState } from 'react'
import { CheckCircle2, Loader2, TriangleAlert } from 'lucide-react'

import type { TripBriefRevision } from '@/types/workspace'


interface Props {
  brief: TripBriefRevision
  busy: boolean
  onPatch: (updates: Record<string, unknown>) => Promise<void>
  onConfirm: () => Promise<void>
}


const paceOptions = ['NO_PREFERENCE', '舒缓', '适中', '紧凑']
const intensityOptions = ['NO_PREFERENCE', '低', '中', '高']


export default function TripBriefConfirmationPanel({ brief, busy, onPatch, onConfirm }: Props) {
  const [travelerCount, setTravelerCount] = useState(brief.traveler_count)
  const [dailyPace, setDailyPace] = useState(brief.daily_pace)
  const [activityIntensity, setActivityIntensity] = useState(brief.activity_intensity)
  const [arrivalLocation, setArrivalLocation] = useState(brief.arrival.location ?? '')
  const [departureLocation, setDepartureLocation] = useState(brief.departure.location ?? '')
  const [hotelName, setHotelName] = useState(brief.accommodation.hotel_name ?? '')
  const [hotelArea, setHotelArea] = useState(brief.accommodation.area ?? '')

  useEffect(() => {
    setTravelerCount(brief.traveler_count)
    setDailyPace(brief.daily_pace)
    setActivityIntensity(brief.activity_intensity)
    setArrivalLocation(brief.arrival.location ?? '')
    setDepartureLocation(brief.departure.location ?? '')
    setHotelName(brief.accommodation.hotel_name ?? '')
    setHotelArea(brief.accommodation.area ?? '')
  }, [brief])

  const inferredFields = useMemo(() => Object.entries(brief.field_provenance)
    .filter(([, value]) => value.origin === 'INFERRED' || value.confirmation === 'UNCONFIRMED')
    .map(([field]) => field), [brief.field_provenance])

  const updates = useMemo(() => {
    const next: Record<string, unknown> = {}
    if (travelerCount !== brief.traveler_count) next.traveler_count = travelerCount
    if (dailyPace !== brief.daily_pace) next.daily_pace = dailyPace
    if (activityIntensity !== brief.activity_intensity) next.activity_intensity = activityIntensity
    if (arrivalLocation !== (brief.arrival.location ?? '')) {
      next.arrival = { ...brief.arrival, location: arrivalLocation || null }
    }
    if (departureLocation !== (brief.departure.location ?? '')) {
      next.departure = { ...brief.departure, location: departureLocation || null }
    }
    if (
      hotelName !== (brief.accommodation.hotel_name ?? '')
      || hotelArea !== (brief.accommodation.area ?? '')
    ) {
      next.accommodation = {
        hotel_name: hotelName || null,
        area: hotelArea || null,
      }
    }
    return next
  }, [
    activityIntensity,
    arrivalLocation,
    brief,
    dailyPace,
    departureLocation,
    hotelArea,
    hotelName,
    travelerCount,
  ])
  const hasUpdates = Object.keys(updates).length > 0
  const confirmed = brief.status === 'CONFIRMED'

  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">确认 TripBrief</h2>
          <p className="mt-1 text-xs leading-5 text-slate-500">
            这些字段来自文本解析；确认后才允许进入权威核验。未填写偏好会保留为 NO_PREFERENCE。
          </p>
        </div>
        <span className={`rounded-full px-2.5 py-1 text-[11px] font-medium ${
          confirmed ? 'bg-emerald-100 text-emerald-800' : 'bg-amber-100 text-amber-800'
        }`}>
          {confirmed ? '已确认' : `待确认 · r${brief.revision}`}
        </span>
      </div>

      {inferredFields.length > 0 && !confirmed && (
        <div className="mt-3 flex gap-2 rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
          <span>仍需人工确认：{inferredFields.join('、')}。推断字段不会自动升级为 HARD。</span>
        </div>
      )}

      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <label className="text-xs text-slate-600">
          城市与日期
          <div className="mt-1 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-800">
            {brief.city} · {brief.date_range.start} 至 {brief.date_range.end}
          </div>
        </label>
        <label className="text-xs text-slate-600">
          出行人数
          <select value={travelerCount} onChange={event => setTravelerCount(Number(event.target.value))} disabled={confirmed || busy} className="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2 text-sm disabled:bg-slate-50">
            {[2, 3, 4, 5].map(value => <option key={value} value={value}>{value} 人</option>)}
          </select>
        </label>
        <label className="text-xs text-slate-600">
          交通方式
          <div className="mt-1 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-800">
            {brief.transport_modes.join(' / ')}
          </div>
        </label>
        <label className="text-xs text-slate-600">
          到达地点
          <input value={arrivalLocation} onChange={event => setArrivalLocation(event.target.value)} disabled={confirmed || busy} placeholder="未提供" className="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2 text-sm disabled:bg-slate-50" />
        </label>
        <label className="text-xs text-slate-600">
          离开地点
          <input value={departureLocation} onChange={event => setDepartureLocation(event.target.value)} disabled={confirmed || busy} placeholder="未提供" className="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2 text-sm disabled:bg-slate-50" />
        </label>
        <label className="text-xs text-slate-600">
          酒店 / 区域
          <div className="mt-1 grid grid-cols-2 gap-2">
            <input value={hotelName} onChange={event => setHotelName(event.target.value)} disabled={confirmed || busy} placeholder="酒店" className="w-full rounded-xl border border-slate-200 px-3 py-2 text-sm disabled:bg-slate-50" />
            <input value={hotelArea} onChange={event => setHotelArea(event.target.value)} disabled={confirmed || busy} placeholder="区域" className="w-full rounded-xl border border-slate-200 px-3 py-2 text-sm disabled:bg-slate-50" />
          </div>
        </label>
        <label className="text-xs text-slate-600">
          每日节奏
          <select value={dailyPace} onChange={event => setDailyPace(event.target.value)} disabled={confirmed || busy} className="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2 text-sm disabled:bg-slate-50">
            {paceOptions.map(value => <option key={value}>{value}</option>)}
          </select>
        </label>
        <label className="text-xs text-slate-600">
          活动强度
          <select value={activityIntensity} onChange={event => setActivityIntensity(event.target.value)} disabled={confirmed || busy} className="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2 text-sm disabled:bg-slate-50">
            {intensityOptions.map(value => <option key={value}>{value}</option>)}
          </select>
        </label>
        <div className="text-xs text-slate-600">
          其他偏好
          <div className="mt-1 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-800">
            饮食 {String(brief.dining_style)} · 住宿 {String(brief.lodging_style)}
          </div>
        </div>
      </div>

      {!confirmed && (
        <div className="mt-4 flex flex-wrap gap-2">
          <button onClick={() => onPatch(updates)} disabled={busy || !hasUpdates} className="rounded-xl border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 disabled:opacity-40">
            保存字段修正
          </button>
          <button onClick={onConfirm} disabled={busy || hasUpdates} className="inline-flex items-center gap-2 rounded-xl bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40">
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
            确认当前 Brief
          </button>
          {hasUpdates && <span className="self-center text-xs text-amber-700">请先保存字段修正，再确认新 revision。</span>}
        </div>
      )}
    </section>
  )
}
