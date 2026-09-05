'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from '@/lib/trip-understanding-v3'

type PendingOperation = {
  resource: string
  etag: string
  key: string
  claimedResource?: string
} & (
  | { type: 'command'; command: api.TripUnderstandingCommand }
  | { type: 'adopt'; token: string }
  | { type: 'map' }
  | { type: 'stay'; token: string }
  | { type: 'claim' }
)
const PENDING_KEY = 'bt_pending_operation'
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

export function useTripExperience() {
  const [resource, setResource] = useState('')
  const [mode, setMode] = useState('FULL')
  const [isDemo, setIsDemo] = useState(false)
  const [result, setResult] = useState<api.UserFacingTripResult | null>(null)
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
  const checksRef = useRef<api.PublicTripChecksView | null>(null)
  const [source, setSource] = useState<api.TripSourceView | null>(null)
  const [sourceLoading, setSourceLoading] = useState(false)
  const [supplementary, setSupplementary] =
    useState<api.TripSupplementaryView | null>(null)
  const [writeStatus, setWriteStatus] = useState<
    'IDLE' | 'WRITING' | 'UNKNOWN' | 'CONFIRMED' | 'FAILED'
  >('IDLE')
  const [unavailable, setUnavailable] = useState<
    'NONE' | 'GONE' | 'NOT_AVAILABLE' | 'LOGIN' | 'FAILED' | 'NETWORK'
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
  const preparation = useRef<Promise<void> | null>(null)
  const checkAttempt = useRef<{
    basis: string
    key: string
    completed: boolean
  } | null>(null)
  const alive = useRef(true)
  const [retry, setRetry] = useState(0)
  const previousMapStatus = useRef<string | null>(null)

  const setTag = useCallback((value: string) => {
    if (previewBasis.current && previewBasis.current.etag !== value)
      setPreviewStale(true)
    current.current.etag = value
    setEtag(value)
    sessionStorage.setItem('bt_active_trip_etag', value)
  }, [])
  const refresh = useCallback(
    async (reference = current.current.resource) => {
      const generation = current.current.generation
      const response = await bounded((signal) =>
        api.readTripUnderstandingResult(reference, signal),
      )
      if (
        !alive.current ||
        generation !== current.current.generation ||
        reference !== current.current.resource
      )
        return null
      if (response.status === 202) {
        setMessage(
          'message' in response.body ? response.body.message : '正在整理行程…',
        )
        return null
      }
      const body = response.body as api.UserFacingTripResult
      api.clearTripUnderstandingInputDraft(reference)
      setResult(body)
      setLoading(false)
      setUnavailable('NONE')
      if (body.is_demo !== undefined) {
        setIsDemo(body.is_demo)
        sessionStorage.setItem('bt_active_trip_is_demo', String(body.is_demo))
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
    },
    [setTag],
  )

  const readMapAndStay = useCallback(async () => {
    const { resource: reference, generation } = current.current
    if (!reference) return
    const responses = await Promise.allSettled([
      bounded((signal) => api.readTripUnderstandingMap(reference, signal)),
      bounded((signal) => api.readTripUnderstandingStay(reference, signal)),
    ])
    if (!alive.current || generation !== current.current.generation) return
    if (responses[0].status === 'fulfilled') setMap(responses[0].value)
    else
      setMap((previous) => ({
        ...(previous || { days: [], points: [], available_actions: [] }),
        status: 'UNAVAILABLE',
        message: '路线暂时未能读取，行程仍可继续使用。',
      }))
    if (responses[1].status === 'fulfilled') setStay(responses[1].value)
    return responses[0].status === 'fulfilled' ? responses[0].value : null
  }, [])

  const loadSource = useCallback(async () => {
    const { resource: reference, generation } = current.current
    if (!reference) return
    setSourceLoading(true)
    try {
      const next = await bounded((signal) =>
        api.readTripSource(reference, signal),
      )
      if (generation === current.current.generation && alive.current)
        setSource(next)
    } catch {
      if (generation === current.current.generation && alive.current)
        setSource({ status: 'UNAVAILABLE', text: null, activities: [] })
    } finally {
      if (generation === current.current.generation && alive.current)
        setSourceLoading(false)
    }
  }, [])

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
      if (preparation.current) {
        if (reason === 'read') return preparation.current
        return preparation.current.then(() => prepareCurrentChecks(reason))
      }
      const { resource: reference, etag: tag, generation } = current.current
      if (!reference || !tag || !alive.current) return Promise.resolve()
      const basis = `${reference}:${tag}`
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
      const attempt = checkAttempt.current
      if (previewBasis.current && reason !== 'read') setPreviewStale(true)
      setChecking(true)
      setChecksError('')
      let promise!: Promise<void>
      promise = (async () => {
        try {
          const prepared = await bounded((signal) =>
            api.materializeTripUnderstanding(
              reference,
              tag,
              signal,
              attempt.key,
            ),
          )
          if (generation !== current.current.generation || !alive.current)
            return
          setTag(prepared.etag)
          attempt.basis = `${reference}:${prepared.etag}`
          const next = await bounded((signal) =>
            api.readTripUnderstandingChecks(reference, signal),
          )
          if (generation === current.current.generation && alive.current) {
            setChecks(next)
            checksRef.current = next
            attempt.completed = true
          }
        } catch {
          if (generation === current.current.generation && alive.current) {
            setChecks(null)
            checksRef.current = null
            setChecksError('暂时没能检查完整，可以稍后重试。')
          }
        } finally {
          if (preparation.current === promise) preparation.current = null
          if (alive.current && generation === current.current.generation)
            setChecking(false)
        }
      })()
      preparation.current = promise
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
      setResult(null)
      setMap(null)
      setStay(null)
      setChecks(null)
      setPreview(null)
      setSource(null)
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
    }
    current.current.generation += 1
    current.current.resource = reference
    current.current.etag = ''
    sessionStorage.setItem('bt_active_trip_ref', reference)
    setResource(reference)
    setMode(sessionStorage.getItem('bt_active_trip_mode') || 'FULL')
    const demoSource =
      sessionStorage.getItem('bt_active_trip_is_demo') === 'true' ||
      sessionStorage.getItem('bt_active_trip_mode') === 'DEMO'
    setIsDemo(demoSource)
    sessionStorage.setItem('bt_active_trip_is_demo', String(demoSource))
    try {
      const stored = JSON.parse(
        sessionStorage.getItem(PENDING_KEY) || 'null',
      ) as PendingOperation | null
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
    const deadline = Date.now() + 90000
    async function poll() {
      try {
        const next = await refresh(reference!)
        if (stopped) return
        if (next) {
          void readMapAndStay()
          void loadSupplementary()
          if (!sessionStorage.getItem(PENDING_KEY)) void prepareChecks()
          if (!sessionStorage.getItem(PENDING_KEY)) setWriteStatus('CONFIRMED')
          return
        }
        if (Date.now() < deadline) timer = setTimeout(poll, 1500)
        else {
          setLoading(false)
          setMessage('整理时间比预计更久。你可以稍后重新读取，已有内容会保留。')
        }
      } catch (error) {
        if (!stopped) {
          setLoading(false)
          const code = error instanceof Error ? error.message : ''
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
          setUnavailable(
            gone
              ? 'GONE'
              : code === 'TRIP_NOT_AVAILABLE'
                ? 'NOT_AVAILABLE'
                : code === 'LOGIN_REQUIRED'
                  ? 'LOGIN'
                  : code === 'UNDERSTANDING_FAILED'
                    ? 'FAILED'
                    : 'NETWORK',
          )
          setMessage(
            gone
              ? '这份行程已过期或已删除，可以重新整理一份。'
              : code === 'TRIP_NOT_AVAILABLE'
                ? '当前无法访问这份行程。请确认使用保存它的账号，或重新读取。'
                : code === 'LOGIN_REQUIRED'
                  ? '请登录保存这份行程的账号后继续。'
                  : code === 'UNDERSTANDING_FAILED'
                    ? '这次没有整理完成，可以回到首页调整文字后重试。'
                    : '连接暂时中断，可以重新读取这份行程。',
          )
        }
      }
    }
    setLoading(true)
    void poll()
    return () => {
      stopped = true
      alive.current = false
      clearTimeout(timer)
    }
  }, [retry, refresh, prepareChecks, readMapAndStay, loadSupplementary])

  useEffect(() => {
    const followAddress = () => setRetry((value) => value + 1)
    window.addEventListener('hashchange', followAddress)
    return () => window.removeEventListener('hashchange', followAddress)
  }, [])

  useEffect(() => {
    if (map?.status !== 'PREPARING') return
    let rounds = 0
    const timer = setInterval(() => {
      rounds += 1
      if (rounds > 40) {
        clearInterval(timer)
        setMap((previous) =>
          previous
            ? {
                ...previous,
                status: 'UNAVAILABLE',
                message: '路线仍未准备好，可以稍后再查看。',
              }
            : previous,
        )
        return
      }
      void readMapAndStay()
    }, 2000)
    return () => clearInterval(timer)
  }, [map?.status, readMapAndStay])

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

  const execute = useCallback(
    async (operation: PendingOperation): Promise<boolean> => {
      if (writing.current) return false
      writing.current = true
      setBusy(true)
      if (operation.type !== 'map') setWriteStatus('WRITING')
      setNotice('')
      await preparation.current
      // The caller captures the tag only after background materialization settles.
      if (!pending) operation = { ...operation, etag: current.current.etag }
      const op = operation
      current.current.generation += 1
      setPreviewLoading(false)
      if (previewBasis.current) setPreviewStale(true)
      setChecks(null)
      checksRef.current = null
      setPending(operation)
      sessionStorage.setItem(PENDING_KEY, JSON.stringify(operation))
      try {
        let expectedTag: string | undefined
        if (op.type === 'command')
          expectedTag = (
            await bounded(() =>
              api.applyTripUnderstandingCommand(
                op.resource,
                op.etag,
                op.command,
                op.key,
              ),
            )
          ).etag
        if (op.type === 'adopt')
          expectedTag = (
            await bounded(() =>
              api.adoptTripUnderstandingChange(op.resource, op.token, op.etag),
            )
          ).etag
        if (op.type === 'stay')
          expectedTag = (
            await bounded(() =>
              api.selectTripUnderstandingStay(op.resource, op.token, op.etag),
            )
          ).etag
        if (operation.type === 'map')
          await bounded(() =>
            api.requestTripUnderstandingMap(
              operation.resource,
              operation.etag,
              operation.key,
            ),
          )
        if (operation.type === 'claim') {
          const claimed = await bounded(() =>
            api.claimTripUnderstanding(operation.resource),
          )
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
          }
          sessionStorage.setItem(PENDING_KEY, JSON.stringify(operation))
          setPending(operation)
          setMode('CLAIMED')
          sessionStorage.setItem('bt_active_trip_mode', 'CLAIMED')
          expectedTag = claimed.etag
        }
        const latest = await refresh()
        if (!latest?.etag) throw new Error('READBACK_REQUIRED')
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
        const latestMap = await readMapAndStay()
        void prepareChecks(
          operation.type === 'map' && latestMap?.status !== 'PREPARING'
            ? 'map'
            : 'read',
        )
        return true
      } catch (error) {
        if (error instanceof Error && rejected.has(error.message)) {
          sessionStorage.removeItem(PENDING_KEY)
          setPending(null)
          if (operation.type !== 'map') setWriteStatus('FAILED')
          if (error.message === 'PREVIEW_STALE') setPreviewStale(true)
          if (
            error.message === 'TRIP_GONE' ||
            error.message === 'TRIP_ALREADY_GONE'
          ) {
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
          setNotice(
            error.message === 'PREVIEW_STALE'
              ? '行程或路线依据已有变化，请重新预览。'
              : error.message === 'COMMAND_REJECTED'
                ? '这次修改没有被接受，请检查时间、地点或安排后重试。'
                : '行程或登录状态已变化，已尝试读取最新内容，请重试。',
          )
          try {
            await refresh()
            await readMapAndStay()
            void prepareChecks()
          } catch {
            /* Keep last known result visible. */
          }
        } else {
          if (operation.type !== 'map') setWriteStatus('UNKNOWN')
          setNotice(
            operation.type === 'map'
              ? '尚未确认路线更新是否已开始，请确认这次请求。'
              : '正在确认保存结果。请确认这次操作，避免重复提交。',
          )
        }
        return false
      } finally {
        writing.current = false
        setBusy(false)
      }
    },
    [pending, prepareChecks, readMapAndStay, refresh],
  )

  const command = (value: api.TripUnderstandingCommand) =>
    execute({
      type: 'command',
      command: value,
      ...current.current,
      key: api.createTripRequestKey(),
    })
  const renderMap = () =>
    execute({
      type: 'map',
      ...current.current,
      key: api.createTripRequestKey(),
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
    setPreviewLoading(true)
    const generation = current.current.generation
    try {
      const next = await bounded((signal) =>
        api.previewTripUnderstandingChange(resource, token, signal),
      )
      if (generation !== current.current.generation || !alive.current)
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
      if (generation === current.current.generation) {
        if (error instanceof Error && error.message === 'CHECK_CHANGED')
          setPreviewStale(true)
        setNotice('这条建议的依据可能已有变化，请重新检查后再预览。')
      }
      return false
    } finally {
      if (generation === current.current.generation) setPreviewLoading(false)
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
  return {
    resource,
    mode,
    isDemo,
    result,
    map,
    stay,
    checks,
    preview,
    previewStale,
    previewLoading,
    refreshPreview,
    source,
    sourceLoading,
    loadSource,
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
    renderMap,
    claim,
    selectStay,
    adopt,
    openPreview,
    closePreview: () => {
      setPreview(null)
      setPreviewStale(false)
      previewBasis.current = null
    },
    markSourceDeleted: () => {
      setSource({ status: 'DELETED', text: null, activities: [] })
      setSupplementary({ status: 'DELETED', days: [] })
      sessionStorage.setItem('bt_active_trip_source_deleted', 'true')
    },
    retry: () => setRetry((value) => value + 1),
    retryChecks: () => prepareChecks('user'),
    retryMap: readMapAndStay,
    reconcile: () => (pending ? execute(pending) : Promise.resolve(false)),
    setNotice,
  }
}
