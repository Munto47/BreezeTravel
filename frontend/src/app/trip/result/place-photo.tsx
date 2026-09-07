'use client'

import { useState } from 'react'
import type { ActivityCardView } from '@/lib/trip-understanding-v3'
import { placeTypePhoto } from '@/lib/place-type-photo'

export default function PlacePhoto({ card }: { card: ActivityCardView }) {
  const [failedSource, setFailedSource] = useState<string | null>(null)
  const [failedFallback, setFailedFallback] = useState<string | null>(null)
  if (card.status !== 'READY') return null
  const fallback = placeTypePhoto(card)
  const primary = card.photo_url
  const typed = !primary || failedSource === primary
  const src = typed ? fallback?.src : primary
  if (!src || (typed && failedFallback === src)) return null
  return <>
    {/* Local type photos are illustrative, never presented as POI evidence. */}
    {/* eslint-disable-next-line @next/next/no-img-element */}
    <img key={src} src={src} alt={typed ? `${fallback!.label}类型配图，非该地点实拍` : ''}
      data-image-type={typed ? fallback!.type : 'poi'} loading="lazy" referrerPolicy="no-referrer"
      onError={() => typed ? setFailedFallback(src) : setFailedSource(src)}
      className="absolute inset-0 h-full w-full object-cover" />
    {typed && <span className="absolute bottom-1.5 right-2 rounded bg-black/45 px-1.5 py-0.5 text-[10px] text-white" title="同类型场景配图，非该地点实拍">类型配图</span>}
  </>
}
