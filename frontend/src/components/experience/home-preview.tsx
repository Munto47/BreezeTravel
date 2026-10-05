import { MapPin, Route, ArrowRight } from 'lucide-react'
import { SketchMap } from './home-decor'
import { HOME_EXAMPLES } from './home-examples'

const DAYS = HOME_EXAMPLES[0].days.map((names, index) => ({
  day: `Day ${index + 1}`, names: names.join(' → '),
  image: ['historic', 'street', 'park'][index], count: names.length,
}))

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
    <div className="four-preview-list"><h3>北京 · 第 1 天</h3><ol>{HOME_EXAMPLES[0].days[0].map((name, index) => <li key={name}><span>{index + 1}</span>{name}</li>)}</ol><div>每天的先后，一目了然<ArrowRight size={15} aria-hidden="true"/></div></div>
    <p className="four-preview-disclaimer">静态展示示例，不是本次整理结果；图片仅为旅行装饰。</p>
  </section>
}
