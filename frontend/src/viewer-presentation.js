const sourceHosts = {
  weibo: new Set(['weibo.com', 'www.weibo.com', 'm.weibo.cn']),
  douyin: new Set(['www.douyin.com', 'douyin.com', 'v.douyin.com', 'www.iesdouyin.com', 'iesdouyin.com']),
}

export function itemLocation(entry, item) {
  return item?.source_entry_id ? { id:item.source_entry_id, root_id:item.root_id, rel_dir:item.rel_dir } : entry
}

export function mediaUrl(entry, rel) {
  const path = [entry.rel_dir, rel].filter(Boolean).join('/').split('/').map(encodeURIComponent).join('/')
  return '/media/roots/' + encodeURIComponent(entry.root_id) + '/' + path
}

export function segmentTime(value) {
  const date = new Date(value)
  if (!value || !Number.isFinite(date.getTime())) return '未知'
  return new Intl.DateTimeFormat('zh-CN', { timeZone:'Asia/Shanghai', month:'2-digit', day:'2-digit',
    hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false }).format(date)
}

// Metadata can come from imported folders. Never turn an arbitrary stored URL
// into a clickable action or infer an identity from an author's display name.
export function mediaProfile(entry) {
  const platform = entry?.platform, meta = entry?.meta || {}
  let source = null
  try {
    const candidate = new URL(meta['原文链接'])
    if (candidate.protocol === 'https:' && !candidate.username && !candidate.password &&
        !candidate.port && sourceHosts[platform]?.has(candidate.hostname)) source = candidate
  } catch { /* Older archives need not have a source link. */ }
  let identity = platform === 'weibo' ? meta['作者UID'] : platform === 'douyin' ? meta['作者sec_uid'] : ''
  if (platform === 'weibo' && !/^\d{1,20}$/.test(String(identity || ''))) {
    identity = source?.pathname.match(/^\/(?:u\/|profile\/)?(\d{1,20})(?:\/|$)/)?.[1] || ''
  }
  const valid = platform === 'weibo' ? /^\d{1,20}$/.test(String(identity || ''))
    : platform === 'douyin' && /^MS4wLjAB[A-Za-z0-9_-]{8,248}$/.test(String(identity || ''))
  const avatar = entry?.author_avatar_url || ''
  const localAvatar = /^\/api\/subs\/[1-9]\d*\/avatar$/.test(avatar) ||
    (platform === 'weibo' && /^\/api\/authors\/weibo\/\d{1,20}\/avatar$/.test(avatar)) ||
    (platform === 'douyin' && /^\/api\/authors\/douyin\/MS4wLjAB[A-Za-z0-9_-]{8,248}\/avatar$/.test(avatar))
  return {
    avatarUrl: localAvatar ? avatar : valid ? `/api/authors/${platform}/${identity}/avatar` : '',
    sourceUrl: source?.href || '',
  }
}
