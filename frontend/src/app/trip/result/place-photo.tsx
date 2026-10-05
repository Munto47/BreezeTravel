'use client'

import { createContext, type ReactNode, useContext, useMemo, useState } from 'react'
import type { ActivityCardView } from '@/lib/trip-understanding-v3'
import { allocatePlacePhotos, placePhotoKey, placeCoverPhoto } from '@/lib/place-type-photo'

const PhotoSelection = createContext<ReturnType<typeof allocatePlacePhotos> | null>(null)

export function PlacePhotoProvider({ days, children }: {
  days: { activities: ActivityCardView[] }[], children: ReactNode
}) {
  const photos = useMemo(() => allocatePlacePhotos(days.flatMap(day => day.activities)), [days])
  return <PhotoSelection.Provider value={photos}>{children}</PhotoSelection.Provider>
}

export default function PlacePhoto({ card }: { card: ActivityCardView }) {
  const [failedSource, setFailedSource] = useState<string | null>(null)
  const [failedFallback, setFailedFallback] = useState<string | null>(null)
  const [loadedPhoto, setLoadedPhoto] = useState<{identity: string; src: string} | null>(null)
  const selection = useContext(PhotoSelection)
  const identity = `${card.city || ''}|${card.name}`
  const fallback = selection?.get(placePhotoKey(card)) || placeCoverPhoto(card)
  const primary = card.photo_url || (loadedPhoto?.identity === identity ? loadedPhoto.src : null)
  const photoReady = !!primary && failedSource !== primary && loadedPhoto?.identity === identity && loadedPhoto.src === primary
  return <>
    {/* Local type photos are illustrative, never presented as POI evidence. */}
    {/* eslint-disable-next-line @next/next/no-img-element */}
    {failedFallback !== fallback.src && <img src={fallback.src} alt=""
      data-image-type={fallback.type} loading="lazy" referrerPolicy="no-referrer"
      onError={() => setFailedFallback(fallback.src)}
      className="absolute inset-0 h-full w-full object-cover" />}
    {primary && failedSource !== primary && <img key={primary} src={primary} alt=""
      data-image-type="poi" loading="lazy" referrerPolicy="no-referrer"
      onLoad={() => setLoadedPhoto({identity, src: primary})}
      onError={() => setFailedSource(primary)}
      style={{opacity: photoReady ? 1 : 0}}
      className="absolute inset-0 h-full w-full object-cover" />}
    {!photoReady && <span title="旅行配图，并非该地点实拍" className="pointer-events-none absolute bottom-1 right-1 rounded bg-slate-950/65 px-1 py-0.5 text-[10px] leading-none text-white">配图</span>}
  </>
}
