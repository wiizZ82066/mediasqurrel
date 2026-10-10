import { shallowRef } from 'vue'

// One local dictionary request is shared by every caption on the page.
// Original text is never rewritten; unknown platform codes stay readable.
export const weiboEmoticons = shallowRef(new Set())
let pending = null, retryAfter = 0, loaded = false

export function loadWeiboEmoticons() {
  if (loaded || Date.now() < retryAfter) return Promise.resolve()
  if (pending) return pending
  pending = (async () => {
    try {
      const response = await fetch('/api/emoticons/weibo', { signal: AbortSignal.timeout(5000) })
      if (!response.ok) throw new Error('Emoticons unavailable')
      const value = await response.json()
      if (!Array.isArray(value.names)) throw new Error('Invalid emoticon dictionary')
      weiboEmoticons.value = new Set(value.names.filter(name =>
        typeof name === 'string' && /^\[[^\[\]\r\n]{1,32}\]$/u.test(name)))
      loaded = true
    } catch {
      retryAfter = Date.now() + 30000
    } finally {
      pending = null
    }
  })()
  return pending
}

export function contentSegments(text, platform, names = new Set()) {
  const value = String(text ?? '')
  if (platform !== 'weibo' || !names.size) return [{ text: value }]
  const segments = []
  let offset = 0
  for (const match of value.matchAll(/\[[^\[\]\r\n]{1,32}\]/gu)) {
    if (!names.has(match[0])) continue
    if (match.index > offset) segments.push({ text: value.slice(offset, match.index) })
    segments.push({ text: match[0], src: '/api/emoticons/weibo/' + encodeURIComponent(match[0]) })
    offset = match.index + match[0].length
  }
  if (offset < value.length || !segments.length) segments.push({ text: value.slice(offset) })
  return segments
}
