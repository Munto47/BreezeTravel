'use client'

import { useRouter } from 'next/navigation'
import type { LucideIcon } from 'lucide-react'
import {
  ArrowLeft,
  Building2,
  Compass,
  FileText,
  MapPinned,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'


export default function AboutPage() {
  const router = useRouter()

  return (
    <main className="min-h-screen bg-[#f8f7f2] px-5 py-6 text-slate-900 sm:px-8">
      <div className="mx-auto w-full max-w-3xl">
        <header className="flex items-center justify-between border-b border-slate-200 pb-5">
          <button type="button" onClick={() => router.push('/')} className="inline-flex items-center gap-2 rounded-full px-3 py-2 text-sm text-slate-600 transition hover:bg-white">
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
            返回首页
          </button>
          <div className="flex items-center gap-2 text-sm font-semibold">
            <Compass className="h-4 w-4 text-emerald-700" aria-hidden="true" />
            BreezeTravel · 行程查
          </div>
        </header>

        <section className="py-10 sm:py-14">
          <div className="inline-flex items-center gap-2 rounded-full bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-800">
            <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
            少填表，先把行程看明白
          </div>
          <h1 className="mt-5 text-3xl font-semibold tracking-tight sm:text-5xl">把一段攻略整理成每天都能照着走的卡片</h1>
          <p className="mt-5 max-w-2xl text-base leading-7 text-slate-600">
            用户粘贴攻略或上传截图后，系统按天整理地点，给出路线、住宿和少量可直接采纳的建议。北京、上海或杭州会提供更深入的地点与路线核对；其他国内城市先提供基础整理，并如实提示能力边界。
          </p>
          <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-500">新版不再把同行人数限制为 2～5 人；当前示例只把 2 人作为可编辑的软假设。</p>

          <div className="mt-9 grid gap-4 sm:grid-cols-3">
            <Feature icon={FileText} title="直接给攻略" text="不要求先选城市、日期、人数，也不用理解项目术语。" />
            <Feature icon={MapPinned} title="先看每日卡片" text="描述句、网址和明确排除的地点不会混入行程。" />
            <Feature icon={ShieldCheck} title="不确定就说明" text="缺少数据时保留待确认，不把猜测说成确定事实。" />
          </div>
        </section>

        <section id="privacy" className="scroll-mt-6 rounded-3xl border border-slate-200 bg-white p-6 shadow-lg shadow-slate-200/40 sm:p-8">
          <h2 className="text-xl font-semibold">隐私与体验数据</h2>
          <div className="mt-4 space-y-3 text-sm leading-6 text-slate-600">
            <p>当前北京示例由服务端固定提供，不会上传你的攻略，也不会调用真实地图或外部智能服务。</p>
            <p>匿名体验由浏览器里的安全会话保护，结果最多保留 24 小时；随机访问地址本身不能替代访问权限。</p>
            <p>未来的攻略、截图和聊天默认不会进入长期记忆。截图处理结束后会清理原图，只保留必要的清理记录。</p>
          </div>
        </section>

        <section className="mt-5 rounded-3xl border border-slate-200 bg-white p-6 sm:p-8">
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-slate-100">
              <Building2 className="h-5 w-5 text-slate-600" aria-hidden="true" />
            </div>
            <div>
              <h2 className="font-semibold">开发主体</h2>
              <p className="mt-1 text-sm text-slate-600">新余高新区微风软件工作室</p>
              <p className="mt-1 text-xs text-slate-400">赣ICP备2026008973号-2</p>
            </div>
          </div>
        </section>

        <footer className="py-8 text-center text-xs text-slate-400">© 2026 BreezeTravel</footer>
      </div>
    </main>
  )
}


function Feature({ icon: Icon, title, text }: { icon: LucideIcon; title: string; text: string }) {
  return (
    <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
      <Icon className="h-5 w-5 text-emerald-700" aria-hidden="true" />
      <h2 className="mt-4 font-semibold">{title}</h2>
      <p className="mt-2 text-sm leading-6 text-slate-500">{text}</p>
    </div>
  )
}
