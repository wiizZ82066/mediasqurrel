import test from 'node:test'
import assert from 'node:assert/strict'
import { homeTime, nextScanLabel, homeThumbnail } from '../src/home-presentation.js'

test('home dates use the plan timezone, never the browser timezone or publication date', () => {
  assert.equal(homeTime('2030-01-01T00:30:00Z'), '08:30')
  assert.match(nextScanLabel({ due_at: '2030-01-01T00:30:00Z', timezone: 'Asia/Shanghai' }, 0), /08:30.*Asia\/Shanghai/)
  assert.match(nextScanLabel({ due_at: '2020-01-01T00:00:00Z' }, Date.UTC(2030, 0, 1)), /等待执行/)
  assert.equal(homeTime(null), '时间待确认')
  assert.equal(nextScanLabel(null), '尚未设置扫描计划')
})
test('home thumbnails keep special relative-path characters encoded and request a bounded size', () => {
  const url = new URL(homeThumbnail({ root_id: 'root', rel_dir: 'Example/a #b', cover: 'photo/1.jpg' }), 'http://localhost')
  assert.equal(url.searchParams.get('p'), 'Example/a #b/photo/1.jpg')
  assert.equal(url.searchParams.get('w'), '480')
  assert.equal(homeThumbnail({}), '')
})
