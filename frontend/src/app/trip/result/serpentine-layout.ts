/** Logical order stays unchanged; only the visual coordinates turn each row. */
export function serpentineLayout(width: number, count: number) {
  const compact = width < 600
  const padding = compact ? 16 : 36
  const gap = compact ? 12 : 18
  const preferred = compact ? 128 : 170
  const columns = Math.max(1, Math.min(6, Math.floor((width - padding * 2 + gap) / (preferred + gap))))
  const cardWidth = Math.min(184, (width - padding * 2 - gap * (columns - 1)) / columns)
  const cardHeight = compact ? 158 : 168
  const step = cardHeight + 64
  const left = (width - columns * cardWidth - (columns - 1) * gap) / 2
  const point = (index: number) => {
    const row = Math.floor(index / columns)
    const offset = index % columns
    const column = row % 2 ? columns - 1 - offset : offset
    return { x: left + column * (cardWidth + gap), y: 48 + row * step, row, reverse: row % 2 === 1 }
  }
  return { width, columns, cardWidth, cardHeight, step, point, height: count === 0 ? 100 : Math.ceil(count / columns) * step + 8 }
}

export function serpentineEdge(layout: ReturnType<typeof serpentineLayout>, index: number) {
  const from = layout.point(index), to = layout.point(index + 1)
  const x1 = from.x + layout.cardWidth / 2, x2 = to.x + layout.cardWidth / 2
  if (from.row === to.row) {
    return { path: `M ${x1} ${from.y} C ${x1} ${from.y - 48} ${x2} ${to.y - 48} ${x2} ${to.y}`,
      x: (x1 + x2) / 2, y: from.y - 33, turn: false }
  }
  const edge = from.reverse ? 7 : layout.width - 7
  const middle = from.y + layout.cardHeight + 38
  return { path: `M ${x1} ${from.y} C ${x1} ${from.y - 35} ${edge} ${from.y - 35} ${edge} ${from.y + 20} L ${edge} ${to.y - 28} Q ${edge} ${to.y - 44} ${(edge + x2) / 2} ${to.y - 44} Q ${x2} ${to.y - 44} ${x2} ${to.y}`,
    x: from.reverse ? 56 : layout.width - 56, y: middle, turn: true }
}
