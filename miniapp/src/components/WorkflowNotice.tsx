import React from 'react'
import { Text, View } from '@tarojs/components'

export interface WorkflowNoticeProps {
  title: string
  detail: string
  tone?: 'info' | 'warning' | 'success'
}

export default function WorkflowNotice({ title, detail, tone = 'info' }: WorkflowNoticeProps) {
  return (
    <View className={`workflow-notice ${tone}`}>
      <Text>{title}</Text>
      <Text>{detail}</Text>
    </View>
  )
}
