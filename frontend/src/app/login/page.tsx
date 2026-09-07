'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { ArrowRight } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import '../experience.css'

type AuthResponse = { token?: string; user_id?: string; nickname?: string }

function returnPath(consume = true) {
  const requested = sessionStorage.getItem('bt_login_return')
  if (consume) sessionStorage.removeItem('bt_login_return')
  if (
    !requested?.startsWith('/') ||
    requested.startsWith('//') ||
    /[\\\u0000-\u001f]/.test(requested)
  )
    return '/'
  try {
    const target = new URL(requested, window.location.origin)
    if (
      target.origin !== window.location.origin ||
      target.pathname === '/login'
    )
      return '/'
    return `${target.pathname}${target.search}${target.hash}`
  } catch {
    return '/'
  }
}

export default function LoginPage() {
  const router = useRouter()
  const { user, hydrate, isHydrated, login } = useAuthStore()
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [nickname, setNickname] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [backPath, setBackPath] = useState('/')
  const submitted = useRef(false)
  const navigated = useRef(false)
  const finish = useCallback(() => {
    if (navigated.current) return
    navigated.current = true
    router.replace(returnPath())
  }, [router])

  useEffect(() => {
    hydrate()
    const target = returnPath(false)
    setBackPath(
      target === '/profile' ||
        target.startsWith('/collaborate') ||
        target.startsWith('/room/')
        ? '/'
        : target,
    )
  }, [hydrate])
  useEffect(() => {
    if (isHydrated && user) finish()
  }, [isHydrated, user, finish])

  async function authenticate(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (submitted.current) return
    if (
      mode === 'register' &&
      !/^(?=.*[A-Za-z])(?=.*\d).{8,64}$/.test(password)
    ) {
      setError('密码请使用 8–64 位字符，并包含字母和数字。')
      return
    }
    submitted.current = true
    setBusy(true)
    setError('')
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), 15000)
    try {
      const response = await fetch(`/api/auth/email-${mode}`, {
        method: 'POST',
        credentials: 'include',
        signal: controller.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: email.trim(),
          password,
          ...(mode === 'register'
            ? { nickname: nickname.trim() || undefined }
            : {}),
        }),
      })
      if (!response.ok) {
        setError(
          response.status === 401
            ? '邮箱或密码不正确，请重新填写。'
            : response.status === 409
              ? '这个邮箱已经注册，可以切换到登录。'
              : response.status === 429
                ? '操作较频繁，请稍后重试。'
                : '暂时未能完成，请检查填写内容后重试。',
        )
        return
      }
      const data = (await response.json()) as AuthResponse
      if (!data.token || !data.user_id || !data.nickname)
        throw new Error('INVALID_RESPONSE')
      login(data.token, { userId: data.user_id, nickname: data.nickname })
      finish()
    } catch {
      setError(
        mode === 'register'
          ? '暂时没有收到结果。可以稍后重试；若提示邮箱已注册，请直接登录。'
          : '暂时没有收到登录结果，请稍后重试。',
      )
    } finally {
      window.clearTimeout(timer)
      submitted.current = false
      setBusy(false)
    }
  }

  return (
    <main className="experience">
      <header className="e-header">
        <Link href="/" className="e-brand">
          行程查<span>TRIPCHECK</span>
        </Link>
        <Link
          href={backPath}
          className="e-button e-button-quiet"
          onClick={() => {
            sessionStorage.removeItem('bt_claim_after_login')
            sessionStorage.removeItem('bt_login_return')
          }}
        >
          {backPath.startsWith('/trip/result')
            ? '返回行程'
            : backPath.startsWith('/collaborate') || backPath.startsWith('/room/')
              ? '返回协同规划'
              : '返回首页'}
        </Link>
      </header>
      <section className="e-auth">
        <div className="e-eyebrow">让下一次出发，接着这次安排</div>
        <h1>{mode === 'login' ? '找回你的行程。' : '为行程留个位置。'}</h1>
        <p className="e-muted">
          登录后可保存当前行程，保留 30 天。未保存的匿名行程保留 24 小时。
        </p>
        <nav className="e-auth-tabs" aria-label="登录方式">
          <button
            type="button"
            aria-pressed={mode === 'login'}
            disabled={busy || !isHydrated}
            onClick={() => {
              setMode('login')
              setError('')
            }}
          >
            邮箱登录
          </button>
          <button
            type="button"
            aria-pressed={mode === 'register'}
            disabled={busy || !isHydrated}
            onClick={() => {
              setMode('register')
              setError('')
            }}
          >
            注册账号
          </button>
        </nav>
        <form onSubmit={authenticate} className="e-auth-form">
          <label className="e-field">
            邮箱
            <input
              type="email"
              autoComplete="email"
              required
              maxLength={254}
              value={email}
              disabled={busy || !isHydrated}
              onChange={(event) => setEmail(event.target.value)}
            />
          </label>
          <label className="e-field">
            密码
            <input
              type="password"
              autoComplete={
                mode === 'register' ? 'new-password' : 'current-password'
              }
              required
              maxLength={64}
              minLength={mode === 'register' ? 8 : undefined}
              value={password}
              disabled={busy || !isHydrated}
              onChange={(event) => setPassword(event.target.value)}
              aria-describedby={
                mode === 'register' ? 'password-help' : undefined
              }
            />
          </label>
          {mode === 'register' && (
            <>
              <p id="password-help" className="e-small e-muted">
                8–64 位，包含字母和数字。
              </p>
              <label className="e-field">
                称呼（选填）
                <input
                  autoComplete="nickname"
                  maxLength={40}
                  value={nickname}
                  disabled={busy || !isHydrated}
                  onChange={(event) => setNickname(event.target.value)}
                />
              </label>
            </>
          )}
          {error && (
            <p className="e-message" role="alert">
              {error}
            </p>
          )}
          <button
            className="e-button e-button-primary"
            type="submit"
            disabled={!isHydrated || busy}
          >
            {busy
              ? '正在处理…'
              : mode === 'login'
                ? '登录并继续'
                : '注册并继续'}
            <ArrowRight aria-hidden="true" />
          </button>
        </form>
        <p className="e-auth-foot e-small e-muted">
          你可以随时删除自己的行程。
          <Link href="/about#privacy">了解隐私与数据</Link>
        </p>
      </section>
    </main>
  )
}
