<script setup>
import { nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api.js'
import { store, onTaskEvent, upsertTaskSummary, removeTaskSummary, toast } from '../store.js'
import ActivityProgress from '../components/ActivityProgress.vue'
import PlatformLogo from '../components/PlatformLogo.vue'

const router = useRouter()
const route = useRoute()
const saved = store.taskListState
const tasks = ref([...saved.items])
const query = ref(saved.q)
const status = ref(saved.status)
const includeHidden = ref(!!saved.includeHidden)
const cursor = ref(saved.cursor)
const nextCursor = ref(saved.nextCursor)
const previous = ref([...saved.previous])
const loading = ref(false)
const error = ref('')
const changed = ref(false)
const busy = reactive({})
const logs = reactive({})
const logElements = new Map()
const logRequests = new Map()
const liveUpdates = new Map()
const progressUpdates = new Map()
let contentElement = null
let pageRequest = null
let pageSequence = 0
let debounceTimer = null
let disposed = false

const statusText = {
  queued: '排队中', running: '运行中', success: '已完成', failed: '失败',
  cancelled: '已取消', interrupted: '运行中断',
}
const isActive = (task) => ['queued', 'running'].includes(task.status)

function formatTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit',
    minute: '2-digit', second: '2-digit', hour12: false,
  }).format(date)
}

function closeLogs(id) {
  logRequests.get(id)?.abort()
  logRequests.delete(id)
  logElements.delete(id)
  delete logs[id]
}

function rememberList() {
  Object.assign(saved, {
    items: [...tasks.value], cursor: cursor.value, nextCursor: nextCursor.value,
    previous: [...previous.value], status: status.value, q: query.value, includeHidden: includeHidden.value,
    scrollTop: contentElement?.scrollTop || 0, loaded: true,
  })
}

async function loadPage({ target = cursor.value, reset = false, restore = false } = {}) {
  pageRequest?.abort()
  const controller = new AbortController()
  pageRequest = controller
  const sequence = ++pageSequence
  loading.value = true
  error.value = ''
  liveUpdates.clear()
  progressUpdates.clear()
  try {
    const result = await api.taskPage({ cursor: target, status: status.value, q: query.value.trim(), includeHidden: includeHidden.value, signal: controller.signal })
    if (disposed || sequence !== pageSequence) return false
    tasks.value = result.items.map((task) => {
      const latest = liveUpdates.get(task.id) || task
      return progressUpdates.has(task.id) ? { ...latest, progress: progressUpdates.get(task.id) } : latest
    })
      .filter((task) => !status.value || task.status === status.value)
    cursor.value = target
    nextCursor.value = result.next_cursor
    if (reset) previous.value = []
    const visible = new Set(tasks.value.map((task) => task.id))
    for (const id of Object.keys(logs)) if (!visible.has(id)) closeLogs(id)
    changed.value = false
    loading.value = false
    await nextTick()
    if (disposed || sequence !== pageSequence) return false
    if (contentElement) contentElement.scrollTop = restore ? saved.scrollTop : 0
    rememberList()
    return true
  } catch (failure) {
    if (sequence === pageSequence && failure.name !== 'AbortError') error.value = failure.message
    return false
  } finally {
    if (sequence === pageSequence) loading.value = false
  }
}

watch([query, status, includeHidden], (values, old) => {
  clearTimeout(debounceTimer)
  pageRequest?.abort()
  ++pageSequence
  loading.value = true
  debounceTimer = setTimeout(() => loadPage({ target: '', reset: true }), values[1] !== old[1] ? 0 : 350)
})

async function nextPage() {
  if (!nextCursor.value || loading.value) return
  const before = cursor.value
  if (await loadPage({ target: nextCursor.value })) previous.value.push(before)
}

async function previousPage() {
  if (!previous.value.length || loading.value) return
  const before = previous.value[previous.value.length - 1]
  if (await loadPage({ target: before })) previous.value.pop()
}

function scrollLog(id) {
  nextTick(() => {
    const view = logs[id]
    const element = logElements.get(id)
    if (view?.mode === 'live' && view.follow && element) element.scrollTop = element.scrollHeight
  })
}

function mergeLogs(view, incoming, baseCursor = null) {
  const lines = new Map(view.items.map((line) => [line.seq, line]))
  for (const line of incoming) if (Number.isInteger(line.seq)) lines.set(line.seq, line)
  if (baseCursor != null) view.liveCursor = baseCursor
  if (view.liveCursor != null) while (lines.has(view.liveCursor + 1)) ++view.liveCursor
  const all = [...lines.values()].sort((a, b) => a.seq - b.seq)
  view.items = all.slice(-500)
  if (all.length > 500) view.notice = '实时窗口保留最近 500 条；较早日志可从头分页查看。'
}

function beginLogRequest(id) {
  logRequests.get(id)?.abort()
  const controller = new AbortController()
  logRequests.set(id, controller)
  const view = logs[id]
  const sequence = ++view.sequence
  view.loading = true
  view.error = ''
  return { view, controller, sequence }
}

function currentLogRequest(id, view, sequence) {
  return !disposed && logs[id] === view && view.sequence === sequence
}

async function loadLatest(id) {
  if (!logs[id]) return
  const { view, controller, sequence } = beginLogRequest(id)
  view.mode = 'live'
  view.follow = true
  view.items = []
  view.liveCursor = null
  view.historyStack = []
  view.notice = ''
  try {
    const result = await api.taskLogs(id, { tail: true, limit: 200, signal: controller.signal })
    if (!currentLogRequest(id, view, sequence)) return
    mergeLogs(view, result.items, result.next_cursor || 0)
    scrollLog(id)
  } catch (failure) {
    if (currentLogRequest(id, view, sequence) && failure.name !== 'AbortError') view.error = failure.message
  } finally {
    if (currentLogRequest(id, view, sequence)) {
      view.loading = false
      if (view.items.some((line) => line.seq > view.liveCursor + 1)) catchUpLogs(id)
    }
  }
}

async function catchUpLogs(id) {
  const current = logs[id]
  if (!current || current.mode !== 'live' || current.loading || current.liveCursor == null) return
  const { view, controller, sequence } = beginLogRequest(id)
  try {
    const result = await api.taskLogs(id, { after: view.liveCursor, limit: 500, signal: controller.signal })
    if (!currentLogRequest(id, view, sequence)) return
    mergeLogs(view, result.items, result.next_cursor)
    if (result.has_more) {
      const latest = await api.taskLogs(id, { tail: true, limit: 500, signal: controller.signal })
      if (!currentLogRequest(id, view, sequence)) return
      view.items = []
      mergeLogs(view, latest.items, latest.next_cursor)
      view.notice = '已恢复最新 500 条日志；断开期间的完整日志可从头分页查看。'
    }
    scrollLog(id)
  } catch (failure) {
    if (currentLogRequest(id, view, sequence) && failure.name !== 'AbortError') view.error = failure.message
  } finally {
    if (currentLogRequest(id, view, sequence)) view.loading = false
  }
}

async function loadHistory(id, after = 0, direction = 'start') {
  if (!logs[id]) return
  const { view, controller, sequence } = beginLogRequest(id)
  const oldAfter = view.historyAfter
  view.mode = 'history'
  view.follow = false
  try {
    const result = await api.taskLogs(id, { after, limit: 200, signal: controller.signal })
    if (!currentLogRequest(id, view, sequence)) return
    if (direction === 'next') view.historyStack.push(oldAfter)
    else if (direction === 'previous') view.historyStack.pop()
    else view.historyStack = []
    view.items = result.items
    view.historyAfter = after
    view.historyNext = result.next_cursor
    view.hasMore = result.has_more
    view.notice = ''
    await nextTick()
    const element = logElements.get(id)
    if (element) element.scrollTop = 0
  } catch (failure) {
    if (currentLogRequest(id, view, sequence) && failure.name !== 'AbortError') view.error = failure.message
  } finally {
    if (currentLogRequest(id, view, sequence)) view.loading = false
  }
}

function toggleLogs(id) {
  if (logs[id]) return closeLogs(id)
  logs[id] = {
    items: [], mode: 'live', liveCursor: null, sequence: 0, loading: false,
    follow: true, notice: '', error: '', historyAfter: 0, historyNext: 0,
    historyStack: [], hasMore: false,
  }
  loadLatest(id)
}

function onLogScroll(id, event) {
  const element = event.target
  if (logs[id]?.mode === 'live') logs[id].follow = element.scrollHeight - element.scrollTop - element.clientHeight < 40
}

const unsubscribe = onTaskEvent((event) => {
  if (event.type === 'task_update') {
    if (loading.value) liveUpdates.set(event.task.id, event.task)
    const index = tasks.value.findIndex((task) => task.id === event.task.id)
    if (index >= 0) {
      if (status.value && status.value !== event.task.status) {
        tasks.value.splice(index, 1)
        closeLogs(event.task.id)
      } else tasks.value.splice(index, 1, event.task)
    } else changed.value = true
  } else if (event.type === 'task_progress') {
    if (loading.value) progressUpdates.set(event.task_id, event.progress)
    const task = tasks.value.find((item) => item.id === event.task_id)
    if (task) task.progress = event.progress
  } else if (event.type === 'task_log') {
    const view = logs[event.task_id]
    if (view?.mode === 'live') {
      mergeLogs(view, [event.log])
      scrollLog(event.task_id)
      if (view.liveCursor != null && event.log.seq > view.liveCursor + 1) catchUpLogs(event.task_id)
    }
  } else if (event.type === 'reconnect') {
    for (const id of Object.keys(logs)) catchUpLogs(id)
    rememberList()
    loadPage({ restore: true })
  }
})

async function act(task, action) {
  if (busy[task.id]) return
  busy[task.id] = true
  try {
    if (action === 'cancel') {
      await api.cancelTask(task.id)
      toast('已发送取消请求', 'info')
    } else if (action === 'retry') {
      const retried = await api.retryTask(task.id)
      upsertTaskSummary(retried)
      toast('已创建重试任务，原任务与日志已保留', 'success')
      query.value = ''
      status.value = ''
      await nextTick()
      clearTimeout(debounceTimer)
      await loadPage({ target: '', reset: true })
    } else if (action === 'hide') {
      if (!window.confirm('隐藏这条任务记录？详细日志与媒体文件仍会保留。')) return
      await api.deleteTask(task.id)
      removeTaskSummary(task.id)
      closeLogs(task.id)
      tasks.value = tasks.value.filter((item) => item.id !== task.id)
      toast('任务记录已隐藏，日志与媒体文件已保留', 'success')
    } else if (action === 'restore') {
      await api.restoreTask(task.id)
      await loadPage({ restore: true })
      toast('任务记录已恢复，可再次查看详细日志', 'success')
    } else if (action === 'deleteLogs') {
      if (!window.confirm('永久删除这条任务的全部详细日志？任务记录和媒体文件会保留，此操作无法撤销。')) return
      await api.deleteTaskLogs(task.id)
      closeLogs(task.id)
      toast('详细日志已删除，任务记录与媒体文件已保留', 'success')
    }
  } catch (failure) {
    toast(failure.message, 'error')
  } finally {
    delete busy[task.id]
  }
}

async function gotoLibrary(task) {
  rememberList()
  try {
    const located = await api.libraryLocate({ task_id: task.id })
    router.push({ path: '/library', query: { entry_id: located.entry_id } })
  } catch (error) { toast('此任务尚未关联媒体索引：' + error.message, 'info') }
}

onMounted(async () => {
  contentElement = document.querySelector('.content')
  if (route.query.task) {
    query.value = String(route.query.task)
    status.value = ''
    await nextTick()
    clearTimeout(debounceTimer)
    loadPage({ target: '', reset: true })
  } else loadPage({ restore: saved.loaded })
})

onBeforeUnmount(() => {
  rememberList()
  disposed = true
  unsubscribe()
  clearTimeout(debounceTimer)
  pageRequest?.abort()
  for (const id of Object.keys(logs)) closeLogs(id)
  liveUpdates.clear()
  progressUpdates.clear()
})
</script>

<template>
  <div class="view-root task-view">
    <h1 class="page-title">任务与历史</h1>
    <p class="page-sub">查看下载状态，按需打开日志。时间按当前设备时区显示。</p>
    <div class="card task-filters">
      <label class="search-field">搜索任务<input v-model="query" class="input" type="search" placeholder="任务 ID、平台或链接" /></label>
      <label>状态<select v-model="status" class="select"><option value="">全部状态</option><option v-for="(label, value) in statusText" :key="value" :value="value">{{ label }}</option></select></label>
      <label class="hidden-filter"><input v-model="includeHidden" type="checkbox"> 包含隐藏任务</label>
      <button class="btn btn-ghost" :disabled="loading" @click="loadPage({ target: '', reset: true })">刷新列表</button>
    </div>
    <p v-if="changed" class="update-note" role="status">任务有更新。刷新列表以查看新增任务。</p>
    <p v-if="loading" class="list-message" role="status">正在加载任务…</p>
    <div v-if="error" class="error-box" role="alert">{{ error }} <button class="btn btn-ghost btn-sm" @click="loadPage()">重试</button></div>
    <div v-if="!loading && !error && !tasks.length" class="card empty">
      <svg class="empty-mark" viewBox="0 0 48 48" fill="none" aria-hidden="true"><rect x="11" y="8" width="26" height="34" rx="5" fill="#e8f1ff" stroke="#5287ca" stroke-width="2"/><path d="M18 19h12M18 26h12M18 33h7" stroke="#5287ca" stroke-width="2" stroke-linecap="round"/><rect x="19" y="5" width="10" height="6" rx="2" fill="#84b1e8"/></svg>
      <p>{{ query || status ? '没有符合条件的任务' : '还没有下载任务' }}</p>
      <router-link to="/" class="btn btn-primary">开始下载</router-link>
    </div>
    <div class="task-list" :aria-busy="loading">
      <article v-for="task in tasks" :key="task.id" class="card task-card">
        <button class="task-head" :aria-expanded="!!logs[task.id]" :aria-controls="`logs-${task.id}`" :disabled="task.hidden" @click="toggleLogs(task.id)">
          <PlatformLogo :platform="task.script_id" :size="36"/>
          <span class="task-info"><span class="task-name">{{ task.script_name }}</span><span class="task-param">{{ task.params?.input || task.params?.url || task.id }}</span><span class="task-meta">{{ formatTime(task.created_at) }} · {{ task.id }}<template v-if="task.exit_code != null"> · 退出码 {{ task.exit_code }}</template></span></span>
          <span class="tag" :class="'tag-' + task.status"><span class="dot"></span>{{ statusText[task.status] || task.status }}</span>
          <span class="chevron" aria-hidden="true">{{ logs[task.id] ? '−' : '+' }}</span>
        </button>
        <p v-if="task.hidden" class="attempt-note">此任务记录已隐藏。恢复后可查看原有日志，媒体文件不受影响。</p>
        <p v-if="task.parent_task_id" class="attempt-note">第 {{ task.attempt || 2 }} 次尝试 · 原任务 {{ task.parent_task_id }}</p>
        <p v-if="task.status === 'interrupted'" class="interrupted-note">程序退出时下载尚未结束。可创建新任务重试，不会自动续传。</p>
        <p v-if="task.error" class="task-error">{{ task.error }}</p>
        <ActivityProgress :progress="task.progress" :status="task.status" :label="`${task.script_name}下载进度`" />
        <div class="task-actions">
          <button v-if="!task.hidden" class="btn btn-ghost btn-sm" @click="toggleLogs(task.id)">{{ logs[task.id] ? '收起日志' : '查看日志' }}</button>
          <button v-if="isActive(task)" class="btn btn-danger-ghost btn-sm" :disabled="busy[task.id]" @click="act(task, 'cancel')">取消任务</button>
          <template v-else-if="!task.hidden">
            <button v-if="['failed', 'cancelled', 'interrupted'].includes(task.status)" class="btn btn-primary btn-sm" :disabled="busy[task.id]" @click="act(task, 'retry')">创建重试任务</button>
            <button v-if="task.output_rel" class="btn btn-ghost btn-sm" @click="gotoLibrary(task)">在媒体库查看</button>
            <button class="btn btn-ghost btn-sm" :disabled="busy[task.id]" @click="act(task, 'hide')">隐藏任务记录</button>
            <button class="btn btn-danger-ghost btn-sm" :disabled="busy[task.id]" @click="act(task, 'deleteLogs')">删除详细日志</button>
          </template>
          <button v-if="task.hidden" class="btn btn-ghost btn-sm" :disabled="busy[task.id]" @click="act(task, 'restore')">恢复任务记录</button>
        </div>
        <p v-if="task.output_rel || task.output_dir" class="task-outdir">输出：{{ task.output_rel || task.output_dir }}</p>
        <section v-if="logs[task.id]" :id="`logs-${task.id}`" class="log-panel" aria-label="任务详细日志">
          <div class="log-tools">
            <span>{{ logs[task.id].mode === 'live' ? '实时日志' : '历史日志' }}</span>
            <button class="btn btn-ghost btn-sm" :disabled="logs[task.id].loading" @click="loadHistory(task.id)">从头查看</button>
            <template v-if="logs[task.id].mode === 'history'">
              <button class="btn btn-ghost btn-sm" :disabled="logs[task.id].loading || !logs[task.id].historyStack.length" @click="loadHistory(task.id, logs[task.id].historyStack.at(-1), 'previous')">上一页</button>
              <button class="btn btn-ghost btn-sm" :disabled="logs[task.id].loading || !logs[task.id].hasMore" @click="loadHistory(task.id, logs[task.id].historyNext, 'next')">下一页</button>
              <button class="btn btn-ghost btn-sm" @click="loadLatest(task.id)">返回实时</button>
            </template>
            <button v-else-if="!logs[task.id].follow" class="btn btn-ghost btn-sm" @click="logs[task.id].follow = true; scrollLog(task.id)">跟随最新</button>
            <span v-if="logs[task.id].loading" role="status">加载中…</span>
          </div>
          <p v-if="logs[task.id].notice" class="log-notice">{{ logs[task.id].notice }}</p>
          <p v-if="logs[task.id].error" class="task-error" role="alert">{{ logs[task.id].error }} <button class="btn btn-ghost btn-sm" @click="logs[task.id].mode === 'live' ? loadLatest(task.id) : loadHistory(task.id, logs[task.id].historyAfter)">重新加载</button></p>
          <div class="terminal" :ref="element => element ? logElements.set(task.id, element) : logElements.delete(task.id)" role="log" aria-live="off" tabindex="0" @scroll="onLogScroll(task.id, $event)">
            <div v-for="line in logs[task.id].items" :key="line.seq" class="t-line"><span class="t-time">{{ formatTime(line.time) }}</span><span class="line-number">#{{ line.seq }}</span>{{ line.text }}</div>
            <div v-if="!logs[task.id].items.length && !logs[task.id].loading" class="t-line">{{ isActive(task) ? '等待任务输出…' : '没有可显示的详细日志。' }}</div>
          </div>
        </section>
      </article>
    </div>
    <nav v-if="tasks.length || previous.length" class="pagination" aria-label="任务分页"><button class="btn btn-ghost" :disabled="loading || !previous.length" @click="previousPage">上一页</button><span>第 {{ previous.length + 1 }} 页 · 本页 {{ tasks.length }} 条</span><button class="btn btn-ghost" :disabled="loading || !nextCursor" @click="nextPage">下一页</button></nav>
  </div>
</template>

<style scoped>
.task-filters { display: flex; gap: 16px; align-items: flex-end; margin-bottom: 18px; }
.task-filters label { display: grid; gap: 7px; font-size: 12.5px; color: var(--text-2); }
.search-field { flex: 1; min-width: 160px; }
.task-filters .select { min-width: 140px; }
.task-filters .btn { flex-shrink: 0; }
.task-list { display: flex; flex-direction: column; gap: 14px; }
.task-card { padding: 18px 22px; }
.task-head { display: flex; align-items: center; gap: 14px; width: 100%; border: 0; background: transparent; text-align: left; padding: 0; color: inherit; font: inherit; cursor: pointer; }
.task-head:focus-visible { outline: 2px solid var(--blue); outline-offset: 5px; border-radius: 8px; }
.hidden-filter { display:flex; align-items:center; gap:6px; white-space:nowrap; font-size:12px; }
.platform-mark { width: 36px; height: 36px; display: grid; place-items: center; flex-shrink: 0; border-radius: 11px; background: #e8eef7; color: #46638a; font-size: 17px; font-weight: 700; }
.platform-mark.weibo { background: #fff0df; color: #c36034; }
.platform-mark.douyin { background: #e8f3f6; color: #2a737d; }
.task-info { flex: 1; min-width: 0; display: grid; gap: 4px; }
.task-name { font-size: 14.5px; font-weight: 650; }
.task-param { font-size: 12.5px; color: var(--text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.task-meta { font-size: 11.5px; color: var(--text-2); overflow-wrap: anywhere; }
.task-head .tag { flex-shrink: 0; }
.chevron { font-size: 20px; color: var(--text-2); line-height: 1; }
.task-actions { margin-top: 12px; display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
.task-outdir, .attempt-note, .interrupted-note { font-size: 12px; color: var(--text-2); overflow-wrap: anywhere; margin: 10px 0 0; }
.interrupted-note { color: #986219; }
.tag-interrupted { background: #fff0d7; color: #986219; }
.task-error, .error-box { font-size: 12.5px; color: #ba4034; overflow-wrap: anywhere; }
.error-box { padding: 12px; background: #fff0ee; border-radius: 12px; margin-bottom: 14px; }
.log-panel { margin-top: 14px; border-top: 1px solid rgba(0,0,0,.07); padding-top: 12px; }
.log-tools { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; font-size: 12px; color: var(--text-2); }
.terminal { margin-top: 10px; max-height: 360px; min-height: 76px; overflow: auto; }
.t-line { white-space: pre-wrap; overflow-wrap: anywhere; }
.t-time { display: inline-block; min-width: 156px; font-variant-numeric: tabular-nums; }
.line-number { color: #939aad; margin-right: 9px; }
.log-notice, .update-note, .list-message { font-size: 12.5px; color: var(--text-2); margin: 10px 0; }
.pagination { display: flex; align-items: center; justify-content: center; gap: 18px; margin: 24px 0; font-size: 12.5px; color: var(--text-2); }
.empty-mark { width: 48px; height: 48px; margin-bottom: 12px; }
.empty a { text-decoration: none; }
.task-view :deep(.btn), .task-card, .platform-mark { transition: none; }
.task-view :deep(.btn:active), .task-view :deep(.btn:hover) { transform: none; }
@media (max-width: 760px) {
  .task-filters { flex-wrap: wrap; gap: 12px; }
  .search-field { flex-basis: 100%; }
  .task-filters label:not(.search-field) { flex: 1; }
  .task-card { padding: 16px; }
  .task-head { gap: 9px; flex-wrap: wrap; }
  .task-head .task-info { min-width: calc(100% - 50px); }
  .task-head .tag { margin-left: 45px; }
  .chevron { margin-left: auto; }
  .pagination { gap: 9px; }
}
</style>
