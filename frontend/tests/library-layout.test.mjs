import assert from 'node:assert/strict'
import { test } from 'node:test'
import { buildLibraryRows, libraryBatchSize, visibleLibraryRows, mergeLibraryEntries, shouldLoadNextLibraryBatch } from '../src/library-layout.js'

test('large library keeps mounted media bounded at both ends and preserves focused cards', () => {
  const items = Array.from({ length: 100000 }, (_, i) => ({ id: String(i) }))
  const layout = buildLibraryRows([{ date: '2026', items }], 960)
  const first = visibleLibraryRows(layout, 0, 900)
  const end = visibleLibraryRows(layout, layout.height - 900, 900)
  assert.ok(first.flatMap(row => row.items || []).length < 60)
  assert.ok(end.flatMap(row => row.items || []).length < 60)
  assert.equal(end.at(-1).items.at(-1).id, '99999')
  const focused = visibleLibraryRows(layout, layout.height - 900, 900, '0')
  assert.ok(focused.some(row => row.items?.some(item => item.id === '0')))
  assert.equal(buildLibraryRows([{ date: '2026', items: items.slice(0, 2) }], 340, true).columns, 1)
})

test('appending refreshes duplicate summaries and retains original order', () => {
  assert.deepEqual(mergeLibraryEntries([{ id: 'a', value: 1 }, { id: 'b' }],
    [{ id: 'b', value: 2 }, { id: 'c' }, { id: 'c', value: 3 }]),
  [{ id: 'a', value: 1 }, { id: 'b', value: 2 }, { id: 'c', value: 3 }])
})

test('responsive columns use wide screens without squeezing cards or losing entries', () => {
  const items = Array.from({ length: 137 }, (_, index) => ({ id: String(index) }))
  for (const [width, columns] of [[358, 1], [540, 2], [806, 3], [1360, 5], [2000, 7], [3280, 12]]) {
    const layout = buildLibraryRows([{ date: '2026', items }], width, width < 460)
    assert.equal(layout.columns, columns)
    const cardWidth = (layout.cardHeight - 142) / .75 + 2
    assert.ok(cardWidth >= 240)
    assert.ok(Math.abs(cardWidth * columns + layout.gap * (columns - 1) - width) < .01)
    assert.deepEqual(layout.rows.flatMap(row => row.items || []), items)
    for (let index = 1; index < layout.rows.length; index++) {
      assert.equal(layout.rows[index].top, layout.rows[index - 1].top + layout.rows[index - 1].height)
    }
  }
})

test('tall wide screens receive enough first-batch media without unbounded reads', () => {
  for (const [width, height] of [[358, 844], [806, 960], [1360, 1080], [3280, 2160]]) {
    const geometry = buildLibraryRows([], width)
    const count = libraryBatchSize(geometry, height)
    assert.ok(count >= 60 && count <= 100)
    const layout = buildLibraryRows([{ date: '2026', items: Array.from({ length: count }, (_, id) => ({ id })) }], width)
    assert.ok(layout.height > height, 'First batch should fill the viewport and allow scrolling')
    assert.equal(shouldLoadNextLibraryBatch({scrollTop:0,gridTop:0,gridBottom:0,layoutHeight:layout.height,viewportBottom:height,windowHeight:height}), false)
  }
  assert.equal(libraryBatchSize(buildLibraryRows([], 10000), 10000), 100)
  const narrow = buildLibraryRows([], 220, true)
  assert.equal(narrow.columns, 1)
  assert.equal(narrow.cardHeight, (220 - 2) * .75 + 142)
  assert.ok(Number.isFinite(libraryBatchSize(buildLibraryRows([], NaN), NaN)))
})

test('initial and transient resize geometry cannot eagerly fetch all library batches', () => {
  const geometry = { scrollTop: 0, gridTop: 340, gridBottom: 340, layoutHeight: 15000, viewportBottom: 16000, windowHeight: 960 }
  assert.equal(shouldLoadNextLibraryBatch(geometry), false)
  assert.equal(shouldLoadNextLibraryBatch({ ...geometry, scrollTop: 100 }), false)
  assert.equal(shouldLoadNextLibraryBatch({ ...geometry, scrollTop: 100, gridBottom: 15340 }), false)
  assert.equal(shouldLoadNextLibraryBatch({ ...geometry, scrollTop: 14100, gridTop: -13800, gridBottom: 1200, viewportBottom: 960 }), true)
  assert.equal(shouldLoadNextLibraryBatch({ ...geometry, scrollTop: 14100, gridTop: -13800, gridBottom: 1200, viewportBottom: 960, windowHeight: 0 }), false)
})
