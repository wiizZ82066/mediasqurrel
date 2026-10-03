<script setup>
// 任务页：队列状态 + 实时日志 + 完成后预览/跳媒体库
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api.js'
import { store, refreshTasks, toast } from '../store.js'

const router = useRouter()
const expanded = ref({})
const termRefs = ref({})

// 预览弹层
const preview = ref(null) // { dir, images, videos }
const previewLoading = ref(false)

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

// 预览任务输出内容
async function openPreview(task) {
  previewLoading.value = true
  try {
    const r = await api.preview(task.output_rel)
    preview.value = { dir: task.output_rel, ...r }
  } catch (e) {
    toast('预览失败: ' + e.message, 'error')
  } finally {
    previewLoading.value = false
  }
}

// 跳转媒体库对应位置（作者/日期来自输出目录相对路径）
function gotoLibrary(task) {
  const parts = (task.output_rel || '').split('/').filter(Boolean)
  if (parts.length < 2) {
    toast('无法解析输出目录', 'error')
    return
  }
  router.push({
    path: '/library',
    query: { author: decodeURIComponent(parts[0]), entry: parts[1] },
  })
}

const mediaUrl = (rel) => '/media/' + rel.split('/').map(encodeURIComponent).join('/')
const thumbUrl = (rel) => '/api/thumb?p=' + encodeURIComponent(rel)

function openImg(rel) {
  window.open(mediaUrl(rel), '_blank')
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
      <div class="task-actions" v-else-if="t.status === 'success' && t.output_rel">
        <span class="task-outdir" :title="t.output_dir">📁 {{ t.output_rel }}</span>
        <button class="btn btn-ghost btn-sm" @click.stop="openPreview(t)" :disabled="previewLoading">
          {{ previewLoading ? '加载中…' : '👀 预览' }}
        </button>
        <router-link
          class="btn btn-ghost btn-sm"
          :to="{ path: '/library', query: { author: t.output_rel.split('/')[0], entry: t.output_rel.split('/')[1] } }"
          @click.stop
        >🖼️ 媒体库</router-link>
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

  <!-- 输出预览弹层 -->
  <transition name="fade">
    <div v-if="preview" class="pv-mask" @click="preview = null">
      <div class="pv-panel" @click.stop>
        <div class="pv-head">
          <span class="pv-title">👀 任务输出预览</span>
          <span class="pv-dir">{{ preview.dir }}</span>
          <button class="btn btn-ghost btn-sm" @click="preview = null">关闭 ✕</button>
        </div>
        <div v-if="!preview.images.length && !preview.videos.length" class="pv-empty">
          该目录暂无媒体文件
        </div>
        <div v-if="preview.images.length" class="pv-grid">
          <img
            v-for="img in preview.images.slice(0, 24)"
            :key="img"
            :src="thumbUrl(img)"
            loading="lazy"
            @click="openImg(img)"
          />
          <div v-if="preview.images.length > 24" class="pv-more">
            +{{ preview.images.length - 24 }} 张
          </div>
        </div>
        <div v-if="preview.videos.length" class="pv-videos">
          <video
            v-for="v in preview.videos.slice(0, 4)"
            :key="v"
            :src="mediaUrl(v)"
            controls
            preload="metadata"
          ></video>
          <div v-if="preview.videos.length > 4" class="pv-more">
            +{{ preview.videos.length - 4 }} 个视频
          </div>
        </div>
      </div>
    </div>
  </transition>
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
.task-actions { margin-top: 12px; padding-left: 38px; display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
.task-actions .btn { text-decoration: none; }
.task-outdir {
  font-size: 12.5px;
  color: var(--text-2);
  font-family: Consolas, monospace;
  word-break: break-all;
}

/* 预览弹层 */
.pv-mask {
  position: fixed;
  inset: 0;
  z-index: 500;
  background: rgba(10, 10, 12, 0.55);
  backdrop-filter: blur(14px);
  display: flex;
  align-items: center;
  justify-content: center;
}
.pv-panel {
  width: min(92vw, 980px);
  max-height: 86vh;
  overflow-y: auto;
  background: rgba(252, 252, 253, 0.97);
  border-radius: 20px;
  box-shadow: 0 30px 90px rgba(0, 0, 0, 0.3);
  padding: 20px 22px;
}
.pv-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.pv-title { font-size: 16px; font-weight: 700; }
.pv-dir {
  flex: 1;
  font-size: 12px;
  color: var(--text-2);
  font-family: Consolas, monospace;
  word-break: break-all;
}
.pv-empty { text-align: center; color: var(--text-2); padding: 40px; }
.pv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 8px;
  margin-bottom: 14px;
}
.pv-grid img {
  width: 100%;
  aspect-ratio: 1;
  object-fit: cover;
  border-radius: 10px;
  cursor: zoom-in;
  transition: transform 200ms var(--ease);
}
.pv-grid img:hover { transform: scale(1.03); }
.pv-videos { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
.pv-videos video { width: 100%; border-radius: 12px; background: #000; }
.pv-more {
  grid-column: 1 / -1;
  text-align: center;
  font-size: 12px;
  color: var(--text-2);
  padding: 8px;
}
.terminal { margin-top: 14px; }

.expand-enter-active { transition: all 250ms ease-out; }
.expand-leave-active { transition: all 150ms ease-in; }
.expand-enter-from, .expand-leave-to { opacity: 0; max-height: 0; }
.expand-enter-to, .expand-leave-from { opacity: 1; max-height: 360px; }
</style>
