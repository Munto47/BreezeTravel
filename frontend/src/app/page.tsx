'use client'

import { useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { ArrowRight } from 'lucide-react'
import { BEIJING_DEMO_TEXT } from '@/lib/trip-demo'
import {
  clearTripUnderstandingSession,
  createDemoTripUnderstanding,
  createFullTripUnderstanding,
  createTripRequestKey,
  readTripUnderstandingResult,
} from '@/lib/trip-understanding-v3'
import { useAuthStore } from '@/stores/authStore'
import {
  releaseFailedTripInput,
  TRIP_INPUT_DRAFT_KEY as INPUT_KEY,
  type TripInputDraft as InputDraft,
} from '@/lib/trip-input-recovery'
import './experience.css'

type Resume = { reference: string; title: string; updated?: string | null }

export default function HomePage() {
  const router = useRouter()
  const { user, hydrate, isHydrated } = useAuthStore()
  const [source, setSource] = useState('')
  const [demo, setDemo] = useState(false)
  const [ready, setReady] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [replaceSample, setReplaceSample] = useState(false)
  const [resume, setResume] = useState<Resume | null>(null)
  const submitted = useRef(false)
  const attempt = useRef<InputDraft | null>(null)

  useEffect(() => {
    if (replaceSample) document.getElementById('keep-input')?.focus()
  }, [replaceSample])

  useEffect(() => {
    hydrate()
    try {
      const draft = JSON.parse(
        sessionStorage.getItem(INPUT_KEY) || 'null',
      ) as InputDraft | null
      if (
        draft &&
        typeof draft.text === 'string' &&
        draft.expires > Date.now()
      ) {
        attempt.current = draft
        setSource(draft.text)
        setDemo(draft.demo && draft.text === BEIJING_DEMO_TEXT)
        if (draft.failedResource && !draft.resource)
          setError(
            '上次没有整理完成，原文已保留，可以直接重试，也可以先修改文字。',
          )
      } else sessionStorage.removeItem(INPUT_KEY)
    } catch {
      sessionStorage.removeItem(INPUT_KEY)
    }
    setReady(true)
    const reference = sessionStorage.getItem('bt_active_trip_ref')
    if (!reference) return
    const controller = new AbortController()
    void readTripUnderstandingResult(reference, controller.signal)
      .then(({ body }) => {
        if (body.status === 'PROCESSING') {
          setResume({ reference, title: '正在整理的行程' })
        } else {
          const city =
            body.assumptions.find((item) => item.key === 'destination')
              ?.value || '上次行程'
          setResume({
            reference,
            title: `${city} · ${body.days.length} 天`,
            updated: body.updated_at,
          })
        }
      })
      .catch((failure) => {
        if (controller.signal.aborted) return
        if (
          failure instanceof Error &&
          failure.message === 'UNDERSTANDING_FAILED'
        ) {
          const recovered = releaseFailedTripInput(reference)
          if (recovered) {
            attempt.current = recovered
            setError(
              '上次没有整理完成，原文已保留，可以直接重试，也可以先修改文字。',
            )
          }
        }
        if (failure instanceof Error && failure.message === 'TRIP_GONE') {
          if (attempt.current?.resource === reference) {
            attempt.current = null
            setSource('')
            setDemo(false)
          }
          try {
            const pending = JSON.parse(
              sessionStorage.getItem('bt_pending_operation') || 'null',
            )
            if (
              pending &&
              [pending.resource, pending.claimedResource].includes(reference)
            )
              sessionStorage.removeItem('bt_pending_operation')
          } catch {
            /* Invalid pending data is not a usable operation. */
          }
          if (sessionStorage.getItem('bt_active_trip_ref') === reference)
            clearTripUnderstandingSession()
        }
        // No unverified, failed or expired resume shortcut.
      })
    return () => controller.abort()
  }, [hydrate])

  useEffect(() => {
    if (!ready || busy) return
    if (!source) {
      sessionStorage.removeItem(INPUT_KEY)
      return
    }
    if (
      !attempt.current ||
      attempt.current.text !== source ||
      attempt.current.demo !== demo
    ) {
      attempt.current = {
        text: source,
        demo,
        key: createTripRequestKey(),
        expires: Date.now() + 24 * 60 * 60 * 1000,
      }
    }
    sessionStorage.setItem(INPUT_KEY, JSON.stringify(attempt.current))
  }, [source, demo, ready, busy])

  function fillDemo() {
    setSource(BEIJING_DEMO_TEXT)
    setDemo(true)
    setReplaceSample(false)
    setError('')
    document.getElementById('trip-source')?.focus()
  }

  async function start(event: React.FormEvent) {
    event.preventDefault()
    if (submitted.current) return
    if (sessionStorage.getItem('bt_pending_operation')) {
      setError(
        '上一份行程还有一次修改等待确认。请先从“继续上次行程”确认结果，再整理新行程。',
      )
      return
    }
    if (!source.trim()) {
      setError('先贴入一份攻略，或填入示例。')
      return
    }
    submitted.current = true
    setBusy(true)
    setError('')
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 15000)
    try {
      const fixed = demo && source === BEIJING_DEMO_TEXT
      if (
        !attempt.current ||
        attempt.current.text !== source ||
        attempt.current.demo !== fixed
      ) {
        attempt.current = {
          text: source,
          demo: fixed,
          key: createTripRequestKey(),
          expires: Date.now() + 24 * 60 * 60 * 1000,
        }
      }
      attempt.current.failedResource = undefined
      const submittedAttempt = attempt.current
      sessionStorage.setItem(INPUT_KEY, JSON.stringify(submittedAttempt))
      const accepted = fixed
        ? await createDemoTripUnderstanding(
            controller.signal,
            submittedAttempt.key,
          )
        : await createFullTripUnderstanding(
            source.trim(),
            submittedAttempt.key,
            controller.signal,
          )
      if (attempt.current.key !== submittedAttempt.key) {
        // A concurrent result read acknowledged that this old attempt failed.
        // Do not bind its late acceptance to the fresh retry key.
        submitted.current = false
        setBusy(false)
        setError('上次没有整理完成，原文已保留，可以直接重试。')
        return
      }
      clearTripUnderstandingSession()
      sessionStorage.removeItem('bt_pending_operation')
      attempt.current.resource = accepted.public_resource_id
      sessionStorage.setItem(INPUT_KEY, JSON.stringify(attempt.current))
      sessionStorage.setItem('bt_active_trip_ref', accepted.public_resource_id)
      sessionStorage.setItem('bt_active_trip_is_demo', String(fixed))
      sessionStorage.setItem(
        'bt_active_trip_mode',
        fixed ? 'DEMO' : user ? 'CLAIMED' : 'FULL',
      )
      router.push(
        `/trip/result#trip=${encodeURIComponent(accepted.public_resource_id)}`,
      )
    } catch (failure) {
      setError(
        failure instanceof Error && failure.message === 'ACTIVE_LIMIT_REACHED'
          ? '当前体验次数已用完，或已有行程正在整理。可以继续已有行程，稍后再来。'
          : '暂时没有收到整理结果，文字仍在这里。重试会确认同一次请求。',
      )
      submitted.current = false
      setBusy(false)
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
        <nav className="e-actions" aria-label="全局导航">
          <Link href="/my-trips" className="e-button e-button-quiet">
            我的行程
          </Link>
          <Link
            href={user ? '/profile' : '/login'}
            className="e-button e-button-quiet"
          >
            {user ? '账号' : '登录'}
          </Link>
        </nav>
      </header>
      <section className="e-home e-home-direct">
        <div className="e-home-intro">
          <h1>把攻略，整理成走得明白的行程</h1>
          <p>粘贴已有攻略，查看每天安排、对应地图和需要调整的地方。</p>
        </div>
        <form onSubmit={start} className="e-input-panel">
          <label className="e-input-label" htmlFor="trip-source">
            你的攻略或行程
          </label>
          <textarea
            id="trip-source"
            data-testid="trip-source-text"
            className="e-source"
            value={source}
            maxLength={50000}
            disabled={!ready || busy}
            onChange={(event) => {
              setSource(event.target.value)
              setDemo(false)
              setError('')
            }}
            placeholder="例如：北京两天。第一天上午10点到故宫，停留两小时，再去景山公园。第二天去天坛，前门作为备选。"
            aria-describedby="input-scope input-mode"
            aria-invalid={Boolean(error)}
          />
          <div className="e-input-footer">
            <span className="e-small e-muted">
              {source.length
                ? `${source.length.toLocaleString()} / 50,000`
                : '无需先填城市、日期或人数'}
            </span>
            <button
              type="submit"
              className="e-button e-button-primary"
              data-testid="create-full-trip"
              disabled={!isHydrated || !ready || busy}
            >
              {busy ? '正在接收…' : '整理行程'}
              <ArrowRight aria-hidden="true" />
            </button>
          </div>
          {error && (
            <p className="e-message" role="alert">
              {error}
            </p>
          )}
        </form>
        <p id="input-mode" className="e-small e-muted">
          {demo
            ? '固定示例已填入。点击整理将打开回放；修改文字后会按真实攻略整理。'
            : '你的文字将用于整理行程与核对地点。'}
        </p>
        <div className="e-entry-secondary">
          <button
            type="button"
            className="e-text-button"
            data-testid="start-demo"
            disabled={!ready || busy}
            onClick={() =>
              source.trim() && source !== BEIJING_DEMO_TEXT
                ? setReplaceSample(true)
                : fillDemo()
            }
          >
            填入北京示例
          </button>
          <span className="e-small e-muted">固定回放，可先查看内容</span>
        </div>
        {replaceSample && (
          <div
            className="e-message"
            role="alertdialog"
            aria-label="替换输入确认"
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                setReplaceSample(false)
                document.getElementById('trip-source')?.focus()
              }
            }}
          >
            <p>示例会替换当前输入，是否继续？</p>
            <div className="e-actions">
              <button
                className="e-button"
                id="keep-input"
                type="button"
                onClick={() => {
                  setReplaceSample(false)
                  document.getElementById('trip-source')?.focus()
                }}
              >
                保留我的文字
              </button>
              <button
                className="e-button e-button-primary"
                type="button"
                onClick={fillDemo}
              >
                填入示例
              </button>
            </div>
          </div>
        )}
        {resume && (
          <div className="e-resume-entry">
            <div>
              <strong>{resume.title}</strong>
              <p className="e-small e-muted">
                当前会话可恢复
                {resume.updated
                  ? ` · 最近编辑 ${new Date(resume.updated).toLocaleString('zh-CN')}`
                  : ''}
              </p>
            </div>
            <Link
              className="e-button"
              href={`/trip/result#trip=${encodeURIComponent(resume.reference)}`}
            >
              继续上次行程
              <ArrowRight aria-hidden="true" />
            </Link>
          </div>
        )}
        <p id="input-scope" className="e-small e-muted">
          北京、上海、杭州支持深入核对；其他国内城市先整理安排。
        </p>
        <p className="e-small e-muted">
          无需登录即可开始。匿名行程保留24小时，保存到账号后默认保留30天，可随时删除。
        </p>
      </section>
      <footer className="e-home-footer">
        <span>让每天的安排更清楚</span>
        <Link href="/about#privacy">隐私与数据</Link>
      </footer>
    </main>
  )
}
