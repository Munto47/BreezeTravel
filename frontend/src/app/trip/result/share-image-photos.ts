import type {ActivityCardView} from '@/lib/trip-understanding-v3'
import {allocatePlacePhotos, placePhotoKey, placeCoverPhoto} from '@/lib/place-type-photo'

const PHOTO_BUDGET_MS = 8000
type ExportPhoto = {image: HTMLImageElement | null; illustration: boolean}

export function throwIfExportAborted(signal?: AbortSignal) {
  if (signal?.aborted) throw new DOMException('图片生成已取消', 'AbortError')
}

function loadPhoto(src: string, signal: AbortSignal): Promise<HTMLImageElement | null> {
  if (signal.aborted) return Promise.resolve(null)
  return new Promise(resolve => {
    const photo = new Image()
    photo.crossOrigin = 'anonymous'
    photo.referrerPolicy = 'no-referrer'
    let settled = false
    const finish = (value: HTMLImageElement | null) => {
      if (settled) return
      settled = true
      photo.onload = null
      photo.onerror = null
      signal.removeEventListener('abort', abort)
      if (!value) photo.removeAttribute('src')
      resolve(value)
    }
    const abort = () => finish(null)
    signal.addEventListener('abort', abort, {once: true})
    photo.onload = () => finish(photo)
    photo.onerror = () => finish(null)
    photo.src = src
  })
}

export async function prepareSharePhotos(cards: ActivityCardView[], resource?: string, signal?: AbortSignal) {
  throwIfExportAborted(signal)
  const controller = new AbortController()
  const cancel = () => controller.abort()
  signal?.addEventListener('abort', cancel, {once: true})
  const timer = setTimeout(cancel, PHOTO_BUDGET_MS)
  const photos = new Map<string, ExportPhoto>()
  const allocation = allocatePlacePhotos(cards)
  const downloads = new Map<string, Promise<HTMLImageElement | null>>()
  const load = (src: string) => {
    if (!downloads.has(src)) downloads.set(src, loadPhoto(src, controller.signal))
    return downloads.get(src)!
  }
  let next = 0
  try {
    await Promise.all(Array.from({length: Math.min(6, cards.length)}, async () => {
      while (next < cards.length && !controller.signal.aborted) {
        const card = cards[next++]
        let image = card.photo_url ? await load(card.photo_url) : null
        if (!image && card.photo_url && resource && !controller.signal.aborted)
          image = await load(`/api/v3/trip-understandings/${encodeURIComponent(resource)}/photo?activity_token=${encodeURIComponent(card.activity_token)}`)
        const illustration = !image
        const fallback = allocation.get(placePhotoKey(card)) || placeCoverPhoto(card)
        if (!image && fallback && !controller.signal.aborted) image = await load(fallback.src)
        photos.set(card.activity_token, {image, illustration})
      }
    }))
    throwIfExportAborted(signal)
    return photos
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', cancel)
    controller.abort()
  }
}

export async function prepareShareFonts(signal?: AbortSignal) {
  throwIfExportAborted(signal)
  await new Promise<void>(resolve => {
    const finish = () => {clearTimeout(timer); signal?.removeEventListener('abort', finish); resolve()}
    const timer = setTimeout(finish, 1000)
    signal?.addEventListener('abort', finish, {once: true})
    void document.fonts.ready.then(finish, finish)
  })
  throwIfExportAborted(signal)
}
