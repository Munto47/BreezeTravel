'use client'

import { useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { ArrowRight, ClipboardPaste, FileText, Sparkles, Map, Landmark, UsersRound, CornerDownLeft } from 'lucide-react'
import ExperienceHeader from '@/components/experience/experience-header'
import HomeDecor from '@/components/experience/home-decor'
import HomePreview from '@/components/experience/home-preview'
import { HOME_EXAMPLES, type HomeExample } from '@/components/experience/home-examples'
import { rememberBrowserTripReference } from '@/lib/browser-resource-ref'
import AccessibleDialog from './trip/result/accessible-dialog'
import {
  clearTripUnderstandingSession,
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
import {tripWaitClock} from '@/lib/trip-wait-clock'

type Resume = { reference: string; title: string; updated?: string | null }
type Replacement = {text: string; label: string; kind: 'example' | 'clipboard'; exampleReference?: InputDraft['exampleReference']}
const EXAMPLE_ICONS = {beijing: Landmark, shenzhen: UsersRound}

export default function HomePage() {
  const router = useRouter()
  const { user, hydrate, isHydrated } = useAuthStore()
  const [source, setSource] = useState('')
  const input = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    const resize = () => {const element=input.current; if(!element || !window.matchMedia('(max-width:1023px)').matches)return; element.style.height='180px'; element.style.height=`${Math.max(180, Math.min(element.scrollHeight, (window.visualViewport?.height || window.innerHeight)/2))}px`}
    resize(); window.visualViewport?.addEventListener('resize', resize)
    return () => window.visualViewport?.removeEventListener('resize', resize)
  }, [source])
  const [ready, setReady] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [replacement, setReplacement] = useState<Replacement | null>(null)
  const [pasteBusy, setPasteBusy] = useState(false)
  const [inputNotice, setInputNotice] = useState('')
  const editSequence = useRef(0)
  const [resume, setResume] = useState<Resume | null>(null)
  const submitted = useRef(false)
  const exampleReference = useRef<InputDraft['exampleReference']>(undefined)
  const attempt = useRef<InputDraft | null>(null)

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
        exampleReference.current = draft.exampleReference
        attempt.current = draft
        setSource(draft.text)
        if (draft.failedResource && !draft.resource)
          setError(
            '上次没有整理完成，原文已保留，可以直接重试，也可以先修改文字。',
          )
      } else sessionStorage.removeItem(INPUT_KEY)
    } catch {
      exampleReference.current = undefined
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
          ['UNDERSTANDING_FAILED', 'INPUT_CAPACITY_EXCEEDED', 'INPUT_DAY_CAPACITY_EXCEEDED'].includes(failure.message)
        ) {
          const recovered = releaseFailedTripInput(reference)
          if (recovered) {
            attempt.current = recovered
            setError(
              failure.message === 'INPUT_CAPACITY_EXCEEDED'
                ? '原文已保留。一次最多整理 160 项，请拆分文字后重新整理。'
                : failure.message === 'INPUT_DAY_CAPACITY_EXCEEDED'
                  ? '原文已保留。一次最多整理 14 天，请拆分文字后重新整理。'
                  : '上次没有整理完成，原文已保留，可以直接重试，也可以先修改文字。',
            )
          }
        }
        if (failure instanceof Error && failure.message === 'TRIP_GONE') {
          if (attempt.current?.resource === reference) {
            attempt.current = null
            setSource('')
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
      exampleReference.current = undefined
      sessionStorage.removeItem(INPUT_KEY)
      return
    }
    if (
      !attempt.current ||
      attempt.current.text !== source ||
      attempt.current.demo !== false
    ) {
      attempt.current = {
        text: source,
        exampleReference: exampleReference.current,
        demo: false,
        key: createTripRequestKey(),
        expires: Date.now() + 24 * 60 * 60 * 1000,
      }
    }
    sessionStorage.setItem(INPUT_KEY, JSON.stringify(attempt.current))
  }, [source, ready, busy])

  function fillText(next: Replacement) {
    editSequence.current += 1
    exampleReference.current = next.exampleReference
    setSource(next.text)
    setReplacement(null)
    setError('')
    setInputNotice(next.kind === 'example' ? `已填入${next.label}。示例使用预处理加速，修改内容会重新整理。` : '已粘贴文字，可以开始整理。')
    document.getElementById('trip-source')?.focus()
  }

  function chooseExample(example: HomeExample) {
    const next: Replacement = {text: example.text, label: example.label, kind: 'example', exampleReference: {id: example.id, version: example.version}}
    if (source.trim() && source !== example.text) setReplacement(next)
    else fillText(next)
  }

  async function pasteText() {
    if (busy || pasteBusy || !ready) return
    const sequence = editSequence.current
    setPasteBusy(true)
    setInputNotice('')
    try {
      const text = await navigator.clipboard.readText()
      // A delayed permission response must not overwrite newly typed text.
      if (sequence !== editSequence.current) {
        setInputNotice('文字已经改动，没有覆盖。需要时可再次粘贴。')
      } else if (!text.trim()) {
        setInputNotice('剪贴板中没有文字。可以直接在输入框中粘贴。')
      } else if (text.length > 50000) {
        setInputNotice('这段文字超过 50,000 字，请分成几份后粘贴。现有文字已保留。')
      } else {
        const next: Replacement = {text, label: '剪贴板文字', kind: 'clipboard'}
        if (source.trim() && source !== text) setReplacement(next)
        else fillText(next)
      }
    } catch {
      setInputNotice('未能读取剪贴板。请在输入框中按 Ctrl / ⌘ + V，或长按后选择粘贴。')
      document.getElementById('trip-source')?.focus()
    } finally {setPasteBusy(false)}
  }

  async function start(event: React.FormEvent) {
    event.preventDefault()
    if (submitted.current || pasteBusy || !ready) return
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
      if (
        !attempt.current ||
        attempt.current.text !== source ||
        attempt.current.demo !== false
      ) {
        attempt.current = {
          text: source,
          exampleReference: exampleReference.current,
          demo: false,
          key: createTripRequestKey(),
          expires: Date.now() + 24 * 60 * 60 * 1000,
        }
      }
      attempt.current.failedResource = undefined
      const submittedAttempt = attempt.current
      submittedAttempt.submittedAt ??= Date.now()
      sessionStorage.setItem(INPUT_KEY, JSON.stringify(submittedAttempt))
      const accepted = await createFullTripUnderstanding(
        source.trim(),
        submittedAttempt.key,
        controller.signal,
        submittedAttempt.exampleReference,
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
      tripWaitClock(accepted.public_resource_id, submittedAttempt.submittedAt)
      sessionStorage.setItem(INPUT_KEY, JSON.stringify(attempt.current))
      sessionStorage.setItem('bt_active_trip_ref', accepted.public_resource_id)
      if (!user) rememberBrowserTripReference(accepted.public_resource_id)
      sessionStorage.setItem('bt_active_trip_is_demo', 'false')
      sessionStorage.setItem(
        'bt_active_trip_mode',
        user ? 'CLAIMED' : 'FULL',
      )
      router.push(
        `/trip/result#trip=${encodeURIComponent(accepted.public_resource_id)}`,
      )
    } catch (failure) {
      const failureCode = failure instanceof Error ? failure.message : ''
      setError(
        failureCode === 'ACTIVE_LIMIT_REACHED'
          ? '当前体验次数已用完，或已有行程正在整理。可以继续已有行程，稍后再来。'
          : failureCode === 'CREATE_SERVICE_UNAVAILABLE'
            ? '整理服务暂时不可用，文字仍在这里。请稍后重试，重试会确认同一次请求。'
            : '暂时没有收到整理结果，文字仍在这里。重试会确认同一次请求。',
      )
      submitted.current = false
      setBusy(false)
    } finally {
      window.clearTimeout(timeout)
    }
  }

  return (
    <main className="four-home">
      <ExperienceHeader />
      <HomeDecor />
      <section className="four-home-main" aria-labelledby="home-title">
        <div className="four-hero-copy">
          <h1 id="home-title"><span>把旅行想法，</span><span>变成清晰的行程</span></h1>
          <p>粘贴行程、攻略或聊天文字，<wbr />整理地点与先后，生成每天的卡片和地图。</p>
        </div>
        <form onSubmit={start} className="four-input-panel">
          <label className="sr-only" htmlFor="trip-source">你的攻略或行程</label>
          <textarea ref={input} id="trip-source" data-testid="trip-source-text" value={source}
            maxLength={50000} disabled={!ready || busy}
            onChange={event => {editSequence.current += 1; setSource(event.target.value); setError(''); setInputNotice('')}}
            placeholder={'例如：北京三日游。第1天先去故宫，再到景山公园……\n把想去的地点和大致顺序贴在这里，剩下的慢慢完善。'}
            aria-invalid={Boolean(error)} aria-describedby={error ? 'home-input-error' : 'home-input-notice'} />
          <div className="four-input-footer">
            <span className="four-character-count" data-testid="source-character-count">{source.length.toLocaleString()} / 50,000</span>
            <div className="four-input-actions">
              <button type="button" className="four-paste-button" disabled={!ready || busy || pasteBusy} onClick={() => void pasteText()}><ClipboardPaste size={17} aria-hidden="true"/>{pasteBusy ? '正在读取…' : '粘贴内容'}</button>
              <button type="submit" className="four-primary" data-testid="create-full-trip" disabled={!isHydrated || !ready || busy || pasteBusy || !source.trim()}>{busy ? '正在接收…' : '开始整理行程'}<ArrowRight size={19} aria-hidden="true"/></button>
            </div>
          </div>
          {error && <p id="home-input-error" className="four-input-message is-error" role="alert">{error}</p>}
          <p id="home-input-notice" className="four-input-message" role="status">{inputNotice}</p>
        </form>
        <div className="four-example-choices" aria-label="填入文字示例">
          <span>试试这些示例：</span>
          {HOME_EXAMPLES.map((example,index) => {const Icon = EXAMPLE_ICONS[example.id]; return <button key={example.id} type="button" data-testid={index === 0 ? 'start-demo' : `home-example-${example.id}`} disabled={!ready || busy || pasteBusy} onClick={() => chooseExample(example)}><Icon size={18} aria-hidden="true"/>{example.label}</button>})}
        </div>
        {resume && <div className="four-resume-entry"><span><CornerDownLeft size={15} aria-hidden="true"/>{resume.title}</span><Link href={`/trip/result#trip=${encodeURIComponent(resume.reference)}`}>继续上次行程<ArrowRight size={15} aria-hidden="true"/></Link></div>}
        <ol className="four-how-it-works" aria-label="整理行程的三个步骤">
          {[{Icon:FileText,title:'粘贴攻略',detail:'已有攻略，或刚冒出的旅行想法'}, {Icon:Sparkles,title:'整理每天安排',detail:'理清地点、先后与原文备选'}, {Icon:Map,title:'生成卡片与地图',detail:'继续调整，保存你的旅行版本'}].map(({Icon,title,detail},index) => <li key={title}><span className="four-step-icon"><Icon aria-hidden="true"/></span><div><h2>{index+1}. {title}</h2><p>{detail}</p></div>{index < 2 && <ArrowRight className="four-step-arrow" aria-hidden="true"/>}</li>)}
        </ol>
      </section>
      <HomePreview />
      <footer className="four-home-footer"><span>让每一次旅行，都更简单、更从容。</span><Link href="/about#privacy">隐私与数据</Link></footer>
      {replacement && <AccessibleDialog titleId="replace-input-title" onClose={() => setReplacement(null)}>
        <h2 id="replace-input-title" className="text-xl font-semibold">替换当前文字？</h2>
        <p className="mt-3 text-sm leading-6 text-slate-600">{replacement.kind === 'example' ? '示例' : '剪贴板文字'}会替换当前输入。已有文字尚未提交，请确认后再替换。</p>
        <div className="mt-5 flex flex-wrap justify-end gap-2"><button type="button" className="four-secondary" data-dialog-initial-focus onClick={() => setReplacement(null)}>保留我的文字</button><button type="button" className="four-primary" onClick={() => fillText(replacement)}>{replacement.kind === 'example' ? '填入示例' : '替换为粘贴文字'}</button></div>
      </AccessibleDialog>}
    </main>
  )
}
