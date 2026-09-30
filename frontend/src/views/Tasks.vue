<script setup>
// 任务页：队列状态 + 实时日志
import { computed, nextTick, onMounted, ref } from 'vue'
import { api } from '../api.js'
import { store, refreshTasks, toast } from '../store.js'

const expanded = ref({})
const termRefs = ref({})

const tasks = computed(() => store.tasks)
const statusText = {
  queued: '排队中', running: '运行中', success: '已完成',
  failed: '失败', cancelled: '已取消',
}

onMounted(refreshTasks)

function toggle(id) {
  expanded.value[id] = !expanded.value[id]
  if (expanded.value[id]) scrollToBottom(id)
}

function scrollToBottom(id) {
  nextTick(() => {
    const el = termRefs.value[id]
    if (el) el.scrollTop = el.scrollHeight
  })
}

function ensureExpanded(task) {
  if (!expanded.value[task.id]) expanded.value[task.id] = true
  scrollToBottom(task.id)
}

async function cancel(task) {
  try {
    await api.cancelTask(task.id)
    toast('已发送取消信号', 'info')
  } catch (e) {
    toast(e.message, 'error')
  }
}

defineExpose({ ensureExpanded })
</script>

<template>
  <div class="view-root">
    <h1 class="page-title">任务队列</h1>
  <p class="page-sub">所有下载任务的执行状态与实时日志</p>

  <div v-if="!tasks.length" class="card empty">
    <div class="empty-icon">🌬️</div>
    <p>还没有任务，去下载页提交一个吧</p>
    <router-link to="/" class="btn btn-primary" style="text-decoration:none">开始下载</router-link>
  </div>

  <div class="task-list">
    <div
      v-for="(t, i) in tasks"
      :key="t.id"
      class="card task-card stagger-item"
      :style="{ animationDelay: i * 40 + 'ms' }"
    >
      <div class="task-head" @click="toggle(t.id)">
        <span class="task-icon">{{ t.script_icon }}</span>
        <div class="task-info">
          <div class="task-name">
            {{ t.script_name }}
            <span class="task-param">
              {{
                (t.params.input || t.params.url || '')
                  .replace(/https?:\/\/\S+/, m => m.slice(0, 42) + (m.length > 42 ? '…' : ''))
                  .slice(0, 60) || '—'
              }}
            </span>
          </div>
          <div class="task-meta">
            {{ (t.created_at || '').replace('T', ' ') }}
            <template v-if="t.exit_code != null"> · 退出码 {{ t.exit_code }}</template>
          </div>
        </div>
        <span class="tag" :class="'tag-' + t.status">
          <span class="dot"></span>{{ statusText[t.status] || t.status }}
        </span>
        <span class="chevron" :class="{ open: expanded[t.id] }">›</span>
      </div>

      <div class="task-actions" v-if="['queued', 'running'].includes(t.status)">
        <button class="btn btn-danger-ghost btn-sm" @click.stop="cancel(t)">取消任务</button>
      </div>
      <div class="task-actions" v-else-if="t.status === 'success' && t.output_dir">
        <span class="task-outdir" :title="t.output_dir">📁 {{ t.output_dir }}</span>
      </div>

      <transition name="expand">
        <div v-if="expanded[t.id]" class="terminal" :ref="el => (termRefs[t.id] = el)">
          <div v-for="(l, li) in t.logs" :key="li" class="t-line">
            <span class="t-time">{{ l.time }}</span>{{ l.text }}
          </div>
          <div v-if="!t.logs?.length" class="t-line loading-breathe">等待输出…</div>
        </div>
      </transition>
    </div>
  </div>
  </div>
</template>

<style scoped>
.task-list { display: flex; flex-direction: column; gap: 14px; }
.task-card { padding: 18px 22px; }
.task-head {
  display: flex;
  align-items: center;
  gap: 14px;
  cursor: pointer;
  user-select: none;
}
.task-icon { font-size: 24px; }
.task-info { flex: 1; min-width: 0; }
.task-name { font-size: 14.5px; font-weight: 600; display: flex; gap: 10px; align-items: baseline; }
.task-param {
  font-size: 12.5px;
  font-weight: 400;
  color: var(--text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 420px;
}
.task-meta { font-size: 12px; color: var(--text-2); margin-top: 3px; }
.chevron {
  font-size: 20px;
  color: var(--text-2);
  transition: transform 250ms cubic-bezier(0.4, 0, 0.2, 1);
  line-height: 1;
}
.chevron.open { transform: rotate(90deg); }
.task-actions { margin-top: 12px; padding-left: 38px; }
.task-outdir {
  font-size: 12.5px;
  color: var(--text-2);
  font-family: Consolas, monospace;
  word-break: break-all;
}
.terminal { margin-top: 14px; }

.expand-enter-active { transition: all 250ms ease-out; }
.expand-leave-active { transition: all 150ms ease-in; }
.expand-enter-from, .expand-leave-to { opacity: 0; max-height: 0; }
.expand-enter-to, .expand-leave-from { opacity: 1; max-height: 360px; }
</style>
