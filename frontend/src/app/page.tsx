'use client'

import { useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { ArrowRight } from 'lucide-react'
import {
  clearTripUnderstandingSession,
  createDemoTripUnderstanding,
  createFullTripUnderstanding,
  createTripRequestKey,
} from '@/lib/trip-understanding-v3'
import { useAuthStore } from '@/stores/authStore'
import './experience.css'

export default function HomePage() {
  const router = useRouter()
  const { user, hydrate, isHydrated } = useAuthStore()
  const [source, setSource] = useState('')
  const [busy, setBusy] = useState<'FULL' | 'DEMO' | null>(null)
  const [error, setError] = useState('')
  const [resumable, setResumable] = useState(false)
  const submitting = useRef(false)
  const attempt = useRef<{ text: string; key: string } | null>(null)
  useEffect(() => {
    hydrate()
    setResumable(Boolean(sessionStorage.getItem('bt_active_trip_ref')))
  }, [hydrate])
  async function start(mode: 'FULL' | 'DEMO') {
    if (submitting.current) return
    const text = source.trim()
    if (mode === 'FULL' && !text) {
      setError('先贴入一份攻略，或从示例开始。')
      return
    }
    submitting.current = true
    setBusy(mode)
    setError('')
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 15000)
    try {
      if (!attempt.current || attempt.current.text !== text)
        attempt.current = { text, key: createTripRequestKey() }
      const accepted =
        mode === 'DEMO'
          ? await createDemoTripUnderstanding(controller.signal)
          : await createFullTripUnderstanding(
              text,
              attempt.current.key,
              controller.signal,
            )
      clearTripUnderstandingSession()
      sessionStorage.removeItem('bt_pending_operation')
      sessionStorage.setItem('bt_active_trip_ref', accepted.public_resource_id)
      sessionStorage.setItem('bt_active_trip_is_demo', String(mode === 'DEMO'))
      sessionStorage.setItem(
        'bt_active_trip_mode',
        mode === 'DEMO' ? 'DEMO' : user ? 'CLAIMED' : 'FULL',
      )
      router.push('/trip/result')
    } catch (failure) {
      setError(
        failure instanceof Error && failure.message === 'ACTIVE_LIMIT_REACHED'
          ? '当前体验次数已用完，或已有行程正在整理。可以继续查看已有行程，稍后再来。'
          : '暂时没有收到整理结果，文字仍在这里。请稍后重试。',
      )
      submitting.current = false
      setBusy(null)
    } finally {
      window.clearTimeout(timeout)
    }
  }
  return (
    <main className="experience">
      <header className="e-header">
        <Link href="/" className="e-brand">
          行程查<span>TRIPCHECK</span>
        </Link>
        <div className="e-actions">
          {resumable && (
            <Link href="/trip/result" className="e-button e-button-quiet">
              继续上次行程
            </Link>
          )}
          <Link
            className="e-button e-button-quiet"
            href={user ? '/profile' : '/login'}
          >
            {user ? '我的账号' : '登录'}
          </Link>
        </div>
      </header>
      <section className="e-home">
        <div className="e-home-intro">
          <div className="e-eyebrow">把攻略，变成出发的安排</div>
          <h1>
            旅途值得期待，
            <br />
            行程值得确认。
          </h1>
          <p>
            贴入已有的攻略，整理每天的地点与路线。哪里赶、哪里不确定，一起看清楚。
          </p>
          <button
            type="button"
            className="e-example-link"
            data-testid="start-demo"
            disabled={!isHydrated || Boolean(busy)}
            onClick={() => void start('DEMO')}
          >
            {busy === 'DEMO' ? '正在打开示例…' : '先看北京三日示例 ↗'}
          </button>
          <p className="e-small">示例为固定回放，与真实整理分开。</p>
        </div>
        <div>
          <form
            className="e-input-panel"
            onSubmit={(event) => {
              event.preventDefault()
              void start('FULL')
            }}
          >
            <label className="e-input-label" htmlFor="trip-source">
              你的攻略或行程
            </label>
            <textarea
              id="trip-source"
              data-testid="trip-source-text"
              className="e-source"
              value={source}
              maxLength={50000}
              onChange={(event) => setSource(event.target.value)}
              placeholder={
                '例如：北京三天，第一天去故宫和景山。\n第二天上午 9 点到天坛，玩两小时，再去前门吃饭。\n第三天颐和园，圆明园先作为备选。'
              }
            />
            <div className="e-input-footer">
              <span className="e-muted e-small">
                {source.length
                  ? `${source.length.toLocaleString()} / 50,000`
                  : '无需先填城市、日期或人数'}
              </span>
              <button
                type="submit"
                className="e-button e-button-primary"
                data-testid="create-full-trip"
                disabled={!isHydrated || Boolean(busy)}
              >
                {busy === 'FULL' ? '正在接收…' : '整理这份行程'}
                <ArrowRight aria-hidden="true" />
              </button>
            </div>
          </form>
          <p className="e-home-note">
            无需登录即可体验，匿名行程保留 24 小时。登录保存后保留 30
            天，可随时删除。
          </p>
          {error && (
            <div role="alert" className="e-message">
              {error}
            </div>
          )}
        </div>
      </section>
      <footer className="e-home-footer">
        <span>
          北京 · 上海 · 杭州提供地点与路线核对；其他城市可整理基础行程。
        </span>
        <Link href="/about#privacy">隐私与数据</Link>
      </footer>
    </main>
  )
}
