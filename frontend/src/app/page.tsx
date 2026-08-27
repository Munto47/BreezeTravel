'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { ArrowRight, CheckCircle2, Compass, FileText, Map, ShieldCheck, Sparkles } from 'lucide-react'

import { createDemoTripUnderstanding, createFullTripUnderstanding } from '@/lib/trip-understanding-v3'
import { useAuthStore } from '@/stores/authStore'


export default function HomePage() {
  const router = useRouter()
  const { user, isHydrated, hydrate } = useAuthStore()
  const [isStarting, setIsStarting] = useState(false)
  const [isCreating, setIsCreating] = useState(false)
  const [sourceText, setSourceText] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    hydrate()
  }, [hydrate])

  const rememberAcceptedResource = (publicResourceId: string) => {
    sessionStorage.setItem('bt_active_trip_ref', publicResourceId)
    sessionStorage.removeItem('bt_active_trip_event_cursor')
    sessionStorage.removeItem('bt_active_trip_etag')
  }

  const startDemo = async () => {
    if (isStarting) return
    setIsStarting(true)
    setError('')
    try {
      const accepted = await createDemoTripUnderstanding()
      rememberAcceptedResource(accepted.public_resource_id)
      router.push('/trip/result')
    } catch {
      setError('暂时没有启动成功，请稍后再试。')
      setIsStarting(false)
    }
  }

  const createFromText = async () => {
    if (isCreating) return
    if (!user) {
      router.push('/login')
      return
    }
    const text = sourceText.trim()
    if (!text) {
      setError('请先粘贴攻略或行程文字。')
      return
    }
    setIsCreating(true)
    setError('')
    try {
      const accepted = await createFullTripUnderstanding(text)
      rememberAcceptedResource(accepted.public_resource_id)
      setSourceText('')
      router.push('/trip/result')
    } catch (createError) {
      if (createError instanceof Error && createError.message === 'ACTIVE_LIMIT_REACHED') {
        setError('已有两份行程正在整理，请稍后再试。')
      } else if (createError instanceof Error && createError.message === 'LOGIN_REQUIRED') {
        setError('登录状态已失效，请重新登录。')
      } else {
        setError('暂时没有整理成功，文字仍保留在当前页面，可以稍后重试。')
      }
      setIsCreating(false)
    }
  }

  return (
    <main className="min-h-screen overflow-hidden bg-[#f8f7f2] text-slate-900">
      <div className="pointer-events-none fixed inset-0">
        <div className="absolute -right-24 -top-28 h-96 w-96 rounded-full bg-amber-200/40 blur-3xl" />
        <div className="absolute -bottom-32 -left-24 h-96 w-96 rounded-full bg-emerald-200/35 blur-3xl" />
      </div>

      <div className="relative mx-auto flex min-h-screen w-full max-w-6xl flex-col px-5 py-6 sm:px-8 lg:px-12">
        <header className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-slate-900 text-white shadow-lg shadow-slate-300/60">
              <Compass className="h-5 w-5" aria-hidden="true" />
            </div>
            <div>
              <p className="text-sm font-semibold tracking-tight">BreezeTravel</p>
              <p className="text-xs text-slate-500">行程查</p>
            </div>
          </div>
          <nav className="flex items-center gap-2 text-sm" aria-label="辅助导航">
            <button type="button" onClick={() => router.push('/about')} className="rounded-full px-4 py-2 text-slate-600 transition hover:bg-white hover:text-slate-900">
              关于
            </button>
            <button type="button" onClick={() => router.push(user ? '/profile' : '/login')} className="rounded-full border border-slate-300 bg-white/80 px-4 py-2 font-medium text-slate-700 transition hover:border-slate-500">
              {isHydrated && user ? user.nickname : '登录'}
            </button>
          </nav>
        </header>

        <section className="grid flex-1 items-center gap-12 py-12 lg:grid-cols-[1.05fr_0.95fr] lg:py-16">
          <div className="max-w-2xl">
            <div className="mb-6 inline-flex items-center gap-2 rounded-full border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-800">
              <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
              不填表，先看看行程卡片
            </div>
            <h1 className="text-4xl font-semibold leading-[1.12] tracking-[-0.04em] text-slate-950 sm:text-5xl lg:text-6xl">
              把攻略变成
              <span className="block text-emerald-700">每天都能照着走的卡片</span>
            </h1>
            <p className="mt-6 max-w-xl text-base leading-7 text-slate-600 sm:text-lg">
              登录后直接粘贴自己的长文本；没有把握的地点会留给你确认。未登录也可以先用固定北京三日示例体验同一条整理、地点核对和结果恢复链路。
            </p>

            {isHydrated && user && (
              <div className="mt-7 rounded-3xl border border-slate-200 bg-white/85 p-4 shadow-lg shadow-slate-200/40 backdrop-blur">
                <label htmlFor="trip-source" className="text-sm font-semibold text-slate-800">粘贴攻略或行程文字</label>
                <p className="mt-1 text-xs leading-5 text-slate-500">不需要先选城市、日期或人数；最多 50,000 个字符。</p>
                <textarea
                  id="trip-source"
                  data-testid="trip-source-text"
                  value={sourceText}
                  onChange={(event) => setSourceText(event.target.value)}
                  maxLength={50_000}
                  rows={7}
                  placeholder={'例如：\nDay 1 去故宫博物院、景山公园\nDay 2 去天坛公园、前门大街'}
                  className="mt-3 w-full resize-y rounded-2xl border border-slate-200 bg-[#fbfaf7] px-4 py-3 text-sm leading-6 outline-none transition placeholder:text-slate-400 focus:border-emerald-500 focus:ring-2 focus:ring-emerald-100"
                />
                <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                  <span className="text-xs text-slate-400">{sourceText.length.toLocaleString()} / 50,000</span>
                  <button
                    data-testid="create-full-trip"
                    type="button"
                    onClick={createFromText}
                    disabled={isCreating}
                    className="inline-flex min-h-11 items-center justify-center gap-2 rounded-2xl bg-emerald-700 px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-emerald-800 disabled:cursor-wait disabled:opacity-70"
                  >
                    {isCreating ? '正在整理你的行程…' : '生成逐日卡片'}
                    {!isCreating && <ArrowRight className="h-4 w-4" aria-hidden="true" />}
                  </button>
                </div>
              </div>
            )}

            <div data-testid="home-no-prerequisites" className="mt-7 flex flex-wrap gap-2">
              {['不用先选城市', '不用填写日期', '不用填写人数', '不用创建房间'].map((label) => (
                <span key={label} className="inline-flex items-center gap-1.5 rounded-full bg-white px-3 py-1.5 text-xs text-slate-600 shadow-sm ring-1 ring-slate-200">
                  <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" aria-hidden="true" />
                  {label}
                </span>
              ))}
            </div>

            <div className="mt-9 flex flex-col items-start gap-3 sm:flex-row sm:items-center">
              <button
                data-testid="start-demo"
                type="button"
                onClick={startDemo}
                disabled={isStarting}
                className="inline-flex min-h-12 items-center justify-center gap-2 rounded-2xl bg-slate-950 px-6 py-3 text-sm font-semibold text-white shadow-xl shadow-slate-300 transition hover:-translate-y-0.5 hover:bg-slate-800 disabled:cursor-wait disabled:opacity-70"
              >
                {isStarting ? '正在准备北京示例…' : '体验北京三日卡片'}
                {!isStarting && <ArrowRight className="h-4 w-4" aria-hidden="true" />}
              </button>
              <p className="text-xs leading-5 text-slate-500">匿名体验仅保留 24 小时 · 不调用真实地图服务</p>
            </div>
            {error && <p role="alert" className="mt-4 rounded-xl bg-amber-50 px-4 py-3 text-sm text-amber-800">{error}</p>}
          </div>

          <div className="relative mx-auto w-full max-w-lg">
            <div className="absolute -inset-5 rounded-[2.5rem] bg-gradient-to-br from-amber-200/45 to-emerald-200/45 blur-2xl" />
            <div className="relative rounded-[2rem] border border-white/80 bg-white/90 p-5 shadow-2xl shadow-slate-300/45 backdrop-blur sm:p-7">
              <div className="mb-5 flex items-center justify-between">
                <div>
                  <p className="text-xs font-medium uppercase tracking-[0.18em] text-slate-400">北京 · 三日示例</p>
                  <p className="mt-1 text-lg font-semibold">先看清每天去哪里</p>
                </div>
                <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-emerald-50 text-emerald-700">
                  <FileText className="h-5 w-5" aria-hidden="true" />
                </div>
              </div>
              <div className="space-y-3">
                {[
                  ['Day 1', '故宫博物院', '景山公园'],
                  ['Day 2', '天坛公园', '前门大街'],
                  ['Day 3', '颐和园', '圆明园'],
                ].map(([day, first, second]) => (
                  <div key={day} className="rounded-2xl border border-slate-100 bg-[#fbfaf7] p-4">
                    <p className="mb-3 text-xs font-semibold text-emerald-700">{day}</p>
                    <div className="flex items-center gap-2 text-sm text-slate-700">
                      <span className="rounded-lg bg-white px-3 py-2 shadow-sm">{first}</span>
                      <ArrowRight className="h-3.5 w-3.5 text-slate-300" aria-hidden="true" />
                      <span className="rounded-lg bg-white px-3 py-2 shadow-sm">{second}</span>
                    </div>
                  </div>
                ))}
              </div>
              <div className="mt-4 grid grid-cols-2 gap-3 text-xs text-slate-500">
                <div className="flex items-center gap-2 rounded-xl bg-slate-50 p-3">
                  <Map className="h-4 w-4 text-slate-400" aria-hidden="true" />
                  地图状态如实显示
                </div>
                <div className="flex items-center gap-2 rounded-xl bg-slate-50 p-3">
                  <ShieldCheck className="h-4 w-4 text-slate-400" aria-hidden="true" />
                  不确定就请你确认
                </div>
              </div>
            </div>
          </div>
        </section>

        <footer className="flex flex-col gap-2 border-t border-slate-200/70 py-5 text-xs text-slate-500 sm:flex-row sm:items-center sm:justify-between">
          <p>© 2026 BreezeTravel · 新余高新区微风软件工作室</p>
          <div className="flex gap-4">
            <button type="button" onClick={() => router.push('/about#privacy')} className="hover:text-slate-900">隐私与数据</button>
            <button type="button" onClick={() => router.push('/about')} className="hover:text-slate-900">产品说明</button>
          </div>
        </footer>
      </div>
    </main>
  )
}
