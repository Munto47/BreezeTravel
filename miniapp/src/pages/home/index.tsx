import { useMemo, useState } from 'react'
import { Button, Picker, Text, Textarea, View } from '@tarojs/components'
import Taro, { useDidShow } from '@tarojs/taro'
import { nanoid } from 'nanoid/non-secure'

import { tripCheckClient } from '@/lib/api'
import { commandRegistry } from '@/lib/commands'
import { uploadScreenshotBatch } from '@/lib/screenshot-flow'
import { clearSession, readLastWorkspaceId, readSession, saveLastWorkspaceId } from '@/lib/storage'

import './index.scss'

const CITIES = ['北京', '上海', '杭州']
const DAY_OPTIONS = [2, 3, 4, 5]

function dateAfter(offset: number): string {
  const date = new Date()
  date.setDate(date.getDate() + offset)
  return date.toISOString().slice(0, 10)
}

function endDate(start: string, days: number): string {
  const date = new Date(`${start}T00:00:00Z`)
  date.setUTCDate(date.getUTCDate() + days - 1)
  return date.toISOString().slice(0, 10)
}

export default function HomePage() {
  const [cityIndex, setCityIndex] = useState(0)
  const [dayIndex, setDayIndex] = useState(1)
  const [startDate, setStartDate] = useState(dateAfter(7))
  const [mode, setMode] = useState<'TEXT' | 'SCREENSHOT'>('TEXT')
  const [rawText, setRawText] = useState('')
  const [files, setFiles] = useState<Array<{ path: string; size: number }>>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const session = readSession()
  const city = CITIES[cityIndex]
  const days = DAY_OPTIONS[dayIndex]
  const canSubmit = useMemo(() => mode === 'TEXT' ? Boolean(rawText.trim()) : files.length > 0, [files.length, mode, rawText])

  useDidShow(() => {
    if (!readSession()) void Taro.reLaunch({ url: '/pages/login/index' })
  })

  const chooseScreenshots = async () => {
    const result = await Taro.chooseMedia({ count: 6, mediaType: ['image'], sourceType: ['album', 'camera'] })
    const selected = result.tempFiles.map(item => ({ path: item.tempFilePath, size: item.size }))
    if (selected.some(item => item.size > 10 * 1024 * 1024)) {
      setError('每张截图不能超过 10MB')
      return
    }
    setFiles(selected)
    setError('')
  }

  const createTrip = async () => {
    if (!session || !canSubmit) return
    setBusy(true)
    setError('')
    try {
      const roomId = `mp-${nanoid(14)}`
      const threadId = `mp-thread-${nanoid(14)}`
      await tripCheckClient.createRoom({ room_id: roomId, thread_id: threadId, trip_city: city, trip_days: days, nickname: session.nickname })
      const workspace = await tripCheckClient.createWorkspace({
        room_id: roomId,
        city,
        trip_date_range: { start: startDate, end: endDate(startDate, days) },
      })
      saveLastWorkspaceId(workspace.workspace_id)
      if (mode === 'TEXT') {
        const body = { source_type: 'AI_TEXT' as const, raw_text: rawText.trim() }
        const scope = `create-import:${workspace.workspace_id}`
        await tripCheckClient.createTextImport(workspace.workspace_id, body, commandRegistry.acquire(scope, body))
        commandRegistry.complete(scope)
      } else {
        await uploadScreenshotBatch(tripCheckClient, commandRegistry, workspace.workspace_id, files)
      }
      await Taro.navigateTo({ url: `/pages/trip/index?workspaceId=${workspace.workspace_id}` })
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  const restore = async () => {
    const workspaceId = readLastWorkspaceId()
    if (workspaceId) await Taro.navigateTo({ url: `/pages/trip/index?workspaceId=${workspaceId}` })
  }

  const logout = async () => {
    clearSession()
    await Taro.reLaunch({ url: '/pages/login/index' })
  }

  return (
    <View className='page home-page'>
      <View className='topline'>
        <View><Text className='eyebrow'>BreezeTravel</Text><Text className='heading'>创建一次行程查</Text></View>
        <Text className='link' onClick={logout}>退出</Text>
      </View>
      <View className='card form-card'>
        <Text className='label'>城市</Text>
        <Picker mode='selector' range={CITIES} value={cityIndex} onChange={event => setCityIndex(Number(event.detail.value))}>
          <View className='field'>{city}</View>
        </Picker>
        <View className='row'>
          <View className='grow'>
            <Text className='label'>天数</Text>
            <Picker mode='selector' range={DAY_OPTIONS} value={dayIndex} onChange={event => setDayIndex(Number(event.detail.value))}>
              <View className='field'>{days} 天</View>
            </Picker>
          </View>
          <View className='grow'>
            <Text className='label'>出发日期</Text>
            <Picker mode='date' value={startDate} onChange={event => setStartDate(String(event.detail.value))}>
              <View className='field'>{startDate}</View>
            </Picker>
          </View>
        </View>
        <View className='segmented'>
          <View className={mode === 'TEXT' ? 'segment active' : 'segment'} onClick={() => setMode('TEXT')}>粘贴文本</View>
          <View className={mode === 'SCREENSHOT' ? 'segment active' : 'segment'} onClick={() => setMode('SCREENSHOT')}>上传截图</View>
        </View>
        {mode === 'TEXT' ? (
          <Textarea className='textarea' maxlength={12000} value={rawText} placeholder='例如：第1天 09:00 颐和园…' onInput={event => setRawText(event.detail.value)} />
        ) : (
          <View className='upload-box' onClick={chooseScreenshots}>
            <Text>{files.length ? `已选择 ${files.length} 张，点击重新选择` : '选择 1～6 张 PNG/JPEG/WebP 截图'}</Text>
            <Text className='hint'>逐张顺序上传，整批齐全后才进入 OCR</Text>
          </View>
        )}
        {error ? <Text className='error'>{error}</Text> : null}
        <Button className='primary' disabled={!canSubmit || busy} loading={busy} onClick={createTrip}>
          {busy ? '正在创建…' : '导入并继续'}
        </Button>
      </View>
      {readLastWorkspaceId() ? <Button className='secondary' onClick={restore}>恢复最近一次行程查</Button> : null}
      <Text className='privacy'>权威状态始终从后端恢复；原始截图在成功、失败、取消或过期后删除。</Text>
    </View>
  )
}
