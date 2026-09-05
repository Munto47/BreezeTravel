'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from '@/lib/trip-understanding-v3'
import {
  releaseCancelledTripInput,
  releaseFailedTripInput,
} from '@/lib/trip-input-recovery'

type PendingOperation = {
  resource: string
  etag: string
  key: string
  claimedResource?: string
  acknowledged?: boolean
  expectedTag?: string
  recoveryChecked?: boolean
  readbackOnly?: boolean
} & (
  | { type: 'command'; command: api.TripUnderstandingCommand }
  | { type: 'adopt'; token: string }
  | { type: 'map'; previousStatus?: api.MapRenderView['status'] }
  | { type: 'stay'; token: string }
  | { type: 'claim' }
)
const PENDING_KEY = 'bt_pending_operation'
const PHASE_RANK: Record<api.TripUnderstandingProgressView['phase'], number> = {
  RECEIVED: 0,
  CARDS_AVAILABLE: 1,
  CHECKING_PLACES: 2,
}

function laterPhase(
  current: api.TripUnderstandingProgressView['phase'],
  candidate: api.TripUnderstandingProgressView['phase'],
) {
  return PHASE_RANK[candidate] >= PHASE_RANK[current] ? candidate : current
}

function cumulativeProgress(
  current: api.TripUnderstandingProgressMetrics,
  candidate: api.TripUnderstandingProgressMetrics,
): api.TripUnderstandingProgressMetrics {
  return {
    day_count: Math.max(current.day_count, candidate.day_count),
    card_count: Math.max(current.card_count, candidate.card_count),
    places_checked: Math.max(current.places_checked, candidate.places_checked),
    places_total: Math.max(current.places_total, candidate.places_total),
  }
}

const rejected = new Set([
  'REVISION_CONFLICT',
  'TRIP_UPDATED',
  'COMMAND_REJECTED',
  'IF_MATCH_REQUIRED',
  'LOGIN_REQUIRED',
  'TRIP_ALREADY_GONE',
  'TRIP_GONE',
  'PREVIEW_STALE',
])

type EnhancementKind = 'map' | 'stay'

type EnhancementReadRecord = {
  resource: string
  generation: number
  epoch: number
  controller: AbortController
  promise: Promise<api.MapRenderView | null | undefined>
}

type EnhancementCycle = {
  resource: string
  generation: number
  epoch: number
  startedAt: number
  rounds: number
}

export async function boundedTripRequest<T>(
  operation: (signal: AbortSignal) => Promise<T>,
  ms = 15000,
): Promise<T> {
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout>
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort()
      reject(new Error('REQUEST_TIMEOUT'))
    }, ms)
  })
  try {
    return await Promise.race([operation(controller.signal), timeout])
  } finally {
    clearTimeout(timer!)
  }
}
const bounded = boundedTripRequest

async function boundedEnhancementRequest<T>(
  operation: (signal: AbortSignal) => Promise<T>,
  parentSignal: AbortSignal,
  ms = 15000,
): Promise<T> {
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout>
  let rejectAbort!: (error: Error) => void
  const aborted = new Promise<never>((_, reject) => {
    rejectAbort = reject
  })
  const abort = () => {
    controller.abort()
    rejectAbort(new Error('REQUEST_ABORTED'))
  }
  if (parentSignal.aborted) abort()
  else parentSignal.addEventListener('abort', abort, { once: true })
  const timedOut = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort()
      reject(new Error('REQUEST_TIMEOUT'))
    }, ms)
  })
  const request = operation(controller.signal)
  try {
    return await Promise.race([request, aborted, timedOut])
  } catch (error) {
    if (controller.signal.aborted) await request.catch(() => undefined)
    throw error
  } finally {
    clearTimeout(timer!)
    parentSignal.removeEventListener('abort', abort)
  }
}

export function useTripExperience() {
  const [resource, setResource] = useState('')
  const [mode, setMode] = useState('FULL')
  const [isDemo, setIsDemo] = useState(false)
  const [result, setResult] = useState<api.UserFacingTripResult | null>(null)
  const [progressSnapshot, setProgressSnapshot] =
    useState<api.UserFacingTripResult | null>(null)
  const [phase, setPhase] =
    useState<api.TripUnderstandingProgressView['phase']>('RECEIVED')
  const [progress, setProgress] =
    useState<api.TripUnderstandingProgressMetrics>({
      day_count: 0,
      card_count: 0,
      places_checked: 0,
      places_total: 0,
    })
  const [streamState, setStreamState] = useState<
    'SYNCING' | 'STREAMING' | 'POLLING' | 'PAUSED'
  >('SYNCING')
  const [cancelling, setCancelling] = useState(false)
  const [map, setMap] = useState<api.MapRenderView | null>(null)
  const [stay, setStay] = useState<api.StaySuggestionView | null>(null)
  const [checks, setChecks] = useState<api.PublicTripChecksView | null>(null)
  const [preview, setPreview] = useState<api.PublicChangePreview | null>(null)
  const [previewStale, setPreviewStale] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const previewBasis = useRef<{
    etag: string
    check: api.PublicTripCheckItem | null
  } | null>(null)
  const previewController = useRef<AbortController | null>(null)
  const checksRef = useRef<api.PublicTripChecksView | null>(null)
  const [supplementary, setSupplementary] =
    useState<api.TripSupplementaryView | null>(null)
  const [writeStatus, setWriteStatus] = useState<
    'IDLE' | 'WRITING' | 'UNKNOWN' | 'CONFIRMED' | 'FAILED'
  >('IDLE')
  const [unavailable, setUnavailable] = useState<
    | 'NONE'
    | 'GONE'
    | 'NOT_AVAILABLE'
    | 'LOGIN'
    | 'FAILED'
    | 'CANCELLED'
    | 'NETWORK'
  >('NONE')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [checking, setChecking] = useState(false)
  const [pending, setPending] = useState<PendingOperation | null>(null)
  const [message, setMessage] = useState('正在整理每天的安排…')
  const [notice, setNotice] = useState('')
  const [checksError, setChecksError] = useState('')
  const [etag, setEtag] = useState('')
  const current = useRef({ resource: '', etag: '', generation: 0 })
  const writing = useRef(false)
  const preparation = useRef<{
    promise: Promise<void>
    basis: string
    generation: number
    controller: AbortController
  } | null>(null)
  const checkAttempt = useRef<{
    basis: string
    key: string
    completed: boolean
  } | null>(null)
  const alive = useRef(true)
  const [retry, setRetry] = useState(0)
  const previousMapStatus = useRef<string | null>(null)
  const mapState = useRef<api.MapRenderView | null>(null)
  const stayState = useRef<api.StaySuggestionView | null>(null)
  const enhancementEpoch = useRef(0)
  const enhancementRead = useRef<EnhancementReadRecord | null>(null)
  const enhancementCycle = useRef<EnhancementCycle | null>(null)
  const [enhancementCycleVersion, setEnhancementCycleVersion] = useState(0)
  const manualEnhancementRetry = useRef<
    Promise<api.MapRenderView | null | undefined> | null
  >(null)
  const eventCursor = useRef(0)
  const retryAfterMs = useRef(500)
  const progressController = useRef<AbortController | null>(null)
  const workspaceOutcome = useRef<{
    status: 'APPLIED' | 'SYNCED' | 'RECONCILING'
    days?: api.UserFacingTripResult['days']
  }>({ status: 'RECONCILING' })
  const resultRead = useRef<{
    key: string
    promise: Promise<{
      body: api.UserFacingTripResult
      etag: string | null
    } | null>
  } | null>(null)

  const invalidateEnhancements = useCallback(() => {
    enhancementEpoch.current += 1
    enhancementCycle.current = null
    enhancementRead.current?.controller.abort()
  }, [])

  const setTag = useCallback((value: string) => {
    if (previewBasis.current && previewBasis.current.etag !== value)
      setPreviewStale(true)
    if (current.current.etag && current.current.etag !== value) {
      invalidateEnhancements()
      mapState.current = null
      stayState.current = null
      setMap(null)
      setStay(null)
    }
    current.current.etag = value
    setEtag(value)
    sessionStorage.setItem('bt_active_trip_etag', value)
  }, [invalidateEnhancements])
  const refresh = useCallback(
    async (
      reference = current.current.resource,
      timeoutMs = 15000,
      parentSignal?: AbortSignal,
    ) => {
      const generation = current.current.generation
      const readKey = `${reference}:${generation}:${timeoutMs}`
      if (resultRead.current?.key === readKey)
        return resultRead.current.promise
      let promise!: Promise<{
        body: api.UserFacingTripResult
        etag: string | null
      } | null>
      promise = (async () => {
        try {
          const response = parentSignal
            ? await boundedEnhancementRequest(
                (signal) =>
                  api.readTripUnderstandingResult(reference, signal),
                parentSignal,
                timeoutMs,
              )
            : await bounded(
                (signal) =>
                  api.readTripUnderstandingResult(reference, signal),
                timeoutMs,
              )
          if (
            !alive.current ||
            generation !== current.current.generation ||
            reference !== current.current.resource
          )
            return null
          if (response.status === 202) {
            const body = response.body as api.TripUnderstandingProgressView
            eventCursor.current = Math.max(
              eventCursor.current,
              body.event_cursor,
            )
            sessionStorage.setItem(
              `bt_trip_event_cursor:${reference}`,
              String(eventCursor.current),
            )
            retryAfterMs.current = body.retry_after_ms
            setMessage(body.message)
            setPhase((value) => laterPhase(value, body.phase))
            setProgress((value) => cumulativeProgress(value, body.progress))
            setProgressSnapshot((value) => body.snapshot || value)
            return null
          }
          const body = response.body as api.UserFacingTripResult
          api.clearTripUnderstandingInputDraft(reference)
          setResult(body)
          setProgressSnapshot(null)
          setLoading(false)
          setStreamState('PAUSED')
          setUnavailable('NONE')
          if (body.is_demo !== undefined) {
            setIsDemo(body.is_demo)
            sessionStorage.setItem(
              'bt_active_trip_is_demo',
              String(body.is_demo),
            )
          }
          if (response.etag) setTag(response.etag)
          if (body.ownership === 'ACCOUNT') {
            setMode('CLAIMED')
            sessionStorage.setItem('bt_active_trip_mode', 'CLAIMED')
          } else if (body.ownership === 'ANONYMOUS') {
            setMode(body.is_demo ? 'DEMO' : 'FULL')
            sessionStorage.setItem(
              'bt_active_trip_mode',
              body.is_demo ? 'DEMO' : 'FULL',
            )
          }
          return { body, etag: response.etag }
        } finally {
          if (resultRead.current?.promise === promise) resultRead.current = null
        }
      })()
      resultRead.current = { key: readKey, promise }
      return promise
    },
    [setTag],
  )

  const readMapAndStay = useCallback(
    async (
      requestedKinds: EnhancementKind[] = ['map', 'stay'],
    ): Promise<api.MapRenderView | null | undefined> => {
      const kinds = [...new Set(requestedKinds)]
      if (!kinds.length) return mapState.current
      const { resource: reference, generation } = current.current
      if (!reference) return
      const epoch = enhancementEpoch.current

      while (enhancementRead.current) {
        const active = enhancementRead.current
        if (
          active.resource === reference &&
          active.generation === generation &&
          active.epoch === epoch
        )
          return active.promise
        active.controller.abort()
        await active.promise.catch(() => undefined)
      }

      let cycle = enhancementCycle.current
      if (
        !cycle ||
        cycle.resource !== reference ||
        cycle.generation !== generation ||
        cycle.epoch !== epoch
      ) {
        cycle = {
          resource: reference,
          generation,
          epoch,
          startedAt: Date.now(),
          rounds: 0,
        }
        enhancementCycle.current = cycle
        setEnhancementCycleVersion((value) => value + 1)
      }
      cycle.rounds += 1

      const controller = new AbortController()
      const promise = (async () => {
        const responses = await Promise.allSettled(
          kinds.map((kind) =>
            boundedEnhancementRequest(
              async (signal) =>
                kind === 'map'
                  ? await api.readTripUnderstandingMap(reference, signal)
                  : await api.readTripUnderstandingStay(reference, signal),
              controller.signal,
            ),
          ),
        )
        if (
          !alive.current ||
          generation !== current.current.generation ||
          epoch !== enhancementEpoch.current ||
          reference !== current.current.resource
        )
          return mapState.current

        let mapOutcome = mapState.current
        responses.forEach((response, index) => {
          const kind = kinds[index]
          if (kind === 'map') {
            const next: api.MapRenderView =
              response.status === 'fulfilled'
                ? (response.value as api.MapRenderView)
                : {
                    status: 'UNAVAILABLE',
                    message: '路线暂时无法显示，行程卡片不受影响。',
                    days: [],
                    points: [],
                    available_actions: [],
                  }
            mapState.current = next
            mapOutcome = next
            setMap(next)
            return
          }
          const next: api.StaySuggestionView =
            response.status === 'fulfilled'
              ? (response.value as api.StaySuggestionView)
              : {
                  status: 'UNAVAILABLE',
                  message: '住宿建议暂时不可用，不影响查看和调整行程。',
                  area_summary: null,
                  searched_scopes: [],
                  candidates: [],
                  available_actions: [],
                }
          stayState.current = next
          setStay(next)
        })
        return mapOutcome
      })()
      const active: EnhancementReadRecord = {
        resource: reference,
        generation,
        epoch,
        controller,
        promise,
      }
      enhancementRead.current = active
      try {
        return await promise
      } finally {
        if (enhancementRead.current === active) enhancementRead.current = null
      }
    },
    [],
  )

  const loadSupplementary = useCallback(async () => {
    const { resource: reference, generation } = current.current
    if (!reference) return
    try {
      const next = await bounded((signal) =>
        api.readTripSupplementary(reference, signal),
      )
      if (generation === current.current.generation && alive.current)
        setSupplementary(next)
    } catch {
      if (generation === current.current.generation && alive.current)
        setSupplementary({ status: 'UNAVAILABLE', days: [] })
    }
  }, [])

  const prepareChecks = useCallback(
    function prepareCurrentChecks(
      reason: 'read' | 'user' | 'map' = 'read',
    ): Promise<void> {
      const { resource: reference, etag: tag, generation } = current.current
      if (!reference || !tag || !alive.current || writing.current)
        return Promise.resolve()
      const basis = `${reference}:${tag}`
      const activePreparation = preparation.current
      if (activePreparation) {
        if (
          reason === 'read' &&
          activePreparation.generation === generation &&
          activePreparation.basis === basis
        )
          return activePreparation.promise
        return activePreparation.promise.then(() => {
          if (
            !alive.current ||
            generation !== current.current.generation ||
            reference !== current.current.resource ||
            tag !== current.current.etag
          )
            return
          return prepareCurrentChecks(reason)
        })
      }
      if (
        reason === 'read' &&
        checkAttempt.current?.basis === basis &&
        checkAttempt.current.completed
      )
        return Promise.resolve()
      if (
        !checkAttempt.current ||
        checkAttempt.current.basis !== basis ||
        reason === 'map' ||
        (reason === 'user' && checkAttempt.current.completed)
      )
        checkAttempt.current = {
          basis,
          key: api.createTripRequestKey(),
          completed: false,
        }
      let attempt = checkAttempt.current
      if (previewBasis.current && reason !== 'read') setPreviewStale(true)
      setChecking(true)
      setChecksError('')
      let effectiveTag = tag
      let effectiveGeneration = generation
      const controller = new AbortController()
      let promise!: Promise<void>
      promise = (async () => {
        try {
          let prepared: Awaited<
            ReturnType<typeof api.materializeTripUnderstanding>
          >
          try {
            prepared = await boundedEnhancementRequest(
              (signal) =>
                api.materializeTripUnderstanding(
                  reference,
                  tag,
                  signal,
                  attempt.key,
                ),
              controller.signal,
            )
          } catch (error) {
            if (!(error instanceof Error) || error.message !== 'TRIP_UPDATED')
              throw error
            const latest = await refresh(reference, 15000, controller.signal)
            if (!latest?.etag) throw error
            effectiveGeneration = current.current.generation
            effectiveTag = latest.etag
            const retryAttempt = {
              basis: `${reference}:${latest.etag}`,
              key: api.createTripRequestKey(),
              completed: false,
            }
            checkAttempt.current = retryAttempt
            attempt = retryAttempt
            prepared = await boundedEnhancementRequest(
              (signal) =>
                api.materializeTripUnderstanding(
                  reference,
                  latest.etag!,
                  signal,
                  retryAttempt.key,
                ),
              controller.signal,
            )
          }
          const exactBasis =
            effectiveGeneration === current.current.generation &&
            effectiveTag === current.current.etag
          const compatibleCurrentResult = prepared.etag === current.current.etag
          if (
            reference !== current.current.resource ||
            (!exactBasis && !compatibleCurrentResult) ||
            !alive.current
          )
            return
          effectiveGeneration = current.current.generation
          setTag(prepared.etag)
          effectiveTag = prepared.etag
          attempt.basis = `${reference}:${prepared.etag}`
          if (preparation.current?.promise === promise) {
            preparation.current.basis = attempt.basis
            preparation.current.generation = effectiveGeneration
          }
          const next = await boundedEnhancementRequest(
            (signal) => api.readTripUnderstandingChecks(reference, signal),
            controller.signal,
          )
          if (
            effectiveGeneration === current.current.generation &&
            reference === current.current.resource &&
            effectiveTag === current.current.etag &&
            alive.current
          ) {
            setChecks(next)
            checksRef.current = next
            attempt.completed = true
          }
        } catch {
          if (
            !controller.signal.aborted &&
            effectiveGeneration === current.current.generation &&
            reference === current.current.resource &&
            effectiveTag === current.current.etag &&
            alive.current
          ) {
            setChecks(null)
            checksRef.current = null
            setChecksError('暂时没能检查完整，可以稍后重试。')
          }
        } finally {
          if (preparation.current?.promise === promise)
            preparation.current = null
          if (
            alive.current &&
            effectiveGeneration === current.current.generation &&
            reference === current.current.resource &&
            effectiveTag === current.current.etag
          )
            setChecking(false)
        }
      })()
      preparation.current = { promise, basis, generation, controller }
      return promise
    },
    [setTag],
  )

  useEffect(() => {
    alive.current = true
    const addressReference = new URLSearchParams(
      window.location.hash.slice(1),
    ).get('trip')
    const storedReference = sessionStorage.getItem('bt_active_trip_ref')
    let reference =
      addressReference && /^[A-Za-z0-9_-]{20,80}$/.test(addressReference)
        ? addressReference
        : storedReference
    try {
      const unconfirmed = JSON.parse(
        sessionStorage.getItem(PENDING_KEY) || 'null',
      ) as PendingOperation | null
      const original = unconfirmed?.claimedResource || unconfirmed?.resource
      if (
        original &&
        original !== reference &&
        /^[A-Za-z0-9_-]{20,80}$/.test(original)
      ) {
        reference = original
        window.history.replaceState(
          null,
          '',
          `/trip/result#trip=${encodeURIComponent(original)}`,
        )
      }
    } catch {
      /* Invalid stored operations are discarded below. */
    }
    if (!reference) {
      setMessage('没有可恢复的行程，请从首页开始。')
      setLoading(false)
      return
    }
    if (reference !== storedReference) {
      sessionStorage.removeItem('bt_active_trip_is_demo')
      sessionStorage.removeItem('bt_active_trip_mode')
      sessionStorage.removeItem('bt_active_trip_source_deleted')
      sessionStorage.removeItem('bt_claim_after_login')
    }
    if (current.current.resource !== reference) {
      current.current.etag = ''
      setEtag('')
      setResult(null)
      setProgressSnapshot(null)
      setPhase('RECEIVED')
      setProgress({
        day_count: 0,
        card_count: 0,
        places_checked: 0,
        places_total: 0,
      })
      setStreamState('SYNCING')
      setCancelling(false)
      setMap(null)
      setStay(null)
      setChecks(null)
      setPreview(null)
      setSupplementary(null)
      setNotice('')
      setPending(null)
      setPreviewStale(false)
      previewBasis.current = null
      checksRef.current = null
      checkAttempt.current = null
      previousMapStatus.current = null
      preparation.current = null
      setWriteStatus('IDLE')
      setUnavailable('NONE')
      setChecking(false)
      setPreviewLoading(false)
      previewController.current?.abort()
      previewController.current = null
    }
    invalidateEnhancements()
    current.current.generation += 1
    current.current.resource = reference
    eventCursor.current = Number(
      sessionStorage.getItem(`bt_trip_event_cursor:${reference}`) || '0',
    )
    sessionStorage.setItem('bt_active_trip_ref', reference)
    setResource(reference)
    setMode(sessionStorage.getItem('bt_active_trip_mode') || 'FULL')
    const demoSource =
      sessionStorage.getItem('bt_active_trip_is_demo') === 'true' ||
      sessionStorage.getItem('bt_active_trip_mode') === 'DEMO'
    setIsDemo(demoSource)
    sessionStorage.setItem('bt_active_trip_is_demo', String(demoSource))
    let storedPendingForRecovery: PendingOperation | null = null
    try {
      const stored = JSON.parse(
        sessionStorage.getItem(PENDING_KEY) || 'null',
      ) as PendingOperation | null
      storedPendingForRecovery = stored
      if (
        stored &&
        (stored.resource === reference || stored.claimedResource === reference)
      ) {
        setPending(stored)
        setWriteStatus(stored.type === 'map' ? 'CONFIRMED' : 'UNKNOWN')
        setNotice('上次修改尚未确认，请先确认保存结果。')
      }
    } catch {
      sessionStorage.removeItem(PENDING_KEY)
    }
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const generation = current.current.generation
    const deadline = Date.now() + 90000
    const wait = (delay: number) =>
      new Promise<void>((resolve) => {
        timer = setTimeout(resolve, delay)
      })
    const finalizeReady = () => {
      void readMapAndStay()
      void loadSupplementary()
      if (!sessionStorage.getItem(PENDING_KEY)) void prepareChecks()
      if (!sessionStorage.getItem(PENDING_KEY)) setWriteStatus('CONFIRMED')
    }
    const handleTerminalError = (error: unknown): boolean => {
      const code = error instanceof Error ? error.message : ''
      if (
        ![
          'TRIP_GONE',
          'TRIP_NOT_AVAILABLE',
          'LOGIN_REQUIRED',
          'UNDERSTANDING_FAILED',
          'UNDERSTANDING_CANCELLED',
        ].includes(code)
      )
        return false
      const recoveredInput =
        code === 'UNDERSTANDING_FAILED'
          ? releaseFailedTripInput(reference!)
          : code === 'UNDERSTANDING_CANCELLED'
            ? releaseCancelledTripInput(reference!)
            : null
      const gone = code === 'TRIP_GONE'
      if (gone) {
        try {
          const operation = JSON.parse(
            sessionStorage.getItem(PENDING_KEY) || 'null',
          ) as PendingOperation | null
          if (
            operation?.resource === reference ||
            operation?.claimedResource === reference
          ) {
            sessionStorage.removeItem(PENDING_KEY)
            setPending(null)
          }
        } catch {
          /* Do not clear a different operation. */
        }
        if (sessionStorage.getItem('bt_active_trip_ref') === reference)
          api.clearTripUnderstandingSession()
      }
      setLoading(false)
      setStreamState('PAUSED')
      setUnavailable(
        gone
          ? 'GONE'
          : code === 'TRIP_NOT_AVAILABLE'
            ? 'NOT_AVAILABLE'
            : code === 'LOGIN_REQUIRED'
              ? 'LOGIN'
              : code === 'UNDERSTANDING_FAILED'
                ? 'FAILED'
                : 'CANCELLED',
      )
      setMessage(
        gone
          ? '这份行程已过期或已删除，可以重新整理一份。'
          : code === 'TRIP_NOT_AVAILABLE'
            ? '当前无法访问这份行程。请确认使用保存它的账号，或重新读取。'
            : code === 'LOGIN_REQUIRED'
              ? '请登录保存这份行程的账号后继续。'
              : code === 'UNDERSTANDING_FAILED'
                ? recoveredInput
                  ? '这次没有整理完成，原文已保留。回到首页即可重试，也可以先修改文字。'
                  : '这次没有整理完成。可以回到首页重新整理。'
                : recoveredInput
                  ? '整理已停止，原文仍在首页，可以修改后重新开始。'
                  : '整理已停止，可以返回首页重新开始。',
      )
      return true
    }
    const authoritativeRead = async () => {
      try {
        const next = await refresh(reference!)
        if (next) {
          finalizeReady()
          return true
        }
      } catch (error) {
        const code = error instanceof Error ? error.message : ''
        const recoverableClaim =
          code === 'TRIP_GONE' &&
          storedPendingForRecovery?.type === 'claim' &&
          !storedPendingForRecovery.claimedResource &&
          storedPendingForRecovery.resource === reference
        if (recoverableClaim) {
          try {
            const recovered = await bounded((signal) =>
              api.claimTripUnderstanding(
                storedPendingForRecovery!.resource,
                storedPendingForRecovery!.key,
                signal,
              ),
            )
            const recoveredOperation: PendingOperation = {
              ...storedPendingForRecovery!,
              claimedResource: recovered.body.public_resource_id,
              acknowledged: true,
              expectedTag: recovered.etag,
            }
            storedPendingForRecovery = recoveredOperation
            sessionStorage.setItem(
              PENDING_KEY,
              JSON.stringify(recoveredOperation),
            )
            setPending(recoveredOperation)
            sessionStorage.setItem(
              'bt_active_trip_ref',
              recovered.body.public_resource_id,
            )
            sessionStorage.setItem('bt_active_trip_mode', 'CLAIMED')
            current.current.resource = recovered.body.public_resource_id
            setResource(recovered.body.public_resource_id)
            setMode('CLAIMED')
            window.history.replaceState(
              null,
              '',
              `/trip/result#trip=${encodeURIComponent(recovered.body.public_resource_id)}`,
            )
            setRetry((value) => value + 1)
            return true
          } catch (claimError) {
            const claimCode =
              claimError instanceof Error ? claimError.message : ''
            if (
              claimCode === 'OPERATION_PENDING' ||
              claimCode === 'CLAIM_FAILED' ||
              claimCode === 'REQUEST_TIMEOUT'
            ) {
              setMessage(
                '账号保存结果仍在确认中；系统会继续使用同一次保存凭据恢复。',
              )
              return false
            }
            if (claimCode === 'LOGIN_REQUIRED') {
              sessionStorage.setItem('bt_claim_after_login', 'true')
              setLoading(false)
              setStreamState('PAUSED')
              setUnavailable('LOGIN')
              setMessage(
                '登录状态已失效。重新登录后会继续使用同一次保存凭据恢复，不会重复创建行程。',
              )
              return true
            }
            if (claimCode !== 'TRIP_ALREADY_GONE') {
              setMessage(
                '账号保存结果仍无法确认；系统已保留同一次保存凭据，将继续安全恢复。',
              )
              return false
            }
          }
        }
        if (handleTerminalError(error)) return true
        throw error
      }
      return false
    }
    async function followProgress() {
      let streamFailures = 0
      setLoading(true)
      setStreamState('SYNCING')
      try {
        if (await authoritativeRead()) return
      } catch {
        streamFailures += 1
      }
      while (
        !stopped &&
        alive.current &&
        current.current.generation === generation &&
        Date.now() < deadline
      ) {
        if (streamFailures >= 2) {
          setStreamState('POLLING')
          await wait(
            Math.min(
              Math.max(500, Math.min(5000, retryAfterMs.current)),
              Math.max(0, deadline - Date.now()),
            ),
          )
          if (stopped) return
          try {
            if (await authoritativeRead()) return
            streamFailures = 0
          } catch {
            streamFailures += 1
            continue
          }
        }
        setStreamState('STREAMING')
        const controller = new AbortController()
        progressController.current = controller
        const cursorBefore = eventCursor.current
        let streamIdleTimer: ReturnType<typeof setTimeout>
        const renewStreamDeadline = () => {
          clearTimeout(streamIdleTimer)
          streamIdleTimer = setTimeout(
            () => controller.abort(),
            Math.min(45000, Math.max(1, deadline - Date.now())),
          )
        }
        renewStreamDeadline()
        try {
          await api.streamTripUnderstandingEvents(
            reference!,
            (event) => {
              if (
                stopped ||
                generation !== current.current.generation ||
                reference !== current.current.resource ||
                event.id <= eventCursor.current
              )
                return
              renewStreamDeadline()
              eventCursor.current = event.id
              sessionStorage.setItem(
                `bt_trip_event_cursor:${reference}`,
                String(event.id),
              )
              setMessage(event.message)
              if (event.phase)
                setPhase((value) => laterPhase(value, event.phase!))
              setProgress((value) =>
                cumulativeProgress(value, event.progress),
              )
              if (event.snapshot) setProgressSnapshot(event.snapshot)
            },
            controller.signal,
            eventCursor.current,
          )
          streamFailures =
            eventCursor.current > cursorBefore ? 0 : streamFailures + 1
        } catch (error) {
          if (
            stopped ||
            generation !== current.current.generation ||
            reference !== current.current.resource
          )
            return
          streamFailures += 1
        } finally {
          clearTimeout(streamIdleTimer!)
          if (progressController.current === controller)
            progressController.current = null
        }
        try {
          if (await authoritativeRead()) return
        } catch {
          streamFailures += 1
        }
      }
      if (!stopped && generation === current.current.generation) {
        setLoading(false)
        setStreamState('PAUSED')
        setMessage(
          progressSnapshot
            ? '已保留当前整理进度。你可以继续等待，或停止后编辑现有卡片。'
            : '整理时间比预计更久。你可以继续等待或停止整理。',
        )
      }
    }
    void followProgress()
    return () => {
      stopped = true
      alive.current = false
      invalidateEnhancements()
      progressController.current?.abort()
      progressController.current = null
      clearTimeout(timer)
    }
  }, [
    retry,
    refresh,
    prepareChecks,
    readMapAndStay,
    loadSupplementary,
    invalidateEnhancements,
  ])

  useEffect(() => {
    const followAddress = () => setRetry((value) => value + 1)
    window.addEventListener('hashchange', followAddress)
    return () => window.removeEventListener('hashchange', followAddress)
  }, [])

  useEffect(() => {
    mapState.current = map
  }, [map])

  useEffect(() => {
    stayState.current = stay
  }, [stay])

  const enhancementsPreparing =
    map?.status === 'PREPARING' || stay?.status === 'PREPARING'

  useEffect(() => {
    if (!enhancementsPreparing) return
    const cycle = enhancementCycle.current
    if (!cycle || cycle.epoch !== enhancementEpoch.current) return
    const activeCycle = cycle
    let stopped = false
    let waitTimer: ReturnType<typeof setTimeout>
    const deadline = activeCycle.startedAt + 10000

    const expirePending = () => {
      if (
        stopped ||
        activeCycle !== enhancementCycle.current ||
        activeCycle.epoch !== enhancementEpoch.current
      )
        return
      if (mapState.current?.status === 'PREPARING') {
        const next: api.MapRenderView = {
          status: 'UNAVAILABLE',
          message: '路线暂时无法显示，行程卡片不受影响。',
          points: [],
          days: [],
          available_actions: [],
        }
        mapState.current = next
        setMap(next)
      }
      if (stayState.current?.status === 'PREPARING') {
        const next: api.StaySuggestionView = {
          status: 'UNAVAILABLE',
          message: '住宿建议暂时不可用，不影响查看和调整行程。',
          area_summary: null,
          searched_scopes: [],
          candidates: [],
          available_actions: [],
        }
        stayState.current = next
        setStay(next)
      }
    }

    const remaining = Math.max(0, deadline - Date.now())
    const deadlineTimer = setTimeout(() => {
      if (
        activeCycle !== enhancementCycle.current ||
        activeCycle.epoch !== enhancementEpoch.current
      )
        return
      const active = enhancementRead.current
      if (active?.epoch === activeCycle.epoch) active.controller.abort()
      expirePending()
    }, remaining)

    const wait = (ms: number) =>
      new Promise<void>((resolve) => {
        waitTimer = setTimeout(resolve, ms)
      })

    async function pollPending() {
      while (
        !stopped &&
        activeCycle === enhancementCycle.current &&
        activeCycle.epoch === enhancementEpoch.current &&
        activeCycle.rounds < 8 &&
        Date.now() < deadline
      ) {
        await wait(Math.min(800, Math.max(0, deadline - Date.now())))
        if (
          stopped ||
          activeCycle !== enhancementCycle.current ||
          activeCycle.epoch !== enhancementEpoch.current ||
          Date.now() >= deadline
        )
          break
        const pendingKinds: EnhancementKind[] = []
        if (mapState.current?.status === 'PREPARING')
          pendingKinds.push('map')
        if (stayState.current?.status === 'PREPARING')
          pendingKinds.push('stay')
        if (!pendingKinds.length) return
        await readMapAndStay(pendingKinds)
      }
      if (!stopped) expirePending()
    }

    void pollPending()
    return () => {
      stopped = true
      clearTimeout(waitTimer!)
      clearTimeout(deadlineTimer)
    }
  }, [enhancementsPreparing, enhancementCycleVersion, readMapAndStay])

  useEffect(() => {
    const prior = previousMapStatus.current
    previousMapStatus.current = map?.status || null
    if (
      prior === 'PREPARING' &&
      map &&
      map.status !== 'PREPARING' &&
      !pending &&
      !writing.current
    )
      void prepareChecks('map')
  }, [map, pending, prepareChecks])

  const stopUnderstanding = useCallback(async (): Promise<boolean> => {
    const { resource: reference } = current.current
    if (!reference || cancelling || result) return false
    setCancelling(true)
    setNotice('')
    try {
      const stopped = await bounded((signal) =>
        api.cancelTripUnderstanding(reference, signal),
      )
      if (!alive.current || reference !== current.current.resource) return false
      invalidateEnhancements()
      current.current.generation += 1
      progressController.current?.abort()
      progressController.current = null
      if (stopped.body.status === 'STOPPED_EMPTY') {
        releaseCancelledTripInput(reference)
        setProgressSnapshot(null)
        setLoading(false)
        setStreamState('PAUSED')
        setUnavailable('CANCELLED')
        setMessage('整理已停止，原文仍在首页，可以修改后重新开始。')
        return true
      }
      const latest = await refresh(reference)
      if (!latest) {
        setNotice('整理已经结束，正在读取最后结果。')
        setRetry((value) => value + 1)
        return true
      }
      setNotice('已停止继续核对地点，当前卡片可以编辑。')
      void readMapAndStay()
      void loadSupplementary()
      void prepareChecks()
      setWriteStatus('CONFIRMED')
      return true
    } catch (error) {
      const code = error instanceof Error ? error.message : ''
      if (code === 'UNDERSTANDING_CANCELLED') {
        releaseCancelledTripInput(reference)
        setProgressSnapshot(null)
        setLoading(false)
        setStreamState('PAUSED')
        setUnavailable('CANCELLED')
        setMessage('整理已停止，原文仍在首页，可以修改后重新开始。')
        return true
      }
      setNotice('尚未确认是否已经停止，系统不会重复创建行程，请稍后重试。')
      return false
    } finally {
      if (alive.current && reference === current.current.resource)
        setCancelling(false)
    }
  }, [
    cancelling,
    invalidateEnhancements,
    loadSupplementary,
    prepareChecks,
    readMapAndStay,
    refresh,
    result,
  ])

  const execute = useCallback(
    async (operation: PendingOperation): Promise<boolean> => {
      if (writing.current) return false
      if (operation.type === 'command')
        workspaceOutcome.current = { status: 'RECONCILING' }
      writing.current = true
      setBusy(true)
      if (operation.type !== 'map') setWriteStatus('WRITING')
      setNotice('')
      const activePreparation = preparation.current
      if (activePreparation) {
        activePreparation.controller.abort()
        await activePreparation.promise
      }
      // The caller captures the tag only after background materialization settles.
      if (!pending) operation = { ...operation, etag: current.current.etag }
      const op = operation
      const operationDeadline = Date.now() + 15000
      const remainingBudget = () =>
        Math.max(1, operationDeadline - Date.now())
      const withinOperationDeadline = <T,>(
        request: (signal: AbortSignal) => Promise<T>,
      ) => bounded(request, remainingBudget())
      invalidateEnhancements()
      previewController.current?.abort()
      current.current.generation += 1
      setPreviewLoading(false)
      if (previewBasis.current) setPreviewStale(true)
      setChecks(null)
      checksRef.current = null
      setPending(operation)
      sessionStorage.setItem(PENDING_KEY, JSON.stringify(operation))
      let writeAcknowledged = false
      const rememberAcknowledgement = (expectedTag?: string) => {
        operation = {
          ...operation,
          acknowledged: true,
          ...(expectedTag ? { expectedTag } : {}),
        }
        setPending(operation)
        sessionStorage.setItem(PENDING_KEY, JSON.stringify(operation))
      }
      try {
        let expectedTag: string | undefined
        if (op.type === 'command') {
          expectedTag = (
            await withinOperationDeadline((signal) =>
              api.applyTripUnderstandingCommand(
                op.resource,
                op.etag,
                op.command,
                op.key,
                signal,
              ),
            )
          ).etag
          writeAcknowledged = true
          rememberAcknowledgement(expectedTag)
        }
        if (op.type === 'adopt') {
          expectedTag = (
            await withinOperationDeadline((signal) =>
              api.adoptTripUnderstandingChange(
                op.resource,
                op.token,
                op.etag,
                op.key,
                signal,
              ),
            )
          ).etag
          writeAcknowledged = true
          rememberAcknowledgement(expectedTag)
        }
        if (op.type === 'stay') {
          expectedTag = (
            await withinOperationDeadline((signal) =>
              api.selectTripUnderstandingStay(
                op.resource,
                op.token,
                op.etag,
                op.key,
                signal,
              ),
            )
          ).etag
          writeAcknowledged = true
          rememberAcknowledgement(expectedTag)
        }
        if (operation.type === 'map') {
          await withinOperationDeadline((signal) =>
            api.requestTripUnderstandingMap(
              operation.resource,
              operation.etag,
              operation.key,
              signal,
            ),
          )
          writeAcknowledged = true
          rememberAcknowledgement()
        }
        if (operation.type === 'claim') {
          const claimed = await withinOperationDeadline((signal) =>
            api.claimTripUnderstanding(
              operation.resource,
              operation.key,
              signal,
            ),
          )
          writeAcknowledged = true
          current.current.resource = claimed.body.public_resource_id
          setResource(claimed.body.public_resource_id)
          sessionStorage.setItem(
            'bt_active_trip_ref',
            claimed.body.public_resource_id,
          )
          window.history.replaceState(
            null,
            '',
            `/trip/result#trip=${encodeURIComponent(claimed.body.public_resource_id)}`,
          )
          operation = {
            ...operation,
            claimedResource: claimed.body.public_resource_id,
            acknowledged: true,
            expectedTag: claimed.etag,
          }
          sessionStorage.setItem(PENDING_KEY, JSON.stringify(operation))
          setPending(operation)
          setMode('CLAIMED')
          sessionStorage.setItem('bt_active_trip_mode', 'CLAIMED')
          expectedTag = claimed.etag
        }
        const latest = await refresh(
          current.current.resource,
          remainingBudget(),
        )
        if (!latest?.etag) throw new Error('READBACK_REQUIRED')
        if (
          ['command', 'adopt', 'stay'].includes(operation.type) &&
          latest.etag === op.etag
        )
          throw new Error('READBACK_REQUIRED')
        if (operation.type === 'command')
          workspaceOutcome.current = {
            status: 'APPLIED',
            days: latest.body.days,
          }
        sessionStorage.removeItem(PENDING_KEY)
        setPending(null)
        if (operation.type !== 'map') setWriteStatus('CONFIRMED')
        if (operation.type === 'adopt') {
          setPreview(null)
          previewBasis.current = null
        }
        setNotice(
          expectedTag && expectedTag !== latest.etag
            ? '这份行程也有其他更新，已显示最新内容。'
            : operation.type === 'map'
              ? '正在准备更新后的路线。'
              : operation.type === 'claim'
                ? '已保存到账号，保留 30 天。'
                : operation.type === 'command' &&
                    operation.command.command_type === 'UNDO'
                  ? '已撤销上次调整，路线状态已重新判断。'
                  : '修改已保留，路线需要更新时请主动更新。',
        )
        if (!['AVAILABLE', 'LIMITED'].includes(latest.body.map.status)) {
          setMap({
            status: latest.body.map.status,
            message: latest.body.map.message,
            points: [],
            days: [],
            available_actions: latest.body.map.available_actions,
          })
        }
        void readMapAndStay().then((latestMap) =>
          prepareChecks(
            operation.type === 'map' && latestMap?.status !== 'PREPARING'
              ? 'map'
              : 'read',
          ),
        )
        return true
      } catch (error) {
        if (error instanceof Error && rejected.has(error.message)) {
          const versionConflict =
            error.message === 'TRIP_UPDATED' ||
            error.message === 'REVISION_CONFLICT'
          if (operation.type === 'command')
            workspaceOutcome.current = { status: 'SYNCED' }
          if (error.message === 'PREVIEW_STALE') setPreviewStale(true)
          if (
            error.message === 'TRIP_GONE' ||
            error.message === 'TRIP_ALREADY_GONE'
          ) {
            sessionStorage.removeItem(PENDING_KEY)
            setPending(null)
            if (operation.type !== 'map') setWriteStatus('FAILED')
            if (
              sessionStorage.getItem('bt_active_trip_ref') ===
              current.current.resource
            )
              api.clearTripUnderstandingSession()
            setResult(null)
            setUnavailable('GONE')
            setMessage('这份行程已过期或已删除，可以重新整理一份。')
            return false
          }
          let latest: Awaited<ReturnType<typeof refresh>> = null
          try {
            latest = await refresh()
          } catch {
            if (versionConflict) {
              operation = { ...operation, readbackOnly: true }
              sessionStorage.setItem(PENDING_KEY, JSON.stringify(operation))
              setPending(operation)
              if (operation.type !== 'map') setWriteStatus('UNKNOWN')
              setNotice(
                '这次操作未被接受，但最新服务端版本暂时无法读取；请再次确认。',
              )
              return false
            }
          }
          sessionStorage.removeItem(PENDING_KEY)
          setPending(null)
          if (operation.type !== 'map') setWriteStatus('FAILED')
          if (operation.type === 'command')
            workspaceOutcome.current = {
              status: 'SYNCED',
              days: latest?.body.days,
            }
          setNotice(
            error.message === 'PREVIEW_STALE'
              ? '行程或路线依据已有变化，请重新预览。'
              : error.message === 'TRIP_UPDATED' ||
                  error.message === 'REVISION_CONFLICT'
                ? '卡片刚刚有更新，已为你读取最新版本，请再试一次。'
              : error.message === 'COMMAND_REJECTED'
                ? '这次修改没有被接受，请检查时间、地点或安排后重试。'
                : '行程或登录状态已变化，已尝试读取最新内容，请重试。',
          )
          try {
            await readMapAndStay()
            void prepareChecks()
          } catch {
            /* Keep last known result visible. */
          }
        } else {
          if (operation.type !== 'map') setWriteStatus('UNKNOWN')
          setNotice(
            operation.type === 'map'
              ? writeAcknowledged
                ? '路线更新已受理，但最新状态暂时无法读取；确认时不会重复创建路线任务。'
                : '路线更新等待时间较长，尚未确认是否已开始；确认时会复用同一请求凭据。'
              : writeAcknowledged
                ? '调整已提交，但保存结果暂时无法确认。请确认这次操作，避免重复提交。'
                : '调整保存等待时间较长，尚未确认是否已生效。请确认这次操作，避免重复提交。',
          )
        }
        return false
      } finally {
        writing.current = false
        setBusy(false)
      }
    },
    [
      invalidateEnhancements,
      pending,
      prepareChecks,
      readMapAndStay,
      refresh,
    ],
  )

  const reconcilePending = useCallback(async (): Promise<boolean> => {
    let operation = pending
    if (!operation || writing.current) return false
    writing.current = true
    setBusy(true)
    setNotice(
      operation.type === 'map'
        ? '正在读取路线任务状态，不会重复提交路线请求。'
        : '正在核对服务端保存结果。',
    )
    let latestBeforeReplay: {
      body: api.UserFacingTripResult
      etag: string | null
    } | null = null

    const rememberOperation = (value: PendingOperation) => {
      operation = value
      setPending(value)
      sessionStorage.setItem(PENDING_KEY, JSON.stringify(value))
    }

    const replayWithSameKey = async (
      value: PendingOperation,
    ): Promise<PendingOperation> => {
      if (value.type === 'map') {
        await bounded((signal) =>
          api.requestTripUnderstandingMap(
            value.resource,
            value.etag,
            value.key,
            signal,
          ),
        )
        return { ...value, acknowledged: true }
      }
      if (value.type === 'command') {
        const response = await bounded((signal) =>
          api.applyTripUnderstandingCommand(
            value.resource,
            value.etag,
            value.command,
            value.key,
            signal,
          ),
        )
        return {
          ...value,
          acknowledged: true,
          expectedTag: response.etag,
        }
      }
      if (value.type === 'adopt') {
        const response = await bounded((signal) =>
          api.adoptTripUnderstandingChange(
            value.resource,
            value.token,
            value.etag,
            value.key,
            signal,
          ),
        )
        return {
          ...value,
          acknowledged: true,
          expectedTag: response.etag,
        }
      }
      if (value.type === 'stay') {
        const response = await bounded((signal) =>
          api.selectTripUnderstandingStay(
            value.resource,
            value.token,
            value.etag,
            value.key,
            signal,
          ),
        )
        return {
          ...value,
          acknowledged: true,
          expectedTag: response.etag,
        }
      }
      if (value.type === 'claim') {
        const response = await bounded((signal) =>
          api.claimTripUnderstanding(value.resource, value.key, signal),
        )
        const claimedResource = response.body.public_resource_id
        current.current.resource = claimedResource
        setResource(claimedResource)
        sessionStorage.setItem('bt_active_trip_ref', claimedResource)
        window.history.replaceState(
          null,
          '',
          `/trip/result#trip=${encodeURIComponent(claimedResource)}`,
        )
        setMode('CLAIMED')
        sessionStorage.setItem('bt_active_trip_mode', 'CLAIMED')
        return {
          ...value,
          claimedResource,
          acknowledged: true,
          expectedTag: response.etag,
        }
      }
      return value
    }

    try {
      if (operation.readbackOnly) {
        const reference = operation.claimedResource || operation.resource
        const latest = await refresh(reference)
        if (!latest?.etag) {
          setNotice(
            '上次操作未被接受，但最新行程暂时无法读取；请稍后再次确认。',
          )
          return false
        }
        if (operation.type === 'command')
          workspaceOutcome.current = {
            status: 'SYNCED',
            days: latest.body.days,
          }
        sessionStorage.removeItem(PENDING_KEY)
        setPending(null)
        if (operation.type !== 'map') setWriteStatus('FAILED')
        setNotice('已读取服务端最新行程；上次操作没有被接受。')
        void readMapAndStay().then(() => prepareChecks())
        return true
      }

      if (operation.type === 'map') {
        if (!operation.acknowledged) {
          setNotice(
            '正在使用同一请求凭据确认路线任务，不会重复创建路线计算。',
          )
          rememberOperation(await replayWithSameKey(operation))
        }
        const latestMap = await readMapAndStay()
        sessionStorage.removeItem(PENDING_KEY)
        setPending(null)
        setNotice('已确认路线更新请求；同一任务不会被重复计算。')
        if (latestMap?.status !== 'PREPARING') void prepareChecks('map')
        return true
      }

      if (operation.type !== 'claim' || operation.acknowledged) {
        const reference = operation.claimedResource || operation.resource
        latestBeforeReplay = await refresh(reference)
        if (!latestBeforeReplay?.etag) {
          setNotice(
            '服务端版本尚未确认这次操作；可以稍后再次确认。',
          )
          return false
        }
      }

      if (!operation.acknowledged) {
        const basisChanged = Boolean(
          latestBeforeReplay?.etag &&
            latestBeforeReplay.etag !== operation.etag,
        )
        if (!basisChanged && !operation.recoveryChecked) {
          rememberOperation({ ...operation, recoveryChecked: true })
          setNotice(
            '服务端版本尚未确认这次操作；再次确认时会使用同一保存凭据安全核验。',
          )
          return false
        }
        setNotice('正在使用同一保存凭据核验，不会重复应用修改。')
        rememberOperation(await replayWithSameKey(operation))
      }

      const reference = operation.claimedResource || operation.resource
      const latest =
        operation.acknowledged && latestBeforeReplay &&
        latestBeforeReplay.etag !== operation.etag
          ? latestBeforeReplay
          : await refresh(reference)
      const changed = Boolean(
        latest?.etag &&
          operation.acknowledged &&
          (latest.etag !== operation.etag ||
            (operation.type === 'claim' && operation.claimedResource)),
      )
      if (!changed) {
        setNotice(
          '服务端版本尚未确认这次操作；可以稍后再次确认。',
        )
        return false
      }
      if (operation.type === 'command')
        workspaceOutcome.current = {
          status: 'APPLIED',
          days: latest?.body.days,
        }
      sessionStorage.removeItem(PENDING_KEY)
      setPending(null)
      setWriteStatus('CONFIRMED')
      if (operation.type === 'adopt') {
        setPreview(null)
        previewBasis.current = null
      }
      if (
        latest &&
        !['AVAILABLE', 'LIMITED'].includes(latest.body.map.status)
      ) {
        setMap({
          status: latest.body.map.status,
          message: latest.body.map.message,
          points: [],
          days: [],
          available_actions: latest.body.map.available_actions,
        })
      }
      setNotice('已读取服务端最新行程，可以继续调整。')
      void readMapAndStay().then(() => prepareChecks())
      return true
    } catch (error) {
      const code = error instanceof Error ? error.message : ''
      if (rejected.has(code)) {
        const versionConflict =
          code === 'TRIP_UPDATED' || code === 'REVISION_CONFLICT'
        if (versionConflict) {
          try {
            const afterConflict = await refresh(
              operation.claimedResource || operation.resource,
            )
            if (!afterConflict?.etag) throw new Error('READBACK_REQUIRED')
            latestBeforeReplay = afterConflict
          } catch {
            rememberOperation({ ...operation, readbackOnly: true })
            if (operation.type !== 'map') setWriteStatus('UNKNOWN')
            if (operation.type === 'command') {
              workspaceOutcome.current = {
                status: 'SYNCED',
                days: latestBeforeReplay?.body.days,
              }
              if (!latestBeforeReplay) {
                setResult((value) =>
                  value
                    ? {
                        ...value,
                        days: value.days.map((day) => ({
                          ...day,
                          activities: [...day.activities],
                        })),
                      }
                    : value,
                )
              }
            }
            setNotice(
              '这次操作未被接受，但最新服务端版本暂时无法读取；请再次确认。',
            )
            return false
          }
        }
        sessionStorage.removeItem(PENDING_KEY)
        setPending(null)
        if (operation.type !== 'map') setWriteStatus('FAILED')
        if (operation.type === 'command') {
          workspaceOutcome.current = {
            status: 'SYNCED',
            days: latestBeforeReplay?.body.days,
          }
          if (!latestBeforeReplay) {
            setResult((value) =>
              value
                ? {
                    ...value,
                    days: value.days.map((day) => ({
                      ...day,
                      activities: [...day.activities],
                    })),
                  }
                : value,
            )
          }
        }
        if (code === 'PREVIEW_STALE') setPreviewStale(true)
        setNotice(
          code === 'PREVIEW_STALE'
            ? '行程或路线依据已有变化，请重新预览。'
            : code === 'TRIP_UPDATED' || code === 'REVISION_CONFLICT'
              ? '卡片刚刚有更新，已显示服务端版本；请核对后再试。'
              : code === 'COMMAND_REJECTED'
                ? '这次修改没有被接受，已恢复服务端行程。'
                : '行程或登录状态已变化，请重新操作。',
        )
        if (!latestBeforeReplay && !versionConflict) {
          try {
            latestBeforeReplay = await refresh()
          } catch {
            /* The last confirmed result remains visible. */
          }
        }
        await readMapAndStay().catch(() => undefined)
        void prepareChecks()
        return false
      }
      if (code === 'OPERATION_PENDING') {
        setNotice('同一次保存仍在处理中；请稍后再次确认。')
        return false
      }
      setNotice(
        operation.type === 'map'
          ? '路线任务仍未确认；系统不会重复提交路线请求。可以稍后再次确认。'
          : '服务端版本尚未确认这次操作；可以稍后再次确认。',
      )
      return false
    } finally {
      writing.current = false
      setBusy(false)
    }
  }, [pending, prepareChecks, readMapAndStay, refresh])

  const command = (value: api.TripUnderstandingCommand) =>
    execute({
      type: 'command',
      command: value,
      ...current.current,
      key: api.createTripRequestKey(),
    })
  const workspaceCommand = async (value: api.TripUnderstandingCommand) => {
    await command(value)
    return workspaceOutcome.current
  }
  const renderMap = () =>
    execute({
      type: 'map',
      ...current.current,
      key: api.createTripRequestKey(),
      previousStatus: map?.status,
    })
  const claim = () =>
    execute({
      type: 'claim',
      ...current.current,
      key: api.createTripRequestKey(),
    })
  const selectStay = (token: string) =>
    execute({
      type: 'stay',
      token,
      ...current.current,
      key: api.createTripRequestKey(),
    })
  const adopt = () =>
    preview && !previewStale
      ? execute({
          type: 'adopt',
          token: preview.change_token,
          ...current.current,
          key: api.createTripRequestKey(),
        })
      : Promise.resolve(false)
  const openPreview = async (token: string): Promise<boolean> => {
    if (writing.current || pending) return false
    previewController.current?.abort()
    const controller = new AbortController()
    previewController.current = controller
    setPreviewLoading(true)
    const generation = current.current.generation
    try {
      const next = await boundedEnhancementRequest(
        (signal) =>
          api.previewTripUnderstandingChange(resource, token, signal),
        controller.signal,
      )
      if (
        controller.signal.aborted ||
        previewController.current !== controller ||
        generation !== current.current.generation ||
        !alive.current
      )
        return false
      setPreview(next)
      setPreviewStale(false)
      previewBasis.current = {
        etag: current.current.etag,
        check:
          checksRef.current?.items.find((item) => item.check_token === token) ||
          null,
      }
      return true
    } catch (error) {
      if (
        controller.signal.aborted ||
        previewController.current !== controller
      )
        return false
      if (generation === current.current.generation) {
        const code = error instanceof Error ? error.message : ''
        if (code === 'CHECK_CHANGED') {
          setPreviewStale(true)
          setNotice(
            '这条建议的依据可能已有变化，请重新检查后再预览。',
          )
        } else if (code === 'REQUEST_TIMEOUT') {
          setNotice(
            '建议预览等待时间较长，已安全停止；你可以重新预览。',
          )
        } else {
          setNotice('暂时没能准备建议预览，可以重新预览。')
        }
      }
      return false
    } finally {
      if (previewController.current === controller) {
        previewController.current = null
        if (generation === current.current.generation) setPreviewLoading(false)
      }
    }
  }
  const refreshPreview = async () => {
    const prior = previewBasis.current?.check
    await prepareChecks('user')
    const sameSet = (values: string[]) => JSON.stringify(values.slice().sort())
    const tokens = prior?.affected_activity_tokens || []
    const available = (checksRef.current?.items || []).filter(
      (item) => item.can_preview && item.basis_status !== 'NEEDS_RECHECK',
    )
    const exact = available.filter(
      (item) =>
        tokens.length > 0 &&
        item.title === prior?.title &&
        sameSet(item.affected_activity_tokens || []) === sameSet(tokens),
    )
    // Public activity tokens can rotate after edits. A unique issue with the
    // same title and affected days may be previewed again, never auto-adopted.
    const sameIssue = available.filter(
      (item) =>
        prior &&
        item.title === prior.title &&
        sameSet(item.affected_days) === sameSet(prior.affected_days),
    )
    const next =
      exact.length === 1
        ? exact[0]
        : sameIssue.length === 1
          ? sameIssue[0]
          : null
    if (next) {
      const opened = await openPreview(next.check_token)
      if (opened) setNotice('已根据当前行程生成新预览，请重新核对后确认。')
      return opened
    }
    setNotice('当前检查没有对应的可采纳建议，请返回行程查看最新问题。')
    return false
  }
  const retryEnhancements = () => {
    if (manualEnhancementRetry.current) return manualEnhancementRetry.current
    invalidateEnhancements()
    let request!: Promise<api.MapRenderView | null | undefined>
    request = readMapAndStay().finally(() => {
      if (manualEnhancementRetry.current === request)
        manualEnhancementRetry.current = null
    })
    manualEnhancementRetry.current = request
    return request
  }
  return {
    resource,
    mode,
    isDemo,
    result,
    progressSnapshot,
    phase,
    progress,
    streamState,
    cancelling,
    map,
    stay,
    checks,
    preview,
    previewStale,
    previewLoading,
    refreshPreview,
    supplementary,
    writeStatus,
    unavailable,
    loading,
    busy,
    checking,
    pending,
    message,
    notice,
    checksError,
    etag,
    locked: busy || Boolean(pending),
    command,
    workspaceCommand,
    stopUnderstanding,
    renderMap,
    claim,
    selectStay,
    adopt,
    openPreview,
    closePreview: () => {
      previewController.current?.abort()
      previewController.current = null
      setPreviewLoading(false)
      setPreview(null)
      setPreviewStale(false)
      previewBasis.current = null
    },
    markSourceDeleted: () => {
      setSupplementary({ status: 'DELETED', days: [] })
      sessionStorage.setItem('bt_active_trip_source_deleted', 'true')
    },
    retry: () => setRetry((value) => value + 1),
    retryChecks: () => prepareChecks('user'),
    retryMap: retryEnhancements,
    reconcile: reconcilePending,
    setNotice,
  }
}
