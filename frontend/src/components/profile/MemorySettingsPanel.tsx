'use client'

import { useEffect, useState } from 'react'
import { Brain, Check, Pencil, Trash2, X } from 'lucide-react'

import { api } from '@/lib/api'

interface MemoryRecord {
  id: string
  content: string
  category: string
  confidence: number
  expires_at?: string
}

export default function MemorySettingsPanel() {
  const [enabled, setEnabled] = useState(true)
  const [memories, setMemories] = useState<MemoryRecord[]>([])
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')

  useEffect(() => {
    Promise.all([
      api.get<MemoryRecord[]>('/api/user/memories'),
      api.get<{ enabled: boolean }>('/api/user/memory-settings'),
    ]).then(([items, settings]) => {
      setMemories(items)
      setEnabled(settings.enabled)
    }).catch(() => {})
  }, [])

  const toggle = async () => {
    const next = !enabled
    await api.post('/api/user/memory-settings', { enabled: next })
    setEnabled(next)
  }

  const save = async (id: string) => {
    const updated = await api.patch<MemoryRecord>(`/api/user/memories/${id}`, { content: draft })
    setMemories(items => items.map(item => item.id === id ? updated : item))
    setEditing(null)
  }

  const remove = async (id: string) => {
    await api.delete(`/api/user/memories/${id}`)
    setMemories(items => items.filter(item => item.id !== id))
  }

  return (
    <section className="mt-6 overflow-hidden rounded-2xl border border-violet-100 bg-white shadow-sm">
      <div className="flex items-center gap-3 border-b border-gray-100 px-4 py-4">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-violet-50"><Brain className="h-4 w-4 text-violet-500" /></div>
        <div className="flex-1">
          <p className="text-sm font-semibold text-gray-800">长期旅行记忆</p>
          <p className="text-[11px] text-gray-400">只作为软偏好；可关闭、纠正或删除。</p>
        </div>
        <button onClick={toggle} aria-label="切换长期记忆" className={`relative h-6 w-11 rounded-full transition ${enabled ? 'bg-violet-500' : 'bg-gray-200'}`}>
          <span className={`absolute top-1 h-4 w-4 rounded-full bg-white transition ${enabled ? 'left-6' : 'left-1'}`} />
        </button>
      </div>
      <div className="divide-y divide-gray-100">
        {memories.length === 0 && <p className="px-4 py-5 text-center text-xs text-gray-400">暂无稳定偏好记录</p>}
        {memories.map(memory => (
          <div key={memory.id} className="px-4 py-3">
            {editing === memory.id ? (
              <div className="flex gap-2">
                <input value={draft} onChange={event => setDraft(event.target.value)} maxLength={300} className="min-w-0 flex-1 rounded-lg border border-violet-200 px-2 py-1 text-xs outline-none" />
                <button onClick={() => void save(memory.id)} aria-label="保存记忆"><Check className="h-4 w-4 text-emerald-500" /></button>
                <button onClick={() => setEditing(null)} aria-label="取消编辑"><X className="h-4 w-4 text-gray-400" /></button>
              </div>
            ) : (
              <div className="flex items-start gap-2">
                <div className="flex-1">
                  <p className="text-xs text-gray-700">{memory.content}</p>
                  <p className="mt-1 text-[10px] text-gray-400">{memory.category} · 可信度 {Math.round(memory.confidence * 100)}%</p>
                </div>
                <button onClick={() => { setEditing(memory.id); setDraft(memory.content) }} aria-label="编辑记忆"><Pencil className="h-3.5 w-3.5 text-gray-400" /></button>
                <button onClick={() => void remove(memory.id)} aria-label="删除记忆"><Trash2 className="h-3.5 w-3.5 text-rose-400" /></button>
              </div>
            )}
          </div>
        ))}
      </div>
    </section>
  )
}
