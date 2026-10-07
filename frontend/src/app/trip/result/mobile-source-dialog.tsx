'use client'

import {useEffect, useState} from 'react'
import {readTripSource, type TripSourceView} from '@/lib/trip-understanding-v3'
import AccessibleDialog from './accessible-dialog'

export default function MobileSourceDialog({resource, onClose}: {resource: string; onClose: () => void}) {
  const [source, setSource] = useState<TripSourceView | null>(null)
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setFailed(false)
    setSource(null)
    void readTripSource(resource, controller.signal).then(value => {
      if (!controller.signal.aborted) setSource(value)
    }).catch(() => {if (!controller.signal.aborted) setFailed(true)})
    return () => controller.abort()
  }, [resource, attempt])
  return <AccessibleDialog titleId="mobile-source-title" onClose={onClose}>
    <header className="mobile-actions-heading"><h2 id="mobile-source-title">行程原文</h2><button type="button" className="e-button" onClick={onClose}>关闭</button></header>
    {failed || source?.status === 'UNAVAILABLE' ? <div role="alert"><p>原文暂时无法读取。</p><button type="button" className="e-button" onClick={() => setAttempt(value => value + 1)}>重试读取原文</button></div>
      : source?.status === 'DELETED' ? <p>原文已删除。</p>
      : source?.text ? <pre className="whitespace-pre-wrap break-words py-4 font-sans text-sm leading-7">{source.text}</pre>
      : <p role="status">{source ? '暂无可查看的原文。' : '正在读取原文…'}</p>}
  </AccessibleDialog>
}
