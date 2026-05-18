'use client'

import { useState, useRef, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { motion, AnimatePresence } from 'framer-motion'
import { Compass, Phone, Shield, ArrowRight, Check } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

type Step = 'phone' | 'code' | 'nickname'

export default function LoginPage() {
  const router = useRouter()
  const { user, login, updateUser } = useAuthStore()
  const [step, setStep] = useState<Step>('phone')
  const [phone, setPhone] = useState('')
  const [code, setCode] = useState(['', '', '', '', '', ''])
  const [nickname, setNickname] = useState('')
  const [countdown, setCountdown] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const codeRefs = useRef<(HTMLInputElement | null)[]>([])
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // 已登录则直接跳主页
  useEffect(() => {
    if (user) router.replace('/')
  }, [user, router])

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
      setError('请输入正确的 11 位手机号')
      return
    }
    setError('')
    setLoading(true)
    try {
      const res = await fetch(`${API_BASE}/api/auth/send-code`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone: trimmed }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || '发送失败')
      startCountdown()
      setStep('code')
      setTimeout(() => codeRefs.current[0]?.focus(), 100)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '发送失败，请重试')
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
    }
  }

  const verifyCode = async (fullCode: string) => {
    setError('')
    setLoading(true)
    try {
      const res = await fetch(`${API_BASE}/api/auth/verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone: phone.trim(), code: fullCode }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || '验证失败')

      if (data.is_new_user) {
        // 新用户：先写入 token，再引导设置昵称
        login(data.token, { userId: data.user_id, nickname: data.nickname })
        setStep('nickname')
      } else {
        login(data.token, { userId: data.user_id, nickname: data.nickname })
        router.replace('/')
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '验证失败')
      setCode(['', '', '', '', '', ''])
      setTimeout(() => codeRefs.current[0]?.focus(), 50)
    } finally {
      setLoading(false)
    }
  }

  const handleSetNickname = async () => {
    const name = nickname.trim() || '旅行者'
    try {
      await fetch(`${API_BASE}/api/user/profile`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem('authToken')}`,
        },
        body: JSON.stringify({ nickname: name }),
      })
    } catch {}
    updateUser({ nickname: name })
    router.replace('/')
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-coral-50/40 via-white to-blue-50/30 flex items-center justify-center p-4">
      <div className="fixed inset-0 pointer-events-none overflow-hidden">
        <div className="absolute -top-40 -right-40 w-96 h-96 bg-coral-100/30 rounded-full blur-3xl" />
        <div className="absolute -bottom-20 -left-20 w-72 h-72 bg-blue-100/30 rounded-full blur-3xl" />
      </div>

      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
        className="w-full max-w-sm relative z-10"
      >
        {/* Logo */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-coral-500 text-white mb-4 shadow-lg shadow-coral-200">
            <Compass className="w-7 h-7" strokeWidth={2} />
          </div>
          <h1 className="text-2xl font-bold text-gray-900">BreezeTravel</h1>
          <p className="text-gray-400 text-sm mt-1">AI 驱动 · 多人协同 · 智能排线</p>
        </div>

        {/* 步骤进度条 */}
        <div className="flex items-center justify-center gap-2 mb-5">
          {(['phone', 'code', 'nickname'] as const).map((s, i) => (
            <div key={s} className="flex items-center gap-2">
              <div className={`w-2 h-2 rounded-full transition-all duration-300 ${
                s === step ? 'bg-coral-500 scale-125' :
                (['phone', 'code', 'nickname'].indexOf(step) > i) ? 'bg-coral-300' : 'bg-gray-200'
              }`} />
              {i < 2 && <div className={`w-8 h-px transition-colors duration-300 ${
                (['phone', 'code', 'nickname'].indexOf(step) > i) ? 'bg-coral-300' : 'bg-gray-200'
              }`} />}
            </div>
          ))}
        </div>

        {/* 卡片 */}
        <div className="glass-panel-solid rounded-2xl overflow-hidden shadow-glass">
          <AnimatePresence mode="wait">

            {/* Step 1: 手机号 */}
            {step === 'phone' && (
              <motion.div
                key="phone"
                initial={{ opacity: 0, x: 20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -20 }}
                className="p-6"
              >
                <div className="flex items-center gap-2 mb-5">
                  <Phone className="w-4 h-4 text-coral-500" />
                  <span className="text-sm font-semibold text-gray-700">手机号登录</span>
                </div>
                <p className="text-xs text-gray-400 mb-4">未注册手机号将自动创建账号</p>
                <div className="flex gap-2 mb-4">
                  <div className="flex items-center px-3 py-2.5 bg-gray-50 border border-gray-200 rounded-xl text-sm text-gray-500 font-medium">
                    +86
                  </div>
                  <input
                    type="tel"
                    value={phone}
                    onChange={e => setPhone(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && handleSendCode()}
                    placeholder="请输入手机号"
                    maxLength={11}
                    className="input-glass flex-1"
                    autoFocus
                  />
                </div>
                {error && <p className="text-xs text-red-500 mb-3">{error}</p>}
                <button
                  onClick={handleSendCode}
                  disabled={loading}
                  className="btn-coral w-full py-3 text-sm flex items-center justify-center gap-2"
                >
                  {loading ? (
                    <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  ) : (
                    <>获取验证码 <ArrowRight className="w-4 h-4" /></>
                  )}
                </button>
              </motion.div>
            )}

            {/* Step 2: 验证码 */}
            {step === 'code' && (
              <motion.div
                key="code"
                initial={{ opacity: 0, x: 20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -20 }}
                className="p-6"
              >
                <div className="flex items-center gap-2 mb-2">
                  <Shield className="w-4 h-4 text-coral-500" />
                  <span className="text-sm font-semibold text-gray-700">输入验证码</span>
                </div>
                <p className="text-xs text-gray-400 mb-5">
                  已发送至 +86 {phone.replace(/(\d{3})\d{4}(\d{4})/, '$1****$2')}
                </p>

                {/* 6 格验证码输入 */}
                <div className="flex gap-2 mb-4 justify-center">
                  {code.map((digit, i) => (
                    <input
                      key={i}
                      ref={el => { codeRefs.current[i] = el }}
                      type="text"
                      inputMode="numeric"
                      maxLength={1}
                      value={digit}
                      onChange={e => handleCodeInput(i, e.target.value)}
                      onKeyDown={e => handleCodeKeyDown(i, e)}
                      onPaste={handleCodePaste}
                      className="w-10 h-12 text-center text-lg font-bold border-2 border-gray-200 rounded-xl focus:border-coral-400 focus:outline-none bg-white transition-colors"
                    />
                  ))}
                </div>

                {error && <p className="text-xs text-red-500 mb-3 text-center">{error}</p>}

                {loading && (
                  <div className="flex justify-center mb-3">
                    <span className="w-5 h-5 border-2 border-coral-200 border-t-coral-500 rounded-full animate-spin" />
                  </div>
                )}

                <div className="flex items-center justify-between text-xs text-gray-400">
                  <button
                    onClick={() => { setStep('phone'); setCode(['','','','','','']); setError('') }}
                    className="hover:text-gray-600 transition-colors"
                  >
                    更换手机号
                  </button>
                  <button
                    onClick={handleSendCode}
                    disabled={countdown > 0 || loading}
                    className="disabled:opacity-40"
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
                initial={{ opacity: 0, scale: 0.96 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0 }}
                className="p-6"
              >
                <div className="text-center mb-5">
                  <div className="w-12 h-12 bg-emerald-100 rounded-full flex items-center justify-center mx-auto mb-3">
                    <Check className="w-6 h-6 text-emerald-500" />
                  </div>
                  <p className="font-semibold text-gray-800">欢迎加入 BreezeTravel！</p>
                  <p className="text-xs text-gray-400 mt-1">取一个旅行代号吧</p>
                </div>
                <input
                  type="text"
                  value={nickname}
                  onChange={e => setNickname(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && handleSetNickname()}
                  placeholder="你的旅行代号（可跳过）"
                  maxLength={20}
                  className="input-glass mb-4"
                  autoFocus
                />
                <button
                  onClick={handleSetNickname}
                  className="btn-coral w-full py-3 text-sm flex items-center justify-center gap-2"
                >
                  开始规划 <ArrowRight className="w-4 h-4" />
                </button>
              </motion.div>
            )}

          </AnimatePresence>
        </div>

        <p className="text-center text-[11px] text-gray-300 mt-6">
          登录即代表同意用户协议与隐私政策
        </p>
      </motion.div>
    </div>
  )
}
