// 全局实时状态：任务列表 + WebSocket 订阅 + 通知中心 + toast
import { reactive } from 'vue'
import { api } from './api.js'

export const store = reactive({
  tasks: [],
  subs: [],
  subScans: {},
  wsConnected: false,
  toasts: [],
  downloadState: { selectedId: '', drafts: {}, task: null },
  libraryState: null,
  preferences: { density: 'comfortable' },
  taskListState: { items: [], cursor: '', nextCursor: null, previous: [], status: '', q: '', scrollTop: 0, loaded: false },
})

let ws = null
let reconnectTimer = null
let taskRevision = 0
let refreshSequence = 0
const taskRevisions = new Map()
const taskListeners = new Set()

export function onTaskEvent(listener) {
  taskListeners.add(listener)
  return () => taskListeners.delete(listener)
}

function emitTaskEvent(message) {
  for (const listener of taskListeners) listener(message)
}

function trimTaskSummaries() {
  let completed = 0
  store.tasks = store.tasks.filter((task) =>
    ['queued', 'running'].includes(task.status) || completed++ < 100,
  )
  const retained = new Set(store.tasks.map((task) => task.id))
  for (const id of taskRevisions.keys()) if (!retained.has(id)) taskRevisions.delete(id)
}

export function upsertTaskSummary(task) {
  // Log payloads belong only to an explicitly opened task panel.
  const { logs, ...summary } = task
  taskRevisions.set(task.id, ++taskRevision)
  const index = store.tasks.findIndex((item) => item.id === task.id)
  if (index >= 0) store.tasks.splice(index, 1, summary)
  else store.tasks.unshift(summary)
  trimTaskSummaries()
}

export function removeTaskSummary(id) {
  store.tasks = store.tasks.filter((task) => task.id !== id)
  taskRevisions.delete(id)
}

const terminalEvents = new Map()
const pendingEvents = new Map()
let eventTimer = null
function backgroundToast(key, text, kind, to) {
  const group = pendingEvents.get(key)
  if (group) group.count++
  else pendingEvents.set(key, { text, kind, to, count: 1 })
  if (!eventTimer) eventTimer = setTimeout(() => {
    eventTimer = null
    for (const [key, event] of pendingEvents) toast(event.text, event.kind, { key, to: event.to, count: event.count })
    pendingEvents.clear()
  }, 900)
}

export function connectWS() {
  if (ws) return
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/ws`)

  ws.onopen = () => {
    store.wsConnected = true
    // REST restores durable snapshots; discard stale connection revisions.
    store.subScans = {}
    refreshTasks()
    refreshSubs().catch(() => {})
    emitTaskEvent({ type: 'reconnect' })
  }
  ws.onclose = () => {
    store.wsConnected = false
    ws = null
    reconnectTimer = setTimeout(connectWS, 2000)
  }
  ws.onmessage = (ev) => {
    let msg
    try { msg = JSON.parse(ev.data) } catch { return }
    if (msg.type === 'task_update') {
      upsertTaskSummary(msg.task)
      if (['success', 'failed', 'interrupted'].includes(msg.task.status) && terminalEvents.get(msg.task.id) !== msg.task.status) {
        terminalEvents.set(msg.task.id, msg.task.status)
        if (terminalEvents.size > 1000) terminalEvents.delete(terminalEvents.keys().next().value)
        const success = msg.task.status === 'success'
        backgroundToast('task-' + msg.task.status, success ? '下载任务已完成' : '下载任务未完成，请查看任务记录', success ? 'success' : 'error', '/tasks')
      }
    } else if (msg.type === 'task_progress') {
      const task = store.tasks.find((t) => t.id === msg.task_id)
      if (task) {
        task.progress = msg.progress
        taskRevisions.set(task.id, ++taskRevision)
      }
    } else if (msg.type === 'sub_scan') {
      store.subScans[msg.sub_id] = msg.scan
      const sub = store.subs.find((s) => s.id === msg.sub_id)
      if (sub) applyScan(sub, msg.scan)
    } else if (msg.type === 'task_log') {
      // No global log retention. Open panels consume bounded, sequenced events.
    } else if (msg.type === 'notification') {
      backgroundToast('subscription-' + (msg.level || 'info'), `${msg.title}: ${msg.text}`, msg.level === 'error' ? 'error' : 'info', '/subs')
    }
    emitTaskEvent(msg)
  }
}

export async function refreshTasks() {
  const sequence = ++refreshSequence
  const before = taskRevision
  try {
    const tasks = await api.tasks()
    if (sequence !== refreshSequence) return
    const live = new Map(store.tasks.map((task) => [task.id, task]))
    const incoming = new Set(tasks.map((task) => task.id))
    const merged = tasks.map(({ logs, ...task }) =>
      (taskRevisions.get(task.id) || 0) > before ? live.get(task.id) || task : task,
    )
    for (const task of store.tasks) {
      if (!incoming.has(task.id) && (['queued', 'running'].includes(task.status) || (taskRevisions.get(task.id) || 0) > before)) {
        merged.push(task)
      }
    }
    store.tasks = merged.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
    trimTaskSummaries()
  } catch { /* 静默 */ }
}

function applyScan(sub, scan) {
  sub.scan = scan
  for (const field of ['last_scan_at', 'last_status', 'last_error']) {
    if (scan?.[field] != null) sub[field] = scan[field]
  }
}

export async function refreshSubs() {
  const subs = await api.subs()
  for (const sub of subs) {
    const live = store.subScans[sub.id]
    // A REST response in flight must not overwrite a newer WebSocket event.
    const scan = live && live.revision > (sub.scan?.revision || 0) ? live : sub.scan
    if (scan) store.subScans[sub.id] = scan
    applyScan(sub, scan)
  }
  store.subs = subs
}

let toastSeq = 0
const toastTimers = new Map()
export function dismissToast(id) {
  clearTimeout(toastTimers.get(id))
  toastTimers.delete(id)
  store.toasts = store.toasts.filter(item => item.id !== id)
}
export function toast(text, kind = 'info', options = {}) {
  const key = options.key || `${kind}:${text}`
  let item = store.toasts.find(t => t.key === key)
  if (item) {
    item.count += options.count || 1
    clearTimeout(toastTimers.get(item.id))
  } else {
    item = { id: ++toastSeq, key, text, kind, count: options.count || 1, to: options.to || '' }
    store.toasts.push(item)
    if (store.toasts.length > 3) dismissToast(store.toasts[0].id)
  }
  toastTimers.set(item.id, setTimeout(() => dismissToast(item.id), kind === 'error' ? 8000 : 5000))
}
