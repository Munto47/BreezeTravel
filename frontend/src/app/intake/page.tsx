'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { ArrowLeft, FileImage, Loader2, MapPin, ShieldCheck } from 'lucide-react'

import { ApiRequestError, api } from '@/lib/api'
import { randomUuid } from '@/lib/randomUuid'
import { useAuthStore } from '@/stores/authStore'
import type { IntakeMaterializationResult, TripIntakeRevision } from '@/types/tripIntake'
import { codePointSlice, evidenceMatchesSource } from '@/types/tripIntake'


function partialDate(value: { year: number | null; month: number; day: number } | undefined): string {
  if (!value?.year) return ''
  return `${value.year}-${String(value.month).padStart(2, '0')}-${String(value.day).padStart(2, '0')}`
}


function normalizedCity(value: string): string {
  return value.trim().replace(/市$/, '')
}


function extractionMatchesCoreFields(
  intake: TripIntakeRevision,
  city: string,
  startDate: string,
  endDate: string,
  partySize: number,
): boolean {
  const primary = intake.extraction.locations.mentions.find(
    item => item.mention_id === intake.extraction.locations.primary_mention_id,
  )
  const total = intake.extraction.party_size.total
  const range = intake.extraction.temporal.date_range
  return (
    intake.extraction.locations.status === 'EXACT'
    && Boolean(primary?.normalized_name)
    && normalizedCity(primary?.normalized_name || '') === normalizedCity(city)
    && total.quantifier === 'EXACT'
    && total.min === partySize
    && total.max === partySize
    && partialDate(range?.start) === startDate
    && partialDate(range?.end) === endDate
  )
}


export default function TripIntakePage() {
  const router = useRouter()
  const { user, isHydrated, hydrate } = useAuthStore()
  const [roomId, setRoomId] = useState('')
  const [rawText, setRawText] = useState('')
  const [sourceType, setSourceType] = useState<'AI_TEXT' | 'MANUAL_TEXT'>('MANUAL_TEXT')
  const [screenshots, setScreenshots] = useState<File[]>([])
  const [intake, setIntake] = useState<TripIntakeRevision | null>(null)
  const [city, setCity] = useState('')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [partySize, setPartySize] = useState(1)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const commandKeys = useRef(new Map<string, string>())

  const keyFor = (scope: string) => {
    const existing = commandKeys.current.get(scope)
    if (existing) return existing
    const value = randomUuid()
    commandKeys.current.set(scope, value)
    return value
  }

  const adoptIntake = (value: TripIntakeRevision) => {
    setIntake(value)
    setRawText(value.sources.map(source => source.text).join('\n\n'))
    const primary = value.extraction.locations.mentions.find(
      item => item.mention_id === value.extraction.locations.primary_mention_id,
    )
    if (primary) setCity(primary.normalized_name?.replace(/市$/, '') || primary.raw_text)
    const range = value.extraction.temporal.date_range
    setStartDate(partialDate(range?.start))
    setEndDate(partialDate(range?.end))
    if (value.extraction.party_size.total.min && value.extraction.party_size.total.min > 0) {
      setPartySize(value.extraction.party_size.total.min)
    }
    const params = new URLSearchParams(window.location.search)
    params.set('roomId', value.room_id)
    params.set('intakeId', value.intake_id)
    params.set('intakeRevision', String(value.revision))
    window.history.replaceState(null, '', `${window.location.pathname}?${params.toString()}`)
  }

  const run = async (label: string, action: () => Promise<void>) => {
    setBusy(label)
    setError(null)
    try {
      await action()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(null)
    }
  }

  useEffect(() => { hydrate() }, [hydrate])
  useEffect(() => {
    if (!isHydrated) return
    if (!user) {
      router.replace('/login')
      return
    }
    const params = new URLSearchParams(window.location.search)
    const requestedRoomId = params.get('roomId') || ''
    const intakeId = params.get('intakeId')
    const revision = Number(params.get('intakeRevision'))
    setRoomId(requestedRoomId)
    if (!requestedRoomId) return
    void run('restore', async () => {
      try {
        const restored = intakeId && revision > 0
          ? await api.get<TripIntakeRevision>(`/api/trip-intakes/${intakeId}/revisions/${revision}`)
          : await api.get<TripIntakeRevision>(`/api/rooms/${requestedRoomId}/trip-intakes/latest`)
        adoptIntake(restored)
      } catch (caught) {
        if (caught instanceof ApiRequestError && caught.status === 404) return
        throw caught
      }
    })
    // Intake identity is intentionally read once from the recovery URL.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isHydrated, router, user])

  const createTextIntake = () => run('create', async () => {
    if (!roomId) throw new Error('缺少 roomId，请从房间入口开始')
    if (!rawText.trim()) throw new Error('请先粘贴行程原文')
    const created = await api.post<TripIntakeRevision>(`/api/rooms/${roomId}/trip-intakes`, {
      source_type: sourceType,
      raw_text: rawText,
    })
    adoptIntake(created)
  })

  const createScreenshotIntake = () => run('screenshot', async () => {
    if (!roomId) throw new Error('缺少 roomId，请从房间入口开始')
    if (screenshots.length < 1 || screenshots.length > 6) throw new Error('请选择 1～6 张截图')
    const oversized = screenshots.find(file => file.size > 10 * 1024 * 1024)
    if (oversized) throw new Error(`${oversized.name} 超过 10MB`)
    const form = new FormData()
    screenshots.forEach(file => form.append('screenshots', file, file.name))
    const created = await api.postFormWithHeaders<TripIntakeRevision>(
      `/api/rooms/${roomId}/trip-intakes/screenshots`,
      form,
      {},
    )
    adoptIntake(created)
  })

  const correctCoreFields = () => run('patch', async () => {
    if (!intake) return
    if (!city.trim() || !startDate || !endDate || partySize < 1) {
      throw new Error('请填写单一国内城市、完整日期和正整数人数')
    }
    const scope = `patch-intake:${intake.intake_id}:${intake.revision}`
    const updated = await api.patchWithHeaders<TripIntakeRevision>(
      `/api/trip-intakes/${intake.intake_id}/revisions/${intake.revision}`,
      { confirmed_values: { city, start_date: startDate, end_date: endDate, party_size: partySize } },
      { 'If-Match': `"${intake.revision}"`, 'Idempotency-Key': keyFor(scope) },
    )
    commandKeys.current.delete(scope)
    adoptIntake(updated)
  })

  const confirm = () => run('confirm', async () => {
    if (!intake) return
    if (!city.trim() || !startDate || !endDate || partySize < 1) {
      throw new Error('请填写单一国内城市、完整日期和正整数人数')
    }
    let target = intake
    if (!extractionMatchesCoreFields(intake, city, startDate, endDate, partySize)) {
      const patchScope = `patch-intake:${intake.intake_id}:${intake.revision}`
      target = await api.patchWithHeaders<TripIntakeRevision>(
        `/api/trip-intakes/${intake.intake_id}/revisions/${intake.revision}`,
        { confirmed_values: { city, start_date: startDate, end_date: endDate, party_size: partySize } },
        { 'If-Match': `"${intake.revision}"`, 'Idempotency-Key': keyFor(patchScope) },
      )
      commandKeys.current.delete(patchScope)
      adoptIntake(target)
    }
    const scope = `confirm-intake:${target.intake_id}:${target.revision}`
    const confirmed = await api.postWithHeaders<TripIntakeRevision>(
      `/api/trip-intakes/${target.intake_id}/revisions/${target.revision}/confirm`,
      {},
      { 'If-Match': `"${target.revision}"`, 'Idempotency-Key': keyFor(scope) },
    )
    commandKeys.current.delete(scope)
    adoptIntake(confirmed)
  })

  const materialize = () => run('materialize', async () => {
    if (!intake) return
    const scope = `materialize-intake:${intake.intake_id}:${intake.revision}`
    const result = await api.postWithHeaders<IntakeMaterializationResult>(
      `/api/trip-intakes/${intake.intake_id}/revisions/${intake.revision}/materialize`,
      {},
      { 'If-Match': `"${intake.revision}"`, 'Idempotency-Key': keyFor(scope) },
    )
    const receipt = result.materialization
    router.push(
      `/import?roomId=${encodeURIComponent(receipt.workspace.room_id)}`
      + `&workspaceId=${encodeURIComponent(receipt.workspace.workspace_id)}`
      + `&importId=${encodeURIComponent(receipt.itinerary_import.import_id)}`,
    )
  })

  const evidenceRows = useMemo(() => {
    if (!intake) return []
    const spans = [
      ...intake.extraction.locations.mentions.flatMap(item => item.evidence),
      ...intake.extraction.party_size.total.evidence,
      ...(intake.extraction.temporal.date_range?.evidence || []),
      ...intake.extraction.preferences.items.flatMap(item => item.evidence),
    ]
    return spans.map(span => {
      const source = intake.sources.find(item => item.source_id === span.source_id)
      return {
        span,
        source,
        actual: source ? codePointSlice(source.text, span.start, span.end) : '',
        valid: Boolean(source && evidenceMatchesSource(span, source)),
      }
    })
  }, [intake])

  if (!isHydrated || !user) return null

  return (
    <main className="min-h-screen bg-slate-50">
      <header className="border-b border-slate-200 bg-white px-4 py-3">
        <div className="mx-auto flex max-w-4xl items-center gap-3">
          <button onClick={() => router.push('/')} className="inline-flex items-center gap-1 text-sm text-slate-500">
            <ArrowLeft className="h-4 w-4" /> 返回
          </button>
          <div className="h-4 w-px bg-slate-200" />
          <ShieldCheck className="h-4 w-4 text-coral-500" />
          <h1 className="text-sm font-semibold">原文 → Intake 草稿 → 用户确认 → 权威行程</h1>
        </div>
      </header>

      <div className="mx-auto max-w-4xl space-y-5 px-4 py-6">
        {!intake && (
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="font-semibold text-slate-900">先输入不受控原文</h2>
            <p className="mt-1 text-xs text-slate-500">这里不要求先选城市、天数或人数；缺失和模糊值会原样保留，不会默认成 2 人。</p>
            <textarea
              value={rawText}
              onChange={event => setRawText(event.target.value)}
              maxLength={12000}
              className="mt-4 min-h-48 w-full rounded-xl border border-slate-200 p-3 text-sm leading-6"
              placeholder="粘贴微信、语音转写或已有行程……"
            />
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <select value={sourceType} onChange={event => setSourceType(event.target.value as typeof sourceType)} className="rounded-xl border border-slate-200 px-3 py-2 text-sm">
                <option value="MANUAL_TEXT">手工/粘贴文本</option>
                <option value="AI_TEXT">AI 行程文本</option>
              </select>
              <button onClick={createTextIntake} disabled={busy !== null} className="rounded-xl bg-coral-500 px-4 py-2 text-sm font-medium text-white disabled:opacity-50">
                {busy === 'create' ? '解析中…' : '生成 Intake 草稿'}
              </button>
            </div>
            <div className="mt-5 rounded-xl border border-dashed border-slate-300 bg-slate-50 p-4">
              <label className="flex cursor-pointer items-center gap-2 text-sm font-medium">
                <FileImage className="h-4 w-4 text-coral-500" /> 选择 1～6 张 PNG/JPEG/WebP
                <input type="file" accept="image/png,image/jpeg,image/webp" multiple className="sr-only" onChange={event => setScreenshots(Array.from(event.target.files || []).slice(0, 6))} />
              </label>
              {screenshots.length > 0 && <p className="mt-2 text-xs text-slate-500">已选 {screenshots.length} 张；每张上限 10MB。</p>}
              <button onClick={createScreenshotIntake} disabled={busy !== null || screenshots.length === 0} className="mt-3 rounded-xl border border-coral-300 px-4 py-2 text-sm font-medium text-coral-600 disabled:opacity-50">
                {busy === 'screenshot' ? 'OCR 与清理中…' : 'OCR 后生成 Intake 草稿'}
              </button>
              <p className="mt-2 text-xs text-slate-500">原图只进入临时目录；终止前删除，只保留 hash、OCR 文本框、置信度和版本回执。</p>
            </div>
          </section>
        )}

        {intake && (
          <>
            <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h2 className="font-semibold">Intake revision {intake.revision}</h2>
                  <p className="mt-1 text-xs text-slate-500">{intake.status} · {intake.extraction.locations.status} · hash {intake.content_hash.slice(0, 12)}…</p>
                </div>
                <MapPin className="h-5 w-5 text-coral-500" />
              </div>
              {intake.extraction.issues.length > 0 && (
                <div className="mt-4 space-y-2">
                  {intake.extraction.issues.map(issue => (
                    <p key={`${issue.code}:${issue.field_path}`} className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900">{issue.field_path} · {issue.message}</p>
                  ))}
                </div>
              )}
              <div className="mt-4 grid gap-3 sm:grid-cols-4">
                <label className="text-xs text-slate-500">国内单一城市<input value={city} onChange={event => setCity(event.target.value)} disabled={intake.status === 'READY'} className="mt-1 w-full rounded-lg border p-2 text-sm text-slate-900" /></label>
                <label className="text-xs text-slate-500">开始日期<input type="date" value={startDate} onChange={event => setStartDate(event.target.value)} disabled={intake.status === 'READY'} className="mt-1 w-full rounded-lg border p-2 text-sm text-slate-900" /></label>
                <label className="text-xs text-slate-500">结束日期<input type="date" value={endDate} onChange={event => setEndDate(event.target.value)} disabled={intake.status === 'READY'} className="mt-1 w-full rounded-lg border p-2 text-sm text-slate-900" /></label>
                <label className="text-xs text-slate-500">人数<input type="number" min={1} value={partySize} onChange={event => setPartySize(Number(event.target.value))} disabled={intake.status === 'READY'} className="mt-1 w-full rounded-lg border p-2 text-sm text-slate-900" /></label>
              </div>
              <div className="mt-4 flex flex-wrap gap-3">
                {intake.status !== 'READY' && <button onClick={correctCoreFields} disabled={busy !== null} className="rounded-xl border border-slate-300 px-4 py-2 text-sm">保存修正并生成新 revision</button>}
                {intake.status !== 'READY' && <button onClick={confirm} disabled={busy !== null} className="rounded-xl bg-coral-500 px-4 py-2 text-sm font-medium text-white">确认城市、日期与人数</button>}
                {intake.status === 'READY' && <button onClick={materialize} disabled={busy !== null} className="inline-flex items-center gap-2 rounded-xl bg-emerald-600 px-4 py-2 text-sm font-medium text-white">{busy === 'materialize' && <Loader2 className="h-4 w-4 animate-spin" />}创建权威行程并进入地点确认</button>}
              </div>
            </section>

            <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
              <h2 className="font-semibold">逐字证据</h2>
              <p className="mt-1 text-xs text-slate-500">偏移按 Unicode code point 解释；Emoji 前后的高亮不会使用 JS 的 UTF-16 slice。</p>
              <div className="mt-3 space-y-2">
                {evidenceRows.map(({ span, source, actual, valid }, index) => (
                  <div key={`${span.source_id}:${span.start}:${index}`} className={`rounded-lg border px-3 py-2 text-xs ${valid ? 'border-emerald-200 bg-emerald-50' : 'border-rose-200 bg-rose-50'}`}>
                    <p className="font-mono">[{span.start}, {span.end}) · {source?.source_id.slice(-18) || 'missing source'}</p>
                    <p className="mt-1">“{actual}” {valid ? '✓' : `≠ “${span.quote}”`}</p>
                  </div>
                ))}
              </div>
            </section>

            {intake.source_type === 'SCREENSHOT_OCR' && (
              <section className="rounded-2xl border border-emerald-200 bg-emerald-50 p-5 text-xs text-emerald-950">
                <h2 className="text-sm font-semibold">OCR 与隐私回执</h2>
                {intake.sources.map(source => (
                  <p key={source.source_id} className="mt-2">{String(source.metadata.ocr_engine || 'OCR')} {String(source.metadata.ocr_engine_version || '')} · 原图保留 {String(source.metadata.raw_asset_retained)}</p>
                ))}
              </section>
            )}
          </>
        )}

        {error && <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div>}
      </div>
    </main>
  )
}
