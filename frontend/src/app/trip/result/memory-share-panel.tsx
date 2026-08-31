'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { MessageSquareText, Share2, Trash2 } from 'lucide-react'

import {
  type DataConsentView,
  type ShareListItemView,
  createTripShare,
  readDataConsents,
  readMyShares,
  revokeShare,
  submitTripFeedback,
} from '@/lib/trip-understanding-v3'

export default function MemorySharePanel({ resourceRef }: { resourceRef: string }) {
  const [consents, setConsents] = useState<DataConsentView | null>(null)
  const [shares, setShares] = useState<ShareListItemView[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [message, setMessage] = useState('')

  const refresh = async () => {
    const [nextConsents, nextShares] = await Promise.all([readDataConsents(), readMyShares()])
    setConsents(nextConsents)
    setShares(nextShares)
  }

  useEffect(() => {
    void refresh().catch(() => setMessage('分享与反馈设置暂时无法读取。'))
  }, [resourceRef])

  const createShare = async () => {
    if (busy) return
    setBusy('create')
    setMessage('')
    try {
      const created = await createTripShare(resourceRef, 7)
      const link = new URL(created.share_url, window.location.origin).toString()
      await navigator.clipboard.writeText(link)
      setMessage('已创建7天有效的只读链接并复制。链接秘密不会显示在页面上。')
      await refresh()
    } catch {
      setMessage('分享创建失败，请稍后重试。')
    } finally {
      setBusy(null)
    }
  }

  const revoke = async (shareRef: string) => {
    if (busy) return
    setBusy(shareRef)
    setMessage('')
    try {
      await revokeShare(shareRef)
      setMessage('链接已撤销，已打开的分享也不再可访问。')
      await refresh()
    } catch {
      setMessage('撤销失败，请稍后重试。')
    } finally {
      setBusy(null)
    }
  }

  const feedback = async (eventType: 'ADOPTED' | 'REJECTED') => {
    if (busy) return
    setBusy(eventType)
    setMessage('')
    try {
      await submitTripFeedback(resourceRef, eventType)
      setMessage(eventType === 'ADOPTED' ? '已记录“这份行程对我有帮助”。' : '已记录“这份行程需要改进”。')
    } catch {
      setMessage('反馈未能保存，请稍后重试。')
    } finally {
      setBusy(null)
    }
  }

  const activeShares = shares.filter((share) => share.status === 'ACTIVE')
  return (
    <section className="mt-6 rounded-3xl border border-slate-200 bg-white p-5" aria-labelledby="trip-share-title" data-testid="g06-memory-share-panel">
      <div className="flex items-start gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-sky-50 text-sky-700"><Share2 className="h-5 w-5" aria-hidden="true" /></div>
        <div><h2 id="trip-share-title" className="font-semibold">分享与反馈</h2><p className="mt-1 text-xs leading-5 text-slate-500">分享页只含朋友看得懂的行程卡片，不含原文、内部标识或编辑权限。</p></div>
      </div>
      {message ? <p role="status" className="mt-4 rounded-2xl bg-sky-50 px-4 py-3 text-sm text-sky-900">{message}</p> : null}
      <div className="mt-4 flex flex-wrap gap-2">
        <button data-testid="create-readonly-share" type="button" disabled={busy !== null} onClick={() => void createShare()} className="min-h-12 rounded-xl bg-sky-700 px-4 text-sm font-semibold text-white disabled:opacity-50">{busy === 'create' ? '正在创建…' : '复制朋友只读链接'}</button>
      </div>
      {activeShares.length > 0 ? <div className="mt-4 space-y-2"><p className="text-xs font-semibold text-slate-700">已生效链接</p>{activeShares.map((share) => <div key={share.share_ref} className="flex items-center justify-between gap-3 rounded-xl bg-slate-50 px-3 py-2"><p className="text-xs text-slate-600">有效至 {new Date(share.expires_at).toLocaleString('zh-CN')}</p><button data-testid="revoke-share" type="button" disabled={busy !== null} onClick={() => void revoke(share.share_ref)} className="inline-flex min-h-10 items-center gap-1 rounded-lg px-2 text-xs font-semibold text-slate-600 hover:bg-white disabled:opacity-50"><Trash2 className="h-3.5 w-3.5" />撤销</button></div>)}</div> : null}
      <div className="mt-5 border-t border-slate-100 pt-4">
        <div className="flex items-center gap-2"><MessageSquareText className="h-4 w-4 text-slate-500" /><p className="text-xs font-semibold text-slate-700">产品反馈</p></div>
        {consents?.feedback_enabled ? <div className="mt-3 flex flex-wrap gap-2"><button type="button" disabled={busy !== null} onClick={() => void feedback('ADOPTED')} className="min-h-11 rounded-xl border border-emerald-200 px-3 text-xs font-semibold text-emerald-800 disabled:opacity-50">这份行程对我有帮助</button><button type="button" disabled={busy !== null} onClick={() => void feedback('REJECTED')} className="min-h-11 rounded-xl border border-slate-200 px-3 text-xs font-semibold text-slate-700 disabled:opacity-50">这份行程需要改进</button></div> : <p className="mt-2 text-xs leading-5 text-slate-500">反馈保存默认关闭。可在 <Link href="/profile" className="font-semibold text-sky-700 underline">偏好与数据用途</Link> 中单独开启；这不会开启训练或评测。</p>}
      </div>
    </section>
  )
}
