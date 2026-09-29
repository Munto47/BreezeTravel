import type {ActivityCardView, UserFacingTripResult} from '@/lib/trip-understanding-v3'
import type {SourceLodgingCard} from '@/lib/confirmed-trip-view'
import {allocatePlacePhotos, placePhotoKey} from '@/lib/place-type-photo'
import {DAY_COLORS} from './result-presentation'

function lines(ctx: CanvasRenderingContext2D, text: string, width: number) {
  const output: string[] = []
  let line = ''
  for (const char of text) {
    if (line && ctx.measureText(line + char).width > width) {output.push(line); line = ''}
    line += char
  }
  if (line) output.push(line)
  return output
}

function loadPhoto(src: string): Promise<HTMLImageElement | null> {
  return new Promise(resolve => {
    const photo = new Image()
    photo.crossOrigin = 'anonymous'
    photo.referrerPolicy = 'no-referrer'
    const finish = (value: HTMLImageElement | null) => {
      clearTimeout(timer); photo.onload = null; photo.onerror = null; resolve(value)
    }
    const timer = setTimeout(() => finish(null), 8000)
    photo.onload = () => finish(photo)
    photo.onerror = () => finish(null)
    photo.src = src
  })
}

/** A shareable itinerary, independent from the editor and its check messages. */
export async function renderShareImage(result: UserFacingTripResult, sourceLodgings: SourceLodgingCard[], resource?: string) {
  await document.fonts.ready
  const cards = result.days.flatMap(day => day.activities.filter(card => card.status === 'READY'))
  const allocation = allocatePlacePhotos(cards)
  const photos = new Map<string, {image: HTMLImageElement | null; illustration: boolean}>()
  // Bound decoding/connections for the 160-place case; one fetch per unique photo.
  const downloads = new Map<string, Promise<HTMLImageElement | null>>()
  const load = (src: string) => {
    if (!downloads.has(src)) downloads.set(src, loadPhoto(src))
    return downloads.get(src)!
  }
  let next = 0
  await Promise.all(Array.from({length: Math.min(6, cards.length)}, async () => {
    while (next < cards.length) {
      const card = cards[next++]
      let image = card.photo_url ? await load(card.photo_url) : null
      if(!image && card.photo_url && resource) image=await load(`/api/v3/trip-understandings/${encodeURIComponent(resource)}/photo?activity_token=${encodeURIComponent(card.activity_token)}`)
      const illustration = !image
      const fallback = allocation.get(placePhotoKey(card))
      if (!image && fallback) image = await load(fallback.src)
      photos.set(card.activity_token, {image, illustration})
    }
  }))
  const width = 1440, padding = 48, gap = 22, columns = 5
  const cardWidth = (width - padding * 2 - 48 - gap * (columns - 1)) / columns
  const canvas = document.createElement('canvas')
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('CANVAS_UNAVAILABLE')
  ctx.font = '600 19px "Microsoft YaHei", sans-serif'
  const days = result.days.map(day => {
    const visible = day.activities.filter(card => card.status === 'READY')
    const names = visible.map(card => lines(ctx, card.name, cardWidth - 24))
    const cardHeight = 165 + Math.max(1, ...names.map(name => name.length)) * 26
    const visitLabel = (card: ActivityCardView, index: number) =>
      `${visible.filter(other => other.name === card.name).length > 1 ? `第 ${index + 1} 站 · ` : ''}${card.name}`
    const notes = [
      ...visible.flatMap((card, index) => card.note ? [`${visitLabel(card, index)}：${card.note}`] : []),
      ...visible.flatMap((card, index) => card.source_details?.length
        ? [`${visitLabel(card, index)}：${card.source_details.filter(detail => !detail.optional).map(detail => detail.name).join(' → ')}`] : []),
      ...(day.source_notes || []).map(note => note.text),
      ...(day.meal_slots || []).filter(slot => slot.selection_status !== 'SELECTED' && slot.preference_text).map(slot => slot.preference_text!),
    ].filter(Boolean)
    ctx.font = '400 17px "Microsoft YaHei", sans-serif'
    const noteLines = notes.flatMap(note => lines(ctx, note, width - padding * 2 - 48))
    ctx.font = '600 19px "Microsoft YaHei", sans-serif'
    const rows = Math.ceil(visible.length / columns)
    return {visible, names, cardHeight, noteLines, height: 88 + rows * (cardHeight + gap) + noteLines.length * 27 + (noteLines.length ? 20 : 0)}
  })
  const stays = new Set<string>()
  sourceLodgings.filter(card => card.status === 'READY').forEach(card => stays.add(
    `${card.name}${card.overnight_days?.length ? ` · ${card.overnight_days.map(n => `第 ${n} 晚`).join('、')}` : ''}`))
  const staySegments = result.stay.segments?.length ? result.stay.segments : [{candidates: result.stay.candidates, overnight_days: []}]
  staySegments.forEach(segment => segment.candidates.filter(card => card.selected).forEach(card => stays.add(
    `${card.name}${segment.overnight_days?.length ? ` · ${segment.overnight_days.join('、')}` : ''}`)))
  ctx.font = '400 18px "Microsoft YaHei", sans-serif'
  const stayLines = [...stays].flatMap(stay => lines(ctx, stay, width - padding * 2 - 48))
  const height = 180 + days.reduce((sum, day) => sum + day.height + 24, 0) + (stayLines.length ? 85 + stayLines.length * 28 : 0) + 60
  // Downscale only extreme long trips, keeping the entire itinerary exportable.
  const scale = Math.min(1, 28000 / height, Math.sqrt(32000000 / (width * height)))
  canvas.width = Math.floor(width * scale); canvas.height = Math.ceil(height * scale)
  ctx.scale(scale, scale)
  ctx.fillStyle = '#f3f8fb'; ctx.fillRect(0, 0, width, height)
  const box = (x: number, y: number, w: number, h: number, fill: string, radius = 18) => {
    ctx.fillStyle = fill; ctx.beginPath(); ctx.roundRect(x, y, w, h, radius); ctx.fill()
  }
  const text = (value: string, x: number, y: number, font: string, color = '#17354b') => {
    ctx.font = `${font} "Microsoft YaHei", sans-serif`; ctx.fillStyle = color; ctx.fillText(value, x, y)
  }
  text('行程查  /  TRIPCHECK', padding, 48, '600 16px', '#1594a8')
  text((result.assumptions.find(item => item.key === 'destination')?.value || '我的旅行') + ` · ${days.length} 天`, padding, 104, '700 36px')
  text(`${cards.length} 个地点，一路好风景`, padding, 141, '400 18px', '#647c8b')
  let y = 180
  days.forEach((day, dayIndex) => {
    box(padding, y, width - padding * 2, day.height, '#ffffff', 22)
    const color = DAY_COLORS[dayIndex % DAY_COLORS.length]
    box(padding + 24, y + 26, 6, 25, color, 3)
    text(`Day ${dayIndex + 1}`, padding + 45, y + 47, '700 25px', color)
    day.visible.forEach((card: ActivityCardView, i) => {
      const x = padding + 24 + (i % columns) * (cardWidth + gap)
      const top = y + 76 + Math.floor(i / columns) * (day.cardHeight + gap)
      box(x, top, cardWidth, day.cardHeight, '#f7fafc', 13)
      const photo = photos.get(card.activity_token)
      if (photo?.image) {
        const img = photo.image, ratio = Math.max(cardWidth / img.width, 140 / img.height)
        ctx.save(); ctx.beginPath(); ctx.roundRect(x, top, cardWidth, 140, 12); ctx.clip()
        ctx.drawImage(img, x + (cardWidth - img.width * ratio) / 2, top + (140 - img.height * ratio) / 2, img.width * ratio, img.height * ratio)
        ctx.restore()
        if (photo.illustration) {box(x + cardWidth - 42, top + 116, 36, 18, '#17354bcc', 4); text('配图', x + cardWidth - 36, top + 129, '400 11px', '#fff')}
      } else {
        box(x, top, cardWidth, 140, '#e9f1f5', 12)
        text(card.category, x + 18, top + 84, '500 24px', '#668494')
      }
      box(x + 10, top + 10, 30, 30, color, 15); text(String(i + 1), x + 19 - (i >= 9 ? 4 : 0), top + 31, '700 16px', '#fff')
      day.names[i].forEach((name, line) => text(name, x + 12, top + 168 + line * 26, '600 19px'))
      if (i % columns !== columns - 1 && i < day.visible.length - 1) text('›', x + cardWidth + 5, top + 90, '400 23px', '#91a6b2')
    })
    const noteY = y + 83 + Math.ceil(day.visible.length / columns) * (day.cardHeight + gap)
    day.noteLines.forEach((note, i) => text(note, padding + 24, noteY + i * 27, '400 17px', '#5e7381'))
    y += day.height + 24
  })
  if (stayLines.length) {
    box(padding, y, width - padding * 2, 65 + stayLines.length * 28, '#fff', 18)
    text('住宿', padding + 24, y + 34, '600 21px')
    stayLines.forEach((stay, i) => text(stay, padding + 24, y + 66 + i * 28, '400 18px'))
  }
  return canvas
}
