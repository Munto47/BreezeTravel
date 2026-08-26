'use client'

import { AnimatePresence, motion } from 'framer-motion'
import { X, AlertCircle, CheckCircle, AlertTriangle, Info } from 'lucide-react'
import { useToastStore } from '@/stores/toastStore'

const ICONS = {
  error: <AlertCircle className="w-4 h-4 shrink-0 text-red-500" />,
  warning: <AlertTriangle className="w-4 h-4 shrink-0 text-amber-500" />,
  success: <CheckCircle className="w-4 h-4 shrink-0 text-emerald-500" />,
  info: <Info className="w-4 h-4 shrink-0 text-blue-500" />,
}

const BG = {
  error: 'border-red-100 bg-red-50/95',
  warning: 'border-amber-100 bg-amber-50/95',
  success: 'border-emerald-100 bg-emerald-50/95',
  info: 'border-blue-100 bg-blue-50/95',
}

export default function ToastContainer() {
  const { toasts, dismiss } = useToastStore()

  return (
    <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-[9999] flex flex-col gap-2 items-center pointer-events-none">
      <AnimatePresence>
        {toasts.map(t => (
          <motion.div
            key={t.id}
            initial={{ opacity: 0, y: 16, scale: 0.95 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.95 }}
            transition={{ duration: 0.2 }}
            className={`pointer-events-auto flex items-start gap-2.5 px-4 py-3 rounded-2xl border shadow-lg backdrop-blur-sm max-w-sm text-sm text-gray-700 ${BG[t.type]}`}
          >
            {ICONS[t.type]}
            <span className="flex-1 leading-snug">{t.message}</span>
            <button
              onClick={() => dismiss(t.id)}
              className="shrink-0 p-0.5 rounded-lg hover:bg-black/10 transition-colors mt-0.5"
            >
              <X className="w-3.5 h-3.5 text-gray-400" />
            </button>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  )
}
