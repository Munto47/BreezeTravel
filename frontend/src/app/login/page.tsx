'use client'

import { useCallback, useState, useRef, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { motion, AnimatePresence, useReducedMotion } from 'framer-motion'
import { Compass, Phone, Shield, ArrowRight, Check, Mail, Lock } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import { useToastStore } from '@/stores/toastStore'

const API_BASE = process.env.NEXT_PUBLIC_API_URL || ''
const AUTH_REQUEST_TIMEOUT_MS = 10_000

type Step = 'phone' | 'code' | 'nickname'
type ErrorResponseBody = { detail?: string }
type SendCodeResponseBody = ErrorResponseBody & { dev_bypass?: boolean }
type LoginResponseBody = ErrorResponseBody & {
  is_new_user?: boolean
  token: string
  user_id: string
  nickname: string
}


async function withAuthDeadline<T>(operation: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const controller = new AbortController()
  let timeout = 0
  const timeoutFailure = new Promise<never>((_resolve, reject) => {
    timeout = window.setTimeout(() => {
      controller.abort()
      reject(new Error('AUTH_REQUEST_TIMEOUT'))
    }, AUTH_REQUEST_TIMEOUT_MS)
  })
  try {
    return await Promise.race([operation(controller.signal), timeoutFailure])
  } finally {
    window.clearTimeout(timeout)
  }
}


async function boundedFetch(input: RequestInfo | URL, init?: RequestInit) {
  return withAuthDeadline((signal) => fetch(input, { ...init, signal }))
}


async function boundedJsonFetch<T>(input: RequestInfo | URL, init?: RequestInit) {
  return withAuthDeadline(async (signal) => {
    const response = await fetch(input, { ...init, signal })
    const data = await response.json() as T
    return { response, data }
  })
}

export default function LoginPage() {
  const reduceMotion = useReducedMotion()
  const router = useRouter()
  const { user, login, updateUser } = useAuthStore()
  const toast = useToastStore(s => s.toast)
  const [step, setStep] = useState<Step>('phone')
  const [phone, setPhone] = useState('')
  const [code, setCode] = useState(['', '', '', '', '', ''])
  const [nickname, setNickname] = useState('')
  const [countdown, setCountdown] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [devBypass, setDevBypass] = useState(false)
  // P1-3：短信兜底通道，邮箱+密码登录/注册
  const [authMode, setAuthMode] = useState<'phone' | 'email'>('phone')
  const [emailMode, setEmailMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [emailNickname, setEmailNickname] = useState('')
  const codeRefs = useRef<(HTMLInputElement | null)[]>([])
  const phoneRef = useRef<HTMLInputElement | null>(null)
  const emailRef = useRef<HTMLInputElement | null>(null)
  const passwordRef = useRef<HTMLInputElement | null>(null)
  const nicknameRef = useRef<HTMLInputElement | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const loginHandledRef = useRef(false)

  const finishLoginNavigation = useCallback(() => {
    const requested = sessionStorage.getItem('bt_login_return')
    sessionStorage.removeItem('bt_login_return')
    const destination = requested?.startsWith('/') && !requested.startsWith('//')
      ? requested
      : '/'
    router.replace(destination)
  }, [router])

  // 已登录则直接跳主页
  useEffect(() => {
    if (user && !loginHandledRef.current) finishLoginNavigation()
  }, [finishLoginNavigation, user])

  useEffect(() => {
    return () => { if (timerRef.current) clearInterval(timerRef.current) }
  }, [])

  const startCountdown = () => {
    setCountdown(60)
    timerRef.current = setInterval(() => {
      setCountdown(prev => {
        if (prev <= 1) { clearInterval(timerRef.current!); return 0 }
        return prev - 1
      })
    }, 1000)
  }

  const handleSendCode = async () => {
    const trimmed = phone.trim()
    if (!/^1[3-9]\d{9}$/.test(trimmed)) {
      const msg = '请输入正确的 11 位手机号'
      setError(msg)
      toast(msg, 'warning')
      window.requestAnimationFrame(() => phoneRef.current?.focus())
      return
    }
    setError('')
    setLoading(true)
    try {
      const { response: res, data } = await boundedJsonFetch<SendCodeResponseBody>(`${API_BASE}/api/auth/send-code`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone: trimmed }),
      })
      if (!res.ok) throw new Error(data.detail || '发送失败')
      startCountdown()
      setStep('code')
      if (data.dev_bypass && process.env.NODE_ENV !== 'production') {
        setDevBypass(true)
        toast('开发模式：验证码固定为 888888', 'info')
      } else {
        toast('验证码已发送', 'success')
      }
      setTimeout(() => codeRefs.current[0]?.focus(), 100)
    } catch {
      const msg = '暂时无法发送验证码，请稍后重试。'
      setError(msg)
      toast(msg, 'warning')
      window.requestAnimationFrame(() => phoneRef.current?.focus())
    } finally {
      setLoading(false)
    }
  }

  const handleCodeInput = (idx: number, val: string) => {
    if (val.length === 6 && /^\d{6}$/.test(val)) {
      const digits = val.split('')
      setCode(digits)
      codeRefs.current[5]?.focus()
      verifyCode(val)
      return
    }
    if (!/^\d?$/.test(val)) return
    const next = [...code]
    next[idx] = val
    setCode(next)
    if (val && idx < 5) codeRefs.current[idx + 1]?.focus()
    if (next.every(d => d !== '')) {
      verifyCode(next.join(''))
    }
  }

  const handleCodePaste = (e: React.ClipboardEvent) => {
    const pasted = e.clipboardData.getData('text').replace(/\D/g, '').slice(0, 6)
    if (pasted.length === 6) {
      e.preventDefault()
      const digits = pasted.split('')
      setCode(digits)
      codeRefs.current[5]?.focus()
      verifyCode(pasted)
    }
  }

  const handleCodeKeyDown = (idx: number, e: React.KeyboardEvent) => {
    if (e.key === 'Backspace' && !code[idx] && idx > 0) {
      codeRefs.current[idx - 1]?.focus()
    } else if (e.key === 'ArrowLeft' && idx > 0) {
      e.preventDefault()
      codeRefs.current[idx - 1]?.focus()
    } else if (e.key === 'ArrowRight' && idx < 5) {
      e.preventDefault()
      codeRefs.current[idx + 1]?.focus()
    }
  }

  const verifyCode = async (fullCode: string) => {
    setError('')
    setLoading(true)
    try {
      const { response: res, data } = await boundedJsonFetch<LoginResponseBody>(`${API_BASE}/api/auth/verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone: phone.trim(), code: fullCode }),
      })
      if (!res.ok) throw new Error(data.detail || '验证失败')

      if (data.is_new_user) {
        // 新用户：先写入 token，再引导设置昵称
        loginHandledRef.current = true
        login(data.token, { userId: data.user_id, nickname: data.nickname })
        setStep('nickname')
      } else {
        loginHandledRef.current = true
        login(data.token, { userId: data.user_id, nickname: data.nickname })
        finishLoginNavigation()
      }
    } catch {
      const msg = '暂时无法完成验证，请检查验证码后重试。'
      setError(msg)
      toast(msg, 'warning')
      setCode(['', '', '', '', '', ''])
      setTimeout(() => codeRefs.current[0]?.focus(), 50)
    } finally {
      setLoading(false)
    }
  }

  const handleTestLogin = async () => {
    setError('')
    setLoading(true)
    try {
      const { response: res, data } = await boundedJsonFetch<LoginResponseBody>(`${API_BASE}/api/auth/test-login`, { method: 'POST' })
      if (!res.ok) throw new Error(data.detail || '测试账号登录失败')
      loginHandledRef.current = true
      login(data.token, { userId: data.user_id, nickname: data.nickname })
      toast(`已用测试账号登录（${data.nickname}）`, 'success')
      finishLoginNavigation()
    } catch {
      const msg = '测试账号暂时无法登录，请稍后重试。'
      setError(msg)
      toast(msg, 'warning')
    } finally {
      setLoading(false)
    }
  }

  const handleEmailSubmit = async () => {
    const e = email.trim().toLowerCase()
    if (!/^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/.test(e)) {
      const msg = '请输入正确的邮箱'
      setError(msg); toast(msg, 'warning'); window.requestAnimationFrame(() => emailRef.current?.focus()); return
    }
    if (!password) {
      const msg = '请输入密码'
      setError(msg); toast(msg, 'warning'); window.requestAnimationFrame(() => passwordRef.current?.focus()); return
    }
    if (emailMode === 'register' && !/^(?=.*[A-Za-z])(?=.*\d).{8,64}$/.test(password)) {
      const msg = '密码至少 8 位，且包含字母 + 数字'
      setError(msg); toast(msg, 'warning'); window.requestAnimationFrame(() => passwordRef.current?.focus()); return
    }
    setError('')
    setLoading(true)
    try {
      const endpoint = emailMode === 'register' ? '/api/auth/email-register' : '/api/auth/email-login'
      const body: Record<string, unknown> = { email: e, password }
      if (emailMode === 'register') body.nickname = emailNickname.trim() || undefined
      const { response: res, data } = await boundedJsonFetch<LoginResponseBody>(`${API_BASE}${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error(data.detail || (emailMode === 'register' ? '注册失败' : '登录失败'))
      loginHandledRef.current = true
      login(data.token, { userId: data.user_id, nickname: data.nickname })
      toast(emailMode === 'register' ? `已注册，欢迎 ${data.nickname}` : `欢迎回来，${data.nickname}`, 'success')
      finishLoginNavigation()
    } catch {
      const msg = emailMode === 'register'
        ? '暂时无法完成注册，请稍后重试。'
        : '暂时无法登录，请检查信息后重试。'
      setError(msg); toast(msg, 'warning')
      window.requestAnimationFrame(() => emailRef.current?.focus())
    } finally {
      setLoading(false)
    }
  }

  const handleSetNickname = async () => {
    const name = nickname.trim() || '旅行者'
    setError('')
    setLoading(true)
    try {
      const response = await boundedFetch(`${API_BASE}/api/user/profile`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem('authToken')}`,
        },
        body: JSON.stringify({ nickname: name }),
      })
      if (!response.ok) throw new Error('PROFILE_UPDATE_FAILED')
      updateUser({ nickname: name })
      finishLoginNavigation()
    } catch {
      const msg = '昵称暂时无法保存，请稍后重试。'
      setError(msg)
      toast(msg, 'warning')
      window.requestAnimationFrame(() => nicknameRef.current?.focus())
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-gradient-to-br from-[#f8f7f2] via-white to-emerald-50/60 p-4 text-slate-900">
      <div className="fixed inset-0 pointer-events-none overflow-hidden">
        <div className="absolute -right-40 -top-40 h-96 w-96 rounded-full bg-emerald-100/45 blur-3xl" />
        <div className="absolute -bottom-20 -left-20 h-72 w-72 rounded-full bg-amber-100/45 blur-3xl" />
      </div>

      <motion.div
        initial={reduceMotion ? { opacity: 1 } : { opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: reduceMotion ? 0 : 0.4 }}
        className="w-full max-w-sm relative z-10"
      >
        {/* Logo */}
        <div className="text-center mb-8">
          <div className="mb-4 inline-flex h-14 w-14 items-center justify-center rounded-2xl bg-emerald-800 text-white shadow-lg shadow-emerald-900/15">
            <Compass className="w-7 h-7" strokeWidth={2} />
          </div>
          <h1 className="text-2xl font-bold text-gray-900">BreezeTravel</h1>
          <p className="text-gray-400 text-sm mt-1">导入行程 · 事实核验 · 有依据的调整建议</p>
        </div>

        {/* 步骤进度条 */}
        <div className="flex items-center justify-center gap-2 mb-5">
          {(['phone', 'code', 'nickname'] as const).map((s, i) => (
            <div key={s} className="flex items-center gap-2">
              <div className={`h-2 w-2 rounded-full transition-all duration-300 motion-reduce:transition-none ${
                s === step ? 'scale-125 bg-emerald-700 motion-reduce:scale-100' :
                (['phone', 'code', 'nickname'].indexOf(step) > i) ? 'bg-emerald-300' : 'bg-slate-200'
              }`} />
              {i < 2 && <div className={`h-px w-8 transition-colors duration-300 motion-reduce:transition-none ${
                (['phone', 'code', 'nickname'].indexOf(step) > i) ? 'bg-emerald-300' : 'bg-slate-200'
              }`} />}
            </div>
          ))}
        </div>

        {/* 卡片 */}
        <div className="overflow-hidden rounded-3xl border border-emerald-950/10 bg-white/95 shadow-[0_24px_70px_-34px_rgba(15,23,42,0.55)] backdrop-blur">
          <AnimatePresence mode="wait">

            {/* Step 1: 手机号 / 邮箱 */}
            {step === 'phone' && (
              <motion.div
                key="phone"
                initial={reduceMotion ? { opacity: 1 } : { opacity: 0, x: 20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={reduceMotion ? { opacity: 0 } : { opacity: 0, x: -20 }}
                transition={{ duration: reduceMotion ? 0 : 0.2 }}
                className="p-6"
              >
                {/* 登录方式 Tab */}
                <div className="mb-5 flex rounded-xl bg-slate-100 p-1" role="tablist" aria-label="登录方式">
                  {([
                    { v: 'phone', label: '手机号', icon: <Phone className="w-3.5 h-3.5" /> },
                    { v: 'email', label: '邮箱', icon: <Mail className="w-3.5 h-3.5" /> },
                  ] as const).map((t) => (
                    <button
                      key={t.v}
                      type="button"
                      role="tab"
                      aria-selected={authMode === t.v}
                      data-testid={t.v === 'email' ? 'auth-email-tab' : 'auth-phone-tab'}
                      onClick={() => { setAuthMode(t.v); setError('') }}
                      className={`flex min-h-11 flex-1 items-center justify-center gap-1.5 rounded-lg py-1.5 text-xs font-medium transition-all motion-reduce:transition-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 ${authMode === t.v ? 'bg-white text-emerald-800 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}
                    >
                      {t.icon}{t.label}
                    </button>
                  ))}
                </div>

                {authMode === 'email' ? (
                  <>
                    <div className="flex items-center gap-2 mb-4">
                      <button
                        type="button"
                        aria-pressed={emailMode === 'login'}
                        onClick={() => { setEmailMode('login'); setError('') }}
                        className={`min-h-10 rounded-lg px-3 py-1 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 ${emailMode === 'login' ? 'bg-emerald-50 text-emerald-800' : 'text-slate-500 hover:text-slate-700'}`}
                      >
                        登录
                      </button>
                      <button
                        type="button"
                        aria-pressed={emailMode === 'register'}
                        onClick={() => { setEmailMode('register'); setError('') }}
                        className={`min-h-10 rounded-lg px-3 py-1 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 ${emailMode === 'register' ? 'bg-emerald-50 text-emerald-800' : 'text-slate-500 hover:text-slate-700'}`}
                      >
                        注册
                      </button>
                      <span className="ml-auto text-[10px] text-gray-400">短信故障时的兜底通道</span>
                    </div>
                    <label htmlFor="auth-email" className="mb-1.5 block text-sm font-medium text-slate-700">邮箱地址</label>
                    <div className="relative mb-3">
                      <Mail className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" aria-hidden="true" />
                      <input
                        id="auth-email"
                        ref={emailRef}
                        data-testid="auth-email"
                        type="email"
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                        placeholder="name@example.com"
                        autoComplete="email"
                        aria-invalid={Boolean(error)}
                        aria-describedby={error ? 'auth-error' : undefined}
                        className="min-h-12 w-full rounded-xl border border-slate-300 bg-white pl-9 pr-3 text-sm outline-none transition motion-reduce:transition-none focus:border-emerald-700 focus:ring-2 focus:ring-emerald-700/20"
                      />
                    </div>
                    <label htmlFor="auth-password" className="mb-1.5 block text-sm font-medium text-slate-700">密码</label>
                    <div className="relative mb-3">
                      <Lock className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" aria-hidden="true" />
                      <input
                        id="auth-password"
                        ref={passwordRef}
                        data-testid="auth-password"
                        type="password"
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        onKeyDown={(e) => e.key === 'Enter' && handleEmailSubmit()}
                        placeholder={emailMode === 'register' ? '至少 8 位，含字母+数字' : '请输入密码'}
                        autoComplete={emailMode === 'register' ? 'new-password' : 'current-password'}
                        aria-invalid={Boolean(error)}
                        aria-describedby={error ? 'auth-error' : undefined}
                        className="min-h-12 w-full rounded-xl border border-slate-300 bg-white pl-9 pr-3 text-sm outline-none transition motion-reduce:transition-none focus:border-emerald-700 focus:ring-2 focus:ring-emerald-700/20"
                      />
                    </div>
                    {emailMode === 'register' && (
                      <label htmlFor="auth-register-nickname" className="mb-3 block text-sm font-medium text-slate-700">
                        昵称 <span className="font-normal text-slate-400">（可选）</span>
                        <input
                          id="auth-register-nickname"
                          data-testid="auth-nickname"
                          type="text"
                          value={emailNickname}
                          onChange={(e) => setEmailNickname(e.target.value)}
                          placeholder="默认使用邮箱前缀"
                          maxLength={20}
                          autoComplete="nickname"
                          className="mt-1.5 min-h-12 w-full rounded-xl border border-slate-300 bg-white px-3 text-sm outline-none transition motion-reduce:transition-none focus:border-emerald-700 focus:ring-2 focus:ring-emerald-700/20"
                        />
                      </label>
                    )}
                    {error && <p id="auth-error" data-tone="neutral" role="alert" className="mb-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700">{error}</p>}
                    <button
                      type="button"
                      data-testid="auth-email-submit"
                      onClick={handleEmailSubmit}
                      disabled={loading}
                      className="flex min-h-12 w-full items-center justify-center gap-2 rounded-xl bg-emerald-800 px-4 py-3 text-sm font-semibold text-white transition motion-reduce:transition-none hover:bg-emerald-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2 disabled:opacity-60"
                    >
                      {loading ? (
                        <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white motion-reduce:animate-none" aria-label="正在提交" />
                      ) : (
                        <>{emailMode === 'register' ? '注册并登录' : '邮箱登录'} <ArrowRight className="w-4 h-4" /></>
                      )}
                    </button>
                  </>
                ) : (
                <>
                <p className="mb-4 text-xs text-slate-500">未注册手机号将自动创建账号</p>
                <label htmlFor="auth-phone" className="mb-1.5 block text-sm font-medium text-slate-700">手机号</label>
                <div className="mb-4 flex gap-2">
                  <div className="flex items-center rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm font-medium text-slate-500" aria-hidden="true">
                    +86
                  </div>
                  <input
                    id="auth-phone"
                    ref={phoneRef}
                    type="tel"
                    value={phone}
                    onChange={e => setPhone(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && handleSendCode()}
                    placeholder="请输入手机号"
                    maxLength={11}
                    autoComplete="tel"
                    aria-invalid={Boolean(error)}
                    aria-describedby={error ? 'auth-error' : undefined}
                    className="min-h-12 min-w-0 flex-1 rounded-xl border border-slate-300 bg-white px-3 text-sm outline-none transition motion-reduce:transition-none focus:border-emerald-700 focus:ring-2 focus:ring-emerald-700/20"
                    autoFocus
                  />
                </div>
                {error && <p id="auth-error" data-tone="neutral" role="alert" className="mb-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700">{error}</p>}
                <button
                  type="button"
                  onClick={handleSendCode}
                  disabled={loading}
                  className="flex min-h-12 w-full items-center justify-center gap-2 rounded-xl bg-emerald-800 px-4 py-3 text-sm font-semibold text-white transition motion-reduce:transition-none hover:bg-emerald-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2 disabled:opacity-60"
                >
                  {loading ? (
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white motion-reduce:animate-none" aria-label="正在发送" />
                  ) : (
                    <>获取验证码 <ArrowRight className="w-4 h-4" /></>
                  )}
                </button>

                {/* 测试入口：仅在本地开发时通过 NEXT_PUBLIC_SHOW_TEST_LOGIN=true 显式启用 */}
                {process.env.NEXT_PUBLIC_SHOW_TEST_LOGIN === 'true' && (
                  <div className="mt-3 pt-3 border-t border-gray-100">
                    <button
                      type="button"
                      onClick={handleTestLogin}
                      disabled={loading}
                      className="flex min-h-11 w-full items-center justify-center gap-1.5 rounded-xl border border-slate-200 bg-slate-50 py-2.5 text-xs text-slate-500 transition-all motion-reduce:transition-none hover:border-emerald-200 hover:bg-emerald-50 hover:text-emerald-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700"
                    >
                      🚀 使用测试账号一键登录（演示用）
                    </button>
                  </div>
                )}
                </>
                )}
              </motion.div>
            )}

            {/* Step 2: 验证码 */}
            {step === 'code' && (
              <motion.div
                key="code"
                initial={reduceMotion ? { opacity: 1 } : { opacity: 0, x: 20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={reduceMotion ? { opacity: 0 } : { opacity: 0, x: -20 }}
                transition={{ duration: reduceMotion ? 0 : 0.2 }}
                className="p-6"
              >
                <div className="flex items-center gap-2 mb-2">
                  <Shield className="h-4 w-4 text-emerald-700" aria-hidden="true" />
                  <span className="text-sm font-semibold text-slate-700">输入验证码</span>
                </div>
                <p className="mb-2 text-xs text-slate-500">
                  已发送至 +86 {phone.replace(/(\d{3})\d{4}(\d{4})/, '$1****$2')}
                </p>
                {devBypass && (
                  <div className="mb-4 px-3 py-2 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-700">
                    🛠 开发模式：固定验证码 <span className="font-mono font-bold">888888</span>
                  </div>
                )}

                {/* 6 格验证码输入 */}
                <fieldset className="mb-4">
                  <legend className="mb-2 text-sm font-medium text-slate-700">六位短信验证码</legend>
                  <div className="flex justify-center gap-2">
                  {code.map((digit, i) => (
                    <input
                      key={i}
                      ref={el => { codeRefs.current[i] = el }}
                      type="text"
                      inputMode="numeric"
                      autoComplete={i === 0 ? 'one-time-code' : 'off'}
                      aria-label={`验证码第 ${i + 1} 位`}
                      aria-invalid={Boolean(error)}
                      aria-describedby={error ? 'auth-error' : undefined}
                      disabled={loading}
                      maxLength={1}
                      value={digit}
                      onChange={e => handleCodeInput(i, e.target.value)}
                      onKeyDown={e => handleCodeKeyDown(i, e)}
                      onPaste={handleCodePaste}
                      className="h-12 w-10 rounded-xl border-2 border-slate-200 bg-white text-center text-lg font-bold outline-none transition-colors motion-reduce:transition-none focus:border-emerald-600 focus:ring-2 focus:ring-emerald-700/20 disabled:opacity-60"
                    />
                  ))}
                  </div>
                </fieldset>

                {error && <p id="auth-error" data-tone="neutral" role="alert" className="mb-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-center text-xs text-slate-700">{error}</p>}

                {loading && (
                  <div className="flex justify-center mb-3">
                    <span className="h-5 w-5 animate-spin rounded-full border-2 border-emerald-200 border-t-emerald-700 motion-reduce:animate-none" aria-label="正在验证" />
                  </div>
                )}

                <div className="flex items-center justify-between text-xs text-gray-400">
                  <button
                    type="button"
                    onClick={() => { setStep('phone'); setCode(['','','','','','']); setError('') }}
                    className="min-h-11 rounded-lg px-2 transition-colors motion-reduce:transition-none hover:text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700"
                  >
                    更换手机号
                  </button>
                  <button
                    type="button"
                    onClick={handleSendCode}
                    disabled={countdown > 0 || loading}
                    className="min-h-11 rounded-lg px-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 disabled:opacity-40"
                  >
                    {countdown > 0 ? `${countdown}s 后重发` : '重新获取'}
                  </button>
                </div>
              </motion.div>
            )}

            {/* Step 3: 新用户设置昵称 */}
            {step === 'nickname' && (
              <motion.div
                key="nickname"
                initial={reduceMotion ? { opacity: 1 } : { opacity: 0, scale: 0.96 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: reduceMotion ? 0 : 0.2 }}
                className="p-6"
              >
                <div className="text-center mb-5">
                  <div className="w-12 h-12 bg-emerald-100 rounded-full flex items-center justify-center mx-auto mb-3">
                    <Check className="w-6 h-6 text-emerald-500" />
                  </div>
                  <p className="font-semibold text-gray-800">欢迎加入 BreezeTravel！</p>
                  <p className="text-xs text-gray-400 mt-1">取一个旅行代号吧</p>
                </div>
                <label htmlFor="profile-nickname" className="mb-4 block text-sm font-medium text-slate-700">
                  旅行代号 <span className="font-normal text-slate-400">（可跳过）</span>
                  <input
                    id="profile-nickname"
                    ref={nicknameRef}
                    type="text"
                    value={nickname}
                    onChange={e => setNickname(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && !loading && handleSetNickname()}
                    placeholder="例如：小风"
                    maxLength={20}
                    autoComplete="nickname"
                    aria-invalid={Boolean(error)}
                    aria-describedby={error ? 'nickname-error' : undefined}
                    className="mt-1.5 min-h-12 w-full rounded-xl border border-slate-300 bg-white px-3 text-sm outline-none transition motion-reduce:transition-none focus:border-emerald-700 focus:ring-2 focus:ring-emerald-700/20"
                    autoFocus
                  />
                </label>
                {error && <p id="nickname-error" data-tone="neutral" role="alert" className="mb-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700">{error}</p>}
                <button
                  type="button"
                  onClick={handleSetNickname}
                  disabled={loading}
                  className="flex min-h-12 w-full items-center justify-center gap-2 rounded-xl bg-emerald-800 px-4 py-3 text-sm font-semibold text-white transition motion-reduce:transition-none hover:bg-emerald-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2 disabled:opacity-60"
                >
                  {loading ? <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white motion-reduce:animate-none" aria-label="正在保存" /> : <>开始核验 <ArrowRight className="w-4 h-4" /></>}
                </button>
              </motion.div>
            )}

          </AnimatePresence>
        </div>

        <p className="text-center text-[11px] text-gray-300 mt-6">
          登录即代表同意用户协议与隐私政策
        </p>
      </motion.div>
    </main>
  )
}
