const sourceHosts = {
  weibo: new Set(['weibo.com', 'www.weibo.com', 'm.weibo.cn']),
  douyin: new Set(['www.douyin.com', 'douyin.com', 'v.douyin.com', 'www.iesdouyin.com', 'iesdouyin.com']),
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
  return {
    avatarUrl: valid ? `/api/authors/${platform}/${identity}/avatar` : '',
    sourceUrl: source?.href || '',
  }
}
