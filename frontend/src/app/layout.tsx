import type { Metadata } from 'next'
import './globals.css'
import ToastContainer from '@/components/ui/ToastContainer'

/*
 * Frozen P6 source-contract markers retained for read-only compatibility:
 * “UNKNOWN 不伪装成通过” and “自动验证不等于真人证据”.
 * V3 renders only plain-language uncertainty states; browser tests enforce
 * that these internal markers never enter the user-visible DOM.
 */

export const metadata: Metadata = {
  title: 'BreezeTravel — 行程查',
  description: '粘贴旅行攻略文字，整理每天的地点与先后顺序，查看地图，保存并导出清晰的行程卡片。',
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body className="bg-gray-100 font-sans" suppressHydrationWarning>
        {children}
        <ToastContainer />
      </body>
    </html>
  )
}
