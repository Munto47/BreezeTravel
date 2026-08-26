import { Button, Text, View } from '@tarojs/components'
import Taro from '@tarojs/taro'

import './index.scss'

export default function LoginPage() {
  const preview = () => Taro.showToast({ title: '微信登录将在认证切片接通', icon: 'none' })

  return (
    <View className='page login-page'>
      <View className='brand-mark'>B</View>
      <Text className='title'>BreezeTravel</Text>
      <Text className='subtitle'>导入行程 · 事实核验 · 有依据的调整建议</Text>
      <Button className='primary' onClick={preview}>微信登录</Button>
      <Text className='disclosure'>仅使用微信身份创建独立账号，不自动合并 Web 账号。</Text>
    </View>
  )
}
