import { useState } from 'react'
import { Button, Text, View } from '@tarojs/components'
import Taro, { useDidShow } from '@tarojs/taro'

import { tripCheckClient } from '@/lib/api'
import { readSession, saveSession } from '@/lib/storage'

import './index.scss'

export default function LoginPage() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useDidShow(() => {
    if (readSession()) void Taro.reLaunch({ url: '/pages/home/index' })
  })

  const login = async () => {
    setBusy(true)
    setError('')
    try {
      const loginResult = await Taro.login({ timeout: 10_000 })
      if (!loginResult.code) throw new Error('微信没有返回登录凭证')
      const session = await tripCheckClient.loginWithWechat(loginResult.code)
      saveSession(session)
      await Taro.reLaunch({ url: '/pages/home/index' })
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <View className='page login-page'>
      <View className='brand-mark'>B</View>
      <Text className='title'>BreezeTravel</Text>
      <Text className='subtitle'>导入行程 · 事实核验 · 有依据的调整建议</Text>
      {error ? <Text className='error'>{error}</Text> : null}
      <Button className='primary' loading={busy} disabled={busy} onClick={login}>
        {busy ? '正在登录…' : '微信登录'}
      </Button>
      <Text className='disclosure'>仅使用微信身份创建独立账号，不自动合并 Web 账号。</Text>
    </View>
  )
}
