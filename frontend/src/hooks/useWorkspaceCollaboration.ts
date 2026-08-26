'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import * as Y from 'yjs'
import { WebsocketProvider } from 'y-websocket'


const Y_WEBSOCKET_URL = process.env.NEXT_PUBLIC_Y_WEBSOCKET_URL || 'ws://localhost:1234'


export interface WorkspaceCollaborationRefs {
  itineraryRevision: number | null
  itineraryContentHash: string | null
  auditReportId: string | null
  auditRevision: number | null
  memberConstraintRevision: number | null
}

export interface WorkspaceEditIntent {
  intentId: string
  userId: string
  operation: string
  baseRevision: number
  createdAt: string
}


/**
 * Synchronizes only collaboration references and short-lived edit intent.
 * PostgreSQL remains the authority for revisions, reports and commands; this
 * CRDT never carries a mutable itinerary or a permission grant.
 */
export function useWorkspaceCollaboration(
  roomId: string | null,
  userId: string | null,
  nickname: string | null,
) {
  const docRef = useRef<Y.Doc | null>(null)
  const [connected, setConnected] = useState(false)
  const [refs, setRefs] = useState<WorkspaceCollaborationRefs>({
    itineraryRevision: null,
    itineraryContentHash: null,
    auditReportId: null,
    auditRevision: null,
    memberConstraintRevision: null,
  })
  const [intents, setIntents] = useState<WorkspaceEditIntent[]>([])

  useEffect(() => {
    if (!roomId || !userId) return
    const doc = new Y.Doc()
    docRef.current = doc
    const itineraryRef = doc.getMap<unknown>('itineraryRef')
    const auditRef = doc.getMap<unknown>('auditRef')
    const memberConstraintsRef = doc.getMap<unknown>('memberConstraintsRef')
    const editIntents = doc.getArray<WorkspaceEditIntent>('workspaceEditIntents')
    let provider: WebsocketProvider | null = null
    let cancelled = false

    const refresh = () => {
      setRefs({
        itineraryRevision: Number.isInteger(itineraryRef.get('revision')) ? Number(itineraryRef.get('revision')) : null,
        itineraryContentHash: typeof itineraryRef.get('contentHash') === 'string' ? String(itineraryRef.get('contentHash')) : null,
        auditReportId: typeof auditRef.get('reportId') === 'string' ? String(auditRef.get('reportId')) : null,
        auditRevision: Number.isInteger(auditRef.get('revision')) ? Number(auditRef.get('revision')) : null,
        memberConstraintRevision: Number.isInteger(memberConstraintsRef.get('revision'))
          ? Number(memberConstraintsRef.get('revision')) : null,
      })
      const latestByUser = new Map<string, WorkspaceEditIntent>()
      for (const intent of editIntents.toArray()) {
        if (!intent?.userId || !intent.intentId) continue
        const existing = latestByUser.get(intent.userId)
        if (!existing || intent.createdAt > existing.createdAt) latestByUser.set(intent.userId, intent)
      }
      setIntents([...latestByUser.values()].sort((a, b) => a.userId.localeCompare(b.userId)))
    }
    const connect = async () => {
      const apiBase = process.env.NEXT_PUBLIC_API_URL || ''
      const token = localStorage.getItem('authToken')
      if (!token) return
      const response = await fetch(`${apiBase}/api/room/${encodeURIComponent(roomId)}/ws-token`, {
        method: 'POST', headers: { Authorization: `Bearer ${token}` },
      })
      if (!response.ok || cancelled) return
      const body = await response.json() as { token: string }
      provider = new WebsocketProvider(Y_WEBSOCKET_URL, roomId, doc, { params: { token: body.token } })
      provider.on('status', ({ status }: { status: string }) => setConnected(status === 'connected'))
      provider.awareness.setLocalStateField('workspaceUser', { userId, nickname: nickname ?? userId })
    }
    void connect()
    itineraryRef.observe(refresh)
    auditRef.observe(refresh)
    memberConstraintsRef.observe(refresh)
    editIntents.observe(refresh)
    refresh()
    return () => {
      cancelled = true
      itineraryRef.unobserve(refresh)
      auditRef.unobserve(refresh)
      memberConstraintsRef.unobserve(refresh)
      editIntents.unobserve(refresh)
      provider?.destroy()
      doc.destroy()
      docRef.current = null
      setConnected(false)
    }
  }, [roomId, userId, nickname])

  const publishReferences = useCallback((next: WorkspaceCollaborationRefs) => {
    const doc = docRef.current
    if (!doc) return
    const itineraryRef = doc.getMap('itineraryRef')
    const auditRef = doc.getMap('auditRef')
    const memberConstraintsRef = doc.getMap('memberConstraintsRef')
    doc.transact(() => {
      itineraryRef.set('revision', next.itineraryRevision)
      itineraryRef.set('contentHash', next.itineraryContentHash)
      auditRef.set('reportId', next.auditReportId)
      auditRef.set('revision', next.auditRevision)
      memberConstraintsRef.set('revision', next.memberConstraintRevision)
    })
  }, [])

  const publishEditIntent = useCallback((operation: string, baseRevision: number) => {
    const doc = docRef.current
    if (!doc || !userId) return
    const intents = doc.getArray<WorkspaceEditIntent>('workspaceEditIntents')
    const item: WorkspaceEditIntent = {
      intentId: crypto.randomUUID(), userId, operation, baseRevision, createdAt: new Date().toISOString(),
    }
    doc.transact(() => {
      const retained = intents.toArray().filter(existing => (
        existing.userId !== userId && Date.now() - Date.parse(existing.createdAt) < 60_000
      ))
      if (retained.length !== intents.length) {
        intents.delete(0, intents.length)
        if (retained.length) intents.push(retained)
      }
      intents.push([item])
    })
  }, [userId])

  return { connected, refs, intents, publishReferences, publishEditIntent }
}
