'use client'

import { useEffect, useRef, useState } from 'react'
import { readMyShares, revokeShare, type ShareListItemView } from '@/lib/trip-understanding-v3'

export default function ShareSettingsPanel() {
  const [shares, setShares] = useState<ShareListItemView[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [revoking, setRevoking] = useState<string | null>(null)
  const [uncertain, setUncertain] = useState<string[]>([])
  const lock = useRef(false)
  const alive = useRef(false)

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError('')
    readMyShares(controller.signal).then(items => {
      if (!controller.signal.aborted) {
        setShares(items)
        setUncertain([])
      }
    }).catch(() => {
      if (!controller.signal.aborted) setError('分享列表暂时无法读取，请重新读取。')
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
  }, [attempt])

  async function revoke(shareRef: string) {
    if (lock.current || loading) return
    lock.current = true
    setRevoking(shareRef)
    setError('')
    setMessage('')
    try {
      await revokeShare(shareRef)
      if (!alive.current) return
      setShares(items => items.map(item => item.share_ref === shareRef ? { ...item, status: 'REVOKED' } : item))
      setMessage('该链接已撤销，之后无法再读取。其他分享和原行程保留；已下载的内容无法收回。')
    } catch {
      if (alive.current) {
        setUncertain(items => items.includes(shareRef) ? items : [...items, shareRef])
        setError('尚未确认撤销结果，请重新读取列表后再试。')
      }
    } finally {
      lock.current = false
      if (alive.current) setRevoking(null)
    }
  }

  return <section className="profile-section" id="shares" aria-labelledby="profile-shares-title">
    <h2 id="profile-shares-title">分享链接</h2>
    <p className="profile-help">按创建时间从新到旧排列。可单独撤销链接，保留原行程和其他分享。已下载的内容无法收回。</p>
    <button type="button" className="e-button mt-4" disabled={loading || revoking !== null} onClick={() => setAttempt(value => value + 1)}>重新读取分享列表</button>
    {loading && <p className="profile-notice" role="status">正在读取分享链接…</p>}
    {error && <p className="profile-notice" role="alert">{error}</p>}
    {message && <p className="profile-notice" role="status">{message}</p>}
    {!loading && !error && shares.length === 0 && <p className="profile-notice">还没有分享链接。</p>}
    <ul className="mt-4 space-y-3" aria-label="已创建的分享链接">
      {shares.map((share, index) => <li key={share.share_ref} className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 p-4">
        <div><h3>分享链接 {index + 1}</h3><p className="profile-help">有效至 {new Date(share.expires_at).toLocaleString('zh-CN', { hour12: false })}</p>
          <p className="profile-help">{uncertain.includes(share.share_ref) ? '状态待重新读取' : share.status === 'ACTIVE' ? '生效中' : share.status === 'REVOKED' ? '已撤销' : '已过期'}</p></div>
        {share.status === 'ACTIVE' && <button type="button" className="e-button" aria-label={`撤销分享链接 ${index + 1}`} disabled={loading || revoking !== null || uncertain.includes(share.share_ref)} onClick={() => void revoke(share.share_ref)}>{revoking === share.share_ref ? '正在撤销…' : '撤销链接'}</button>}
      </li>)}
    </ul>
  </section>
}
