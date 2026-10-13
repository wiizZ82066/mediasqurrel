// Only visible rows (plus nearby overscan) mount media elements. Loaded summary
// records stay available for a stable return position without retaining images.
export function buildLibraryRows(groups, width, compact = false) {
  const columns = width >= 880 ? 3 : width >= 460 ? 2 : 1
  const gap = compact ? 12 : 18
  const cardHeight = ((Math.max(width, 240) - gap * (columns - 1)) / columns - 2) * .75 + 142
  const rows = []
  let top = 0
  for (const group of groups) {
    if (!group.items.length) continue
    rows.push({ key: `heading:${group.date}`, type: 'heading', date: group.date, count: group.items.length, top, height: 56 })
    top += 56
    for (let index = 0; index < group.items.length; index += columns) {
      const items = group.items.slice(index, index + columns)
      rows.push({ key: items[0].id, type: 'cards', items, top, height: cardHeight + gap })
      top += cardHeight + gap
    }
  }
  return { rows, height: top, columns, cardHeight, gap }
}

export function visibleLibraryRows(layout, top, height, focusedId = null) {
  const before = Math.max(0, top - height)
  const after = top + height * 2
  // Binary search avoids walking every loaded row on each scroll frame.
  let low = 0, high = layout.rows.length
  while (low < high) {
    const middle = (low + high) >>> 1
    if (layout.rows[middle].top + layout.rows[middle].height < before) low = middle + 1
    else high = middle
  }
  const rows = []
  for (let index = low; index < layout.rows.length && layout.rows[index].top <= after; index++) rows.push(layout.rows[index])
  if (focusedId && !rows.some(row => row.items?.some(item => item.id === focusedId))) {
    const focused = layout.rows.find(row => row.items?.some(item => item.id === focusedId))
    if (focused) rows.push(focused)
  }
  return rows
}

export function mergeLibraryEntries(previous, incoming) {
  const positions = new Map(previous.map((item, index) => [item.id, index]))
  const result = previous.slice()
  for (const item of incoming) {
    if (positions.has(item.id)) result[positions.get(item.id)] = item
    else { positions.set(item.id, result.length); result.push(item) }
  }
  return result
}

export function shouldLoadNextLibraryBatch({ scrollTop, gridTop, gridBottom, layoutHeight, viewportBottom, windowHeight }) {
  // A still-open first screen must never eagerly walk the entire cursor chain.
  // Model height protects against DOM styles pending a Vue/resize layout flush;
  // window bounds protect against a temporarily expanding scroll container.
  if (!(scrollTop > 0) || !(layoutHeight > 0) || !(windowHeight > 0)) return false
  const bottom = Math.max(gridBottom, gridTop + layoutHeight)
  const edge = Math.min(viewportBottom, windowHeight)
  return Number.isFinite(bottom) && Number.isFinite(edge) && bottom < edge + 600
}
