'use client'

import { createContext, type ReactNode, useContext, useMemo, useState } from 'react'
import type { ActivityCardView } from '@/lib/trip-understanding-v3'
import { allocatePlacePhotos, placePhotoKey, placeTypePhoto } from '@/lib/place-type-photo'

const PhotoSelection = createContext<ReturnType<typeof allocatePlacePhotos> | null>(null)

export function PlacePhotoProvider({ days, children }: {
  days: { activities: ActivityCardView[] }[], children: ReactNode
}) {
  const photos = useMemo(() => allocatePlacePhotos(days.flatMap(day => day.activities.filter(card => card.status === 'READY'))), [days])
  return <PhotoSelection.Provider value={photos}>{children}</PhotoSelection.Provider>
}

export default function PlacePhoto({ card }: { card: ActivityCardView }) {
  const [failedSource, setFailedSource] = useState<string | null>(null)
  const [failedFallback, setFailedFallback] = useState<string | null>(null)
  const selection = useContext(PhotoSelection)
  if (card.status !== 'READY') return null
  const fallback = selection?.get(placePhotoKey(card)) || placeTypePhoto(card)
  const primary = card.photo_url
  const typed = !primary || failedSource === primary
  const src = typed ? fallback?.src : primary
  if (!src || (typed && failedFallback === src)) return null
  return <>
    {/* Local type photos are illustrative, never presented as POI evidence. */}
    {/* eslint-disable-next-line @next/next/no-img-element */}
    <img key={src} src={src} alt=""
      data-image-type={typed ? fallback!.type : 'poi'} loading="lazy" referrerPolicy="no-referrer"
      onError={() => typed ? setFailedFallback(src) : setFailedSource(src)}
      className="absolute inset-0 h-full w-full object-cover" />
    {typed && <span title="按地点类型展示的配图，并非该地点实拍" className="pointer-events-none absolute bottom-1 right-1 rounded bg-slate-950/65 px-1 py-0.5 text-[10px] leading-none text-white">配图</span>}
  </>
}
