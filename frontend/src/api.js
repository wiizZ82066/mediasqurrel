// API 封装：REST + WebSocket
const base = ''

async function request(path, options = {}) {
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
    const error = new Error(detail)
    error.status = res.status
    throw error
  }
  return res
}

async function req(path, options = {}) {
  return (await request(path, options)).json()
}

function waitForIndex(delay, signal) {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted()
    const abort = () => { clearTimeout(timer); reject(signal.reason) }
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, delay)
    signal.addEventListener('abort', abort, { once: true })
  })
}

// Only an explicit HTTP 202 is pending; missing entries and network failures are not retried.
async function locateLibrary(params, signal, { onIndexing, timeoutMs = 30000 } = {}) {
  const controller = new AbortController()
  const abort = () => controller.abort(signal.reason)
  signal?.throwIfAborted()
  signal?.addEventListener('abort', abort, { once: true })
  let indexing = false
  const timer = setTimeout(() => {
    const error = new Error(indexing
      ? '媒体库索引仍在同步，完成后会自动显示，也可到任务页查看。'
      : '读取媒体索引超时，请稍后重试。')
    error.code = indexing ? 'LIBRARY_INDEXING' : 'LIBRARY_LOOKUP_TIMEOUT'
    controller.abort(error)
  }, timeoutMs)
  try {
    const path = '/api/library/locate?' + new URLSearchParams(params)
    while (true) {
      controller.signal.throwIfAborted()
      const response = await request(path, { signal: controller.signal })
      const data = await response.json()
      controller.signal.throwIfAborted()
      if (response.status !== 202) return data
      if (data.status !== 'indexing') throw new Error('媒体索引返回了无法识别的状态，请稍后重试。')
      indexing = true
      onIndexing?.(data.message || '下载已完成，正在同步媒体库索引…')
      const seconds = Number(data.retry_after)
      const delay = Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : 1000
      await waitForIndex(Math.min(5000, Math.max(250, delay)), controller.signal)
    }
  } catch (error) {
    if (controller.signal.aborted) throw controller.signal.reason
    throw error
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
  }
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
  taskPage: ({ limit = 50, cursor = '', status = '', q = '', includeHidden = false, signal } = {}) =>
    req('/api/tasks/page?' + new URLSearchParams({ limit, cursor, status, q, include_hidden: includeHidden ? 1 : 0 }), { signal }),
  taskLogs: (id, { after = 0, limit = 200, tail = false, signal } = {}) =>
    req(`/api/tasks/${encodeURIComponent(id)}/logs?` + new URLSearchParams({ after, limit, tail: tail ? 1 : 0 }), { signal }),
  cancelTask: (id) => req(`/api/tasks/${id}/cancel`, { method: 'POST' }),
  retryTask: (id) => req(`/api/tasks/${encodeURIComponent(id)}/retry`, { method: 'POST' }),
  restoreTask: (id) => req(`/api/tasks/${encodeURIComponent(id)}/restore`, { method: 'POST' }),
  deleteTask: (id) => req(`/api/tasks/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  deleteTaskLogs: (id) => req(`/api/tasks/${encodeURIComponent(id)}/logs`, { method: 'DELETE' }),
  library: (refresh = false) => req(`/api/library${refresh ? '?refresh=1' : ''}`),
  libraryRoots: () => req('/api/library/roots'),
  libraryEntries: ({ signal, ...params } = {}) => req('/api/library/entries?' + new URLSearchParams(params), { signal }),
  libraryEntry: (id, signal) => req('/api/library/entries/' + encodeURIComponent(id), { signal }),
  libraryLocate: locateLibrary,
  libraryAuthors: ({ signal, ...params } = {}) => req('/api/library/authors?' + new URLSearchParams(params), { signal }),
  libraryDates: ({ signal, ...params } = {}) => req('/api/library/dates?' + new URLSearchParams(params), { signal }),
  libraryScan: (rootId) => req('/api/library/scan', { method: 'POST', body: JSON.stringify({ root_id: rootId }) }),
  libraryScans: () => req('/api/library/scans'),
  cancelLibraryScan: (id) => req(`/api/library/scans/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
  settings: () => req('/api/settings'),
  saveSettings: (values) => req('/api/settings', { method:'PATCH', body:JSON.stringify(values) }),
  diagnostics: () => req('/api/settings/diagnostics'),
  checkBrowser: () => req('/api/settings/check-browser', { method:'POST' }),
  checkDownload: (body) => req('/api/settings/check-download', { method:'POST', body:JSON.stringify(body) }),
  exportDiagnostics: () => req('/api/settings/diagnostics-export', { method:'POST' }),
  maintenancePlans: () => req('/api/maintenance/plans'),
  maintenancePlan: (body) => req('/api/maintenance/plan', { method:'POST', body:JSON.stringify(body) }),
  maintenanceStatus: (id) => req(`/api/maintenance/plans/${id}`),
  executeMaintenance: (plan) => req(`/api/maintenance/plans/${plan.id}/execute`, { method:'POST', body:JSON.stringify({confirmation_token:plan.confirmation_token}) }),
  cancelMaintenance: (id) => req(`/api/maintenance/plans/${id}/cancel`, { method:'POST' }),
  applyPaths: (body) => req('/api/settings/paths/apply', { method:'POST', body:JSON.stringify({...body,confirmed:true}) }),
  browse: (path = '') => req(`/api/browse?path=${encodeURIComponent(path)}`),
  preview: (dir) => req(`/api/preview?dir=${encodeURIComponent(dir)}`),
  search: (q) => req(`/api/search?q=${encodeURIComponent(q)}`),
  subs: () => req('/api/subs'),
  localAuthors: () => req('/api/subs/local-authors'),
  searchBlogger: (platform, q, signal) =>
    req(`/api/subs/search?platform=${platform}&q=${encodeURIComponent(q)}`, { signal }),
  subSchedules: (id) => req(`/api/subs/${id}/schedules`),
  addSchedule: (id, body) => req(`/api/subs/${id}/schedules`, { method:'POST', body:JSON.stringify(body) }),
  updateSchedule: (id, body) => req(`/api/schedules/${id}`, { method:'PATCH', body:JSON.stringify(body) }),
  removeSchedule: (id) => req(`/api/schedules/${id}`, { method:'DELETE' }),
  scheduleRuns: (id, offset=0) => req(`/api/subs/${id}/schedule-runs?limit=20&offset=${offset}`),
  subScans: (id, offset=0) => req(`/api/subs/scans?sub_id=${id}&limit=20&offset=${offset}`),
  scanOptions: (id) => req(`/api/subs/${id}/scan-options`),
  saveScanOptions: (id, body) => req(`/api/subs/${id}/scan-options`, {method:'PATCH',body:JSON.stringify(body)}),
  verifyDouyin: () => req('/api/subs/verify-douyin', { method: 'POST' }),
  loginDouyin: () => req('/api/subs/login-douyin', { method: 'POST' }),
  douyinAuth: () => req('/api/subs/douyin-auth'),
  weiboAuth: () => req('/api/subs/weibo-auth'),
  loginWeibo: () => req('/api/subs/login-weibo', { method: 'POST' }),
  addSub: (body) => req('/api/subs', { method: 'POST', body: JSON.stringify(body) }),
  updateSub: (id, body) => req(`/api/subs/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  removeSub: (id) => req(`/api/subs/${id}`, { method: 'DELETE' }),
  scanSub: (id) => req(`/api/subs/${id}/scan`, { method: 'POST' }),
}
