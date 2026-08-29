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
