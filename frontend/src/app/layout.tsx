import type { Metadata } from 'next'
import { Inter } from 'next/font/google'
import './globals.css'

const inter = Inter({
  subsets: ['latin'],
  display: 'swap',
  variable: '--font-inter',
})

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
    <html lang="zh-CN" className={inter.variable} suppressHydrationWarning>
      <body className={`${inter.className} bg-gray-100`} suppressHydrationWarning>{children}</body>
    </html>
  )
}
