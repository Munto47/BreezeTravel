import { useMemo, useState } from 'react'
import { Button, Text, Textarea, View } from '@tarojs/components'
import Taro, { useDidShow } from '@tarojs/taro'

import { tripCheckClient } from '@/lib/api'
import { commandRegistry } from '@/lib/commands'
import { clearSession, readLastTripResourceId, readSession, saveLastTripResourceId } from '@/lib/storage'

import './index.scss'

export default function HomePage() {
  const [rawText, setRawText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const session = readSession()
  const canSubmit = useMemo(() => Boolean(rawText.trim()), [rawText])

  useDidShow(() => {
    if (!readSession()) void Taro.reLaunch({ url: '/pages/login/index' })
  })

  const createTrip = async () => {
    if (!session || !canSubmit) return
    setBusy(true)
    setError('')
    try {
      const body = { mode: 'FULL', source: { type: 'TEXT', text: rawText.trim() } }
      const scope = 'create-trip-understanding'
      const accepted = await tripCheckClient.createFullTripUnderstanding(
        rawText.trim(),
        commandRegistry.acquire(scope, body),
      )
      commandRegistry.complete(scope)
      saveLastTripResourceId(accepted.public_resource_id)
      await Taro.navigateTo({ url: `/pages/trip/index?publicResourceId=${accepted.public_resource_id}` })
    } catch {
      setError('暂时无法生成行程，请稍后重试。')
    } finally {
      setBusy(false)
    }
  }

  const restore = async () => {
    const publicResourceId = readLastTripResourceId()
    if (publicResourceId) await Taro.navigateTo({ url: `/pages/trip/index?publicResourceId=${publicResourceId}` })
  }

  const logout = async () => {
    clearSession()
    await Taro.reLaunch({ url: '/pages/login/index' })
  }

  return (
    <View className='page home-page'>
      <View className='topline'>
        <View><Text className='eyebrow'>BreezeTravel</Text><Text className='heading'>粘贴攻略，生成每日行程</Text></View>
        <Text className='link' onClick={logout}>退出</Text>
      </View>
      <View className='card form-card'>
        <Text className='label'>攻略或行程文字</Text>
        <Textarea className='textarea' maxlength={50000} value={rawText} placeholder='例如：第1天 09:00 颐和园…' onInput={event => setRawText(event.detail.value)} />
        {error ? <Text className='feedback'>{error}</Text> : null}
        <Button className='primary' disabled={!canSubmit || busy} loading={busy} onClick={createTrip}>
          {busy ? '正在整理…' : '生成每日行程'}
        </Button>
      </View>
      {readLastTripResourceId() ? <Button className='secondary' onClick={restore}>继续最近一次行程</Button> : null}
      <Text className='privacy'>无需先填城市、日期或人数；系统会给出可编辑的软假设。攻略原文不会进入长期记忆。</Text>
    </View>
  )
}
