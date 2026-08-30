'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import {
  ArrowLeft,
  BedDouble,
  BusFront,
  CalendarDays,
  CheckCircle2,
  Compass,
  Footprints,
  Map,
  MapPin,
  Pencil,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Users,
  X,
} from 'lucide-react'

import ItineraryWorkspace from './itinerary-workspace'
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

type ActiveChecksRequest = {
  id: number
  resourceRef: string
  key: string
  controller: AbortController
}

type WorkspaceCommandResult =
  | { status: 'APPLIED' | 'SYNCED'; days: UserFacingTripResult['days'] }
  | { status: 'RECONCILING' }


const CHECKS_REQUEST_TIMEOUT_MS = 10_000


function scrollToResultSection(id: string) {
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  document.getElementById(id)?.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth' })
}


export default function TripResultPage() {
  const router = useRouter()
  const { user, isHydrated, hydrate } = useAuthStore()
  const [resourceRef, setResourceRef] = useState<string | null>(null)
  const [activeMode, setActiveMode] = useState<'DEMO' | 'FULL' | 'CLAIMED' | null>(null)
  const [result, setResult] = useState<UserFacingTripResult | null>(null)
  const [etag, setEtag] = useState<string | null>(null)
  const [message, setMessage] = useState('正在整理每天行程')
  const [error, setError] = useState('')
  const [commandError, setCommandError] = useState('')
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [editorName, setEditorName] = useState('')
  const [editorCategory, setEditorCategory] = useState('地点')
  const [editorAddress, setEditorAddress] = useState('地点待确认')
  const [editorTime, setEditorTime] = useState('')
  const [privacyBusy, setPrivacyBusy] = useState<'CLAIM' | 'SOURCE' | 'TRIP' | null>(null)
  const [privacyMessage, setPrivacyMessage] = useState('')
  const [sourceDeleted, setSourceDeleted] = useState(false)
  const [tripDeleted, setTripDeleted] = useState(false)
  const [mapView, setMapView] = useState<MapRenderView | null>(null)
  const [stayView, setStayView] = useState<StaySuggestionView | null>(null)
  const [enhancementBusy, setEnhancementBusy] = useState<'MAP' | 'STAY' | null>(null)
  const [checksView, setChecksView] = useState<PublicTripChecksView | null>(null)
  const [changePreview, setChangePreview] = useState<PublicChangePreview | null>(null)
  const [checkBusy, setCheckBusy] = useState<'PREPARE' | 'PREVIEW' | 'ADOPT' | null>(null)
  const [checkMessage, setCheckMessage] = useState('')
  const [checksRetryGeneration, setChecksRetryGeneration] = useState(0)
  const [mutationLocked, setMutationLocked] = useState(false)
  const [reconciliationRequired, setReconciliationRequired] = useState(false)
  const [reconciliationBusy, setReconciliationBusy] = useState(false)
  const [reconciliationKind, setReconciliationKind] = useState<'RESULT' | 'TRIP_DELETE'>('RESULT')
  const activeResourceRef = useRef<string | null>(null)
  const mountedRef = useRef(false)
  const checksRequestSequence = useRef(0)
  const activeChecksRequest = useRef<ActiveChecksRequest | null>(null)
  const completedChecksKey = useRef<string | null>(null)
  const commandInFlightRef = useRef(false)
  const mutationLockRef = useRef(false)
  const authoritativeKeyRef = useRef<string | null>(null)
  const enhancementGenerationRef = useRef(0)
  const resultAvailable = result !== null
  const currentChecksKey = resourceRef && etag ? `${resourceRef}:${etag}` : null
  const currentChecksKeyRef = useRef<string | null>(currentChecksKey)
  currentChecksKeyRef.current = currentChecksKey

  useEffect(() => {
    hydrate()
  }, [hydrate])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      checksRequestSequence.current += 1
      activeChecksRequest.current?.controller.abort()
      activeChecksRequest.current = null
    }
  }, [])

  const beginMutation = useCallback(() => {
    if (mutationLockRef.current) return false
    mutationLockRef.current = true
    setMutationLocked(true)
    return true
  }, [])

  const finishMutation = useCallback(() => {
    mutationLockRef.current = false
    setMutationLocked(false)
    setReconciliationRequired(false)
    setReconciliationKind('RESULT')
  }, [])

  const holdForReconciliation = useCallback((
    message: string,
    kind: 'RESULT' | 'TRIP_DELETE' = 'RESULT',
  ) => {
    setReconciliationKind(kind)
    setReconciliationRequired(true)
    setCommandError(message)
  }, [])

  const refreshEnhancements = useCallback(async (
    reference: string,
    generation = enhancementGenerationRef.current,
  ) => {
    const [mapResult, stayResult] = await Promise.allSettled([
      readTripUnderstandingMap(reference),
      readTripUnderstandingStay(reference),
    ])
    if (
      activeResourceRef.current !== reference
      || enhancementGenerationRef.current !== generation
    ) return
    if (mapResult.status === 'fulfilled') setMapView(mapResult.value)
    if (stayResult.status === 'fulfilled') setStayView(stayResult.value)
  }, [])

  const refresh = useCallback(async (reference: string, suppressOpenError = false) => {
    try {
      const response = await readTripUnderstandingResult(reference)
      if (activeResourceRef.current !== reference) return null
      if (response.body.status !== 'PROCESSING') {
        let enhancementGeneration = enhancementGenerationRef.current
        const authoritativeKey = response.etag ? `${reference}:${response.etag}` : null
        if (response.etag && authoritativeKeyRef.current !== authoritativeKey) {
          const checksAlreadyMatch = completedChecksKey.current === `${reference}:${response.etag}`
          enhancementGeneration = enhancementGenerationRef.current + 1
          enhancementGenerationRef.current = enhancementGeneration
          setMapView(null)
          setStayView(null)
          if (!checksAlreadyMatch) setChecksView(null)
          setChangePreview(null)
          if (!checksAlreadyMatch) completedChecksKey.current = null
        }
        authoritativeKeyRef.current = authoritativeKey
        setResult(response.body)
        if (response.etag) {
          setEtag(response.etag)
          sessionStorage.setItem('bt_active_trip_etag', response.etag)
        }
        setMessage('卡片已可用')
        void refreshEnhancements(reference, enhancementGeneration)
        return response.body
      }
      setMessage(response.body.message)
      return null
    } catch {
      if (!suppressOpenError && activeResourceRef.current === reference) {
        setError('这份体验暂时无法打开，请返回首页重新开始。')
      }
      return null
    }
  }, [refreshEnhancements])

  useEffect(() => {
    if (!resourceRef || !resultAvailable) return
    const preparing = mapView?.status === 'PREPARING' || stayView?.status === 'PREPARING'
    if (!preparing) return
    const timer = window.setInterval(() => {
      void refreshEnhancements(resourceRef)
    }, 800)
    return () => window.clearInterval(timer)
  }, [mapView?.status, refreshEnhancements, resourceRef, resultAvailable, stayView?.status])

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
      if (previousRequest.resourceRef === resourceRef && previousRequest.key === attemptKey) return
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
    void (async () => {
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
        if (etag !== prepared.etag) {
          setEtag(prepared.etag)
          sessionStorage.setItem('bt_active_trip_etag', prepared.etag)
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
  }, [
    checksRetryGeneration,
    checksView,
    etag,
    mapView?.status,
    mutationLocked,
    refresh,
    resourceRef,
    resultAvailable,
    stayView?.status,
  ])

  const retryChecks = useCallback(() => {
    if (checkBusy || mutationLockRef.current || !resourceRef || !etag) return
    completedChecksKey.current = null
    setCheckMessage('')
    setChecksRetryGeneration((generation) => generation + 1)
  }, [checkBusy, etag, resourceRef])

  const retryResultReadback = useCallback(async () => {
    if (!resourceRef || !reconciliationRequired || reconciliationBusy) return
    setReconciliationBusy(true)
    if (reconciliationKind === 'TRIP_DELETE') {
      try {
        await deleteTripUnderstanding(resourceRef)
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
    const latest = await refresh(resourceRef, true)
    if (latest) {
      finishMutation()
      setEditor(null)
      setCommandError('已读取服务端最新行程，可以继续调整。')
    } else {
      setCommandError('仍在确认服务端保存结果。请保持此页面打开，稍后再重新读取。')
    }
    setReconciliationBusy(false)
  }, [finishMutation, reconciliationBusy, reconciliationKind, reconciliationRequired, refresh, resourceRef])

  const handleMapRender = useCallback(async () => {
    if (!resourceRef || !etag || enhancementBusy || !beginMutation()) return
    let reconciliationHeld = false
    setEnhancementBusy('MAP')
    setCommandError('')
    try {
      await requestTripUnderstandingMap(resourceRef, etag)
      await refreshEnhancements(resourceRef)
    } catch (mapFailure) {
      const latest = await refresh(resourceRef, true)
      if (!latest) {
        reconciliationHeld = true
        holdForReconciliation('路线更新结果暂时无法确认；确认前其他写入已暂停。')
      }
      if (mapFailure instanceof Error && mapFailure.message === 'REVISION_CONFLICT') {
        if (latest) setCommandError('行程刚刚有更新，已为你读取最新版本。')
      } else {
        if (latest) setCommandError('路线更新请求未能确认，已按服务端最新行程恢复。')
      }
    } finally {
      setEnhancementBusy(null)
      if (!reconciliationHeld) finishMutation()
    }
  }, [beginMutation, enhancementBusy, etag, finishMutation, holdForReconciliation, refresh, refreshEnhancements, resourceRef])

  const handleStaySelection = useCallback(async (candidateToken: string) => {
    if (!resourceRef || !etag || enhancementBusy || !beginMutation()) return
    let reconciliationHeld = false
    setEnhancementBusy('STAY')
    setCommandError('')
    try {
      const selectedStay = await selectTripUnderstandingStay(resourceRef, candidateToken, etag)
      setMapView(null)
      setStayView(null)
      setEtag(selectedStay.etag)
      setChecksView(null)
      setChangePreview(null)
      sessionStorage.setItem('bt_active_trip_etag', selectedStay.etag)
      if (!(await refresh(resourceRef, true))) {
        reconciliationHeld = true
        holdForReconciliation('住宿选择已提交，正在确认服务端最新行程；确认前其他写入已暂停。')
      }
    } catch (stayFailure) {
      const latest = await refresh(resourceRef, true)
      if (!latest) {
        reconciliationHeld = true
        holdForReconciliation('住宿选择的结果暂时无法确认；确认前其他写入已暂停。')
      }
      if (stayFailure instanceof Error && stayFailure.message === 'REVISION_CONFLICT') {
        if (latest) setCommandError('住宿候选已经变化，已为你读取最新版本。')
      } else {
        if (latest) setCommandError('住宿选择请求未完成，已按服务端最新行程恢复。')
      }
    } finally {
      setEnhancementBusy(null)
      if (!reconciliationHeld) finishMutation()
    }
  }, [beginMutation, enhancementBusy, etag, finishMutation, holdForReconciliation, refresh, resourceRef])

  const handleChangePreview = useCallback(async (checkToken: string) => {
    if (!resourceRef || checkBusy) return
    const previewKey = currentChecksKeyRef.current
    setCheckBusy('PREVIEW')
    setCheckMessage('')
    try {
      const preview = await previewTripUnderstandingChange(resourceRef, checkToken)
      if (mutationLockRef.current || currentChecksKeyRef.current !== previewKey) {
        setChangePreview(null)
        return
      }
      setChangePreview(preview)
    } catch {
      setChangePreview(null)
      setCheckMessage('这项建议已经变化，请刷新后再试。')
    } finally {
      setCheckBusy(null)
    }
  }, [checkBusy, resourceRef])

  const handleChangeAdopt = useCallback(async () => {
    if (!resourceRef || !etag || !changePreview || checkBusy || !beginMutation()) return
    let reconciliationHeld = false
    setCheckBusy('ADOPT')
    setCheckMessage('')
    try {
      const adopted = await adoptTripUnderstandingChange(
        resourceRef,
        changePreview.change_token,
        etag,
      )
      setEtag(adopted.etag)
      sessionStorage.setItem('bt_active_trip_etag', adopted.etag)
      completedChecksKey.current = `${resourceRef}:${adopted.etag}`
      setChecksView(adopted.body.checks)
      setChangePreview(null)
      setCheckMessage(adopted.body.message)
      if (!(await refresh(resourceRef, true))) {
        reconciliationHeld = true
        holdForReconciliation('改动已提交，正在确认服务端最新行程；确认前其他写入已暂停。')
      }
    } catch (adoptFailure) {
      setChangePreview(null)
      const latest = await refresh(resourceRef, true)
      if (!latest) {
        reconciliationHeld = true
        holdForReconciliation('改动结果暂时无法确认；确认前其他写入已暂停。')
      }
      if (adoptFailure instanceof Error && adoptFailure.message === 'TRIP_UPDATED') {
        if (latest) setCheckMessage('行程刚刚有更新，已读取最新内容，请重新预览。')
      } else {
        if (latest) setCheckMessage('改动请求未完成，已按服务端最新行程恢复。')
      }
    } finally {
      setCheckBusy(null)
      if (!reconciliationHeld) finishMutation()
    }
  }, [beginMutation, changePreview, checkBusy, etag, finishMutation, holdForReconciliation, refresh, resourceRef])

  const runCommand = useCallback(async (command: TripUnderstandingCommand): Promise<WorkspaceCommandResult> => {
    if (!resourceRef || !etag || commandInFlightRef.current || !beginMutation()) {
      return { status: 'SYNCED', days: result?.days || [] }
    }
    let reconciliationHeld = false
    commandInFlightRef.current = true
    setCommandError('')
    try {
      const applied = await applyTripUnderstandingCommand(resourceRef, etag, command)
      setMapView(null)
      setStayView(null)
      setEtag(applied.etag)
      setChecksView(null)
      setChangePreview(null)
      sessionStorage.setItem('bt_active_trip_etag', applied.etag)
      const refreshed = await refresh(resourceRef, true)
      if (!refreshed) {
        reconciliationHeld = true
        holdForReconciliation('调整已提交，但保存结果暂时无法确认；确认前其他写入已暂停。')
        return { status: 'RECONCILING' }
      }
      setEditor(null)
      return { status: 'APPLIED', days: refreshed.days }
    } catch (commandFailure) {
      const refreshed = await refresh(resourceRef, true)
      if (!refreshed) {
        reconciliationHeld = true
        holdForReconciliation('调整结果暂时无法确认；确认前其他写入已暂停。')
        return { status: 'RECONCILING' }
      }
      if (commandFailure instanceof Error && commandFailure.message === 'REVISION_CONFLICT') {
        setCommandError('卡片刚刚有更新，已为你读取最新版本，请再试一次。')
      } else {
        setCommandError('调整请求未能确认，已按服务端最新行程恢复。')
      }
      return { status: 'SYNCED', days: refreshed.days }
    } finally {
      commandInFlightRef.current = false
      if (!reconciliationHeld) finishMutation()
    }
  }, [beginMutation, etag, finishMutation, holdForReconciliation, refresh, resourceRef, result?.days])

  const openEditor = (state: EditorState) => {
    if (mutationLockRef.current) return
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
    if (editor.mode === 'INSERT') {
      await runCommand({
        command_type: 'ACTIVITY_INSERT',
        day_index: editor.dayIndex,
        position: editor.position,
        name: editorName.trim(),
        category: editorCategory.trim() || '地点',
        area_or_address: editorAddress.trim() || '地点待确认',
        time_hint: editorTime.trim() || null,
      })
    } else if (editor.mode === 'EDIT' && editor.card) {
      await runCommand({
        command_type: 'ACTIVITY_TEXT_EDIT',
        activity_token: editor.card.activity_token,
        name: editorName.trim(),
        time_hint: editorTime.trim() || null,
      })
    } else if (editor.mode === 'REPLACE' && editor.card) {
      await runCommand({
        command_type: 'PLACE_REPLACE',
        activity_token: editor.card.activity_token,
        replacement: {
          name: editorName.trim(),
          category: editorCategory.trim() || '地点',
          area_or_address: editorAddress.trim() || '地点待确认',
        },
      })
    }
  }

  const handleClaim = async () => {
    if (!resourceRef || privacyBusy || mutationLockRef.current) return
    if (!user) {
      sessionStorage.setItem('bt_login_return', '/trip/result')
      router.push('/login')
      return
    }
    if (!beginMutation()) return
    let reconciliationHeld = false
    setPrivacyBusy('CLAIM')
    setPrivacyMessage('')
    try {
      const claimed = await claimTripUnderstanding(resourceRef)
      const nextReference = claimed.body.public_resource_id
      clearTripUnderstandingSession()
      sessionStorage.setItem('bt_active_trip_ref', nextReference)
      sessionStorage.setItem('bt_active_trip_mode', 'CLAIMED')
      sessionStorage.removeItem('bt_active_trip_event_cursor')
      sessionStorage.setItem('bt_active_trip_etag', claimed.etag)
      activeResourceRef.current = nextReference
      setResourceRef(nextReference)
      setActiveMode('CLAIMED')
      setEtag(claimed.etag)
      if (await refresh(nextReference, true)) {
        setPrivacyMessage('已保存到你的账号，匿名访问凭证已经失效。')
      } else {
        reconciliationHeld = true
        holdForReconciliation('账号保存已提交，正在确认服务端最新行程；确认前其他写入已暂停。')
      }
    } catch {
      const latest = await refresh(activeResourceRef.current || resourceRef, true)
      if (latest) {
        setPrivacyMessage('账号保存请求未完成，已按服务端当前行程恢复。')
      } else {
        reconciliationHeld = true
        holdForReconciliation('账号保存结果暂时无法确认；确认前其他写入已暂停。')
      }
    } finally {
      setPrivacyBusy(null)
      if (!reconciliationHeld) finishMutation()
    }
  }

  const handleDeleteSource = async () => {
    if (!resourceRef || privacyBusy || sourceDeleted || mutationLockRef.current) return
    const confirmed = window.confirm(
      '删除原文后，攻略文字将永久不可恢复；当前逐日卡片会保留。确定继续吗？',
    )
    if (!confirmed) return
    if (!beginMutation()) return
    let reconciliationHeld = false
    setPrivacyBusy('SOURCE')
    setPrivacyMessage('')
    try {
      await deleteTripUnderstandingSource(resourceRef)
      if (await refresh(resourceRef, true)) {
        sessionStorage.setItem('bt_active_trip_source_deleted', 'true')
        setSourceDeleted(true)
        setPrivacyMessage('原文已永久删除，逐日卡片仍可继续查看和调整。')
      } else {
        reconciliationHeld = true
        holdForReconciliation('原文删除已提交，正在确认服务端结果；确认前其他写入已暂停。')
      }
    } catch {
      const latest = await refresh(resourceRef, true)
      if (latest) {
        setPrivacyMessage('原文删除请求未完成，已读取服务端当前行程。')
      } else {
        reconciliationHeld = true
        holdForReconciliation('原文删除结果暂时无法确认；确认前其他写入已暂停。')
      }
    } finally {
      setPrivacyBusy(null)
      if (!reconciliationHeld) finishMutation()
    }
  }

  const handleDeleteTrip = async () => {
    if (!resourceRef || privacyBusy || mutationLockRef.current) return
    const confirmed = window.confirm(
      '删除整份行程会永久移除原文、卡片和相关结果，之后无法恢复。确定删除吗？',
    )
    if (!confirmed) return
    if (!beginMutation()) return
    let reconciliationHeld = false
    setPrivacyBusy('TRIP')
    setPrivacyMessage('')
    try {
      await deleteTripUnderstanding(resourceRef)
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
    let interval: ReturnType<typeof setInterval> | undefined
    const eventController = new AbortController()
    void streamTripUnderstandingEvents(
      resourceRef,
      (event) => {
        if (disposed) return
        setMessage(event.message)
        if (event.type === 'result_available') {
          void refresh(resourceRef).then((ready) => {
            if (ready) {
              eventController.abort()
              if (interval) {
                clearInterval(interval)
                interval = undefined
              }
            }
          })
        }
      },
      eventController.signal,
    ).catch((streamError: unknown) => {
      if (!disposed && !(streamError instanceof DOMException && streamError.name === 'AbortError')) {
        void refresh(resourceRef)
      }
    })
    void refresh(resourceRef).then((ready) => {
      if (ready && interval) {
        clearInterval(interval)
        interval = undefined
      }
    })
    interval = setInterval(() => {
      if (!disposed) {
        void refresh(resourceRef).then((ready) => {
          if (ready && interval) clearInterval(interval)
        })
      }
    }, 1000)
    return () => {
      disposed = true
      eventController.abort()
      if (interval) clearInterval(interval)
    }
  }, [refresh, resourceRef])

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
          <button type="button" onClick={() => router.push('/')} className="mt-6 rounded-2xl bg-slate-950 px-5 py-3 text-sm font-semibold text-white">
            返回首页
          </button>
        </div>
      </main>
    )
  }

  if (!resourceRef || !result) {
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

          <nav className="hidden items-center gap-1 lg:flex" aria-label="结果页导航">
            <button type="button" onClick={() => scrollToResultSection('itinerary-overview')} className="min-h-12 rounded-xl px-4 text-sm font-medium text-slate-600 hover:bg-white hover:text-emerald-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700">行程总览</button>
            <button type="button" onClick={() => scrollToResultSection('trip-map-stay')} className="min-h-12 rounded-xl px-4 text-sm font-medium text-slate-600 hover:bg-white hover:text-emerald-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700">地图与住宿</button>
            <button type="button" onClick={() => scrollToResultSection('trip-check-area')} className="min-h-12 rounded-xl px-4 text-sm font-medium text-slate-600 hover:bg-white hover:text-emerald-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700">优先检查</button>
          </nav>

          <button type="button" onClick={() => router.push('/')} className="inline-flex min-h-12 items-center gap-2 rounded-xl border border-emerald-950/10 bg-white px-4 text-sm font-semibold text-slate-700 shadow-sm transition hover:border-emerald-700/30 hover:text-emerald-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700" aria-label="返回首页">
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
            <span className="hidden sm:inline">返回首页</span>
          </button>
        </div>
      </header>

      <main className="min-h-screen overflow-hidden bg-[#f8f7f2] text-slate-900">
      <div className="relative mx-auto w-full max-w-[1540px] px-5 pb-14 sm:px-8 lg:px-12">
        <div className="pointer-events-none absolute -right-32 top-44 h-80 w-80 rounded-full bg-emerald-100/35 blur-3xl" aria-hidden="true" />
        <div className="pointer-events-none absolute -left-40 top-[42rem] h-72 w-72 rounded-full bg-amber-100/45 blur-3xl" aria-hidden="true" />

        <section className="relative border-b border-emerald-950/10 py-5" aria-label="行程摘要">
          <div className="flex flex-wrap items-center gap-y-3 divide-x divide-slate-200">
            {result.assumptions.map((assumption) => {
              const Icon = ASSUMPTION_ICONS[assumption.key]
              return (
                <button
                  key={assumption.key}
                  type="button"
                  disabled={mutationLocked || !assumption.editable}
                  onClick={() => {
                    const value = window.prompt(`修改${assumption.label}`, assumption.value)?.trim()
                    if (value && value !== assumption.value) {
                      void runCommand({ command_type: 'ASSUMPTION_SET', key: assumption.key, value })
                    }
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

        <section id="itinerary-overview" className="relative scroll-mt-28 pt-8 sm:pt-10">
          <div className="max-w-3xl">
            <div className="inline-flex items-center gap-2 rounded-full border border-emerald-900/10 bg-white/80 px-3 py-1.5 text-xs font-semibold text-emerald-800 shadow-sm">
              <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
              {result.status === 'READY' ? '每天的地点已经排好' : '先查看已整理的地点'}
            </div>
            <h1 className="mt-4 text-3xl font-semibold tracking-[-0.035em] text-slate-900 sm:text-5xl">按天查看，照着走<span className="text-emerald-800">更轻松</span></h1>
            <p className="mt-3 text-sm leading-6 text-slate-600 sm:text-base">这是清晰的游览顺序，不伪装成实时路线。拖动卡片后会自动保存，需要时再手动更新地图。</p>
          </div>

          {commandError && (
            <div role="status" aria-live="polite" className="mt-5 rounded-2xl border border-amber-200/70 bg-amber-50 px-4 py-3 text-sm text-amber-900">
              <p>{commandError}</p>
              {reconciliationRequired && (
                <button
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

          <ItineraryWorkspace
            days={result.days}
            disabled={mutationLocked}
            mapStatus={mapView?.status || result.map.status}
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

          <div id="trip-map-stay" className="mt-8 grid scroll-mt-28 gap-5 xl:grid-cols-[1.35fr_1fr]">
            <MapTheater
              view={mapView || {
                status: result.map.status,
                message: result.map.message,
                days: [],
                available_actions: result.map.available_actions,
              }}
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

          <div id="trip-check-area" className="scroll-mt-28">
            <TripCheckPanel
              view={checksView}
              preview={changePreview}
              busy={checkBusy}
              mutationLocked={mutationLocked}
              message={checkMessage}
              onPreview={(checkToken) => void handleChangePreview(checkToken)}
              onAdopt={() => void handleChangeAdopt()}
              onClosePreview={() => setChangePreview(null)}
              onRetry={retryChecks}
            />
          </div>

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
                  onClick={() => void handleDeleteSource()}
                  className="min-h-12 rounded-xl border border-slate-300 px-4 text-sm font-medium text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 disabled:opacity-50"
                >
                  {sourceDeleted ? '原文已删除' : privacyBusy === 'SOURCE' ? '正在删除原文…' : '删除原文，保留卡片'}
                </button>
              )}
              <button
                data-testid="delete-entire-trip"
                type="button"
                disabled={mutationLocked || privacyBusy !== null}
                onClick={() => void handleDeleteTrip()}
                className="min-h-12 rounded-xl border border-slate-300 px-4 text-sm font-medium text-slate-600 hover:border-rose-300 hover:text-rose-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-700 disabled:opacity-50"
              >
                {privacyBusy === 'TRIP' ? '正在删除行程…' : '永久删除整份行程'}
              </button>
            </div>
          </section>
        </section>
      </div>

      {editor && (
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-950/25 p-4 backdrop-blur-sm sm:items-center" role="dialog" aria-modal="true" aria-labelledby="card-editor-title" onKeyDown={(event) => { if (event.key === 'Escape') setEditor(null) }}>
          <div className="w-full max-w-md rounded-3xl bg-white p-6 shadow-2xl">
            <div className="flex items-center justify-between gap-4">
              <h2 id="card-editor-title" className="text-xl font-semibold">
                {editor.mode === 'INSERT' ? '新增地点' : editor.mode === 'REPLACE' ? '替换地点' : '编辑卡片文字'}
              </h2>
              <button type="button" onClick={() => setEditor(null)} className="inline-flex min-h-12 min-w-12 items-center justify-center rounded-xl bg-slate-100 text-slate-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700" aria-label="关闭编辑">
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
            <div className="mt-5 space-y-4">
              <label className="block text-sm font-medium text-slate-700">
                地点名称
                <input
                  data-testid="card-editor-name"
                  autoFocus
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
          </div>
        </div>
      )}
    </main>
    </>
  )
}


const DAY_COLORS = ['#047857', '#2563eb', '#7c3aed', '#d97706', '#0f766e', '#be185d']


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
  const labelClass = {
    必须调整: 'bg-rose-50 text-rose-700',
    可以更好: 'bg-blue-50 text-blue-700',
    需要确认: 'bg-amber-50 text-amber-800',
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
          {view.items.map((item) => (
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
          {view.items.length === 0 && (
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
  busy,
  disabled,
  onRender,
}: {
  view: MapRenderView
  busy: boolean
  disabled: boolean
  onRender: () => void
}) {
  const [mode, setMode] = useState<'walking' | 'transit'>('walking')
  const allPoints = view.days.flatMap((day) => day.routes.flatMap((route) => route[mode].geometry))
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
  const renderAction = view.available_actions.includes('RENDER_MAP') || ['NEEDS_UPDATE', 'LIMITED', 'UNAVAILABLE'].includes(view.status)

  return (
    <section data-testid="map-theater" className="overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm" aria-labelledby="map-theater-title">
      <div className="flex flex-col gap-4 border-b border-slate-100 p-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-emerald-50 text-emerald-700">
            <Map className="h-5 w-5" aria-hidden="true" />
          </span>
          <div>
            <h2 id="map-theater-title" className="font-semibold">路线地图</h2>
            <p role="status" className="mt-1 text-xs leading-5 text-slate-500">{view.message}</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex rounded-xl bg-slate-100 p-1" aria-label="路线方式">
            <button
              data-testid="map-mode-walking"
              type="button"
              onClick={() => setMode('walking')}
              className={`inline-flex min-h-12 items-center gap-1.5 rounded-lg px-3 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 ${mode === 'walking' ? 'bg-white text-emerald-700 shadow-sm' : 'text-slate-600'}`}
            >
              <Footprints className="h-3.5 w-3.5" aria-hidden="true" />步行
            </button>
            <button
              data-testid="map-mode-transit"
              type="button"
              onClick={() => setMode('transit')}
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

      <div className="bg-[#f4f1e8] p-4">
        {allPoints.length >= 2 ? (
          <svg viewBox={`0 0 ${width} ${height}`} className="h-64 w-full rounded-2xl bg-[#eef2e9]" role="img" aria-label={`${mode === 'walking' ? '步行' : '公交'}路线图`}>
            <defs>
              <pattern id="map-grid" width="32" height="32" patternUnits="userSpaceOnUse">
                <path d="M 32 0 L 0 0 0 32" fill="none" stroke="#dbe3d5" strokeWidth="1" />
              </pattern>
            </defs>
            <rect width="100%" height="100%" fill="url(#map-grid)" />
            {view.days.map((day, dayIndex) => day.routes.map((route, routeIndex) => {
              const points = route[mode].geometry
              if (points.length < 2) return null
              const color = DAY_COLORS[dayIndex % DAY_COLORS.length]
              return (
                <g key={`${day.label}-${routeIndex}`}>
                  <polyline
                    points={points.map((point) => svgPoint(point.longitude, point.latitude)).join(' ')}
                    fill="none"
                    stroke={color}
                    strokeWidth="6"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeDasharray={mode === 'walking' ? '3 10' : undefined}
                  />
                  {points.filter((_point, index) => index === 0 || index === points.length - 1).map((point, pointIndex) => {
                    const [x, y] = svgPoint(point.longitude, point.latitude).split(',')
                    return <circle key={pointIndex} cx={x} cy={y} r="7" fill="white" stroke={color} strokeWidth="4" />
                  })}
                </g>
              )
            }))}
          </svg>
        ) : (
          <div className="flex h-64 items-center justify-center rounded-2xl border border-dashed border-slate-300 bg-white/60 px-8 text-center text-sm leading-6 text-slate-500">
            {view.status === 'PREPARING' ? '路线正在后台准备，卡片可以先查看和调整。' : '地图线条暂不可用，下面的路线摘要仍然有效。'}
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
                <div key={`${route.from_name}-${route.to_name}-${routeIndex}`} className="flex items-center justify-between gap-3 rounded-xl bg-slate-50 px-3 py-2.5 text-xs">
                  <span className="min-w-0 truncate text-slate-600">{route.from_name} → {route.to_name}</span>
                  <span className="shrink-0 font-medium text-slate-700">
                    {route[mode].status === 'AVAILABLE' ? `${route[mode].duration_minutes} 分钟` : '暂不可用'}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
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
        {view.candidates.map((candidate, index) => (
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
            <p className="mt-3 text-xs leading-5 text-slate-500">{candidate.reason}</p>
            {candidate.evidence_gap && <p className="mt-2 text-xs leading-5 text-amber-700">{candidate.evidence_gap}</p>}
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
