import assert from 'node:assert/strict'
import { test } from 'node:test'
import { contentSegments, loadWeiboEmoticons, weiboEmoticons } from '../src/content-text.js'

test('known Weibo codes use local images while unknown text, newlines and Unicode stay intact', () => {
  const text = '你好👩🏽‍💻\n[兔子][未收录]1 < 2 & 3 [兔子]❤️'
  const parts = contentSegments(text, 'weibo', new Set(['[兔子]']))
  assert.equal(parts.map(part => part.text).join(''), text)
  const images = parts.filter(part => part.src)
  assert.equal(images.length, 2)
  assert.ok(images.every(part => part.src === '/api/emoticons/weibo/%5B%E5%85%94%E5%AD%90%5D'))
  assert.ok(parts.some(part => part.text.includes('[未收录]1 < 2 & 3')))
})

test('platforms never borrow each other’s emoji meanings and unknown codes stay as text', () => {
  for (const platform of ['douyin', '', undefined]) {
    assert.deepEqual(contentSegments('[兔子]😀', platform, new Set(['[兔子]'])), [{ text: '[兔子]😀' }])
  }
  assert.deepEqual(contentSegments('[兔子]', 'weibo'), [{ text: '[兔子]' }])
  assert.deepEqual(contentSegments('', 'weibo', new Set(['[兔子]'])), [{ text: '' }])
})

test('caption data cannot introduce HTML or a third-party image URL', () => {
  const text = '<img src=x onerror=alert(1)>[https://outside.invalid/x]'
  const parts = contentSegments(text, 'weibo', new Set(['[兔子]']))
  assert.deepEqual(parts, [{ text }])
  const encoded = contentSegments('[../../secret]', 'weibo', new Set(['[../../secret]']))
  assert.equal(encoded[0].src, '/api/emoticons/weibo/%5B..%2F..%2Fsecret%5D')
})

test('all captions share one local dictionary request and validate its names', async t => {
  let resolve
  const result = new Promise(yes => { resolve = yes })
  const fetch = t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/emoticons/weibo')
    assert.ok(options.signal)
    await result
    return { ok: true, json: async () => ({ names: ['[兔子]', '[浮云]', null, '<script>', '[nested[bad]]'] }) }
  })
  const first = loadWeiboEmoticons(), second = loadWeiboEmoticons()
  assert.equal(first, second)
  resolve()
  await first
  assert.deepEqual([...weiboEmoticons.value], ['[兔子]', '[浮云]'])
  await loadWeiboEmoticons()
  assert.equal(fetch.mock.callCount(), 1)
})
