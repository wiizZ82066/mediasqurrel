// Build canonical public profile links instead of trusting saved URL schemes.
export function subscriptionHomepage(sub) {
  const id = String(sub.blogger_id || '').trim()
  if (sub.platform === 'weibo' && /^\d{1,30}$/.test(id)) return `https://weibo.com/u/${id}`
  if (sub.platform === 'douyin' && /^[A-Za-z0-9_.-]{1,256}$/.test(id)) return `https://www.douyin.com/user/${encodeURIComponent(id)}`
  return ''
}

export function newerBatch(current, incoming) {
  if (current?.started_at === incoming?.started_at && (current?.revision || 0) > (incoming?.revision || 0)) return current
  if (current?.started_at && incoming?.started_at && current.started_at > incoming.started_at) return current
  return incoming
}
