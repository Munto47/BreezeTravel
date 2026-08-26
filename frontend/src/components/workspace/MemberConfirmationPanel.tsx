'use client'

import { useEffect, useMemo, useState } from 'react'
import { CheckCircle2, Copy, Link2, Loader2, RefreshCw, ShieldAlert, Trash2, Users } from 'lucide-react'

import { api } from '@/lib/api'
import type { IssuedShareLink, MemberConstraintWriteResult, ShareScope, WorkspaceMemberView } from '@/types/workspace'

type IssuedLinkState = { link: IssuedShareLink['link']; rawToken: string | null }

interface Props {
  workspaceId: string
  itineraryRevision: number
  memberConstraintRevision: number | null
  currentUserId: string
  disabled?: boolean
  onConstraintSaved: () => Promise<void> | void
}

const DEFAULT_SCOPES: ShareScope[] = ['ACKNOWLEDGE', 'CONSTRAINT_WRITE']

function currentRevisionLabel(member: WorkspaceMemberView, itineraryRevision: number) {
  if (member.confirmed_itinerary_revision === itineraryRevision) return '已确认当前版本'
  if (member.confirmed_itinerary_revision && member.confirmed_itinerary_revision < itineraryRevision) return `已确认旧版 r${member.confirmed_itinerary_revision}`
  return '待确认'
}

function constraintValue(value: unknown) {
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  try { return JSON.stringify(value) } catch { return '结构化值' }
}

export default function MemberConfirmationPanel({
  workspaceId, itineraryRevision, memberConstraintRevision, currentUserId, disabled = false, onConstraintSaved,
}: Props) {
  const [members, setMembers] = useState<WorkspaceMemberView[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [constraintType, setConstraintType] = useState('latest_return_time')
  const [constraintValueInput, setConstraintValueInput] = useState('20:30')
  const [waivableBy, setWaivableBy] = useState<string[]>([])
  const [recipientMemberId, setRecipientMemberId] = useState('')
  const [scopes, setScopes] = useState<ShareScope[]>(DEFAULT_SCOPES)
  const [issued, setIssued] = useState<IssuedLinkState[]>([])

  const load = async () => {
    setLoading(true)
    try {
      const data = await api.get<WorkspaceMemberView[]>(`/api/trip-workspaces/${workspaceId}/members`)
      setMembers(data)
      setRecipientMemberId(current => data.some(member => member.member_id === current && member.member_id !== currentUserId)
        ? current
        : (data.find(member => member.member_id !== currentUserId)?.member_id ?? ''))
      setError(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '成员信息加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [workspaceId])

  const otherMembers = useMemo(() => members.filter(member => member.member_id !== currentUserId), [currentUserId, members])
  const confirmedCount = members.filter(member => member.confirmed_itinerary_revision === itineraryRevision).length

  const writeMyHardConstraint = async () => {
    if (disabled || busy) return
    const value = constraintValueInput.trim()
    if (!constraintType.trim() || !value) {
      setError('请填写约束类型和值；这会作为本人明确确认的硬约束保存。')
      return
    }
    setBusy('constraint')
    setError(null)
    try {
      await api.put<MemberConstraintWriteResult>(
        `/api/trip-workspaces/${workspaceId}/members/${encodeURIComponent(currentUserId)}/constraints`,
        {
          expected_base_revision: memberConstraintRevision ?? 0,
          constraint: {
            constraint_id: crypto.randomUUID(),
            owner_member_id: currentUserId,
            type: constraintType.trim(),
            operator: 'EQ',
            value,
            hardness: 'HARD',
            priority: 100,
            source: 'MEMBER_EXPLICIT',
            confirmation_status: 'CONFIRMED',
            // Keep the waiver authority tied to actual workspace members too;
            // a free-text ID would make the provenance display misleading.
            waivable_by: waivableBy,
          },
        },
      )
      await Promise.all([load(), Promise.resolve(onConstraintSaved())])
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '本人约束保存失败')
    } finally {
      setBusy(null)
    }
  }

  const toggleScope = (scope: ShareScope) => {
    setScopes(previous => previous.includes(scope) ? previous.filter(item => item !== scope) : [...previous, scope])
  }

  const issueLink = async () => {
    if (disabled || busy) return
    if (!recipientMemberId || scopes.length === 0) {
      setError('请从服务端成员列表选择接收人，并至少选择一种受限能力。')
      return
    }
    setBusy('share')
    setError(null)
    try {
      const issuedLink = await api.post<IssuedShareLink>(`/api/trip-workspaces/${workspaceId}/share-links`, {
        recipient_member_id: recipientMemberId,
        // The captured report is optional, but input capabilities must be
        // bound to a real room member and this immutable itinerary revision.
        scopes: scopes.includes('REPORT_READ') ? scopes : ['REPORT_READ', ...scopes],
      })
      setIssued(previous => [{ link: issuedLink.link, rawToken: issuedLink.token }, ...previous])
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '受限链接签发失败')
    } finally {
      setBusy(null)
    }
  }

  const revoke = async (linkId: string) => {
    if (disabled || busy) return
    setBusy(`revoke:${linkId}`)
    setError(null)
    try {
      const link = await api.delete<IssuedShareLink['link']>(`/api/trip-workspaces/${workspaceId}/share-links/${linkId}`)
      setIssued(previous => previous.map(item => item.link.share_link_id === linkId ? { ...item, link, rawToken: null } : item))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '撤销链接失败')
    } finally {
      setBusy(null)
    }
  }

  const copyToken = async (linkId: string, token: string) => {
    try {
      await navigator.clipboard.writeText(token)
      setIssued(previous => previous.map(item => item.link.share_link_id === linkId ? { ...item, rawToken: null } : item))
    } catch {
      setError('复制失败；令牌仅在本次签发显示，关闭前请手动复制。')
    }
  }

  return (
    <section data-testid="member-confirmation-panel" className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-start gap-2">
        <Users className="mt-0.5 h-4 w-4 text-indigo-600" />
        <div>
          <h2 className="text-sm font-semibold text-slate-900">成员确认与受限分享</h2>
          <p className="mt-1 text-xs text-slate-500">以服务端成员、约束 revision 与回执为准；协同在线状态不构成确认。</p>
        </div>
        <button aria-label="刷新成员状态" onClick={() => void load()} disabled={loading || !!busy} className="ml-auto rounded-lg p-1.5 text-slate-500 hover:bg-slate-100 disabled:opacity-40"><RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} /></button>
      </div>

      <p className="mt-3 rounded-lg bg-slate-50 p-2 text-xs text-slate-700">当前行程 r{itineraryRevision}：{confirmedCount}/{members.length} 位成员已确认。</p>
      {loading ? <p className="mt-3 flex items-center gap-2 text-xs text-slate-500"><Loader2 className="h-3.5 w-3.5 animate-spin" />加载成员权威状态…</p> : (
        <div className="mt-3 space-y-2">
          {members.length === 0 ? <p className="text-xs text-slate-400">服务端尚无成员记录，不能签发输入权限。</p> : members.map(member => (
            <article key={member.member_id} className="rounded-xl border border-slate-200 p-3 text-xs">
              <div className="flex items-center gap-2">
                <p className="font-medium text-slate-800">{member.profile?.display_name ?? member.member_id}{member.member_id === currentUserId ? '（我）' : ''}</p>
                <span className={`ml-auto rounded-full px-2 py-0.5 text-[10px] ${member.confirmed_itinerary_revision === itineraryRevision ? 'bg-emerald-100 text-emerald-800' : 'bg-amber-100 text-amber-800'}`}>{currentRevisionLabel(member, itineraryRevision)}</span>
              </div>
              {member.constraints.length === 0 ? <p className="mt-2 text-slate-400">尚无已写入的成员约束</p> : (
                <ul className="mt-2 space-y-1.5">
                  {member.constraints.map(constraint => <li key={constraint.constraint_id} className="rounded-lg bg-slate-50 p-2 text-slate-700">
                    <span className={`mr-1 rounded px-1 py-0.5 text-[10px] ${constraint.hardness === 'HARD' ? 'bg-rose-100 text-rose-800' : 'bg-slate-200 text-slate-700'}`}>{constraint.hardness}</span>
                    {constraint.type} {constraint.operator} {constraintValue(constraint.value)}
                    <p className="mt-1 text-[10px] text-slate-500">来源 {constraint.source} · {constraint.confirmation_status} · 可豁免：{constraint.waivable_by.length ? constraint.waivable_by.join('、') : '无'}</p>
                  </li>)}
                </ul>
              )}
            </article>
          ))}
        </div>
      )}

      <div className="mt-4 border-t border-slate-100 pt-4">
        <div className="flex items-center gap-1.5"><ShieldAlert className="h-3.5 w-3.5 text-rose-600" /><h3 className="text-xs font-semibold text-slate-900">写入我的已确认硬约束</h3></div>
        <p className="mt-1 text-[11px] text-slate-500">仅当前登录成员可写；来源固定为 MEMBER_EXPLICIT，服务端会使旧审计失效。</p>
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          <input aria-label="约束类型" value={constraintType} onChange={event => setConstraintType(event.target.value)} disabled={disabled || !!busy} className="rounded-lg border border-slate-200 px-2 py-1.5 text-xs" placeholder="例如 latest_return_time" />
          <input aria-label="约束值" value={constraintValueInput} onChange={event => setConstraintValueInput(event.target.value)} disabled={disabled || !!busy} className="rounded-lg border border-slate-200 px-2 py-1.5 text-xs" placeholder="例如 20:30" />
          <label className="text-xs text-slate-600 sm:col-span-2">可豁免成员（仅工作台实际成员；留空表示不可豁免）
            <select aria-label="可豁免成员" multiple value={waivableBy} onChange={event => setWaivableBy(Array.from(event.currentTarget.selectedOptions, option => option.value))} disabled={disabled || !!busy || members.length === 0} className="mt-1 h-16 w-full rounded-lg border border-slate-200 px-2 py-1.5 text-xs">
              {members.map(member => <option key={member.member_id} value={member.member_id}>{member.profile?.display_name ?? member.member_id}</option>)}
            </select>
          </label>
        </div>
        <button onClick={() => void writeMyHardConstraint()} disabled={disabled || !!busy} className="mt-2 rounded-lg bg-rose-700 px-3 py-2 text-xs font-medium text-white disabled:opacity-40">{busy === 'constraint' ? '保存中…' : '以本人确认写入 HARD 约束'}</button>
      </div>

      <div className="mt-4 border-t border-slate-100 pt-4">
        <div className="flex items-center gap-1.5"><Link2 className="h-3.5 w-3.5 text-indigo-600" /><h3 className="text-xs font-semibold text-slate-900">签发成员专属确认 / 约束链接</h3></div>
        <p className="mt-1 text-[11px] text-slate-500">仅选择服务端已有成员；令牌只在签发响应中显示一次，服务端只存摘要。</p>
        <select aria-label="链接接收成员" value={recipientMemberId} onChange={event => setRecipientMemberId(event.target.value)} disabled={disabled || !!busy || otherMembers.length === 0} className="mt-2 w-full rounded-lg border border-slate-200 px-2 py-1.5 text-xs">
          <option value="">选择成员</option>
          {otherMembers.map(member => <option key={member.member_id} value={member.member_id}>{member.profile?.display_name ?? member.member_id}</option>)}
        </select>
        <div className="mt-2 flex flex-wrap gap-2 text-xs">
          {DEFAULT_SCOPES.map(scope => <label key={scope} className="flex items-center gap-1"><input type="checkbox" checked={scopes.includes(scope)} onChange={() => toggleScope(scope)} disabled={disabled || !!busy} />{scope === 'ACKNOWLEDGE' ? '确认当前版本' : '写入本人约束'}</label>)}
        </div>
        <button onClick={() => void issueLink()} disabled={disabled || !!busy || otherMembers.length === 0} className="mt-2 rounded-lg bg-slate-900 px-3 py-2 text-xs font-medium text-white disabled:opacity-40">{busy === 'share' ? '签发中…' : '签发受限链接'}</button>
        {issued.length > 0 && <div className="mt-3 space-y-2">
          {issued.map(item => <div key={item.link.share_link_id} className="rounded-lg border border-indigo-200 bg-indigo-50 p-2 text-xs text-indigo-950">
            <p>绑定 {item.link.recipient_member_id} · r{item.link.itinerary_revision} · {item.link.revoked_at ? '已撤销' : '有效'} </p>
            {item.rawToken ? <div className="mt-2 rounded bg-white p-2"><p className="break-all font-mono text-[10px]">{item.rawToken}</p><div className="mt-2 flex gap-2"><button onClick={() => void copyToken(item.link.share_link_id, item.rawToken!)} className="flex items-center gap-1 rounded border bg-white px-2 py-1"><Copy className="h-3 w-3" />复制并隐藏</button><button onClick={() => setIssued(previous => previous.map(value => value.link.share_link_id === item.link.share_link_id ? { ...value, rawToken: null } : value))} className="rounded border bg-white px-2 py-1">我已安全保存</button></div></div> : <p className="mt-1 text-[10px] text-indigo-700">原始令牌已隐藏，无法再次读取。</p>}
            {!item.link.revoked_at && <button onClick={() => void revoke(item.link.share_link_id)} disabled={!!busy} className="mt-2 flex items-center gap-1 rounded border border-rose-200 bg-white px-2 py-1 text-rose-700 disabled:opacity-40"><Trash2 className="h-3 w-3" />撤销此链接</button>}
          </div>)}
        </div>}
      </div>
      {error && <p role="alert" className="mt-3 rounded-lg bg-rose-50 p-2 text-xs text-rose-800">{error}</p>}
    </section>
  )
}
