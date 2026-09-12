import { MapPin, Route, ArrowRight } from 'lucide-react'
import { SketchMap } from './home-decor'

const DAYS = [
  {day: 'Day 1', names: '故宫博物院 → 景山公园 → 什刹海', image: 'historic', count: 3},
  {day: 'Day 2', names: '天坛公园 → 前门大街 → 大栅栏', image: 'street', count: 3},
  {day: 'Day 3', names: '颐和园 → 圆明园', image: 'park', count: 2},
]

export default function HomePreview() {
  return <section className="four-home-preview" aria-labelledby="home-preview-title" data-testid="home-example-preview">
    <div className="four-preview-cards">
      <div className="four-preview-heading"><h2 id="home-preview-title"><Route size={15} aria-hidden="true"/>生成效果示例</h2><span><MapPin size={13} aria-hidden="true"/>北京 · 3 天</span></div>
      <div className="four-preview-days">{DAYS.map(day=><article className="four-preview-day" key={day.day}><strong>{day.day}</strong><small>{day.count} 个地点</small>
        {/* Category photos decorate this explicitly labelled example. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={`/place-types/${day.image}.jpg`} alt="" loading="lazy"/><p>{day.names}</p></article>)}</div>
    </div>
    <div className="four-preview-map"><SketchMap detailed/><span>示意地图 · 非实际路线</span></div>
    <div className="four-preview-list"><h3>北京 · 第 1 天</h3><ol><li><span>1</span>故宫博物院</li><li><span>2</span>景山公园</li><li><span>3</span>什刹海</li></ol><div>每天的先后，一目了然<ArrowRight size={15} aria-hidden="true"/></div></div>
    <p className="four-preview-disclaimer">静态展示示例，不是本次整理结果；图片仅为旅行装饰。</p>
  </section>
}
