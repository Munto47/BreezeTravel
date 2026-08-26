import type { Metadata } from 'next'
import './globals.css'
import ToastContainer from '@/components/ui/ToastContainer'

export const metadata: Metadata = {
  title: 'BreezeTravel — 行程查',
  description: '核验北京、上海或杭州的单城市行程，并给出有依据的风险与调整建议',
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
