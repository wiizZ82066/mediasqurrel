// 全局实时状态：任务列表 + WebSocket 订阅 + 通知中心 + toast
import { reactive } from 'vue'
import { api } from './api.js'

export const store = reactive({
  tasks: [],
  subs: [],
  subScans: {},
  wsConnected: false,
  toasts: [],
  notifications: [],   // 通知中心（最近 50 条）
  unread: 0,
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

export function pushNotification(n) {
  store.notifications.unshift(n)
  if (store.notifications.length > 50) store.notifications.pop()
  store.unread++
}

export function markNotificationsRead() {
  store.unread = 0
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
      if (['success', 'failed'].includes(msg.task.status)) {
        toast(
          msg.task.status === 'success'
            ? `${msg.task.script_icon} ${msg.task.script_name} 下载成功`
            : `${msg.task.script_icon} ${msg.task.script_name} 下载失败`,
          msg.task.status === 'success' ? 'success' : 'error',
        )
        pushNotification({
          level: msg.task.status === 'success' ? 'success' : 'error',
          title: msg.task.status === 'success' ? '下载完成' : '下载失败',
          text: msg.task.script_name,
          time: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
        })
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
      pushNotification(msg)
      toast(`🔔 ${msg.title}: ${msg.text}`, 'info')
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
export function toast(text, kind = 'info') {
  const id = ++toastSeq
  store.toasts.push({ id, text, kind })
  setTimeout(() => {
    const i = store.toasts.findIndex((t) => t.id === id)
    if (i >= 0) store.toasts.splice(i, 1)
  }, 3600)
}
