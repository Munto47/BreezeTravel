import { useRef, useState } from 'react'
import { Button, Input, ScrollView, Text, View } from '@tarojs/components'
import { useDidHide, useDidShow, useRouter } from '@tarojs/taro'

import type {
  ActivityCardView,
  AssumptionChipView,
  MapRenderView,
  PublicChangePreview,
  PublicTripChecksView,
  StaySuggestionView,
  TripUnderstandingProgressView,
  TripUnderstandingCommand,
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

type EditorState =
  | { mode: 'ASSUMPTION'; assumption: AssumptionChipView }
  | { mode: 'INSERT'; dayIndex: number; position: number }
  | { mode: 'EDIT' | 'REPLACE' | 'MOVE'; dayIndex: number; activity: ActivityCardView }

function findingBadgeClass(label: string): string {
  if (label === '必须调整') return 'failed'
  if (label === '可以更好') return 'waiting'
  return 'needs_confirmation'
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
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [draftName, setDraftName] = useState('')
  const [draftCategory, setDraftCategory] = useState('地点')
  const [draftAddress, setDraftAddress] = useState('地点待确认')
  const [draftTime, setDraftTime] = useState('')
  const [draftDay, setDraftDay] = useState('1')
  const [draftPosition, setDraftPosition] = useState('0')
  const [expandedActivity, setExpandedActivity] = useState('')
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
    } catch {
      if (active.current) setError('暂时无法读取行程，请稍后重试。')
    }
  }

  const openEditor = (next: EditorState) => {
    setEditor(next)
    if (next.mode === 'ASSUMPTION') {
      setDraftName(next.assumption.value)
    } else if (next.mode === 'INSERT') {
      setDraftName('')
      setDraftCategory('地点')
      setDraftAddress('地点待确认')
      setDraftTime('')
      setDraftDay(String(next.dayIndex))
      setDraftPosition(String(next.position))
    } else {
      setDraftName(next.activity.name)
      setDraftCategory(next.activity.category || '地点')
      setDraftAddress(next.activity.area_or_address || '地点待确认')
      setDraftTime(next.activity.time_hint || '')
      setDraftDay(String(next.dayIndex))
      setDraftPosition('1')
    }
    setError('')
  }

  const applyCommand = async (command: TripUnderstandingCommand, scope: string, failureMessage: string) => {
    if (!etag.current) return false
    setBusy(scope)
    setError('')
    try {
      const response = await tripCheckClient.applyTripUnderstandingCommand(
        publicResourceId,
        command,
        etag.current,
        commandRegistry.acquire(scope, command),
      )
      commandRegistry.complete(scope)
      etag.current = response.headers.etag || response.headers.ETag || etag.current
      checksPrepared.current = false
      setChecks(null)
      setChangePreview(null)
      setEditor(null)
      await load()
      return true
    } catch {
      setError(failureMessage)
      return false
    } finally {
      setBusy('')
    }
  }

  const submitEditor = async () => {
    if (!editor) return
    const value = draftName.trim()
    if (editor.mode !== 'MOVE' && !value) {
      setError('请填写内容后再保存。')
      return
    }
    if (editor.mode === 'ASSUMPTION') {
      await applyCommand(
        { command_type: 'ASSUMPTION_SET', key: editor.assumption.key, value },
        `assumption:${editor.assumption.key}`,
        '这项假设暂时无法修改。',
      )
    } else if (editor.mode === 'INSERT') {
      await applyCommand(
        {
          command_type: 'ACTIVITY_INSERT',
          day_index: editor.dayIndex,
          position: editor.position,
          name: value,
          category: draftCategory.trim() || '地点',
          area_or_address: draftAddress.trim() || '地点待确认',
          time_hint: draftTime.trim() || null,
        },
        `insert:${editor.dayIndex}:${editor.position}`,
        '暂时无法新增地点。',
      )
    } else if (editor.mode === 'EDIT') {
      await applyCommand(
        {
          command_type: 'ACTIVITY_TEXT_EDIT',
          activity_token: editor.activity.activity_token,
          name: value,
          time_hint: draftTime.trim() || null,
        },
        `edit:${editor.activity.activity_token}`,
        '暂时无法保存卡片文字。',
      )
    } else if (editor.mode === 'REPLACE') {
      await applyCommand(
        {
          command_type: 'PLACE_REPLACE',
          activity_token: editor.activity.activity_token,
          replacement: {
            name: value,
            category: draftCategory.trim() || '地点',
            area_or_address: draftAddress.trim() || '地点待确认',
          },
        },
        `replace:${editor.activity.activity_token}`,
        '暂时无法替换地点。',
      )
    } else {
      const targetDay = Number.parseInt(draftDay, 10)
      const displayedPosition = Number.parseInt(draftPosition, 10)
      if (!Number.isInteger(targetDay) || targetDay < 1 || !Number.isInteger(displayedPosition) || displayedPosition < 1) {
        setError('请填写有效的目标天数和位置。')
        return
      }
      await applyCommand(
        {
          command_type: 'ACTIVITY_MOVE',
          activity_token: editor.activity.activity_token,
          target_day_index: targetDay,
          target_position: displayedPosition - 1,
        },
        `move:${editor.activity.activity_token}`,
        '暂时无法移动地点。',
      )
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
    const scope = `delete:${activityToken}`
    await applyCommand(
      { command_type: 'ACTIVITY_DELETE', activity_token: activityToken },
      scope,
      '这张卡片暂时无法删除。',
    )
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
    } catch {
      setError('路线暂时无法更新，请稍后再试。')
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
    } catch {
      setError('住宿暂时无法选择，请稍后再试。')
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
    } catch {
      setError('暂时无法预览这项调整。')
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
    } catch {
      setError('暂时无法采纳这项调整。')
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
      {error ? <Text className='feedback'>{error}</Text> : null}

      <View className='card section'>
        <Text className='section-title'>当前假设</Text>
        {result.assumptions.map(item => (
          <View className='assumption-row' key={item.key}>
            <View className='assumption-copy'>
              <Text className='label compact-label'>{item.label}</Text>
              <Text className='copy compact-copy'>{item.value}</Text>
            </View>
            {item.editable ? <Button className='text-action' disabled={Boolean(busy)} onClick={() => openEditor({ mode: 'ASSUMPTION', assumption: item })}>修改</Button> : null}
          </View>
        ))}
      </View>

      {result.days.map((day, dayOffset) => (
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
              {expandedActivity === activity.activity_token ? (
                <View className='details'>
                  <Text className='copy compact-copy'>类别：{activity.category || '地点待确认'}</Text>
                  <Text className='copy compact-copy'>区域或地址：{activity.area_or_address || '地点待确认'}</Text>
                  <Text className='copy compact-copy'>时间：{activity.time_hint || '未指定'}</Text>
                  {activity.knowledge_suggestions?.map((suggestion, index) => (
                    <Text className='suggestion' key={`${activity.activity_token}-suggestion-${index}`}>{suggestion.text} · {suggestion.source_name}</Text>
                  ))}
                </View>
              ) : null}
              <View className='action-grid'>
                {activity.available_actions.includes('VIEW_DETAILS') ? <Button className='text-action' disabled={Boolean(busy)} onClick={() => setExpandedActivity(expandedActivity === activity.activity_token ? '' : activity.activity_token)}>{expandedActivity === activity.activity_token ? '收起详情' : '查看详情'}</Button> : null}
                <Button className='text-action' disabled={Boolean(busy)} onClick={() => openEditor({ mode: 'EDIT', dayIndex: dayOffset + 1, activity })}>编辑文字</Button>
                {activity.available_actions.includes('REPLACE') ? <Button className='text-action' disabled={Boolean(busy)} onClick={() => openEditor({ mode: 'REPLACE', dayIndex: dayOffset + 1, activity })}>替换地点</Button> : null}
                {activity.available_actions.includes('MOVE') ? <Button className='text-action' disabled={Boolean(busy)} onClick={() => openEditor({ mode: 'MOVE', dayIndex: dayOffset + 1, activity })}>移动位置</Button> : null}
                {activity.available_actions.includes('DELETE') ? <Button className='text-action danger-action' disabled={Boolean(busy)} loading={busy === `delete:${activity.activity_token}`} onClick={() => void removeActivity(activity.activity_token)}>删除卡片</Button> : null}
              </View>
            </View>
          ))}
          <Button className='secondary compact' disabled={Boolean(busy)} onClick={() => openEditor({ mode: 'INSERT', dayIndex: dayOffset + 1, position: day.activities.length })}>新增地点</Button>
        </View>
      ))}

      {editor ? (
        <View className='card section editor-panel'>
          <View className='section-head'>
            <Text className='section-title'>{{ ASSUMPTION: `修改${editor.mode === 'ASSUMPTION' ? editor.assumption.label : ''}`, INSERT: '新增地点', EDIT: '编辑卡片文字', REPLACE: '替换地点', MOVE: '移动位置' }[editor.mode]}</Text>
            <Button className='text-action' disabled={Boolean(busy)} onClick={() => setEditor(null)}>关闭</Button>
          </View>
          {editor.mode === 'MOVE' ? (
            <>
              <Text className='label'>目标天数</Text>
              <Input className='field' type='number' value={draftDay} onInput={event => setDraftDay(event.detail.value)} />
              <Text className='label'>排在第几个（1 表示最前）</Text>
              <Input className='field' type='number' value={draftPosition} onInput={event => setDraftPosition(event.detail.value)} />
            </>
          ) : (
            <>
              <Text className='label'>{editor.mode === 'ASSUMPTION' ? '新的值' : '地点名称'}</Text>
              <Input className='field' maxlength={editor.mode === 'ASSUMPTION' ? 80 : 40} value={draftName} onInput={event => setDraftName(event.detail.value)} />
              {editor.mode === 'INSERT' || editor.mode === 'REPLACE' ? (
                <>
                  <Text className='label'>类别</Text>
                  <Input className='field' maxlength={40} value={draftCategory} onInput={event => setDraftCategory(event.detail.value)} />
                  <Text className='label'>区域或地址</Text>
                  <Input className='field' maxlength={120} value={draftAddress} onInput={event => setDraftAddress(event.detail.value)} />
                </>
              ) : null}
              {editor.mode === 'INSERT' || editor.mode === 'EDIT' ? (
                <>
                  <Text className='label'>时间提示（可选）</Text>
                  <Input className='field' maxlength={80} value={draftTime} onInput={event => setDraftTime(event.detail.value)} />
                </>
              ) : null}
            </>
          )}
          <Button className='primary' disabled={Boolean(busy)} loading={busy.startsWith(editor.mode.toLowerCase())} onClick={() => void submitEditor()}>保存修改</Button>
        </View>
      ) : null}

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
              <Text className={`badge ${findingBadgeClass(item.label)}`}>{item.label}</Text>
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
