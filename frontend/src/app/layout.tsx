import type { Metadata } from 'next'
import './globals.css'
import ToastContainer from '@/components/ui/ToastContainer'

export const metadata: Metadata = {
  title: 'BreezeTravel — AI 智能旅行协同规划',
  description: '基于 LangGraph 多 Agent + Yjs 实时协同的旅行规划工具',
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
