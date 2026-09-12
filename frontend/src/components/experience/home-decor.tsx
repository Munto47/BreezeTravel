import { MapPin, Mountain, Sparkles, Utensils, BedDouble } from 'lucide-react'

export function SketchMap({ detailed = false }: {detailed?: boolean}) {
  return <svg className="four-sketch-map" viewBox="0 0 320 215" fill="none" aria-hidden="true">
    <rect width="320" height="215" rx="20" fill="#f3f7f4"/>
    <path d="M0 40 320 120M0 100 320 180M45 0 120 215M130 0 205 215M220 0 295 215" stroke="#e3ebe3" strokeWidth="23"/>
    <path d="m0 165 320-60M0 75 320-60M66 0 8 215M180 0l-15 215M278 0l-35 215" stroke="white" strokeWidth="8"/>
    <path d="M315 0c-32 35-31 59-65 75s-61 24-72 67-9 56-35 73" stroke="#d1eaf1" strokeWidth="20"/>
    <path d="m92 67 0 69 96 0 0 35 61 0" stroke="#4baff1" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" strokeDasharray={detailed ? undefined : '7 6'}/>
    {[[92,67],[188,105],[249,171]].map(([x,y],index)=><g key={index} transform={`translate(${x},${y})`}><path d="M0 10s-13-12-13-21a13 13 0 0 1 26 0C13-2 0 10 0 10Z" fill="#229aee" stroke="white" strokeWidth="2"/><text x="0" y="-7" textAnchor="middle" dominantBaseline="middle" fill="white" fontSize="12" fontWeight="700">{index+1}</text></g>)}
  </svg>
}

export default function HomeDecor() {
  return <div className="four-home-decor" aria-hidden="true">
    <div className="four-cloud four-cloud-left"/><div className="four-cloud four-cloud-right"/>
    <div className="four-note four-note-left">一段文字<br/>把想去的地方<br/>慢慢串起来<svg viewBox="0 0 100 65" fill="none"><path d="M5 3C8 33 28 53 83 52m-12-9 14 10-14 9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg></div>
    <div className="four-photo-stack">
      {/* Licensed category photography is decorative, not a photo of a named stop. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <div className="four-polaroid four-polaroid-back"><img src="/place-types/modern.jpg" alt=""/><span>GO SOMEWHERE NEW</span></div>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <div className="four-polaroid four-polaroid-front"><img src="/place-types/historic.jpg" alt=""/><span>A LITTLE ADVENTURE</span></div>
    </div>
    <div className="four-note four-note-right">你的旅行想法<br/>值得好好安排<svg viewBox="0 0 110 65" fill="none"><path d="M76 4c-3 39-44 37-37 15 8-20 32 32-22 42" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></svg></div>
    <div className="four-map-paper"><SketchMap/><MapPin className="four-paper-pin" fill="currentColor" stroke="white" strokeWidth={1.5}/></div>
    <div className="four-category-note"><span><Mountain/>想去的景点</span><span><Utensils/>想吃的美食</span><span><BedDouble/>沿途的住宿</span></div>
    <Sparkles className="four-spark four-spark-one"/><Sparkles className="four-spark four-spark-two"/>
    <svg className="four-leaves" viewBox="0 0 180 360" fill="none"><path d="M-5 365C90 246 7 170 87 18" stroke="#708c63" strokeWidth="9"/>{[[44,267,-35],[39,204,38],[45,144,-35],[65,85,30],[8,326,42]].map(([x,y,angle],index)=><ellipse key={index} cx={x} cy={y} rx="26" ry="67" fill={index%2?'#97b686':'#789c69'} transform={`rotate(${angle} ${x} ${y})`}/>)}</svg>
  </div>
}
