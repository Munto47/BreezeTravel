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
      setResult(body)
      setLoading(false)
      if (response.etag) setTag(response.etag)
      if (body.ownership === 'ACCOUNT') {
        setMode('CLAIMED')
        sessionStorage.setItem('bt_active_trip_mode', 'CLAIMED')
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
      setChecking(true)
      setChecksError('')
      const promise = (async () => {
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
            attempt.completed = true
          }
        } catch {
          if (generation === current.current.generation && alive.current) {
            setChecks(null)
            setChecksError('暂时没能检查完整，可以稍后重试。')
          }
        } finally {
          preparation.current = null
          if (alive.current) setChecking(false)
        }
      })()
      preparation.current = promise
      return promise
    },
    [setTag],
  )

  useEffect(() => {
    alive.current = true
    const reference = sessionStorage.getItem('bt_active_trip_ref')
    if (!reference) {
      setMessage('没有可恢复的行程，请从首页开始。')
      setLoading(false)
      return
    }
    current.current.resource = reference
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
          if (!sessionStorage.getItem(PENDING_KEY)) void prepareChecks()
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
          setMessage(
            error instanceof Error && error.message === 'UNDERSTANDING_FAILED'
              ? '这次没有整理完成。可以回到首页，调整文字后重新尝试。'
              : '暂时无法读取这份行程。可能已过期，也可能连接中断。',
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
  }, [retry, refresh, prepareChecks, readMapAndStay])

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
      setNotice('')
      await preparation.current
      // The caller captures the tag only after background materialization settles.
      if (!pending) operation = { ...operation, etag: current.current.etag }
      const op = operation
      current.current.generation += 1
      setPreview(null)
      setChecks(null)
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
        setNotice(
          expectedTag && expectedTag !== latest.etag
            ? '这份行程也有其他更新，已显示最新内容。'
            : operation.type === 'map'
              ? '正在准备更新后的路线。'
              : operation.type === 'claim'
                ? '已保存到账号，保留 30 天。'
                : '修改已保存。路线需要时再更新。',
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
          setNotice(
            error.message === 'COMMAND_REJECTED'
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
        } else setNotice('尚未确认保存结果。为避免重复修改，请先确认这次操作。')
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
    preview
      ? execute({
          type: 'adopt',
          token: preview.change_token,
          ...current.current,
          key: api.createTripRequestKey(),
        })
      : Promise.resolve(false)
  const openPreview = async (token: string) => {
    if (writing.current || pending) return
    setBusy(true)
    const generation = current.current.generation
    try {
      const next = await bounded((signal) =>
        api.previewTripUnderstandingChange(resource, token, signal),
      )
      if (generation === current.current.generation) setPreview(next)
    } catch {
      setNotice('这条建议暂时无法预览，请重新检查后再试。')
    } finally {
      setBusy(false)
    }
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
    closePreview: () => setPreview(null),
    retry: () => setRetry((value) => value + 1),
    retryChecks: () => prepareChecks('user'),
    retryMap: readMapAndStay,
    reconcile: () => (pending ? execute(pending) : Promise.resolve(false)),
    setNotice,
  }
}
