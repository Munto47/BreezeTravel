import Link from 'next/link'
import { ArrowRight, ClipboardPaste, MapPinned, Layers3, Save, ImageDown, Check } from 'lucide-react'
import ExperienceHeader from '@/components/experience/experience-header'
import '../experience.css'

export const metadata = {title: '使用指南 · 行程查', description: '从粘贴文字到调整行程、查看地图、保存和导出，了解行程查的使用方式。'}

const STEPS = [
  {Icon: ClipboardPaste, title: '把攻略文字贴进来', body: '复制已有攻略或自己写的旅行想法。写清第几天、先去哪里、再去哪里；也可以先填入首页示例，再改成自己的安排。', hint: '目前仅接收文字。链接和图片需要先整理成文字。'},
  {Icon: Layers3, title: '核对每天的安排', body: '整理后查看每日地点和原文安排。备选地点单独保留，不会直接加入主线；地点不明确时，打开待确认内容选择正确地点。', hint: '看到“尚未整理完成”时，请对照原文补全，不要把已显示的部分当作全部结果。'},
  {Icon: MapPinned, title: '调整顺序，查看地图', body: '可以添加、替换、删除地点，或把卡片移到其他日期。改动后手动更新路线，再在地图上查看地点和连接。', hint: '按相对先后安排行程，不需要填写时刻或日历日期。没有查到的路线不会编造。'},
  {Icon: Save, title: '保存自己的版本', body: '改动后可以撤销。登录后将行程保存到账号，再从“我的行程”继续编辑；离开前留意页面的保存状态。', hint: '如果提示保存结果尚未确认，先使用页面的恢复入口确认结果，避免重复操作。'},
  {Icon: ImageDown, title: '带走一张行程图', body: '点击“导出图片”，检查预览后下载 PNG。图片会保留全部旅行日，以及备选和未完成提示，方便随时查看。', hint: '图片导出不包含地图。攻略中的内部安排和示意照片不代表地点或开放情况已单独核验。'},
]

export default function GuidePage() {
  return <main className="four-guide">
    <ExperienceHeader active="guide"/>
    <div className="four-guide-content">
      <div className="four-guide-intro"><span className="four-eyebrow">从一段文字开始</span><h1>让旅行安排，清楚一点。</h1><p>不用先填一大张表。先放下想法，再把每天慢慢安排好。</p><Link href="/" className="four-primary">开始整理行程<ArrowRight size={18} aria-hidden="true"/></Link></div>
      <ol className="four-guide-steps">{STEPS.map(({Icon,title,body,hint},index)=><li key={title}><div className="four-guide-step-icon"><Icon aria-hidden="true"/><span>{index+1}</span></div><div><h2>{title}</h2><p>{body}</p><p className="four-guide-hint"><Check size={15} aria-hidden="true"/>{hint}</p></div></li>)}</ol>
      <div className="four-guide-footer"><p>已经有了第一版？下次从“我的行程”继续。</p><Link href="/my-trips">打开我的行程<ArrowRight size={16} aria-hidden="true"/></Link></div>
    </div>
    <footer className="four-home-footer"><span>让每一次旅行，都更简单、更从容。</span><Link href="/about#privacy">隐私与数据</Link></footer>
  </main>
}
