// 全局实时状态：任务列表 + WebSocket 订阅 + 通知中心 + toast
import { reactive } from 'vue'
import { api } from './api.js'

export const store = reactive({
  tasks: [],
  wsConnected: false,
  toasts: [],
  notifications: [],   // 通知中心（最近 50 条）
  unread: 0,
})

let ws = null
let reconnectTimer = null

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
    refreshTasks()
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
      const i = store.tasks.findIndex((t) => t.id === msg.task.id)
      if (i >= 0) store.tasks.splice(i, 1, msg.task)
      else store.tasks.unshift(msg.task)
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
    } else if (msg.type === 'task_log') {
      const t = store.tasks.find((x) => x.id === msg.task_id)
      if (t) {
        t.logs = t.logs || []
        t.logs.push(msg.log)
        t._scroll = true
      }
    } else if (msg.type === 'notification') {
      pushNotification(msg)
      toast(`🔔 ${msg.title}: ${msg.text}`, 'info')
    }
  }
}

export async function refreshTasks() {
  try {
    store.tasks = await api.tasks()
  } catch { /* 静默 */ }
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
