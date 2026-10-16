import type {ActivityCardView, UserFacingTripResult} from '@/lib/trip-understanding-v3'
import type {SourceLodgingCard} from '@/lib/confirmed-trip-view'
import {DAY_COLORS} from './result-presentation'
import {lodgingExportLines} from './lodging-export'
import {exportStatus, shareDayNotes, unassignedShareNotes, type ShareImageContentOptions} from './share-image-content'
import {prepareShareFonts, prepareSharePhotos, throwIfExportAborted} from './share-image-photos'

export type ShareImageStage = 'PHOTOS' | 'DRAWING' | 'ENCODING'
type ShareImageOptions = ShareImageContentOptions & {signal?: AbortSignal; onStage?: (stage: ShareImageStage) => void}

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

/** Share the current itinerary together with its unresolved arrangements. */
export async function renderShareImage(result: UserFacingTripResult, sourceLodgings: SourceLodgingCard[], resource?: string, options: ShareImageOptions = {}) {
  if (options.supplementary === null) throw new Error('SUPPLEMENTARY_NOT_READY')
  throwIfExportAborted(options.signal)
  const cards = result.days.flatMap(day => day.activities)
  options.onStage?.('PHOTOS')
  const [photos] = await Promise.all([prepareSharePhotos(cards, resource, options.signal), prepareShareFonts(options.signal)])
  throwIfExportAborted(options.signal)
  options.onStage?.('DRAWING')
  const width = 1440, padding = 48, gap = 22, columns = 5
  const cardWidth = (width - padding * 2 - 48 - gap * (columns - 1)) / columns
  const canvas = document.createElement('canvas')
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('CANVAS_UNAVAILABLE')
  ctx.font = '600 19px "Microsoft YaHei", sans-serif'
  const days = result.days.map((day, index) => {
    const visible = day.activities
    const names = visible.map(card => lines(ctx, card.name, cardWidth - 24))
    const cardHeight = 191 + Math.max(1, ...names.map(name => name.length)) * 26
    const notes = shareDayNotes(result, index, options)
    ctx.font = '400 17px "Microsoft YaHei", sans-serif'
    const noteLines = notes.flatMap(note => lines(ctx, note, width - padding * 2 - 48))
    ctx.font = '600 19px "Microsoft YaHei", sans-serif'
    const rows = Math.ceil(visible.length / columns)
    return {visible, names, cardHeight, noteLines, height: 88 + rows * (cardHeight + gap) + noteLines.length * 27 + (noteLines.length ? 20 : 0)}
  })
  ctx.font = '400 18px "Microsoft YaHei", sans-serif'
  const sections = [
    {title: '住宿', rows: lodgingExportLines(result, sourceLodgings).map(row => row.text)},
    {title: '未指定日期 · 原文补充安排', rows: unassignedShareNotes(result, options.supplementary)},
  ].map(section => ({...section, lines: section.rows.flatMap(row => lines(ctx, row, width - padding * 2 - 48))}))
    .filter(section => section.lines.length)
  ctx.font = '700 36px "Microsoft YaHei", sans-serif'
  const titleLines = lines(ctx, (result.assumptions.find(item => item.key === 'destination')?.value || '我的旅行') + ` · ${days.length} 天`, width - padding * 2)
  ctx.font = '400 18px "Microsoft YaHei", sans-serif'
  const notices = exportStatus(result, options).flatMap(notice => lines(ctx, notice, width - padding * 2))
  const summaryY = 141 + (titleLines.length - 1) * 44
  const headerHeight = summaryY + 39 + notices.length * 27
  const height = headerHeight + days.reduce((sum, day) => sum + day.height + 24, 0) + sections.reduce((sum, section) => sum + 85 + section.lines.length * 28, 0) + 60
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
  titleLines.forEach((line, index) => text(line, padding, 104 + index * 44, '700 36px'))
  const confirmed = cards.filter(card => card.status === 'READY').length
  text(`${cards.length} 个安排 · ${confirmed} 个已确认${confirmed < cards.length ? ' · 含待确认地点' : ''}`, padding, summaryY, '400 18px', '#647c8b')
  notices.forEach((notice, index) => text(notice, padding, summaryY + 30 + index * 27, '400 18px', '#946015'))
  let y = headerHeight
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
      if (card.status !== 'READY') text(card.semantic_review === 'PENDING' ? '安排待复核' : '地点待确认', x + 12, top + day.cardHeight - 12, '500 16px', '#946015')
      if (i % columns !== columns - 1 && i < day.visible.length - 1) text('›', x + cardWidth + 5, top + 90, '400 23px', '#91a6b2')
    })
    const noteY = y + 83 + Math.ceil(day.visible.length / columns) * (day.cardHeight + gap)
    day.noteLines.forEach((note, i) => text(note, padding + 24, noteY + i * 27, '400 17px', '#5e7381'))
    y += day.height + 24
  })
  for (const section of sections) {
    box(padding, y, width - padding * 2, 65 + section.lines.length * 28, '#fff', 18)
    text(section.title, padding + 24, y + 34, '600 21px')
    section.lines.forEach((line, i) => text(line, padding + 24, y + 66 + i * 28, '400 18px'))
    y += 85 + section.lines.length * 28
  }
  throwIfExportAborted(options.signal)
  return canvas
}
