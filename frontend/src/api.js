// API 封装：REST + WebSocket
const base = ''

async function req(path, options = {}) {
  const res = await fetch(base + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const j = await res.json()
      detail = j.detail || detail
    } catch { /* ignore */ }
    throw new Error(detail)
  }
  return res.json()
}

export const api = {
  scripts: () => req('/api/scripts'),
  createTask: (scriptId, params) =>
    req('/api/tasks', {
      method: 'POST',
      body: JSON.stringify({ script_id: scriptId, params }),
    }),
  tasks: () => req('/api/tasks'),
  task: (id, signal) => req(`/api/tasks/${encodeURIComponent(id)}`, { signal }),
  taskPage: ({ limit = 50, cursor = '', status = '', q = '', signal } = {}) =>
    req('/api/tasks/page?' + new URLSearchParams({ limit, cursor, status, q }), { signal }),
  taskLogs: (id, { after = 0, limit = 200, tail = false, signal } = {}) =>
    req(`/api/tasks/${encodeURIComponent(id)}/logs?` + new URLSearchParams({ after, limit, tail: tail ? 1 : 0 }), { signal }),
  cancelTask: (id) => req(`/api/tasks/${id}/cancel`, { method: 'POST' }),
  retryTask: (id) => req(`/api/tasks/${encodeURIComponent(id)}/retry`, { method: 'POST' }),
  deleteTask: (id) => req(`/api/tasks/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  deleteTaskLogs: (id) => req(`/api/tasks/${encodeURIComponent(id)}/logs`, { method: 'DELETE' }),
  library: (refresh = false) => req(`/api/library${refresh ? '?refresh=1' : ''}`),
  browse: (path = '') => req(`/api/browse?path=${encodeURIComponent(path)}`),
  preview: (dir) => req(`/api/preview?dir=${encodeURIComponent(dir)}`),
  search: (q) => req(`/api/search?q=${encodeURIComponent(q)}`),
  subs: () => req('/api/subs'),
  localAuthors: () => req('/api/subs/local-authors'),
  searchBlogger: (platform, q) =>
    req(`/api/subs/search?platform=${platform}&q=${encodeURIComponent(q)}`),
  verifyDouyin: () => req('/api/subs/verify-douyin', { method: 'POST' }),
  loginDouyin: () => req('/api/subs/login-douyin', { method: 'POST' }),
  douyinAuth: () => req('/api/subs/douyin-auth'),
  addSub: (body) => req('/api/subs', { method: 'POST', body: JSON.stringify(body) }),
  updateSub: (id, body) => req(`/api/subs/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  removeSub: (id) => req(`/api/subs/${id}`, { method: 'DELETE' }),
  scanSub: (id) => req(`/api/subs/${id}/scan`, { method: 'POST' }),
}
