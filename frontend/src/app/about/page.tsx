'use client'

import { useRouter } from 'next/navigation'
import { motion } from 'framer-motion'
import {
  Compass, ArrowLeft, Building2, Globe, Mail,
  Sparkles, Route, MessageSquare, ShieldCheck, Cpu, Database,
  MapPin, FileCheck2, RefreshCw,
} from 'lucide-react'

export default function AboutPage() {
  const router = useRouter()

  const techStack = [
    { icon: <Cpu className="w-3.5 h-3.5" />, label: 'FastAPI 模块化单体' },
    { icon: <Database className="w-3.5 h-3.5" />, label: 'PostgreSQL 权威状态' },
    { icon: <ShieldCheck className="w-3.5 h-3.5" />, label: 'AuditEngine 确定性核验' },
    { icon: <FileCheck2 className="w-3.5 h-3.5" />, label: 'Provider Evidence Receipt' },
    { icon: <RefreshCw className="w-3.5 h-3.5" />, label: 'Revision 与完整 postcheck' },
    { icon: <Route className="w-3.5 h-3.5" />, label: '地点与路线证据绑定' },
  ]

  return (
    <div className="min-h-screen bg-gradient-to-br from-coral-50/40 via-white to-blue-50/30 p-4 overflow-auto">
      {/* 背景装饰 */}
      <div className="fixed inset-0 pointer-events-none overflow-hidden">
        <div className="absolute -top-40 -right-40 w-96 h-96 bg-coral-100/30 rounded-full blur-3xl" />
        <div className="absolute -bottom-20 -left-20 w-72 h-72 bg-blue-100/30 rounded-full blur-3xl" />
      </div>

      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
        className="w-full max-w-2xl mx-auto relative z-10 py-6"
      >
        {/* 顶栏 */}
        <div className="flex items-center justify-between mb-6">
          <button
            onClick={() => router.push('/')}
            className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-coral-500 transition-colors px-3 py-1.5 rounded-xl hover:bg-coral-50 border border-gray-200"
          >
            <ArrowLeft className="w-3.5 h-3.5" />
            返回首页
          </button>
          <div className="flex items-center gap-2">
            <div className="w-9 h-9 rounded-xl bg-coral-500 flex items-center justify-center shadow-md shadow-coral-200">
              <Compass className="w-4.5 h-4.5 text-white" strokeWidth={2} />
            </div>
            <span className="text-sm font-bold text-gray-900">BreezeTravel</span>
          </div>
        </div>

        {/* Hero */}
        <div className="glass-panel-solid rounded-2xl shadow-glass p-8 mb-4 text-center">
          <div className="inline-flex items-center gap-1.5 text-[10px] text-coral-500 bg-coral-50 px-3 py-1 rounded-full mb-3 border border-coral-100">
            <Sparkles className="w-3 h-3" /> 关于本项目
          </div>
          <h1 className="text-2xl font-bold text-gray-900 mb-2">BreezeTravel · 微风出行</h1>
          <p className="text-sm text-gray-500 leading-relaxed max-w-md mx-auto">
            帮助 2～5 人核验北京、上海或杭州的 2～5 天单城市行程，发现地点、时间、交通、住宿、天气与风险问题。
          </p>
        </div>

        {/* 产品特性 */}
        <div className="glass-panel-solid rounded-2xl shadow-glass p-6 mb-4">
          <p className="text-[11px] font-medium text-gray-500 mb-3 uppercase tracking-wider">产品特性</p>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {[
              { icon: <FileCheck2 className="w-4 h-4 text-coral-500" />, title: '有依据地核验', desc: '事实、Finding 与回执分开保存' },
              { icon: <ShieldCheck className="w-4 h-4 text-blue-500" />, title: '不确定性确认', desc: 'UNKNOWN 不伪装成通过' },
              { icon: <RefreshCw className="w-4 h-4 text-emerald-500" />, title: '修改可追溯', desc: '新 Revision 后完整 postcheck' },
            ].map(f => (
              <div key={f.title} className="bg-gray-50/80 rounded-xl p-3 border border-gray-100">
                <div className="flex items-center gap-1.5 mb-1">{f.icon}<span className="text-sm font-semibold text-gray-800">{f.title}</span></div>
                <p className="text-xs text-gray-400">{f.desc}</p>
              </div>
            ))}
          </div>
        </div>

        {/* 开发主体 / 备案信息 */}
        <div className="glass-panel-solid rounded-2xl shadow-glass p-6 mb-4">
          <p className="text-[11px] font-medium text-gray-500 mb-3 uppercase tracking-wider">开发主体 & 网站信息</p>
          <div className="space-y-3">
            <InfoRow icon={<Building2 className="w-4 h-4 text-coral-500" />} label="开发主体">
              <span className="font-semibold text-gray-900">新余高新区微风软件工作室</span>
              <span className="ml-2 text-[11px] text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">个体工商户</span>
            </InfoRow>
            <InfoRow icon={<ShieldCheck className="w-4 h-4 text-coral-500" />} label="统一社会信用代码">
              <code className="text-gray-700 font-mono text-sm bg-gray-50 px-2 py-0.5 rounded border border-gray-100">92360504MAEM4YY03C</code>
            </InfoRow>
            <InfoRow icon={<MapPin className="w-4 h-4 text-blue-500" />} label="注册地">
              <span className="text-gray-700">江西省新余市 高新技术产业开发区</span>
            </InfoRow>
            <InfoRow icon={<Globe className="w-4 h-4 text-emerald-500" />} label="官方网址">
              <a
                href="https://www.breezetravel.cn"
                target="_blank"
                rel="noopener noreferrer"
                className="text-coral-500 hover:underline font-mono"
              >
                www.breezetravel.cn
              </a>
            </InfoRow>
            <InfoRow icon={<ShieldCheck className="w-4 h-4 text-amber-500" />} label="ICP 备案号">
              <a
                href="https://beian.miit.gov.cn/"
                target="_blank"
                rel="noopener noreferrer"
                className="text-gray-700 font-mono text-sm hover:text-coral-500 hover:underline"
              >
                赣ICP备2026008973号-2
              </a>
            </InfoRow>
          </div>
        </div>

        {/* 技术栈 */}
        <div className="glass-panel-solid rounded-2xl shadow-glass p-6 mb-4">
          <p className="text-[11px] font-medium text-gray-500 mb-3 uppercase tracking-wider">核心技术栈</p>
          <div className="flex flex-wrap gap-2">
            {techStack.map(t => (
              <span
                key={t.label}
                className="inline-flex items-center gap-1.5 text-xs text-gray-600 bg-gray-50 px-3 py-1.5 rounded-full border border-gray-100"
              >
                {t.icon}{t.label}
              </span>
            ))}
          </div>
        </div>

        {/* 知识产权声明 */}
        <div className="glass-panel-solid rounded-2xl shadow-glass p-6 mb-4">
          <p className="text-[11px] font-medium text-gray-500 mb-3 uppercase tracking-wider">知识产权声明</p>
          <div className="space-y-2 text-xs text-gray-600 leading-relaxed">
            <p>
              本产品（BreezeTravel · 微风出行）由
              <span className="font-semibold text-gray-900"> 新余高新区微风软件工作室 </span>
              独立设计、开发并享有完整著作权。
            </p>
            <p>
              产品名称 <span className="font-mono text-coral-500">BreezeTravel</span>、域名
              <span className="font-mono text-coral-500"> www.breezetravel.cn</span>、相关源代码与 UI 设计
              均为本工作室依法持有或在相应许可范围内使用的资产。
            </p>
            <p>
              未经书面授权，任何单位或个人不得复制、分发、二次开发或用于商业用途。
            </p>
          </div>
        </div>

        {/* 联系方式 */}
        <div className="glass-panel-solid rounded-2xl shadow-glass p-6 mb-6">
          <p className="text-[11px] font-medium text-gray-500 mb-3 uppercase tracking-wider">联系我们</p>
          <div className="space-y-2">
            <InfoRow icon={<Mail className="w-4 h-4 text-coral-500" />} label="商务合作">
              <span className="text-gray-700">通过官网联系表单提交</span>
            </InfoRow>
            <InfoRow icon={<MessageSquare className="w-4 h-4 text-gray-700" />} label="技术反馈">
              <span className="text-gray-700">访问 www.breezetravel.cn 留言</span>
            </InfoRow>
          </div>
        </div>

        <p className="text-center text-[11px] text-gray-300 mb-2">
          © 2026 新余高新区微风软件工作室 · BreezeTravel All Rights Reserved.
        </p>
        <p className="text-center text-[11px] text-gray-400 mb-1">
          <a
            href="https://beian.miit.gov.cn/"
            target="_blank"
            rel="noopener noreferrer"
            className="hover:text-coral-500 hover:underline"
          >
            赣ICP备2026008973号-2
          </a>
        </p>
        <p className="text-center text-[10px] text-gray-300">
          当前能力与证据等级以只读候选 evidence 中的披露为准；自动验证不等于真人证据。
        </p>
      </motion.div>
    </div>
  )
}

function InfoRow({ icon, label, children }: { icon: React.ReactNode; label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-3 py-1.5">
      <div className="w-7 h-7 rounded-lg bg-white border border-gray-100 flex items-center justify-center flex-shrink-0 shadow-sm">
        {icon}
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-[10px] text-gray-400 uppercase tracking-wider mb-0.5">{label}</p>
        <div className="text-sm">{children}</div>
      </div>
    </div>
  )
}
