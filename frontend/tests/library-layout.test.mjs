import assert from 'node:assert/strict'
import { test } from 'node:test'
import { buildLibraryRows, visibleLibraryRows, mergeLibraryEntries, shouldLoadNextLibraryBatch } from '../src/library-layout.js'

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

test('initial and transient resize geometry cannot eagerly fetch all library batches', () => {
  const geometry = { scrollTop: 0, gridTop: 340, gridBottom: 340, layoutHeight: 15000, viewportBottom: 16000, windowHeight: 960 }
  assert.equal(shouldLoadNextLibraryBatch(geometry), false)
  assert.equal(shouldLoadNextLibraryBatch({ ...geometry, scrollTop: 100 }), false)
  assert.equal(shouldLoadNextLibraryBatch({ ...geometry, scrollTop: 100, gridBottom: 15340 }), false)
  assert.equal(shouldLoadNextLibraryBatch({ ...geometry, scrollTop: 14100, gridTop: -13800, gridBottom: 1200, viewportBottom: 960 }), true)
  assert.equal(shouldLoadNextLibraryBatch({ ...geometry, scrollTop: 14100, gridTop: -13800, gridBottom: 1200, viewportBottom: 960, windowHeight: 0 }), false)
})
