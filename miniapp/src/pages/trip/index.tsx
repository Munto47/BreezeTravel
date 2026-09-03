import { useRef, useState } from 'react'
import { Button, ScrollView, Text, View } from '@tarojs/components'
import { useDidHide, useDidShow, useRouter } from '@tarojs/taro'

import type {
  MapRenderView,
  PublicChangePreview,
  PublicTripChecksView,
  StaySuggestionView,
  TripUnderstandingProgressView,
  UserFacingTripResult,
} from '@breezetravel/trip-check-client'

import { tripCheckClient } from '@/lib/api'
import { commandRegistry } from '@/lib/commands'

import './index.scss'

function statusLabel(status: string): string {
  return {
    PREPARING: '准备中',
    AVAILABLE: '已准备',
    NEEDS_UPDATE: '需要更新',
    LIMITED: '信息有限',
    UNAVAILABLE: '暂不可用',
  }[status] || '处理中'
}

export default function TripPage() {
  const router = useRouter()
  const publicResourceId = String(router.params.publicResourceId || '')
  const [progress, setProgress] = useState('正在整理攻略…')
  const [result, setResult] = useState<UserFacingTripResult | null>(null)
  const [map, setMap] = useState<MapRenderView | null>(null)
  const [stay, setStay] = useState<StaySuggestionView | null>(null)
  const [checks, setChecks] = useState<PublicTripChecksView | null>(null)
  const [changePreview, setChangePreview] = useState<PublicChangePreview | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const active = useRef(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const etag = useRef('')
  const checksPrepared = useRef(false)

  const clearTimer = () => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = null
  }

  const loadEnhancements = async (): Promise<{
    map: MapRenderView | null
    stay: StaySuggestionView | null
  }> => {
    const [nextMap, nextStay] = await Promise.allSettled([
      tripCheckClient.getTripUnderstandingMap(publicResourceId),
      tripCheckClient.getTripUnderstandingStaySuggestions(publicResourceId),
    ])
    const loadedMap = nextMap.status === 'fulfilled' ? nextMap.value : null
    const loadedStay = nextStay.status === 'fulfilled' ? nextStay.value : null
    if (active.current) {
      if (loadedMap) setMap(loadedMap)
      if (loadedStay) setStay(loadedStay)
    }
    return { map: loadedMap, stay: loadedStay }
  }

  const prepareChecks = async () => {
    if (checksPrepared.current || !etag.current) return
    checksPrepared.current = true
    try {
      const prepared = await tripCheckClient.materializeTripUnderstanding(
        publicResourceId,
        etag.current,
        commandRegistry.acquire(`materialize:${publicResourceId}`, {}),
      )
      commandRegistry.complete(`materialize:${publicResourceId}`)
      etag.current = prepared.headers.etag || prepared.headers.ETag || etag.current
      const value = await tripCheckClient.getTripUnderstandingChecks(publicResourceId)
      if (active.current) setChecks(value)
    } catch {
      checksPrepared.current = false
    }
  }

  const load = async () => {
    clearTimer()
    if (!publicResourceId) {
      setError('这次行程链接不完整，请返回首页重新生成。')
      return
    }
    try {
      const response = await tripCheckClient.getTripUnderstandingResult(publicResourceId)
      if (!active.current) return
      if (response.status === 202) {
        const value = response.data as TripUnderstandingProgressView
        setProgress(value.message)
        timer.current = setTimeout(() => void load(), Math.max(500, value.retry_after_ms))
        return
      }
      etag.current = response.headers.etag || response.headers.ETag || ''
      const loadedResult = response.data as UserFacingTripResult
      setResult(loadedResult)
      setProgress('')
      const enhancements = await loadEnhancements()
      const mapStatus = enhancements.map?.status || loadedResult.map.status
      const stayStatus = enhancements.stay?.status || loadedResult.stay.status
      if (mapStatus === 'PREPARING' || stayStatus === 'PREPARING') {
        timer.current = setTimeout(() => void load(), 1000)
        return
      }
      await prepareChecks()
    } catch (caught) {
      if (active.current) setError(caught instanceof Error ? caught.message : '暂时无法读取行程，请稍后重试。')
    }
  }

  useDidShow(() => {
    active.current = true
    void load()
  })

  useDidHide(() => {
    active.current = false
    clearTimer()
  })

  const removeActivity = async (activityToken: string) => {
    if (!etag.current) return
    const scope = `delete:${activityToken}`
    setBusy(scope)
    setError('')
    try {
      await tripCheckClient.applyTripUnderstandingCommand(
        publicResourceId,
        { command_type: 'ACTIVITY_DELETE', activity_token: activityToken },
        etag.current,
        commandRegistry.acquire(scope, { activityToken }),
      )
      commandRegistry.complete(scope)
      checksPrepared.current = false
      setChecks(null)
      setChangePreview(null)
      await load()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '这张卡片暂时无法删除。')
    } finally {
      setBusy('')
    }
  }

  const refreshMap = async () => {
    if (!etag.current) return
    const scope = `map:${publicResourceId}`
    setBusy(scope)
    setError('')
    try {
      const value = await tripCheckClient.requestTripUnderstandingMap(
        publicResourceId,
        etag.current,
        commandRegistry.acquire(scope, {}),
      )
      commandRegistry.complete(scope)
      setMap({ status: value.status, message: value.message, days: [], available_actions: [] })
      timer.current = setTimeout(() => void loadEnhancements(), 1000)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '路线暂时无法更新。')
    } finally {
      setBusy('')
    }
  }

  const selectStay = async (candidateToken: string) => {
    if (!etag.current) return
    const scope = `stay:${candidateToken}`
    setBusy(scope)
    setError('')
    try {
      const response = await tripCheckClient.selectTripUnderstandingStay(
        publicResourceId,
        candidateToken,
        etag.current,
        commandRegistry.acquire(scope, { candidateToken }),
      )
      commandRegistry.complete(scope)
      etag.current = response.headers.etag || response.headers.ETag || etag.current
      checksPrepared.current = false
      setChecks(null)
      setChangePreview(null)
      await loadEnhancements()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '住宿暂时无法选择。')
    } finally {
      setBusy('')
    }
  }

  const previewChange = async (checkToken: string) => {
    const scope = `preview:${checkToken}`
    setBusy(scope)
    setError('')
    try {
      const value = await tripCheckClient.previewTripUnderstandingChange(
        publicResourceId,
        checkToken,
        commandRegistry.acquire(scope, { checkToken }),
      )
      commandRegistry.complete(scope)
      setChangePreview(value)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法预览这项调整。')
    } finally {
      setBusy('')
    }
  }

  const adoptChange = async () => {
    if (!changePreview || !etag.current) return
    const scope = `adopt:${changePreview.change_token}`
    setBusy(scope)
    setError('')
    try {
      const response = await tripCheckClient.adoptTripUnderstandingChange(
        publicResourceId,
        changePreview.change_token,
        etag.current,
        commandRegistry.acquire(scope, { changeToken: changePreview.change_token }),
      )
      commandRegistry.complete(scope)
      etag.current = response.headers.etag || response.headers.ETag || etag.current
      setChecks(response.data.checks)
      setChangePreview(null)
      checksPrepared.current = true
      await load()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法采纳这项调整。')
    } finally {
      setBusy('')
    }
  }

  if (!result) {
    return <View className='loading'><Text>{error || progress || '正在读取每日行程…'}</Text></View>
  }

  return (
    <ScrollView scrollY className='trip-page'>
      <View className='hero'>
        <Text className='eyebrow'>BreezeTravel · 行程查</Text>
        <Text className='heading'>你的每日行程</Text>
        <Text className='subtle'>地点不确定时会明确标成“需要确认”，不会硬猜。</Text>
      </View>
      {error ? <Text className='error'>{error}</Text> : null}

      <View className='card section'>
        <Text className='section-title'>当前假设</Text>
        <Text className='copy'>{result.assumptions.map(item => `${item.label}：${item.value}`).join(' · ')}</Text>
      </View>

      {result.days.map(day => (
        <View className='card section' key={day.label}>
          <View className='section-head'>
            <Text className='section-title'>{day.label}</Text>
            <Text className='badge'>{day.activities.length} 个地点</Text>
          </View>
          {day.activities.length === 0 ? <Text className='copy'>这一天还没有卡片。</Text> : null}
          {day.activities.map(activity => (
            <View className='resolution' key={activity.activity_token}>
              <View className='section-head'>
                <Text className='resolution-name'>{activity.name}</Text>
                <Text className={`badge ${activity.status.toLowerCase()}`}>
                  {activity.status === 'READY' ? '可查看' : '需要确认'}
                </Text>
              </View>
              <Text className='copy'>{[activity.time_hint, activity.area_or_address].filter(Boolean).join(' · ')}</Text>
              {activity.available_actions.includes('DELETE') ? (
                <Button className='secondary compact' disabled={Boolean(busy)} loading={busy === `delete:${activity.activity_token}`} onClick={() => void removeActivity(activity.activity_token)}>
                  删除这张卡片
                </Button>
              ) : null}
            </View>
          ))}
        </View>
      ))}

      <View className='card section'>
        <View className='section-head'>
          <Text className='section-title'>路线</Text>
          <Text className='badge'>{statusLabel(map?.status || result.map.status)}</Text>
        </View>
        <Text className='copy'>{map?.message || result.map.message}</Text>
        {map?.days.flatMap(day => day.routes).map((route, index) => (
          <Text className='copy' key={`${route.from_name}-${route.to_name}-${index}`}>
            {route.from_name} → {route.to_name} · {route.message}
          </Text>
        ))}
        {(map?.status || result.map.status) === 'NEEDS_UPDATE' ? (
          <Button className='primary' disabled={Boolean(busy)} loading={busy.startsWith('map:')} onClick={() => void refreshMap()}>
            重新准备路线
          </Button>
        ) : null}
      </View>

      <View className='card section'>
        <View className='section-head'>
          <Text className='section-title'>住宿建议</Text>
          <Text className='badge'>{statusLabel(stay?.status || result.stay.status)}</Text>
        </View>
        <Text className='copy'>{stay?.message || result.stay.message}</Text>
        {(stay?.candidates || result.stay.candidates).map(candidate => (
          <View className='advice' key={candidate.candidate_token}>
            <Text className='resolution-name'>{candidate.name}</Text>
            <Text className='copy'>{candidate.commute_summary} · {candidate.reason}</Text>
            {!candidate.selected ? (
              <Button className='secondary compact' disabled={Boolean(busy)} loading={busy === `stay:${candidate.candidate_token}`} onClick={() => void selectStay(candidate.candidate_token)}>
                选择这家住宿
              </Button>
            ) : <Text className='success'>已选择</Text>}
          </View>
        ))}
      </View>

      <View className='card section'>
        <Text className='section-title'>优先看看这三项</Text>
        <Text className='copy'>{checks?.message || '正在准备少量、可直接采纳的建议。'}</Text>
        {checks?.items.map(item => (
          <View className='finding' key={item.check_token}>
            <View className='section-head'>
              <Text className='finding-title'>{item.title}</Text>
              <Text className={`badge ${item.label === '必须调整' ? 'failed' : item.label === '可以更好' ? 'waiting' : ''}`}>{item.label}</Text>
            </View>
            <Text className='copy'>{item.message}</Text>
            {item.can_preview ? (
              <Button className='secondary compact' disabled={Boolean(busy)} loading={busy === `preview:${item.check_token}`} onClick={() => void previewChange(item.check_token)}>
                预览调整
              </Button>
            ) : null}
          </View>
        ))}
        {changePreview ? (
          <View className='repair'>
            <Text className='finding-title'>{changePreview.title}</Text>
            <Text className='copy'>{changePreview.summary}</Text>
            {changePreview.before.map((item, index) => <Text className='subtle' key={`before-${index}`}>调整前：{item}</Text>)}
            {changePreview.after.map((item, index) => <Text className='copy' key={`after-${index}`}>调整后：{item}</Text>)}
            <View className='button-row'>
              <Button className='primary half' disabled={Boolean(busy)} loading={busy.startsWith('adopt:')} onClick={() => void adoptChange()}>采纳调整</Button>
              <Button className='secondary half' disabled={Boolean(busy)} onClick={() => setChangePreview(null)}>暂不调整</Button>
            </View>
          </View>
        ) : null}
      </View>
      <View className='bottom-space' />
    </ScrollView>
  )
}
