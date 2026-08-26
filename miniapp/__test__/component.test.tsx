import React from 'react'
import { render } from '@tarojs/test-utils-react/dist/pure'

jest.mock('@tarojs/components', () => {
  const runtime = require('react')
  return {
    View: ({ children, className }: { children: React.ReactNode; className?: string }) => runtime.createElement('div', { className }, children),
    Text: ({ children }: { children: React.ReactNode }) => runtime.createElement('span', null, children),
  }
})

import WorkflowNotice from '@/components/WorkflowNotice'

test('Taro test utils render workflow feedback', () => {
  const consoleError = jest.spyOn(console, 'error').mockImplementation(() => undefined)
  const rendered = render(
    <WorkflowNotice title='PARTIAL' detail='天气字段不可用，已保留其他事实' tone='warning' />,
    {},
  )
  expect(rendered.container.textContent).toContain('PARTIAL')
  expect(rendered.container.textContent).toContain('天气字段不可用')
  rendered.unmount()
  consoleError.mockRestore()
})
