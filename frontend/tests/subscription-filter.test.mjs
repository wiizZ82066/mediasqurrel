import test from 'node:test'
import assert from 'node:assert/strict'
import { parseDownloadTags, downloadFilterLabel } from '../src/subscription-filter.js'

test('download tags parse line/comma separated topics while preserving spaces inside a topic', () => {
  assert.deepEqual(parseDownloadTags(' #旅行#，美食\n长 话题 '), { tags: ['#旅行#', '美食', '长 话题'] })
  assert.deepEqual(parseDownloadTags(''), { tags: [] })
  assert.equal(downloadFilterLabel({ tags: ['旅行', '美食'] }), '命中任一标签：#旅行、#美食')
  assert.equal(downloadFilterLabel(), '下载全部新内容')
})

test('download tag input rejects excessive count and length', () => {
  assert.throws(() => parseDownloadTags(Array.from({ length: 21 }, (_, i) => `tag${i}`).join(',')), /20/)
  assert.throws(() => parseDownloadTags('字'.repeat(81)), /80/)
})
