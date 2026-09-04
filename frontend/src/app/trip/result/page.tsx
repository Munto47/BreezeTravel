'use client'

import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react'
import { useRouter } from 'next/navigation'
import {
  ArrowLeft,
  BedDouble,
  BusFront,
  CalendarDays,
  CheckCircle2,
  ChevronLeft,
  Compass,
  Footprints,
  Map,
  MapPin,
  List,
  Pencil,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Users,
  X,
} from 'lucide-react'

import ItineraryWorkspace from './itinerary-workspace'
import MemorySharePanel from './memory-share-panel'
import AccessibleDialog from './accessible-dialog'
import ResultNavigation from './result-navigation'
import {
  DAY_COLORS,
  routeGeometrySegments,
  topPublicChecks,
  type ResultViewId,
} from './result-presentation'
import {
  type ActivityCardView,
  type MapRenderView,
  type PublicChangePreview,
  type PublicTripChecksView,
  type StaySuggestionView,
  type TripUnderstandingCommand,
  type UserFacingTripResult,
  applyTripUnderstandingCommand,
  adoptTripUnderstandingChange,
  claimTripUnderstanding,
  clearTripUnderstandingSession,
  deleteTripUnderstanding,
  deleteTripUnderstandingSource,
  materializeTripUnderstanding,
  previewTripUnderstandingChange,
  readTripUnderstandingChecks,
  readTripUnderstandingResult,
  readTripUnderstandingMap,
  readTripUnderstandingStay,
  requestTripUnderstandingMap,
  selectTripUnderstandingStay,
  streamTripUnderstandingEvents,
} from '@/lib/trip-understanding-v3'
import { useAuthStore } from '@/stores/authStore'


const ASSUMPTION_ICONS = {
  destination: MapPin,
  calendar: CalendarDays,
  party_size: Users,
}


type EditorState = {
  mode: 'INSERT' | 'EDIT' | 'REPLACE'
  dayIndex: number
  position: number
  card?: ActivityCardView
}

type AssumptionEditorState = UserFacingTripResult['assumptions'][number]

type ActiveChecksRequest = {
  id: number
  resourceRef: string
  key: string
  controller: AbortController
}

type ActivePreviewRequest = {
  id: number
  key: string
  controller: AbortController
}

type ActiveEnhancementSession = {
  id: number
  key: string
  resourceRef: string
  generation: number
  manual: boolean
  cancelled: boolean
  controllers: Set<AbortController>
  timer: number | null
  releaseTimer: (() => void) | null
}

type BoundedEnhancementRead<T> =
  | { status: 'fulfilled'; value: T }
  | { status: 'rejected'; reason: unknown }

type WorkspaceCommandResult =
  | { status: 'APPLIED' | 'SYNCED'; days: UserFacingTripResult['days'] }
  | { status: 'RECONCILING' }

type RefreshedResult = {
  body: UserFacingTripResult
  etag: string | null
}


const CHECKS_REQUEST_TIMEOUT_MS = 10_000
const RESULT_REQUEST_TIMEOUT_MS = 10_000
const MUTATION_REQUEST_TIMEOUT_MS = 10_000
const ENHANCEMENT_REQUEST_TIMEOUT_MS = 3_000
const ENHANCEMENT_SESSION_BUDGET_MS = 10_000
const ENHANCEMENT_MAX_ROUNDS = 8
const ENHANCEMENT_POLL_INTERVAL_MS = 800


function boundedRequest<T>(request: Promise<T>, timeoutMs: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error('REQUEST_TIMEOUT')), timeoutMs)
    request.then(
      (value) => {
        window.clearTimeout(timeout)
        resolve(value)
      },
      (reason) => {
        window.clearTimeout(timeout)
        reject(reason)
      },
    )
  })
}


function requestTimedOut(reason: unknown): boolean {
  return reason instanceof Error && reason.message === 'REQUEST_TIMEOUT'
}


function mutationRequest<T>(request: () => Promise<T>, deadlineAt: number): Promise<T> {
  const remainingMs = deadlineAt - Date.now()
  if (remainingMs <= 0) return Promise.reject(new Error('REQUEST_TIMEOUT'))
  return boundedRequest(request(), remainingMs)
}


function revisionReadbackConfirmed(
  refreshed: RefreshedResult,
  baseEtag: string | null,
  expectedEtag: string | null,
): boolean {
  if (!refreshed.etag) return false
  if (expectedEtag) {
    return (baseEtag === null || expectedEtag !== baseEtag) && refreshed.etag === expectedEtag
  }
  return Boolean(baseEtag && refreshed.etag !== baseEtag)
}
const RESULT_WAIT_BUDGET_MS = 60_000


function stopEnhancementSession(session: ActiveEnhancementSession | null) {
  if (!session || session.cancelled) return
  session.cancelled = true
  if (session.timer !== null) {
    window.clearTimeout(session.timer)
    session.timer = null
  }
  const releaseTimer = session.releaseTimer
  session.releaseTimer = null
  releaseTimer?.()
  session.controllers.forEach((controller) => controller.abort())
}


function fallbackMapView(result: UserFacingTripResult | null): MapRenderView {
  const source = result?.map
  if (source && source.status !== 'PREPARING') {
    return { ...source, days: [] }
  }
  return {
    status: 'UNAVAILABLE',
    message: '路线详情暂时不可用，不影响继续查看行程。',
    days: [],
    available_actions: [],
  }
}


function fallbackStayView(result: UserFacingTripResult | null): StaySuggestionView {
  const source = result?.stay
  if (source && source.status !== 'PREPARING') return source
  return {
    status: 'UNAVAILABLE',
    message: '住宿建议暂时不可用，不影响继续查看行程。',
    area_summary: null,
    searched_scopes: [],
    candidates: [],
    available_actions: [],
  }
}


function pendingRevisionMapView(): MapRenderView {
  return {
    status: 'NEEDS_UPDATE',
    message: '行程已调整，需要手动更新路线。',
    days: [],
    available_actions: ['RENDER_MAP'],
  }
}


function pendingRevisionStayView(): StaySuggestionView {
  return {
    status: 'NEEDS_UPDATE',
    message: '行程已调整，住宿建议需要重新确认。',
    area_summary: null,
    searched_scopes: [],
    candidates: [],
    available_actions: [],
  }
}


export default function TripResultPage() {
  const router = useRouter()
  const { user, isHydrated, hydrate } = useAuthStore()
  const [resourceRef, setResourceRef] = useState<string | null>(null)
  const [activeMode, setActiveMode] = useState<'DEMO' | 'FULL' | 'CLAIMED' | null>(null)
  const [result, setResult] = useState<UserFacingTripResult | null>(null)
  const [activeView, setActiveView] = useState<ResultViewId>('ITINERARY')
  const [etag, setEtag] = useState<string | null>(null)
  const [message, setMessage] = useState('正在整理每天行程')
  const [error, setError] = useState('')
  const [resultRecovery, setResultRecovery] = useState('')
  const [resultRetryGeneration, setResultRetryGeneration] = useState(0)
  const [commandError, setCommandError] = useState('')
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [editorName, setEditorName] = useState('')
  const [editorCategory, setEditorCategory] = useState('地点')
  const [editorAddress, setEditorAddress] = useState('地点待确认')
  const [editorTime, setEditorTime] = useState('')
  const [assumptionEditor, setAssumptionEditor] = useState<AssumptionEditorState | null>(null)
  const [assumptionValue, setAssumptionValue] = useState('')
  const [privacyConfirmation, setPrivacyConfirmation] = useState<'SOURCE' | 'TRIP' | null>(null)
  const [privacyBusy, setPrivacyBusy] = useState<'CLAIM' | 'SOURCE' | 'TRIP' | null>(null)
  const [privacyMessage, setPrivacyMessage] = useState('')
  const [sourceDeleted, setSourceDeleted] = useState(false)
  const [tripDeleted, setTripDeleted] = useState(false)
  const [mapView, setMapView] = useState<MapRenderView | null>(null)
  const [stayView, setStayView] = useState<StaySuggestionView | null>(null)
  const [enhancementBusy, setEnhancementBusy] = useState<'MAP' | 'STAY' | null>(null)
  const [enhancementReadBusy, setEnhancementReadBusy] = useState(false)
  const [enhancementRecoveryAvailable, setEnhancementRecoveryAvailable] = useState(false)
  const [checksView, setChecksView] = useState<PublicTripChecksView | null>(null)
  const [changePreview, setChangePreview] = useState<PublicChangePreview | null>(null)
  const [checkBusy, setCheckBusy] = useState<'PREPARE' | 'PREVIEW' | 'ADOPT' | null>(null)
  const [checkMessage, setCheckMessage] = useState('')
  const [checksRetryGeneration, setChecksRetryGeneration] = useState(0)
  const [mutationLocked, setMutationLocked] = useState(false)
  const [reconciliationRequired, setReconciliationRequired] = useState(false)
  const [reconciliationBusy, setReconciliationBusy] = useState(false)
  const [reconciliationKind, setReconciliationKind] = useState<'RESULT' | 'MAP' | 'CLAIM' | 'SOURCE_DELETE' | 'TRIP_DELETE'>('RESULT')
  const activeResourceRef = useRef<string | null>(null)
  const mountedRef = useRef(false)
  const resultRef = useRef<UserFacingTripResult | null>(result)
  const checksRequestSequence = useRef(0)
  const activeChecksRequest = useRef<ActiveChecksRequest | null>(null)
  const activeChecksPromise = useRef<Promise<void> | null>(null)
  const completedChecksKey = useRef<string | null>(null)
  const previewRequestSequence = useRef(0)
  const activePreviewRequest = useRef<ActivePreviewRequest | null>(null)
  const commandInFlightRef = useRef(false)
  const mutationLockRef = useRef(false)
  const authoritativeKeyRef = useRef<string | null>(null)
  const enhancementGenerationRef = useRef(0)
  const enhancementSessionSequence = useRef(0)
  const activeEnhancementSession = useRef<ActiveEnhancementSession | null>(null)
  const activeEnhancementPromise = useRef<Promise<void> | null>(null)
  const settledEnhancementKey = useRef<string | null>(null)
  const editorTriggerRef = useRef<HTMLElement | null>(null)
  const editorNameRef = useRef<HTMLInputElement | null>(null)
  const reconciliationActionRef = useRef<HTMLButtonElement | null>(null)
  const reconciliationResourceRef = useRef<string | null>(null)
  const reconciliationBaseEtagRef = useRef<string | null>(null)
  const reconciliationExpectedEtagRef = useRef<string | null>(null)
  const reconciliationAcceptAnyResultRef = useRef(false)
  const reconciliationMapBaseStatusRef = useRef<MapRenderView['status'] | null>(null)
  const reconciliationMapRequestAcceptedRef = useRef(false)
  const resultAvailable = result !== null
  const currentChecksKey = resourceRef && etag ? `${resourceRef}:${etag}` : null
  const currentChecksKeyRef = useRef<string | null>(currentChecksKey)
  resultRef.current = result
  currentChecksKeyRef.current = currentChecksKey

  useEffect(() => {
    hydrate()
  }, [hydrate])

  useEffect(() => {
    if (!reconciliationRequired) return
    let secondFrame = 0
    const firstFrame = window.requestAnimationFrame(() => {
      secondFrame = window.requestAnimationFrame(() => reconciliationActionRef.current?.focus())
    })
    return () => {
      window.cancelAnimationFrame(firstFrame)
      if (secondFrame) window.cancelAnimationFrame(secondFrame)
    }
  }, [reconciliationRequired])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      checksRequestSequence.current += 1
      activeChecksRequest.current?.controller.abort()
      activeChecksRequest.current = null
      previewRequestSequence.current += 1
      activePreviewRequest.current?.controller.abort()
      activePreviewRequest.current = null
      enhancementSessionSequence.current += 1
      stopEnhancementSession(activeEnhancementSession.current)
      activeEnhancementSession.current = null
    }
  }, [])

  const restoreEditorFocus = useCallback((dayIndex: number, preferDayHeading = false) => {
    window.requestAnimationFrame(() => {
      const trigger = editorTriggerRef.current
      if (!preferDayHeading && trigger?.isConnected) {
        trigger.focus()
        return
      }
      document.querySelector<HTMLElement>(`[data-day-heading="${dayIndex}"]`)?.focus()
    })
  }, [])

  const closeEditor = useCallback((preferDayHeading = false) => {
    if (!editor) return
    const dayIndex = editor.dayIndex
    setEditor(null)
    restoreEditorFocus(dayIndex, preferDayHeading)
  }, [editor, restoreEditorFocus])

  const cancelActivePreview = useCallback((clearBusy = true) => {
    previewRequestSequence.current += 1
    activePreviewRequest.current?.controller.abort()
    activePreviewRequest.current = null
    if (clearBusy && mountedRef.current) {
      setCheckBusy((current) => current === 'PREVIEW' ? null : current)
    }
  }, [])

  const cancelActiveEnhancement = useCallback((clearBusy = true) => {
    enhancementSessionSequence.current += 1
    stopEnhancementSession(activeEnhancementSession.current)
    activeEnhancementSession.current = null
    if (clearBusy && mountedRef.current) setEnhancementReadBusy(false)
  }, [])

  const invalidatePreview = useCallback(() => {
    cancelActivePreview()
    setChangePreview(null)
  }, [cancelActivePreview])

  const invalidateDerivedViews = useCallback(() => {
    cancelActiveEnhancement()
    enhancementGenerationRef.current += 1
    settledEnhancementKey.current = null
    completedChecksKey.current = null
    setEnhancementRecoveryAvailable(false)
    setMapView(pendingRevisionMapView())
    setStayView(pendingRevisionStayView())
    setChecksView(null)
    invalidatePreview()
  }, [cancelActiveEnhancement, invalidatePreview])

  const stagePendingRevision = useCallback((
    reference: string,
    nextEtag: string,
    nextChecks: PublicTripChecksView | null = null,
  ) => {
    const nextKey = `${reference}:${nextEtag}`
    cancelActiveEnhancement()
    enhancementGenerationRef.current += 1
    settledEnhancementKey.current = null
    currentChecksKeyRef.current = nextKey
    completedChecksKey.current = nextChecks ? nextKey : null
    setEnhancementRecoveryAvailable(false)
    setMapView(pendingRevisionMapView())
    setStayView(pendingRevisionStayView())
    setChecksView(nextChecks)
    invalidatePreview()
    setEtag(nextEtag)
    sessionStorage.setItem('bt_active_trip_etag', nextEtag)
  }, [cancelActiveEnhancement, invalidatePreview])

  const beginMutation = useCallback(async () => {
    if (mutationLockRef.current) return false
    mutationLockRef.current = true
    setMutationLocked(true)
    cancelActivePreview()
    const pendingEnhancements = activeEnhancementPromise.current
    cancelActiveEnhancement()
    const pendingChecks = activeChecksPromise.current
    activeChecksRequest.current?.controller.abort()
    const pendingReads = [pendingChecks, pendingEnhancements].filter(
      (pending): pending is Promise<void> => pending !== null,
    )
    if (pendingReads.length > 0) await Promise.allSettled(pendingReads)
    return true
  }, [cancelActiveEnhancement, cancelActivePreview])

  const finishMutation = useCallback(() => {
    mutationLockRef.current = false
    setMutationLocked(false)
    setReconciliationRequired(false)
    setReconciliationKind('RESULT')
    reconciliationResourceRef.current = null
    reconciliationBaseEtagRef.current = null
    reconciliationExpectedEtagRef.current = null
    reconciliationAcceptAnyResultRef.current = false
    reconciliationMapBaseStatusRef.current = null
    reconciliationMapRequestAcceptedRef.current = false
  }, [])

  const holdForReconciliation = useCallback((
    message: string,
    kind: 'RESULT' | 'MAP' | 'CLAIM' | 'SOURCE_DELETE' | 'TRIP_DELETE' = 'RESULT',
    reference: string | null = null,
    baseEtag: string | null = null,
    expectedEtag: string | null = null,
    acceptAnyResult = false,
  ) => {
    cancelActiveEnhancement()
    reconciliationResourceRef.current = reference
    reconciliationBaseEtagRef.current = baseEtag
    reconciliationExpectedEtagRef.current = expectedEtag
    reconciliationAcceptAnyResultRef.current = acceptAnyResult
    setReconciliationKind(kind)
    setReconciliationRequired(true)
    setCommandError(message)
  }, [cancelActiveEnhancement])

  const holdMapForReconciliation = useCallback((
    message: string,
    reference: string,
    baseStatus: MapRenderView['status'],
    requestAccepted: boolean,
  ) => {
    reconciliationMapBaseStatusRef.current = baseStatus
    reconciliationMapRequestAcceptedRef.current = requestAccepted
    holdForReconciliation(message, 'MAP', reference)
  }, [holdForReconciliation])

  const refreshEnhancements = useCallback((
    reference: string,
    generation = enhancementGenerationRef.current,
    fallbackResult = resultRef.current,
    options: { force?: boolean; manual?: boolean } = {},
  ) => {
    const key = `${reference}:${generation}`
    const currentSession = activeEnhancementSession.current
    if (currentSession && !currentSession.cancelled && currentSession.key === key) {
      return activeEnhancementPromise.current || Promise.resolve()
    }
    if (!options.force && settledEnhancementKey.current === key) return Promise.resolve()

    const previousPromise = activeEnhancementPromise.current
    stopEnhancementSession(currentSession)
    const sessionId = enhancementSessionSequence.current + 1
    enhancementSessionSequence.current = sessionId
    const session: ActiveEnhancementSession = {
      id: sessionId,
      key,
      resourceRef: reference,
      generation,
      manual: options.manual === true,
      cancelled: false,
      controllers: new Set(),
      timer: null,
      releaseTimer: null,
    }
    activeEnhancementSession.current = session
    settledEnhancementKey.current = null
    setEnhancementRecoveryAvailable(false)
    if (session.manual) setEnhancementReadBusy(true)

    const isCurrent = () => (
      mountedRef.current
      && !session.cancelled
      && activeResourceRef.current === reference
      && enhancementGenerationRef.current === generation
      && activeEnhancementSession.current?.id === session.id
    )

    const sessionPromise = (async () => {
      if (previousPromise) await Promise.allSettled([previousPromise])
      if (!isCurrent()) return

      const startedAt = Date.now()
      let round = 0
      let mapPending = true
      let stayPending = true
      let mapResult: MapRenderView | null = null
      let stayResult: StaySuggestionView | null = null
      let degraded = false

      async function readWithBound<T>(
        reader: (signal: AbortSignal) => Promise<T>,
      ): Promise<BoundedEnhancementRead<T>> {
        const remainingBudget = ENHANCEMENT_SESSION_BUDGET_MS - (Date.now() - startedAt)
        const controller = new AbortController()
        session.controllers.add(controller)
        const timeout = window.setTimeout(
          () => controller.abort(),
          Math.max(0, Math.min(ENHANCEMENT_REQUEST_TIMEOUT_MS, remainingBudget)),
        )
        try {
          return { status: 'fulfilled' as const, value: await reader(controller.signal) }
        } catch (reason) {
          return { status: 'rejected' as const, reason }
        } finally {
          window.clearTimeout(timeout)
          session.controllers.delete(controller)
        }
      }

      while (isCurrent() && round < ENHANCEMENT_MAX_ROUNDS) {
        const remainingBudget = ENHANCEMENT_SESSION_BUDGET_MS - (Date.now() - startedAt)
        if (remainingBudget <= 0) break
        round += 1

        const mapReadPromise: Promise<BoundedEnhancementRead<MapRenderView> | null> = mapPending
          ? readWithBound((signal) => readTripUnderstandingMap(reference, signal))
          : Promise.resolve(null)
        const stayReadPromise: Promise<BoundedEnhancementRead<StaySuggestionView> | null> = stayPending
          ? readWithBound((signal) => readTripUnderstandingStay(reference, signal))
          : Promise.resolve(null)
        const mapRead = await mapReadPromise
        const stayRead = await stayReadPromise
        if (!isCurrent()) return

        if (mapRead) {
          if (mapRead.status === 'fulfilled') {
            mapResult = mapRead.value
            mapPending = mapRead.value.status === 'PREPARING'
          } else {
            mapResult = fallbackMapView(fallbackResult)
            mapPending = false
            degraded = true
          }
        }
        if (stayRead) {
          if (stayRead.status === 'fulfilled') {
            stayResult = stayRead.value
            stayPending = stayRead.value.status === 'PREPARING'
          } else {
            stayResult = fallbackStayView(fallbackResult)
            stayPending = false
            degraded = true
          }
        }

        if (mapResult) setMapView(mapResult)
        if (stayResult) setStayView(stayResult)
        if (!mapPending && !stayPending) {
          settledEnhancementKey.current = key
          setEnhancementRecoveryAvailable(degraded)
          return
        }
        if (round >= ENHANCEMENT_MAX_ROUNDS) break

        const budgetAfterRead = ENHANCEMENT_SESSION_BUDGET_MS - (Date.now() - startedAt)
        if (budgetAfterRead <= 0) break
        await new Promise<void>((resolve) => {
          let released = false
          const release = () => {
            if (released) return
            released = true
            if (session.timer !== null) window.clearTimeout(session.timer)
            session.timer = null
            session.releaseTimer = null
            resolve()
          }
          session.releaseTimer = release
          session.timer = window.setTimeout(
            release,
            Math.min(ENHANCEMENT_POLL_INTERVAL_MS, budgetAfterRead),
          )
        })
      }

      if (!isCurrent()) return
      if (mapPending) setMapView(fallbackMapView(fallbackResult))
      if (stayPending) setStayView(fallbackStayView(fallbackResult))
      settledEnhancementKey.current = key
      setEnhancementRecoveryAvailable(true)
    })()

    activeEnhancementPromise.current = sessionPromise
    void sessionPromise.then(() => {
      if (activeEnhancementPromise.current === sessionPromise) {
        activeEnhancementPromise.current = null
      }
      if (activeEnhancementSession.current?.id === session.id) {
        activeEnhancementSession.current = null
        if (session.manual && mountedRef.current) setEnhancementReadBusy(false)
      }
    })
    return sessionPromise
  }, [])

  const refresh = useCallback(async (
    reference: string,
    suppressOpenError = false,
    isAttemptCurrent: () => boolean = () => true,
    deadlineAt?: number,
  ) => {
    try {
      const remainingMs = deadlineAt === undefined
        ? RESULT_REQUEST_TIMEOUT_MS
        : Math.min(RESULT_REQUEST_TIMEOUT_MS, deadlineAt - Date.now())
      if (remainingMs <= 0) throw new Error('REQUEST_TIMEOUT')
      const response = await boundedRequest(
        readTripUnderstandingResult(reference),
        remainingMs,
      )
      if (activeResourceRef.current !== reference || !isAttemptCurrent()) return null
      if (response.body.status !== 'PROCESSING') {
        let enhancementGeneration = enhancementGenerationRef.current
        const authoritativeKey = response.etag ? `${reference}:${response.etag}` : null
        if (response.etag && authoritativeKeyRef.current !== authoritativeKey) {
          const checksAlreadyMatch = completedChecksKey.current === `${reference}:${response.etag}`
          cancelActiveEnhancement()
          enhancementGeneration = enhancementGenerationRef.current + 1
          enhancementGenerationRef.current = enhancementGeneration
          settledEnhancementKey.current = null
          currentChecksKeyRef.current = authoritativeKey
          setEnhancementRecoveryAvailable(false)
          setMapView(null)
          setStayView(null)
          if (!checksAlreadyMatch) setChecksView(null)
          invalidatePreview()
          if (!checksAlreadyMatch) completedChecksKey.current = null
        }
        authoritativeKeyRef.current = authoritativeKey
        resultRef.current = response.body
        setResult(response.body)
        setResultRecovery('')
        setError('')
        if (response.etag) {
          setEtag(response.etag)
          sessionStorage.setItem('bt_active_trip_etag', response.etag)
        }
        setMessage('卡片已可用')
        void refreshEnhancements(reference, enhancementGeneration, response.body)
        return { body: response.body, etag: response.etag }
      }
      setMessage(response.body.message)
      return null
    } catch {
      if (!suppressOpenError && activeResourceRef.current === reference && isAttemptCurrent()) {
        setError('这份体验暂时无法打开，请返回首页重新开始。')
      }
      return null
    }
  }, [cancelActiveEnhancement, invalidatePreview, refreshEnhancements])

  useEffect(() => {
    if (mutationLocked) {
      activeChecksRequest.current?.controller.abort()
      return
    }
    if (!resourceRef || !resultAvailable || !etag || !mapView || !stayView) return
    if (mapView.status === 'PREPARING' || stayView.status === 'PREPARING') return
    const attemptKey = `${resourceRef}:${etag}`
    if (completedChecksKey.current === attemptKey && checksView) return
    const previousRequest = activeChecksRequest.current
    if (previousRequest) {
      if (previousRequest.resourceRef === resourceRef) return
      previousRequest.controller.abort()
      return
    }
    const requestId = checksRequestSequence.current + 1
    const controller = new AbortController()
    checksRequestSequence.current = requestId
    activeChecksRequest.current = { id: requestId, resourceRef, key: attemptKey, controller }
    let queueCurrentGeneration = false
    const timeout = window.setTimeout(() => controller.abort(), CHECKS_REQUEST_TIMEOUT_MS)
    setCheckBusy('PREPARE')
    setCheckMessage('')
    const checksPromise = (async () => {
      try {
        const prepared = await materializeTripUnderstanding(resourceRef, etag, controller.signal)
        const preparedKey = `${resourceRef}:${prepared.etag}`
        const currentKey = currentChecksKeyRef.current
        const activeRequest = activeChecksRequest.current
        if (
          !mountedRef.current
          || activeResourceRef.current !== resourceRef
          || activeRequest?.id !== requestId
          || (currentKey !== attemptKey && currentKey !== preparedKey)
        ) {
          queueCurrentGeneration = activeRequest?.id === requestId
          return
        }
        activeChecksRequest.current = { id: requestId, resourceRef, key: preparedKey, controller }
        if (etag !== prepared.etag && currentKey === attemptKey) {
          stagePendingRevision(resourceRef, prepared.etag)
        }
        const checks = await readTripUnderstandingChecks(resourceRef, controller.signal)
        const settledKey = currentChecksKeyRef.current
        if (
          !mountedRef.current
          || activeResourceRef.current !== resourceRef
          || activeChecksRequest.current?.id !== requestId
          || (settledKey !== attemptKey && settledKey !== preparedKey)
        ) {
          queueCurrentGeneration = activeChecksRequest.current?.id === requestId
          return
        }
        completedChecksKey.current = preparedKey
        setChecksView(checks)
      } catch (checksFailure) {
        if (
          !mountedRef.current
          || activeResourceRef.current !== resourceRef
          || activeChecksRequest.current?.id !== requestId
        ) return
        if (controller.signal.aborted) {
          queueCurrentGeneration = mutationLockRef.current || currentChecksKeyRef.current !== attemptKey
          if (!queueCurrentGeneration) {
            setCheckMessage('优先检查等待时间较长，已安全停止；你可以手动重新准备。')
          }
        } else if (checksFailure instanceof Error && checksFailure.message === 'TRIP_UPDATED') {
          setCheckMessage('行程刚刚有更新，正在读取最新内容。')
          await refresh(resourceRef, true)
          queueCurrentGeneration = true
        } else {
          setCheckMessage('优先检查暂时没有准备好，不影响查看和调整行程。')
        }
      } finally {
        window.clearTimeout(timeout)
        if (activeChecksRequest.current?.id === requestId) {
          activeChecksRequest.current = null
          if (mountedRef.current) {
            setCheckBusy(null)
            if (queueCurrentGeneration) {
              setChecksRetryGeneration((generation) => generation + 1)
            }
          }
        }
      }
    })()
    activeChecksPromise.current = checksPromise
    void checksPromise.then(
      () => {
        if (activeChecksPromise.current === checksPromise) activeChecksPromise.current = null
      },
      () => {
        if (activeChecksPromise.current === checksPromise) activeChecksPromise.current = null
      },
    )
  }, [
    checksRetryGeneration,
    checksView,
    etag,
    mapView?.status,
    mutationLocked,
    refresh,
    resourceRef,
    resultAvailable,
    stagePendingRevision,
    stayView?.status,
  ])

  const retryChecks = useCallback(() => {
    if (checkBusy || mutationLockRef.current || !resourceRef || !etag) return
    completedChecksKey.current = null
    setCheckMessage('')
    setChecksRetryGeneration((generation) => generation + 1)
  }, [checkBusy, etag, resourceRef])

  const retryEnhancements = useCallback(() => {
    if (!resourceRef || !resultRef.current || mutationLockRef.current) return
    void refreshEnhancements(
      resourceRef,
      enhancementGenerationRef.current,
      resultRef.current,
      { force: true, manual: true },
    )
  }, [refreshEnhancements, resourceRef])

  const retryResultReadback = useCallback(async () => {
    if (!resourceRef || !reconciliationRequired || reconciliationBusy) return
    const deadlineAt = Date.now() + MUTATION_REQUEST_TIMEOUT_MS
    setReconciliationBusy(true)
    if (reconciliationKind === 'TRIP_DELETE') {
      try {
        await mutationRequest(
          () => deleteTripUnderstanding(resourceRef),
          deadlineAt,
        )
        clearTripUnderstandingSession()
        activeResourceRef.current = null
        setTripDeleted(true)
        finishMutation()
      } catch {
        setCommandError('仍在确认整份行程的删除结果。请保持此页面打开，稍后再重新确认。')
      } finally {
        setReconciliationBusy(false)
      }
      return
    }
    if (reconciliationKind === 'MAP') {
      const mapReference = reconciliationResourceRef.current || resourceRef
      const controller = new AbortController()
      const remainingMs = Math.max(0, deadlineAt - Date.now())
      const timeout = window.setTimeout(() => controller.abort(), remainingMs)
      try {
        const latestMap = await mutationRequest(
          () => readTripUnderstandingMap(mapReference, controller.signal),
          deadlineAt,
        )
        const sameUnconfirmedStatus = (
          !reconciliationMapRequestAcceptedRef.current
          && reconciliationMapBaseStatusRef.current !== null
          && latestMap.status === reconciliationMapBaseStatusRef.current
        )
        if (latestMap.status === 'NEEDS_UPDATE' || sameUnconfirmedStatus) {
          setCommandError('路线任务仍未确认，请稍后再重新读取；不会重复提交路线请求。')
        } else {
          setMapView(latestMap)
          setEnhancementRecoveryAvailable(latestMap.status === 'PREPARING')
          setCommandError('已读取服务端当前路线状态，可以继续调整。')
          finishMutation()
        }
      } catch {
        setCommandError('仍在确认路线任务状态。请保持此页面打开，稍后再重新读取。')
      } finally {
        window.clearTimeout(timeout)
        setReconciliationBusy(false)
      }
      return
    }
    if (reconciliationKind === 'CLAIM') {
      const claimReference = reconciliationResourceRef.current || resourceRef
      try {
        const claimed = await mutationRequest(
          () => claimTripUnderstanding(claimReference),
          deadlineAt,
        )
        const nextReference = claimed.body.public_resource_id
        clearTripUnderstandingSession()
        sessionStorage.setItem('bt_active_trip_ref', nextReference)
        sessionStorage.setItem('bt_active_trip_mode', 'CLAIMED')
        sessionStorage.removeItem('bt_active_trip_event_cursor')
        sessionStorage.setItem('bt_active_trip_etag', claimed.etag)
        activeResourceRef.current = nextReference
        setResourceRef(nextReference)
        setActiveMode('CLAIMED')
        stagePendingRevision(nextReference, claimed.etag)
        const latest = await refresh(nextReference, true, () => true, deadlineAt)
        if (latest && revisionReadbackConfirmed(latest, null, claimed.etag)) {
          finishMutation()
          setPrivacyMessage('已保存到你的账号，匿名访问凭证已经失效。')
          setCommandError('账号保存结果已经确认，可以继续调整。')
        } else {
          holdForReconciliation(
            '账号保存已提交，仍在确认服务端最新行程；确认前其他写入已暂停。',
            'RESULT',
            nextReference,
            null,
            claimed.etag,
          )
        }
      } catch {
        setCommandError('仍在确认账号保存结果。请保持此页面打开，稍后再重新确认。')
      } finally {
        setReconciliationBusy(false)
      }
      return
    }
    if (reconciliationKind === 'SOURCE_DELETE') {
      const sourceReference = reconciliationResourceRef.current || resourceRef
      try {
        await mutationRequest(
          () => deleteTripUnderstandingSource(sourceReference),
          deadlineAt,
        )
        const latest = await refresh(sourceReference, true, () => true, deadlineAt)
        if (latest) {
          sessionStorage.setItem('bt_active_trip_source_deleted', 'true')
          setSourceDeleted(true)
          setPrivacyMessage('原文已永久删除，逐日卡片仍可继续查看和调整。')
          setCommandError('原文删除结果已经确认，可以继续调整。')
          finishMutation()
        } else {
          setCommandError('仍在确认原文删除结果。请保持此页面打开，稍后再重新确认。')
        }
      } catch {
        setCommandError('仍在确认原文删除结果。请保持此页面打开，稍后再重新确认。')
      } finally {
        setReconciliationBusy(false)
      }
      return
    }
    const resultReference = reconciliationResourceRef.current || resourceRef
    const latest = await refresh(resultReference, true, () => true, deadlineAt)
    const baseEtag = reconciliationBaseEtagRef.current
    const expectedEtag = reconciliationExpectedEtagRef.current
    const resultConfirmed = latest && (
      reconciliationAcceptAnyResultRef.current
      || revisionReadbackConfirmed(latest, baseEtag, expectedEtag)
    )
    if (resultConfirmed) {
      finishMutation()
      closeEditor(true)
      setCommandError('已读取服务端最新行程，可以继续调整。')
    } else {
      setCommandError('服务端版本尚未确认这次操作；不会重复提交，请稍后再重新读取。')
    }
    setReconciliationBusy(false)
  }, [closeEditor, finishMutation, holdForReconciliation, reconciliationBusy, reconciliationKind, reconciliationRequired, refresh, resourceRef, stagePendingRevision])

  const handleMapRender = useCallback(async () => {
    if (!resourceRef || !etag || enhancementBusy) return
    const currentMap = mapView || (resultRef.current ? fallbackMapView(resultRef.current) : null)
    if (!currentMap?.available_actions.includes('RENDER_MAP')) return
    if (!(await beginMutation())) return
    const deadlineAt = Date.now() + MUTATION_REQUEST_TIMEOUT_MS
    let reconciliationHeld = false
    let mapRequestAccepted = false
    setEnhancementBusy('MAP')
    setCommandError('')
    try {
      await mutationRequest(
        () => requestTripUnderstandingMap(resourceRef, etag),
        deadlineAt,
      )
      mapRequestAccepted = true
      await mutationRequest(
        () => refreshEnhancements(
          resourceRef,
          enhancementGenerationRef.current,
          resultRef.current,
          { force: true },
        ),
        deadlineAt,
      )
    } catch (mapFailure) {
      if (requestTimedOut(mapFailure)) {
        reconciliationHeld = true
        holdMapForReconciliation(
          '路线更新等待时间较长，结果暂时无法确认；确认前其他写入已暂停。',
          resourceRef,
          currentMap.status,
          mapRequestAccepted,
        )
      } else if (mapFailure instanceof Error && mapFailure.message === 'MAP_RENDER_FAILED') {
        setCommandError('路线更新请求未被接受，你可以稍后再试。')
      } else if (mapFailure instanceof Error && mapFailure.message === 'REVISION_CONFLICT') {
        const latest = await refresh(resourceRef, true, () => true, deadlineAt)
        if (latest && revisionReadbackConfirmed(latest, etag, null)) {
          setCommandError('行程刚刚有更新，已为你读取最新版本。')
        } else {
          reconciliationHeld = true
          holdForReconciliation(
            '行程版本和路线状态暂时无法确认；确认前其他写入已暂停。',
            'RESULT',
            resourceRef,
            etag,
          )
        }
      } else {
        reconciliationHeld = true
        holdMapForReconciliation(
          '路线更新结果暂时无法确认；确认前其他写入已暂停。',
          resourceRef,
          currentMap.status,
          mapRequestAccepted,
        )
      }
    } finally {
      setEnhancementBusy(null)
      if (!reconciliationHeld) finishMutation()
    }
  }, [beginMutation, enhancementBusy, etag, finishMutation, holdForReconciliation, holdMapForReconciliation, mapView, refresh, refreshEnhancements, resourceRef])

  const handleStaySelection = useCallback(async (candidateToken: string) => {
    if (!resourceRef || !etag || enhancementBusy) return
    if (!(await beginMutation())) return
    const deadlineAt = Date.now() + MUTATION_REQUEST_TIMEOUT_MS
    let reconciliationHeld = false
    setEnhancementBusy('STAY')
    setCommandError('')
    try {
      const selectedStay = await mutationRequest(
        () => selectTripUnderstandingStay(resourceRef, candidateToken, etag),
        deadlineAt,
      )
      stagePendingRevision(resourceRef, selectedStay.etag)
      const refreshed = await refresh(resourceRef, true, () => true, deadlineAt)
      if (!refreshed || !revisionReadbackConfirmed(refreshed, etag, selectedStay.etag)) {
        reconciliationHeld = true
        holdForReconciliation(
          '住宿选择已提交，正在确认服务端最新行程；确认前其他写入已暂停。',
          'RESULT',
          resourceRef,
          etag,
          selectedStay.etag,
        )
      }
    } catch (stayFailure) {
      invalidateDerivedViews()
      if (requestTimedOut(stayFailure)) {
        reconciliationHeld = true
        holdForReconciliation(
          '住宿选择等待时间较长，结果暂时无法确认；确认前其他写入已暂停。',
          'RESULT',
          resourceRef,
          etag,
        )
      } else {
        const latest = await refresh(resourceRef, true, () => true, deadlineAt)
        const knownRejected = stayFailure instanceof Error
          && stayFailure.message === 'STAY_SELECTION_FAILED'
        const readbackConfirmed = Boolean(latest && (
          knownRejected || revisionReadbackConfirmed(latest, etag, null)
        ))
        if (!latest || !readbackConfirmed) {
          reconciliationHeld = true
          holdForReconciliation(
            '住宿选择的结果暂时无法确认；确认前其他写入已暂停。',
            'RESULT',
            resourceRef,
            etag,
            null,
            knownRejected,
          )
        }
        if (stayFailure instanceof Error && stayFailure.message === 'REVISION_CONFLICT') {
          if (readbackConfirmed) setCommandError('住宿候选已经变化，已为你读取最新版本。')
        } else if (readbackConfirmed) {
          setCommandError('住宿选择请求未完成，已按服务端最新行程恢复。')
        }
      }
    } finally {
      setEnhancementBusy(null)
      if (!reconciliationHeld) finishMutation()
    }
  }, [beginMutation, enhancementBusy, etag, finishMutation, holdForReconciliation, invalidateDerivedViews, refresh, resourceRef, stagePendingRevision])

  const handleChangePreview = useCallback(async (checkToken: string) => {
    if (!resourceRef || checkBusy) return
    const previewKey = currentChecksKeyRef.current
    if (!previewKey) return
    const requestId = previewRequestSequence.current + 1
    activePreviewRequest.current?.controller.abort()
    const controller = new AbortController()
    previewRequestSequence.current = requestId
    activePreviewRequest.current = { id: requestId, key: previewKey, controller }
    const timeout = window.setTimeout(() => controller.abort(), CHECKS_REQUEST_TIMEOUT_MS)
    setCheckBusy('PREVIEW')
    setCheckMessage('')
    try {
      const preview = await previewTripUnderstandingChange(resourceRef, checkToken, controller.signal)
      const activePreview = activePreviewRequest.current
      if (
        mutationLockRef.current
        || currentChecksKeyRef.current !== previewKey
        || activePreview?.id !== requestId
        || activePreview.key !== previewKey
      ) return
      setChangePreview(preview)
    } catch {
      const activePreview = activePreviewRequest.current
      if (activePreview?.id !== requestId || activePreview.key !== previewKey) return
      setChangePreview(null)
      setCheckMessage(controller.signal.aborted
        ? '建议预览等待时间较长，已安全停止；你可以重新预览。'
        : '这项建议已经变化，请刷新后再试。')
    } finally {
      window.clearTimeout(timeout)
      const activePreview = activePreviewRequest.current
      if (activePreview?.id === requestId && activePreview.key === previewKey) {
        activePreviewRequest.current = null
        setCheckBusy(null)
      }
    }
  }, [checkBusy, resourceRef])

  const handleChangeAdopt = useCallback(async () => {
    if (!resourceRef || !etag || !changePreview || checkBusy) return
    if (!(await beginMutation())) return
    const deadlineAt = Date.now() + MUTATION_REQUEST_TIMEOUT_MS
    let reconciliationHeld = false
    setCheckBusy('ADOPT')
    setCheckMessage('')
    try {
      const adopted = await mutationRequest(
        () => adoptTripUnderstandingChange(
          resourceRef,
          changePreview.change_token,
          etag,
        ),
        deadlineAt,
      )
      stagePendingRevision(resourceRef, adopted.etag, adopted.body.checks)
      const refreshed = await refresh(resourceRef, true, () => true, deadlineAt)
      if (!refreshed || !revisionReadbackConfirmed(refreshed, etag, adopted.etag)) {
        reconciliationHeld = true
        holdForReconciliation(
          '改动已提交，正在确认服务端最新行程；确认前其他写入已暂停。',
          'RESULT',
          resourceRef,
          etag,
          adopted.etag,
        )
      } else {
        setCheckMessage(adopted.body.message)
      }
    } catch (adoptFailure) {
      invalidateDerivedViews()
      if (requestTimedOut(adoptFailure)) {
        reconciliationHeld = true
        holdForReconciliation(
          '改动保存等待时间较长，结果暂时无法确认；确认前其他写入已暂停。',
          'RESULT',
          resourceRef,
          etag,
        )
      } else {
        const latest = await refresh(resourceRef, true, () => true, deadlineAt)
        const knownRejected = adoptFailure instanceof Error
          && adoptFailure.message === 'CHANGE_ADOPT_FAILED'
        const readbackConfirmed = Boolean(latest && (
          knownRejected || revisionReadbackConfirmed(latest, etag, null)
        ))
        if (!latest || !readbackConfirmed) {
          reconciliationHeld = true
          holdForReconciliation(
            '改动结果暂时无法确认；确认前其他写入已暂停。',
            'RESULT',
            resourceRef,
            etag,
            null,
            knownRejected,
          )
        }
        if (adoptFailure instanceof Error && adoptFailure.message === 'TRIP_UPDATED') {
          if (readbackConfirmed) setCheckMessage('行程刚刚有更新，已读取最新内容，请重新预览。')
        } else if (readbackConfirmed) {
          setCheckMessage('改动请求未完成，已按服务端最新行程恢复。')
        }
      }
    } finally {
      setCheckBusy(null)
      if (!reconciliationHeld) finishMutation()
    }
  }, [beginMutation, changePreview, checkBusy, etag, finishMutation, holdForReconciliation, invalidateDerivedViews, refresh, resourceRef, stagePendingRevision])

  const runCommand = useCallback(async (command: TripUnderstandingCommand): Promise<WorkspaceCommandResult> => {
    if (!resourceRef || !etag || commandInFlightRef.current) {
      return { status: 'SYNCED', days: result?.days || [] }
    }
    if (!(await beginMutation())) return { status: 'SYNCED', days: result?.days || [] }
    const deadlineAt = Date.now() + MUTATION_REQUEST_TIMEOUT_MS
    let reconciliationHeld = false
    commandInFlightRef.current = true
    setCommandError('')
    try {
      const applied = await mutationRequest(
        () => applyTripUnderstandingCommand(resourceRef, etag, command),
        deadlineAt,
      )
      stagePendingRevision(resourceRef, applied.etag)
      const refreshed = await refresh(resourceRef, true, () => true, deadlineAt)
      if (!refreshed || !revisionReadbackConfirmed(refreshed, etag, applied.etag)) {
        reconciliationHeld = true
        holdForReconciliation(
          '调整已提交，但保存结果暂时无法确认；确认前其他写入已暂停。',
          'RESULT',
          resourceRef,
          etag,
          applied.etag,
        )
        return { status: 'RECONCILING' }
      }
      return { status: 'APPLIED', days: refreshed.body.days }
    } catch (commandFailure) {
      invalidateDerivedViews()
      if (requestTimedOut(commandFailure)) {
        reconciliationHeld = true
        holdForReconciliation(
          '调整保存等待时间较长，结果暂时无法确认；确认前其他写入已暂停。',
          'RESULT',
          resourceRef,
          etag,
        )
        return { status: 'RECONCILING' }
      }
      const refreshed = await refresh(resourceRef, true, () => true, deadlineAt)
      const knownRejected = commandFailure instanceof Error
        && (commandFailure.message === 'COMMAND_FAILED' || commandFailure.message === 'IF_MATCH_REQUIRED')
      const readbackConfirmed = Boolean(refreshed && (
        knownRejected || revisionReadbackConfirmed(refreshed, etag, null)
      ))
      if (!refreshed || !readbackConfirmed) {
        reconciliationHeld = true
        holdForReconciliation(
          '调整结果暂时无法确认；确认前其他写入已暂停。',
          'RESULT',
          resourceRef,
          etag,
          null,
          knownRejected,
        )
        return { status: 'RECONCILING' }
      }
      if (commandFailure instanceof Error && commandFailure.message === 'REVISION_CONFLICT') {
        setCommandError('卡片刚刚有更新，已为你读取最新版本，请再试一次。')
      } else {
        setCommandError('调整请求未能确认，已按服务端最新行程恢复。')
      }
      return { status: 'SYNCED', days: refreshed.body.days }
    } finally {
      commandInFlightRef.current = false
      if (!reconciliationHeld) finishMutation()
    }
  }, [beginMutation, etag, finishMutation, holdForReconciliation, invalidateDerivedViews, refresh, resourceRef, result?.days, stagePendingRevision])

  const openEditor = (state: EditorState) => {
    if (mutationLockRef.current) return
    const activeElement = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
    editorTriggerRef.current = activeElement?.closest('[role="dialog"]')
      ? null
      : activeElement
    setEditor(state)
    setEditorName(state.card?.name || '')
    setEditorCategory(state.card?.category || '地点')
    setEditorAddress(state.card?.area_or_address || '地点待确认')
    setEditorTime(state.card?.time_hint || '')
    setCommandError('')
  }

  const submitEditor = async () => {
    if (!editor || !editorName.trim()) {
      setCommandError('请填写地点名称。')
      return
    }
    let outcome: WorkspaceCommandResult | null = null
    if (editor.mode === 'INSERT') {
      outcome = await runCommand({
        command_type: 'ACTIVITY_INSERT',
        day_index: editor.dayIndex,
        position: editor.position,
        name: editorName.trim(),
        category: editorCategory.trim() || '地点',
        area_or_address: editorAddress.trim() || '地点待确认',
        time_hint: editorTime.trim() || null,
      })
    } else if (editor.mode === 'EDIT' && editor.card) {
      outcome = await runCommand({
        command_type: 'ACTIVITY_TEXT_EDIT',
        activity_token: editor.card.activity_token,
        name: editorName.trim(),
        time_hint: editorTime.trim() || null,
      })
    } else if (editor.mode === 'REPLACE' && editor.card) {
      outcome = await runCommand({
        command_type: 'PLACE_REPLACE',
        activity_token: editor.card.activity_token,
        replacement: {
          name: editorName.trim(),
          category: editorCategory.trim() || '地点',
          area_or_address: editorAddress.trim() || '地点待确认',
        },
      })
    }
    if (outcome?.status === 'APPLIED' || outcome?.status === 'RECONCILING') {
      closeEditor(true)
    } else if (outcome) {
      window.requestAnimationFrame(() => {
        editorNameRef.current?.focus()
      })
    }
  }

  const submitAssumption = async () => {
    if (!assumptionEditor) return
    const value = assumptionValue.trim()
    if (!value || value === assumptionEditor.value) {
      setAssumptionEditor(null)
      return
    }
    const outcome = await runCommand({
      command_type: 'ASSUMPTION_SET',
      key: assumptionEditor.key,
      value,
    })
    if (outcome.status === 'APPLIED' || outcome.status === 'RECONCILING') {
      setAssumptionEditor(null)
    }
  }

  const handleClaim = async () => {
    if (!resourceRef || privacyBusy || mutationLockRef.current) return
    if (!user) {
      sessionStorage.setItem('bt_login_return', '/trip/result')
      router.push('/login')
      return
    }
    if (!(await beginMutation())) return
    const deadlineAt = Date.now() + MUTATION_REQUEST_TIMEOUT_MS
    let reconciliationHeld = false
    setPrivacyBusy('CLAIM')
    setPrivacyMessage('')
    try {
      const claimed = await mutationRequest(
        () => claimTripUnderstanding(resourceRef),
        deadlineAt,
      )
      const nextReference = claimed.body.public_resource_id
      clearTripUnderstandingSession()
      sessionStorage.setItem('bt_active_trip_ref', nextReference)
      sessionStorage.setItem('bt_active_trip_mode', 'CLAIMED')
      sessionStorage.removeItem('bt_active_trip_event_cursor')
      sessionStorage.setItem('bt_active_trip_etag', claimed.etag)
      activeResourceRef.current = nextReference
      setResourceRef(nextReference)
      setActiveMode('CLAIMED')
      stagePendingRevision(nextReference, claimed.etag)
      const refreshed = await refresh(nextReference, true, () => true, deadlineAt)
      if (refreshed && revisionReadbackConfirmed(refreshed, null, claimed.etag)) {
        setPrivacyMessage('已保存到你的账号，匿名访问凭证已经失效。')
      } else {
        reconciliationHeld = true
        holdForReconciliation(
          '账号保存已提交，正在确认服务端最新行程；确认前其他写入已暂停。',
          'RESULT',
          nextReference,
          null,
          claimed.etag,
        )
      }
    } catch (claimFailure) {
      invalidateDerivedViews()
      if (requestTimedOut(claimFailure)) {
        reconciliationHeld = true
        holdForReconciliation(
          '账号保存等待时间较长，结果暂时无法确认；确认前其他写入已暂停。',
          'CLAIM',
          resourceRef,
        )
      } else if (claimFailure instanceof Error && [
        'CLAIM_FAILED',
        'LOGIN_REQUIRED',
        'TRIP_ALREADY_GONE',
      ].includes(claimFailure.message)) {
        await refresh(resourceRef, true, () => true, deadlineAt)
        setPrivacyMessage('账号保存请求未被接受，你可以稍后再试。')
      } else {
        reconciliationHeld = true
        holdForReconciliation(
          '账号保存结果暂时无法确认；确认前其他写入已暂停。',
          'CLAIM',
          resourceRef,
        )
      }
    } finally {
      setPrivacyBusy(null)
      if (!reconciliationHeld) finishMutation()
    }
  }

  const handleDeleteSource = async () => {
    if (!resourceRef || privacyBusy || sourceDeleted || mutationLockRef.current) return
    if (!(await beginMutation())) return
    const deadlineAt = Date.now() + MUTATION_REQUEST_TIMEOUT_MS
    let reconciliationHeld = false
    setPrivacyBusy('SOURCE')
    setPrivacyMessage('')
    try {
      await mutationRequest(
        () => deleteTripUnderstandingSource(resourceRef),
        deadlineAt,
      )
      if (await refresh(resourceRef, true, () => true, deadlineAt)) {
        sessionStorage.setItem('bt_active_trip_source_deleted', 'true')
        setSourceDeleted(true)
        setPrivacyMessage('原文已永久删除，逐日卡片仍可继续查看和调整。')
      } else {
        reconciliationHeld = true
        holdForReconciliation(
          '原文删除已提交，正在确认服务端结果；确认前其他写入已暂停。',
          'SOURCE_DELETE',
          resourceRef,
        )
      }
    } catch (sourceDeleteFailure) {
      if (requestTimedOut(sourceDeleteFailure)) {
        reconciliationHeld = true
        holdForReconciliation(
          '原文删除等待时间较长，结果暂时无法确认；确认前其他写入已暂停。',
          'SOURCE_DELETE',
          resourceRef,
        )
      } else if (sourceDeleteFailure instanceof Error && [
        'SOURCE_DELETE_FAILED',
        'LOGIN_REQUIRED',
      ].includes(sourceDeleteFailure.message)) {
        await refresh(resourceRef, true, () => true, deadlineAt)
        setPrivacyMessage('原文删除请求未被接受，你可以稍后再试。')
      } else {
        reconciliationHeld = true
        holdForReconciliation(
          '原文删除结果暂时无法确认；确认前其他写入已暂停。',
          'SOURCE_DELETE',
          resourceRef,
        )
      }
    } finally {
      setPrivacyBusy(null)
      if (!reconciliationHeld) finishMutation()
    }
  }

  const handleDeleteTrip = async () => {
    if (!resourceRef || privacyBusy || mutationLockRef.current) return
    if (!(await beginMutation())) return
    const deadlineAt = Date.now() + MUTATION_REQUEST_TIMEOUT_MS
    let reconciliationHeld = false
    setPrivacyBusy('TRIP')
    setPrivacyMessage('')
    try {
      await mutationRequest(
        () => deleteTripUnderstanding(resourceRef),
        deadlineAt,
      )
      clearTripUnderstandingSession()
      activeResourceRef.current = null
      setTripDeleted(true)
    } catch {
      reconciliationHeld = true
      holdForReconciliation(
        '尚未确认整份行程的删除结果；确认前其他写入已暂停。',
        'TRIP_DELETE',
      )
    } finally {
      setPrivacyBusy(null)
      if (!reconciliationHeld) finishMutation()
    }
  }

  useEffect(() => {
    const reference = sessionStorage.getItem('bt_active_trip_ref')
    if (!reference) {
      setError('当前标签页里没有可恢复的体验，请返回首页重新开始。')
      return
    }
    const storedMode = sessionStorage.getItem('bt_active_trip_mode')
    if (storedMode === 'DEMO' || storedMode === 'FULL' || storedMode === 'CLAIMED') {
      setActiveMode(storedMode)
    }
    setSourceDeleted(sessionStorage.getItem('bt_active_trip_source_deleted') === 'true')
    activeResourceRef.current = reference
    setResourceRef(reference)
  }, [])

  useEffect(() => {
    if (!resourceRef) return
    activeResourceRef.current = resourceRef
    let disposed = false
    let expired = false
    let readInFlight = false
    let interval: ReturnType<typeof setInterval> | undefined
    let deadline: ReturnType<typeof setTimeout> | undefined
    const eventController = new AbortController()
    const isAttemptCurrent = () => !disposed && !expired
    const stopPolling = () => {
      if (interval) {
        clearInterval(interval)
        interval = undefined
      }
      if (deadline) {
        clearTimeout(deadline)
        deadline = undefined
      }
    }
    const stopAttempt = () => {
      eventController.abort()
      stopPolling()
    }
    const poll = async (closeStreamOnReady = false) => {
      if (!isAttemptCurrent() || readInFlight) return null
      readInFlight = true
      const ready = await refresh(resourceRef, true, isAttemptCurrent)
      readInFlight = false
      if (ready) {
        stopPolling()
        if (closeStreamOnReady) eventController.abort()
      }
      return ready
    }

    setResultRecovery('')
    void streamTripUnderstandingEvents(
      resourceRef,
      (event) => {
        if (!isAttemptCurrent()) return
        setMessage(event.message)
        if (event.type === 'result_available') {
          void poll(true)
        }
      },
      eventController.signal,
    ).catch((streamError: unknown) => {
      if (isAttemptCurrent() && !(streamError instanceof DOMException && streamError.name === 'AbortError')) {
        void poll()
      }
    })
    void poll()
    interval = setInterval(() => {
      void poll()
    }, 1000)
    deadline = setTimeout(() => {
      if (!isAttemptCurrent() || resultRef.current) return
      expired = true
      stopAttempt()
      setResultRecovery('整理时间比预期更长，本轮读取已安全停止。你可以重试，结果不会丢失。')
    }, RESULT_WAIT_BUDGET_MS)
    return () => {
      disposed = true
      stopAttempt()
      cancelActiveEnhancement()
      cancelActivePreview()
    }
  }, [cancelActiveEnhancement, cancelActivePreview, refresh, resourceRef, resultRetryGeneration])

  const retryInitialResult = useCallback(() => {
    setError('')
    setResultRecovery('')
    setMessage('正在重新读取行程')
    setResultRetryGeneration((generation) => generation + 1)
  }, [])

  if (tripDeleted) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-[#f8f7f2] p-6">
        <div data-testid="trip-deleted" className="w-full max-w-md rounded-3xl border border-slate-200 bg-white p-8 text-center shadow-xl">
          <ShieldCheck className="mx-auto h-10 w-10 text-emerald-700" aria-hidden="true" />
          <h1 className="mt-4 text-xl font-semibold">这份行程已永久删除</h1>
          <p className="mt-2 text-sm leading-6 text-slate-500">我们已重新读取确认它不再可访问。</p>
          <button type="button" onClick={() => router.push('/')} className="mt-6 rounded-2xl bg-slate-950 px-5 py-3 text-sm font-semibold text-white">
            返回首页
          </button>
        </div>
      </main>
    )
  }

  if (error) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-[#f8f7f2] p-6">
        <div className="w-full max-w-md rounded-3xl border border-slate-200 bg-white p-8 text-center shadow-xl">
          <Compass className="mx-auto h-10 w-10 text-slate-700" aria-hidden="true" />
          <h1 className="mt-4 text-xl font-semibold">暂时无法恢复</h1>
          <p className="mt-2 text-sm leading-6 text-slate-500">{error}</p>
          <div className="mt-6 flex flex-col gap-2 sm:flex-row sm:justify-center">
            {resourceRef && (
              <button type="button" onClick={retryInitialResult} className="min-h-12 rounded-2xl bg-emerald-700 px-5 py-3 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2">
                重试
              </button>
            )}
            <button type="button" onClick={() => router.push('/')} className="min-h-12 rounded-2xl bg-slate-950 px-5 py-3 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-900 focus-visible:ring-offset-2">
              返回首页
            </button>
          </div>
        </div>
      </main>
    )
  }

  if (!resourceRef || !result) {
    if (resultRecovery) {
      return (
        <main className="flex min-h-screen items-center justify-center bg-[#f8f7f2] p-6">
          <div data-testid="trip-progress-recovery" role="status" className="w-full max-w-md rounded-3xl border border-amber-200/70 bg-white p-8 text-center shadow-xl">
            <Compass className="mx-auto h-10 w-10 text-emerald-700" aria-hidden="true" />
            <h1 className="mt-4 text-xl font-semibold">行程仍在整理</h1>
            <p className="mt-2 text-sm leading-6 text-slate-600">{resultRecovery}</p>
            <div className="mt-6 grid grid-cols-2 gap-3">
              <button data-testid="retry-initial-result" type="button" onClick={retryInitialResult} className="min-h-12 rounded-2xl bg-emerald-700 px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2">
                重试
              </button>
              <button type="button" onClick={() => router.push('/')} className="min-h-12 rounded-2xl border border-slate-300 px-4 text-sm font-semibold text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-700">
                返回首页
              </button>
            </div>
          </div>
        </main>
      )
    }
    return (
      <main className="flex min-h-screen items-center justify-center bg-[#f8f7f2] p-6">
        <div data-testid="trip-progress" className="w-full max-w-md rounded-3xl border border-white bg-white/90 p-8 text-center shadow-xl">
          <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-emerald-50 text-emerald-700">
            <Sparkles className="h-6 w-6 animate-pulse motion-reduce:animate-none" aria-hidden="true" />
          </div>
          <h1 className="mt-5 text-xl font-semibold">{message}</h1>
          <p className="mt-2 text-sm text-slate-500">页面可以安全刷新，整理结果会继续保留。</p>
          <div className="mt-6 h-2 overflow-hidden rounded-full bg-slate-100">
            <div className="h-full w-2/3 animate-pulse rounded-full bg-emerald-500 motion-reduce:animate-none" />
          </div>
        </div>
      </main>
    )
  }

  return (
    <>
      <header className="sticky top-0 z-40 border-b border-emerald-950/10 bg-[#fbfaf6]/95 backdrop-blur-xl">
        <div className="mx-auto flex min-h-[4.75rem] w-full max-w-[1540px] items-center justify-between gap-4 px-5 sm:px-8 lg:px-12">
          <button
            type="button"
            onClick={() => router.push('/')}
            className="group inline-flex min-h-12 items-center gap-3 rounded-xl text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2"
          >
              <span className="flex h-11 w-11 items-center justify-center rounded-2xl bg-emerald-800 text-white shadow-sm transition motion-reduce:transition-none group-hover:-rotate-6 motion-reduce:group-hover:rotate-0">
              <Compass className="h-6 w-6" aria-hidden="true" />
            </span>
            <span>
              <span className="block text-base font-semibold tracking-tight text-slate-900">行程查 · <span className="text-emerald-800">BreezeTravel</span></span>
              <span className="hidden text-xs text-slate-600 sm:block">把攻略变成每天能照着走的卡片</span>
            </span>
            <span className="sr-only">，返回首页</span>
          </button>

          <button type="button" onClick={() => router.push('/')} className="inline-flex min-h-12 items-center gap-2 rounded-xl border border-emerald-950/10 bg-white px-4 text-sm font-semibold text-slate-700 shadow-sm transition hover:border-emerald-700/30 hover:text-emerald-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700" aria-label="返回首页">
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
            <span className="hidden sm:inline">返回首页</span>
          </button>
        </div>
      </header>

      <ResultNavigation activeView={activeView} onChange={setActiveView} />

      <main className="min-h-screen overflow-x-hidden bg-[#f8f7f2] pb-28 text-slate-900 lg:pb-0 lg:pl-[5.75rem]">
      <div className="relative mx-auto w-full max-w-[1540px] px-5 pb-14 sm:px-8 lg:px-10">
        <div className="pointer-events-none absolute -right-32 top-44 h-80 w-80 rounded-full bg-emerald-100/35 blur-3xl" aria-hidden="true" />
        <div className="pointer-events-none absolute -left-40 top-[42rem] h-72 w-72 rounded-full bg-amber-100/45 blur-3xl" aria-hidden="true" />

        <section className="relative border-b border-emerald-950/10 py-5" aria-label="行程摘要">
          <div className="flex flex-wrap items-center gap-y-3 divide-x divide-slate-200">
            {result.assumptions.map((assumption) => {
              const Icon = ASSUMPTION_ICONS[assumption.key]
              return (
                <button
                  key={assumption.key}
                  data-testid={`edit-assumption-${assumption.key}`}
                  type="button"
                  disabled={mutationLocked || !assumption.editable}
                  onClick={() => {
                    setAssumptionEditor(assumption)
                    setAssumptionValue(assumption.value)
                  }}
                  className="group flex min-h-12 items-center gap-3 px-4 text-left first:pl-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 disabled:cursor-wait disabled:opacity-60 sm:px-7"
                >
                  <Icon className="h-5 w-5 text-emerald-700" aria-hidden="true" />
                  <span>
                    <span className="flex items-center gap-1 text-[11px] text-slate-600">{assumption.label}<Pencil className="h-3 w-3 opacity-0 transition group-hover:opacity-100" aria-hidden="true" /></span>
                    <span className="mt-0.5 block text-sm font-semibold text-slate-800">{assumption.value}</span>
                  </span>
                  <span className="sr-only">，点击修改</span>
                </button>
              )
            })}
            <div className="flex min-h-12 items-center gap-3 px-4 sm:px-7">
              <ShieldCheck className="h-5 w-5 text-emerald-700" aria-hidden="true" />
              <span>
                <span className="block text-[11px] text-slate-600">整理状态</span>
                <span className="mt-0.5 block text-sm font-semibold text-emerald-800">{result.status === 'READY' ? '地点卡片已整理' : '部分地点待确认'}</span>
              </span>
            </div>
          </div>
        </section>

        {commandError && (
          <div
            data-testid="result-operation-status"
            role="status"
            aria-live="polite"
            className="relative mt-5 rounded-2xl border border-amber-200/70 bg-amber-50 px-4 py-3 text-sm text-amber-900"
          >
            <p>{commandError}</p>
            {reconciliationRequired && (
              <button
                ref={reconciliationActionRef}
                data-testid="retry-result-readback"
                type="button"
                disabled={reconciliationBusy}
                onClick={() => void retryResultReadback()}
                className="mt-3 min-h-12 rounded-xl border border-amber-700/30 bg-white px-4 font-semibold text-amber-950 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-800 disabled:opacity-60"
              >
                {reconciliationBusy ? '正在读取最新结果…' : '重新读取服务端结果'}
              </button>
            )}
          </div>
        )}

        <div className="relative pt-8 sm:pt-10">
          <section
            id="itinerary-view"
            data-testid="result-view-itinerary"
            hidden={activeView !== 'ITINERARY'}
            aria-labelledby="itinerary-view-title"
          >
          <div className="max-w-3xl">
            <div className="inline-flex items-center gap-2 rounded-full border border-emerald-900/10 bg-white/80 px-3 py-1.5 text-xs font-semibold text-emerald-800 shadow-sm">
              <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
              {result.status === 'READY' ? '每天的地点已经排好' : '先查看已整理的地点'}
            </div>
            <h1 id="itinerary-view-title" className="mt-4 text-3xl font-semibold tracking-[-0.035em] text-slate-900 sm:text-5xl">按天查看，照着走<span className="text-emerald-800">更轻松</span></h1>
            <p className="mt-3 text-sm leading-6 text-slate-600 sm:text-base">这是清晰的游览顺序，不伪装成实时路线。拖动卡片后会自动保存，需要时再手动更新地图。</p>
          </div>

          <ItineraryWorkspace
            days={result.days}
            disabled={mutationLocked}
            mapView={mapView || {
              status: result.map.status,
              message: result.map.message,
              days: [],
              available_actions: result.map.available_actions,
            }}
            checkStatus={checkBusy
              ? '正在准备'
              : checksView
                ? `已准备 ${checksView.items.length} 项`
                : checkMessage
                  ? '可重新准备'
                  : '等待准备'}
            onCommand={runCommand}
            onAdd={(dayIndex, position) => openEditor({ mode: 'INSERT', dayIndex, position })}
            onEdit={(item) => openEditor({ ...item, mode: 'EDIT', card: item.card })}
            onReplace={(item) => openEditor({ ...item, mode: 'REPLACE', card: item.card })}
          />
          </section>

          <section
            id="map-stay-view"
            data-testid="result-view-map-stay"
            hidden={activeView !== 'MAP_STAY'}
            aria-labelledby="map-stay-view-title"
          >
          <div className="mb-6 max-w-3xl">
            <p className="text-xs font-semibold uppercase tracking-[0.18em] text-emerald-700">地图与住宿</p>
            <h1 id="map-stay-view-title" className="mt-2 text-3xl font-semibold tracking-[-0.035em] text-slate-900 sm:text-4xl">看清路线，也住得更顺路</h1>
            <p className="mt-2 text-sm leading-6 text-slate-600">地图只展示服务端已经准备好的步行或公交结果；没有路线线条时仍可查看地点与文字摘要。</p>
          </div>
          <div id="trip-map-stay" className="grid gap-5 xl:grid-cols-[1.35fr_1fr]">
            <MapTheater
              view={mapView || {
                status: result.map.status,
                message: result.map.message,
                days: [],
                available_actions: result.map.available_actions,
              }}
              itineraryDays={result.days}
              busy={enhancementBusy === 'MAP'}
              disabled={mutationLocked}
              onRender={() => void handleMapRender()}
            />
            <StayPanel
              view={stayView || result.stay}
              busy={enhancementBusy === 'STAY'}
              disabled={mutationLocked}
              onChoose={(candidateToken) => void handleStaySelection(candidateToken)}
            />
          </div>

          {(enhancementRecoveryAvailable || enhancementReadBusy) && (
            <div
              data-testid="enhancement-read-recovery"
              role="status"
              aria-live="polite"
              className="mt-4 rounded-2xl border border-amber-200/70 bg-amber-50 px-4 py-3 text-sm text-amber-950"
            >
              <p>路线或住宿状态暂时未能完整读取，不影响继续查看优先检查。</p>
              <button
                data-testid="retry-enhancements"
                type="button"
                disabled={mutationLocked || enhancementReadBusy}
                onClick={retryEnhancements}
                className="mt-3 min-h-12 rounded-xl border border-amber-700/30 bg-white px-4 font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-800 disabled:opacity-60"
              >
                {enhancementReadBusy ? '正在重新读取路线与住宿状态…' : '重新读取路线与住宿状态'}
              </button>
            </div>
          )}
          </section>

          <section
            id="checks-view"
            data-testid="result-view-checks"
            hidden={activeView !== 'CHECKS'}
            aria-label="优先检查"
          >
          <div id="trip-check-area">
            <TripCheckPanel
              view={checksView}
              preview={changePreview}
              busy={checkBusy}
              mutationLocked={mutationLocked}
              message={checkMessage}
              onPreview={(checkToken) => void handleChangePreview(checkToken)}
              onAdopt={() => void handleChangeAdopt()}
              onClosePreview={invalidatePreview}
              onRetry={retryChecks}
            />
          </div>
          </section>

          <div hidden={activeView !== 'ITINERARY'}>
          {user && activeMode !== 'DEMO' && resourceRef ? (
            <MemorySharePanel resourceRef={resourceRef} />
          ) : null}

          <section className="mt-6 rounded-3xl border border-slate-200 bg-white p-5" aria-labelledby="trip-privacy-title">
            <div className="flex items-start gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-emerald-50 text-emerald-700">
                <ShieldCheck className="h-5 w-5" aria-hidden="true" />
              </div>
              <div>
                <h2 id="trip-privacy-title" className="font-semibold">隐私与保留</h2>
                <p className="mt-1 text-xs leading-5 text-slate-500">你可以只删除攻略原文并保留卡片，也可以永久删除整份行程。完成提示只会在服务端回读确认后出现。</p>
              </div>
            </div>
            {privacyMessage && (
              <p role="status" className="mt-4 rounded-2xl bg-amber-50 px-4 py-3 text-sm text-amber-800">{privacyMessage}</p>
            )}
            <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:flex-wrap">
              {activeMode === 'DEMO' && (
                <button
                  data-testid={user ? 'claim-demo-trip' : 'login-to-claim-demo'}
                  type="button"
                  disabled={mutationLocked || privacyBusy !== null || !isHydrated}
                  onClick={() => void handleClaim()}
                  className="min-h-12 rounded-xl bg-emerald-700 px-4 text-sm font-medium text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2 disabled:opacity-50"
                >
                  {privacyBusy === 'CLAIM' ? '正在保存…' : user ? '保存到我的账号' : '登录后保存这份体验'}
                </button>
              )}
              {user && activeMode !== 'DEMO' && (
                <button
                  data-testid="delete-trip-source"
                  type="button"
                  disabled={mutationLocked || privacyBusy !== null || sourceDeleted}
                  onClick={() => setPrivacyConfirmation('SOURCE')}
                  className="min-h-12 rounded-xl border border-slate-300 px-4 text-sm font-medium text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 disabled:opacity-50"
                >
                  {sourceDeleted ? '原文已删除' : privacyBusy === 'SOURCE' ? '正在删除原文…' : '删除原文，保留卡片'}
                </button>
              )}
              <button
                data-testid="delete-entire-trip"
                type="button"
                disabled={mutationLocked || privacyBusy !== null}
                onClick={() => setPrivacyConfirmation('TRIP')}
                className="min-h-12 rounded-xl border border-slate-300 px-4 text-sm font-medium text-slate-600 hover:border-rose-300 hover:text-rose-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-700 disabled:opacity-50"
              >
                {privacyBusy === 'TRIP' ? '正在删除行程…' : '永久删除整份行程'}
              </button>
            </div>
          </section>
          </div>
        </div>
      </div>

      {editor && (
        <AccessibleDialog
          titleId="card-editor-title"
          onClose={() => closeEditor()}
          returnFocusRef={editorTriggerRef}
          dismissDisabled={mutationLocked}
        >
            <div className="flex items-center justify-between gap-4">
              <h2 id="card-editor-title" className="text-xl font-semibold">
                {editor.mode === 'INSERT' ? '新增地点' : editor.mode === 'REPLACE' ? '替换地点' : '编辑卡片文字'}
              </h2>
              <button type="button" onClick={() => closeEditor()} className="inline-flex min-h-12 min-w-12 items-center justify-center rounded-xl bg-slate-100 text-slate-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700" aria-label="关闭编辑">
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            <div className="mt-5 space-y-4">
              <label className="block text-sm font-medium text-slate-700">
                地点名称
                <input
                  ref={editorNameRef}
                  data-testid="card-editor-name"
                  data-dialog-initial-focus
                  value={editorName}
                  onChange={(event) => setEditorName(event.target.value)}
                  maxLength={40}
                  className="mt-2 min-h-12 w-full rounded-xl border border-slate-300 px-3 outline-none focus:border-emerald-700 focus:ring-2 focus:ring-emerald-700/20"
                />
              </label>
              {editor.mode !== 'EDIT' && (
                <>
                  <label className="block text-sm font-medium text-slate-700">
                    类别
                    <input value={editorCategory} onChange={(event) => setEditorCategory(event.target.value)} maxLength={40} className="mt-2 min-h-12 w-full rounded-xl border border-slate-300 px-3 outline-none focus:border-emerald-700 focus:ring-2 focus:ring-emerald-700/20" />
                  </label>
                  <label className="block text-sm font-medium text-slate-700">
                    区域或地址
                    <input value={editorAddress} onChange={(event) => setEditorAddress(event.target.value)} maxLength={120} className="mt-2 min-h-12 w-full rounded-xl border border-slate-300 px-3 outline-none focus:border-emerald-700 focus:ring-2 focus:ring-emerald-700/20" />
                  </label>
                </>
              )}
              {editor.mode !== 'REPLACE' && (
                <label className="block text-sm font-medium text-slate-700">
                  时间提示（可选）
                  <input value={editorTime} onChange={(event) => setEditorTime(event.target.value)} maxLength={80} className="mt-2 min-h-12 w-full rounded-xl border border-slate-300 px-3 outline-none focus:border-emerald-700 focus:ring-2 focus:ring-emerald-700/20" />
                </label>
              )}
            </div>
            <button
              data-testid="save-card-editor"
              type="button"
              disabled={mutationLocked}
              onClick={() => void submitEditor()}
              className="mt-6 min-h-12 w-full rounded-2xl bg-emerald-700 px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2 disabled:cursor-wait disabled:opacity-70"
            >
              {mutationLocked ? '正在保存…' : '保存调整'}
            </button>
        </AccessibleDialog>
      )}

      {assumptionEditor && (
        <AccessibleDialog
          titleId="assumption-editor-title"
          descriptionId="assumption-editor-description"
          onClose={() => setAssumptionEditor(null)}
          dismissDisabled={mutationLocked}
        >
          <div className="flex items-start justify-between gap-4">
            <div>
              <p className="text-xs font-semibold text-emerald-700">可编辑的行程假设</p>
              <h2 id="assumption-editor-title" className="mt-1 text-xl font-semibold">修改{assumptionEditor.label}</h2>
            </div>
            <button type="button" onClick={() => setAssumptionEditor(null)} disabled={mutationLocked} className="inline-flex min-h-12 min-w-12 items-center justify-center rounded-xl bg-slate-100 text-slate-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 disabled:opacity-50" aria-label="关闭假设编辑">
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
          <p id="assumption-editor-description" className="mt-3 text-sm leading-6 text-slate-600">这是系统为缺失信息做的临时假设，你可以随时改成更合适的值。</p>
          <label className="mt-5 block text-sm font-medium text-slate-700">
            {assumptionEditor.label}
            <input
              data-testid="assumption-editor-input"
              data-dialog-initial-focus
              value={assumptionValue}
              onChange={(event) => setAssumptionValue(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault()
                  void submitAssumption()
                }
              }}
              maxLength={80}
              className="mt-2 min-h-12 w-full rounded-xl border border-slate-300 px-3 outline-none focus:border-emerald-700 focus:ring-2 focus:ring-emerald-700/20"
            />
          </label>
          <div className="mt-6 grid grid-cols-2 gap-3">
            <button type="button" disabled={mutationLocked} onClick={() => setAssumptionEditor(null)} className="min-h-12 rounded-xl border border-slate-300 px-4 text-sm font-semibold text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 disabled:opacity-50">取消</button>
            <button data-testid="save-assumption" type="button" disabled={mutationLocked || !assumptionValue.trim()} onClick={() => void submitAssumption()} className="min-h-12 rounded-xl bg-emerald-700 px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2 disabled:opacity-50">{mutationLocked ? '正在保存…' : '保存修改'}</button>
          </div>
        </AccessibleDialog>
      )}

      {privacyConfirmation && (
        <AccessibleDialog
          titleId="privacy-confirm-title"
          descriptionId="privacy-confirm-description"
          onClose={() => setPrivacyConfirmation(null)}
        >
          <div className="flex items-start justify-between gap-4">
            <div>
              <p className="text-xs font-semibold text-amber-700">不可恢复的操作</p>
              <h2 id="privacy-confirm-title" className="mt-1 text-xl font-semibold">
                {privacyConfirmation === 'SOURCE' ? '删除攻略原文？' : '永久删除整份行程？'}
              </h2>
            </div>
            <button type="button" onClick={() => setPrivacyConfirmation(null)} className="inline-flex min-h-12 min-w-12 items-center justify-center rounded-xl bg-slate-100 text-slate-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-700" aria-label="关闭删除确认">
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
          <p id="privacy-confirm-description" className="mt-4 text-sm leading-6 text-slate-600">
            {privacyConfirmation === 'SOURCE'
              ? '攻略文字将永久不可恢复；当前逐日卡片会保留。'
              : '原文、卡片和相关结果都会永久移除，之后无法恢复。'}
          </p>
          <div className="mt-6 grid grid-cols-2 gap-3">
            <button data-dialog-initial-focus type="button" onClick={() => setPrivacyConfirmation(null)} className="min-h-12 rounded-xl border border-slate-300 px-4 text-sm font-semibold text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700">取消</button>
            <button
              data-testid={privacyConfirmation === 'SOURCE' ? 'confirm-delete-source' : 'confirm-delete-trip'}
              type="button"
              onClick={() => {
                const kind = privacyConfirmation
                setPrivacyConfirmation(null)
                if (kind === 'SOURCE') void handleDeleteSource()
                else void handleDeleteTrip()
              }}
              className="min-h-12 rounded-xl bg-slate-900 px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-900 focus-visible:ring-offset-2"
            >
              确认删除
            </button>
          </div>
        </AccessibleDialog>
      )}
    </main>
    </>
  )
}


function TripCheckPanel({
  view,
  preview,
  busy,
  mutationLocked,
  message,
  onPreview,
  onAdopt,
  onClosePreview,
  onRetry,
}: {
  view: PublicTripChecksView | null
  preview: PublicChangePreview | null
  busy: 'PREPARE' | 'PREVIEW' | 'ADOPT' | null
  mutationLocked: boolean
  message: string
  onPreview: (checkToken: string) => void
  onAdopt: () => void
  onClosePreview: () => void
  onRetry: () => void
}) {
  const visibleItems = topPublicChecks(view)
  const labelClass = {
    必须调整: 'bg-rose-50 text-rose-700',
    可以更好: 'bg-amber-50 text-amber-800',
    需要确认: 'bg-blue-50 text-blue-700',
  }
  return (
    <section
      data-testid="trip-checks"
      className="mt-6 rounded-3xl border border-slate-200 bg-white p-5 shadow-sm"
      aria-labelledby="trip-checks-title"
    >
      <div className="flex items-start gap-3">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-violet-50 text-violet-700">
          <Sparkles className="h-5 w-5" aria-hidden="true" />
        </span>
        <div className="min-w-0 flex-1">
          <h2 id="trip-checks-title" className="font-semibold">优先检查</h2>
          <p role="status" className="mt-1 text-xs leading-5 text-slate-500">
            {message || view?.message || (busy === 'PREPARE' ? '正在准备最值得处理的三项…' : '路线和住宿准备后会自动检查。')}
          </p>
        </div>
      </div>

      {!view && message && busy === null && (
        <button
          type="button"
          disabled={mutationLocked}
          onClick={onRetry}
          className="mt-4 min-h-12 rounded-xl border border-violet-200 bg-violet-50 px-4 py-2.5 text-sm font-semibold text-violet-800 transition motion-reduce:transition-none hover:bg-violet-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-600 focus-visible:ring-offset-2 disabled:opacity-50"
        >
          重新准备检查
        </button>
      )}

      {view && (
        <div className="mt-4 grid gap-3 lg:grid-cols-3">
          {visibleItems.map((item) => (
            <article key={item.check_token} data-testid="trip-check-item" className="rounded-2xl border border-slate-200 p-4">
              <div className="flex flex-wrap items-center gap-2">
                <span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${labelClass[item.label]}`}>
                  {item.label}
                </span>
                {item.affected_days.map((day) => (
                  <span key={day} className="text-xs text-slate-600">{day}</span>
                ))}
              </div>
              <h3 className="mt-3 font-semibold text-slate-800">{item.title}</h3>
              <p className="mt-1 text-xs leading-5 text-slate-500">{item.message}</p>
              {item.can_preview && (
                <button
                  data-testid="preview-change"
                  type="button"
                  disabled={busy !== null}
                  onClick={() => onPreview(item.check_token)}
                  className="mt-4 min-h-12 w-full rounded-xl bg-violet-700 px-3 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-700 focus-visible:ring-offset-2 disabled:opacity-50"
                >
                  {busy === 'PREVIEW' ? '正在准备预览…' : '预览怎么调整'}
                </button>
              )}
            </article>
          ))}
          {visibleItems.length === 0 && (
            <div className="rounded-2xl bg-emerald-50 p-4 text-sm leading-6 text-emerald-800 lg:col-span-3">
              当前没有需要优先处理的问题。
            </div>
          )}
        </div>
      )}
      {view && view.remaining_must_adjust > 0 && (
        <p className="mt-3 text-xs text-slate-500">
          另外还有 {view.remaining_must_adjust} 项必须调整，可在处理后继续查看。
        </p>
      )}

      {preview && (
        <div data-testid="change-preview" className="mt-5 rounded-2xl border border-violet-200 bg-violet-50/60 p-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="text-xs font-semibold text-violet-700">改动预览</p>
              <h3 className="mt-1 font-semibold text-slate-800">{preview.title}</h3>
              <p className="mt-1 text-sm leading-6 text-slate-600">{preview.summary}</p>
            </div>
            <button type="button" onClick={onClosePreview} className="inline-flex min-h-12 min-w-12 items-center justify-center rounded-xl bg-white text-slate-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-700" aria-label="关闭改动预览">
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <div className="rounded-xl bg-white/80 p-3">
              <p className="text-xs font-semibold text-slate-500">现在</p>
              {preview.before.map((item) => <p key={item} className="mt-1 text-xs leading-5 text-slate-600">{item}</p>)}
            </div>
            <div className="rounded-xl bg-white/80 p-3">
              <p className="text-xs font-semibold text-violet-700">采纳后</p>
              {preview.after.map((item) => <p key={item} className="mt-1 text-xs leading-5 text-slate-600">{item}</p>)}
            </div>
          </div>
          <button
            data-testid="adopt-change"
            type="button"
            disabled={busy !== null || mutationLocked}
            onClick={onAdopt}
            className="mt-4 min-h-12 w-full rounded-xl bg-violet-700 px-4 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-700 focus-visible:ring-offset-2 disabled:opacity-50"
          >
            {busy === 'ADOPT' ? '正在保存并重新检查…' : '采纳这次改动'}
          </button>
        </div>
      )}
    </section>
  )
}


function MapTheater({
  view,
  itineraryDays,
  busy,
  disabled,
  onRender,
}: {
  view: MapRenderView
  itineraryDays: UserFacingTripResult['days']
  busy: boolean
  disabled: boolean
  onRender: () => void
}) {
  const [mode, setMode] = useState<'walking' | 'transit'>('walking')
  const [directoryOpen, setDirectoryOpen] = useState(false)
  useEffect(() => {
    setDirectoryOpen(window.matchMedia('(min-width: 640px)').matches)
  }, [])
  const segments = routeGeometrySegments(view, mode)
  const allPoints = segments.flatMap((segment) => segment.points)
  const longitudes = allPoints.map((point) => point.longitude)
  const latitudes = allPoints.map((point) => point.latitude)
  const minimumLongitude = Math.min(...longitudes)
  const maximumLongitude = Math.max(...longitudes)
  const minimumLatitude = Math.min(...latitudes)
  const maximumLatitude = Math.max(...latitudes)
  const width = 640
  const height = 300
  const padding = 28
  const svgPoint = (longitude: number, latitude: number) => {
    const longitudeRange = Math.max(0.0001, maximumLongitude - minimumLongitude)
    const latitudeRange = Math.max(0.0001, maximumLatitude - minimumLatitude)
    const x = padding + ((longitude - minimumLongitude) / longitudeRange) * (width - padding * 2)
    const y = padding + ((maximumLatitude - latitude) / latitudeRange) * (height - padding * 2)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }
  const renderAction = view.available_actions.includes('RENDER_MAP')

  return (
    <section data-testid="map-theater" className="overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm" aria-labelledby="map-theater-title">
      <div className="flex flex-col gap-4 border-b border-slate-100 p-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-emerald-50 text-emerald-700">
            <Map className="h-5 w-5" aria-hidden="true" />
          </span>
          <div>
            <h2 id="map-theater-title" className="font-semibold">路线地图</h2>
            <p role="status" className="mt-1 text-xs leading-5 text-slate-500">{mapTheaterStatusMessage(view.status)}</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex rounded-xl bg-slate-100 p-1" role="group" aria-label="路线方式">
            <button
              data-testid="map-mode-walking"
              type="button"
              onClick={() => setMode('walking')}
              aria-pressed={mode === 'walking'}
              className={`inline-flex min-h-12 items-center gap-1.5 rounded-lg px-3 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 ${mode === 'walking' ? 'bg-white text-emerald-700 shadow-sm' : 'text-slate-600'}`}
            >
              <Footprints className="h-3.5 w-3.5" aria-hidden="true" />步行
            </button>
            <button
              data-testid="map-mode-transit"
              type="button"
              onClick={() => setMode('transit')}
              aria-pressed={mode === 'transit'}
              className={`inline-flex min-h-12 items-center gap-1.5 rounded-lg px-3 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-700 ${mode === 'transit' ? 'bg-white text-blue-700 shadow-sm' : 'text-slate-600'}`}
            >
              <BusFront className="h-3.5 w-3.5" aria-hidden="true" />公交
            </button>
          </div>
          {renderAction && (
            <button
              data-testid="render-map"
              type="button"
              disabled={disabled || busy || view.status === 'PREPARING'}
              onClick={onRender}
              className="inline-flex min-h-12 items-center gap-1.5 rounded-xl bg-emerald-700 px-3 text-xs font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2 disabled:opacity-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${busy ? 'animate-spin motion-reduce:animate-none' : ''}`} aria-hidden="true" />
              重新渲染地图
            </button>
          )}
        </div>
      </div>

      <div className="relative min-h-[26rem] overflow-hidden bg-[#eef2e9]">
        <div className="absolute inset-0 opacity-70" aria-hidden="true" style={{ backgroundImage: 'linear-gradient(#dbe3d5 1px, transparent 1px), linear-gradient(90deg, #dbe3d5 1px, transparent 1px)', backgroundSize: '32px 32px' }} />

        <button
          data-testid="map-directory-toggle"
          type="button"
          aria-expanded={directoryOpen}
          aria-controls="map-place-directory"
          onClick={() => setDirectoryOpen((open) => !open)}
          className="absolute left-4 top-4 z-20 inline-flex min-h-12 items-center gap-2 rounded-xl border border-emerald-950/10 bg-white/95 px-3 text-xs font-semibold text-emerald-900 shadow-lg backdrop-blur focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2"
        >
          {directoryOpen ? <ChevronLeft className="h-4 w-4" aria-hidden="true" /> : <List className="h-4 w-4" aria-hidden="true" />}
          {directoryOpen ? '收起地点' : '查看地点'}
        </button>

        <aside
          id="map-place-directory"
          data-testid="map-place-directory"
          hidden={!directoryOpen}
          className="absolute bottom-4 left-4 top-[4.5rem] z-20 w-[calc(100%-5rem)] max-w-[17.375rem] overflow-y-auto rounded-2xl border border-emerald-950/10 bg-white/95 p-4 shadow-xl backdrop-blur"
          aria-label="逐日地点目录"
        >
          <h3 className="text-sm font-semibold text-slate-800">逐日地点</h3>
          <p className="mt-1 text-xs leading-5 text-slate-500">目录来自当前行程卡片，不代表地图已有坐标。</p>
          <div className="mt-4 space-y-4">
            {itineraryDays.map((day, dayIndex) => (
              <div key={`${day.label}-${dayIndex}`} data-day-index={dayIndex}>
                <p className="flex items-center gap-2 text-xs font-semibold text-slate-700">
                  <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: DAY_COLORS[dayIndex % DAY_COLORS.length] }} aria-hidden="true" />
                  {day.label}
                </p>
                <ol className="mt-2 space-y-1.5">
                  {day.activities.map((activity, activityIndex) => (
                    <li key={activity.activity_token} className="flex items-start gap-2 text-xs leading-5 text-slate-600">
                      <span className="mt-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-slate-100 text-[9px] font-semibold text-slate-600">{activityIndex + 1}</span>
                      <span>{activity.name}</span>
                    </li>
                  ))}
                  {day.activities.length === 0 && <li className="text-xs text-slate-400">当天尚无地点</li>}
                </ol>
              </div>
            ))}
          </div>
        </aside>

        {segments.length > 0 ? (
          <svg viewBox={`0 0 ${width} ${height}`} className="absolute inset-0 h-full w-full" role="img" aria-label={`${mode === 'walking' ? '步行' : '公交'}路线图`}>
            <defs>
              <pattern id="map-grid" width="32" height="32" patternUnits="userSpaceOnUse">
                <path d="M 32 0 L 0 0 0 32" fill="none" stroke="#dbe3d5" strokeWidth="1" />
              </pattern>
            </defs>
            {segments.map((segment) => {
              const color = DAY_COLORS[segment.dayIndex % DAY_COLORS.length]
              return (
                <g key={`${segment.dayLabel}-${segment.routeIndex}`} data-day-index={segment.dayIndex}>
                  <polyline
                    data-testid="map-route-line"
                    points={segment.points.map((point) => svgPoint(point.longitude, point.latitude)).join(' ')}
                    fill="none"
                    stroke={color}
                    strokeWidth="6"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeDasharray={mode === 'walking' ? '3 10' : undefined}
                  />
                  {segment.points.filter((_point, index) => index === 0 || index === segment.points.length - 1).map((point, pointIndex) => {
                    const [x, y] = svgPoint(point.longitude, point.latitude).split(',')
                    return <circle data-testid="map-route-marker" key={pointIndex} cx={x} cy={y} r="7" fill="white" stroke={color} strokeWidth="4" />
                  })}
                </g>
              )
            })}
          </svg>
        ) : (
          <div className="relative z-10 flex min-h-[26rem] items-center justify-center px-8 text-center text-sm leading-6 text-slate-600">
            <span className="max-w-xs rounded-2xl border border-slate-300/80 bg-white/85 px-5 py-4 shadow-sm">
            {view.status === 'PREPARING' ? '路线正在后台准备，卡片可以先查看和调整。' : '地图线条暂不可用，下面的路线摘要仍然有效。'}
            </span>
          </div>
        )}
      </div>

      <div className="space-y-4 p-5">
        {view.days.map((day, dayIndex) => (
          <div key={day.label}>
            <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-slate-600">
              <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: DAY_COLORS[dayIndex % DAY_COLORS.length] }} />
              {day.label}
            </div>
            <div className="space-y-2">
              {day.routes.map((route, routeIndex) => (
                <div key={`${route.from_name}-${route.to_name}-${routeIndex}`} className="rounded-xl bg-slate-50 px-3 py-2.5 text-xs">
                  <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between sm:gap-3">
                    <span className="min-w-0 text-slate-600">{route.from_name} → {route.to_name}</span>
                    <span className="shrink-0 font-medium text-slate-700">
                      {mapRouteSummary(view, route, mode)}
                    </span>
                  </div>
                  {route.message && <p className="mt-1 leading-5 text-slate-500">{route.message}</p>}
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}


function mapTheaterStatusMessage(status: MapRenderView['status']) {
  if (status === 'PREPARING') return '路线正在后台准备，可以先查看行程卡片。'
  if (status === 'AVAILABLE') return '路线已准备，可以切换步行或公交查看。'
  if (status === 'NEEDS_UPDATE') return '行程有变化，地图需要手动更新。'
  if (status === 'LIMITED') return '部分路线暂时无法显示，已保留可用结果。'
  return '路线暂时无法显示，行程卡片不受影响。'
}


function mapRouteSummary(
  view: MapRenderView,
  route: MapRenderView['days'][number]['routes'][number],
  mode: 'walking' | 'transit',
) {
  if (view.status === 'NEEDS_UPDATE') return '路线需要更新'
  const duration = route[mode].duration_minutes
  if (
    (view.status === 'AVAILABLE' || view.status === 'LIMITED')
    && route[mode].status === 'AVAILABLE'
    && typeof duration === 'number'
    && Number.isFinite(duration)
    && duration > 0
  ) return `${mode === 'walking' ? '步行' : '公交'} ${duration} 分钟`
  return '路线待确认'
}


function StayPanel({
  view,
  busy,
  disabled,
  onChoose,
}: {
  view: StaySuggestionView
  busy: boolean
  disabled: boolean
  onChoose: (candidateToken: string) => void
}) {
  return (
    <section data-testid="stay-panel" className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm" aria-labelledby="stay-panel-title">
      <div className="flex items-start gap-3">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-blue-50 text-blue-700">
          <BedDouble className="h-5 w-5" aria-hidden="true" />
        </span>
        <div>
          <h2 id="stay-panel-title" className="font-semibold">整程住宿</h2>
          <p role="status" className="mt-1 text-xs leading-5 text-slate-500">{view.message}</p>
        </div>
      </div>
      {view.area_summary && (
        <div className="mt-4 rounded-2xl bg-blue-50/70 p-3 text-xs leading-5 text-blue-900">
          建议区域：{view.area_summary}
          {view.searched_scopes.length > 0 && <span className="block text-blue-700">已比较：{view.searched_scopes.join('、')}</span>}
        </div>
      )}
      <div className="mt-4 space-y-3">
        {view.candidates.slice(0, 3).map((candidate, index) => (
          <article key={candidate.candidate_token} data-testid="stay-candidate" className="rounded-2xl border border-slate-200 p-4">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-xs font-medium text-blue-700">候选 {index + 1} · {candidate.brand}</p>
                <h3 className="mt-1 truncate font-semibold text-slate-800">{candidate.name}</h3>
                <p className="mt-1 text-xs leading-5 text-slate-600">{candidate.area_or_address}</p>
              </div>
              {candidate.selected && <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-600" aria-label="已选择" />}
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-slate-600">
              <span className="rounded-xl bg-slate-50 px-2.5 py-2">最久约 {candidate.max_single_leg_minutes} 分钟</span>
              <span className="rounded-xl bg-slate-50 px-2.5 py-2">共 {candidate.transfer_count} 次换乘</span>
            </div>
            <p className="mt-3 rounded-xl bg-blue-50/60 px-3 py-2 text-xs leading-5 text-blue-900">{candidate.commute_summary}</p>
            <p className="mt-3 text-xs leading-5 text-slate-500">{candidate.reason}</p>
            {!candidate.selected && candidate.available_actions.includes('CHOOSE_STAY') && (
              <button
                data-testid="choose-stay"
                type="button"
                disabled={disabled || busy}
                onClick={() => onChoose(candidate.candidate_token)}
                className="mt-3 min-h-12 w-full rounded-xl bg-blue-700 px-3 text-sm font-semibold text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-700 focus-visible:ring-offset-2 disabled:opacity-50"
              >
                {busy ? '正在保存…' : '整程住这里'}
              </button>
            )}
          </article>
        ))}
        {view.candidates.length === 0 && view.status !== 'PREPARING' && (
          <div className="rounded-2xl bg-slate-50 p-4 text-sm leading-6 text-slate-500">
            当前没有合格候选，不影响继续查看和调整行程。
          </div>
        )}
      </div>
    </section>
  )
}
