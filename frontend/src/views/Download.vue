<script setup>
// 下载页：选脚本 -> 动态渲染 manifest 表单 -> 提交任务 + 输出目录选择 + 实时预览
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api.js'
import { store, toast } from '../store.js'

const router = useRouter()
const scripts = ref([])
const selectedId = ref('')
const form = ref({})
const submitting = ref(false)

// 目录选择器
const browsing = ref(false)
const browsePath = ref('')
const browseDirs = ref([])
const browseTarget = ref(null) // 正在为哪个参数浏览

// 输出预览
const previewDir = ref('')
const previewData = ref(null)
const carouselIdx = ref(0)
const trackedTaskId = ref(null)
let pollTimer = null

const selected = computed(() => scripts.value.find((s) => s.id === selectedId.value))

onMounted(async () => {
  try {
    scripts.value = await api.scripts()
    if (scripts.value.length) select(scripts.value[0].id)
  } catch (e) {
    toast('加载脚本清单失败: ' + e.message, 'error')
  }
  pollTimer = setInterval(pollPreview, 2000)
})

onBeforeUnmount(() => clearInterval(pollTimer))

function select(id) {
  selectedId.value = id
  const s = scripts.value.find((x) => x.id === id)
  const defaults = {}
  for (const p of s?.params || []) {
    defaults[p.name] = p.default ?? (p.kind === 'switch' ? false : '')
  }
  form.value = defaults
}

// 智能识别：粘贴内容自动匹配脚本
function smartPick(text) {
  const hit = scripts.value.find((s) =>
    (s.link_hints || []).some((h) => text.includes(h)),
  )
  if (hit && hit.id !== selectedId.value) select(hit.id)
}

function onPasteInput(evt, paramName) {
  const text = (evt.clipboardData || window.clipboardData).getData('text') || ''
  if (paramName === 'input' || paramName === 'url') smartPick(text)
}

// ---------- 目录选择器 ----------
function openBrowser(paramName) {
  browsing.value = true
  browseTarget.value = paramName
  browsePath.value = ''
  loadBrowse('')
}

async function loadBrowse(path) {
  try {
    const r = await api.browse(path)
    browsePath.value = r.current
    browseDirs.value = r.dirs
  } catch (e) {
    toast('目录加载失败: ' + e.message, 'error')
  }
}

function pickDir(rel) {
  form.value[browseTarget.value] = rel
  browsing.value = false
  toast('已选择输出目录: ' + rel, 'info')
}

function browseUp() {
  const parts = browsePath.value.split('/').filter(Boolean)
  parts.pop()
  loadBrowse(parts.join('/'))
}

// ---------- 输出预览 ----------
async function submit() {
  if (!selected.value) return
  submitting.value = true
  try {
    const task = await api.createTask(selectedId.value, { ...form.value })
    toast(`${selected.value.icon} 已创建任务，正在排队执行`, 'success')
    trackedTaskId.value = task.id
    carouselIdx.value = 0
  } catch (e) {
    toast(e.message, 'error')
  } finally {
    submitting.value = false
  }
}

// 跟踪任务输出目录 + 轮询内容
async function pollPreview() {
  // 优先：用户手动选择/输入的输出目录，立即预览其内容
  const outParam = selected.value?.params.find((p) => p.label.includes('目录'))
  const manual = outParam ? String(form.value[outParam.name] || '').trim() : ''
  if (manual) {
    if (manual !== previewDir.value) {
      previewDir.value = manual
      carouselIdx.value = 0
    }
  } else {
    // 其次：跟踪中的任务，同步其输出目录
    const t = store.tasks.find((x) => x.id === trackedTaskId.value)
    if (t?.output_rel && t.output_rel !== previewDir.value) {
      previewDir.value = t.output_rel
    }
    // 无跟踪任务时，退回展示最近一次成功任务的输出
    if (!previewDir.value) {
      const done = store.tasks.find((x) => x.status === 'success' && x.output_rel)
      if (done) previewDir.value = done.output_rel
    }
  }
  if (!previewDir.value) return
  try {
    const r = await api.preview(previewDir.value)
    // 只在内容变化时替换，避免轮询导致重渲染闪烁
    const sig = r.images.join('|') + '#' + r.videos.join('|')
    const old = previewData.value
    if (!old || old._sig !== sig) {
      r._sig = sig
      previewData.value = r
      if (carouselIdx.value >= r.images.length) carouselIdx.value = 0
    }
  } catch { /* 目录尚不存在等情况静默 */ }
}

const mediaUrl = (rel) => '/media/' + rel.split('/').map(encodeURIComponent).join('/')
const thumbUrl = (rel) => '/api/thumb?p=' + encodeURIComponent(rel)

// 堆叠轮播：以 carouselIdx 为中心，前后各叠 2 张
function stackStyle(i, total) {
  const offset = i - carouselIdx.value
  const wrap = (n, m) => ((n % m) + m) % m // 循环绕回
  const off = wrap(offset, Math.max(total, 1))
  const rel = off > total / 2 ? off - total : off // 取最短方向
  const depth = Math.min(Math.abs(rel), 3)
  return {
    transform: `translateX(${rel * 46}px) translateY(${depth * 8}px) rotate(${rel * 5}deg) scale(${1 - depth * 0.05})`,
    zIndex: 30 - depth,
    opacity: depth >= 3 ? 0 : 1 - depth * 0.22,
    pointerEvents: rel === 0 ? 'auto' : 'none',
    transition: 'all 450ms cubic-bezier(.4,0,.2,1)',
  }
}

function carouselNext() {
  const n = previewData.value?.images.length || 0
  if (n) carouselIdx.value = (carouselIdx.value + 1) % n
}
function carouselPrev() {
  const n = previewData.value?.images.length || 0
  if (n) carouselIdx.value = (carouselIdx.value - 1 + n) % n
}
</script>

<template>
  <div class="view-root">
    <h1 class="page-title">下载内容</h1>
    <p class="page-sub">粘贴链接或分享文案，选择脚本开始下载</p>

    <!-- 脚本选择 -->
    <div class="grid grid-4" style="margin-bottom: 24px">
      <div
        v-for="(s, i) in scripts"
        :key="s.id"
        class="card hoverable script-card stagger-item"
        :class="{ picked: s.id === selectedId }"
        :style="{ animationDelay: i * 40 + 'ms' }"
        @click="select(s.id)"
      >
        <div class="script-icon">{{ s.icon }}</div>
        <div class="script-name">{{ s.name }}</div>
        <div class="script-desc">{{ s.description }}</div>
        <div v-if="!s.available" class="script-warn">⚠️ 脚本文件缺失</div>
      </div>
      <div v-if="!scripts.length" class="card loading-breathe" style="grid-column: 1 / -1; text-align:center; color: var(--text-2)">
        正在加载脚本清单…
      </div>
    </div>

    <!-- 动态参数表单 -->
    <div v-if="selected" class="card">
      <template v-for="p in selected.params" :key="p.name">
        <div v-if="p.kind === 'switch'" class="field">
          <div class="switch-row">
            <div>
              <label style="margin:0">{{ p.label }}</label>
              <div class="hint" style="margin-top:3px">{{ p.help }}</div>
            </div>
            <label class="switch">
              <input type="checkbox" v-model="form[p.name]" />
              <span class="track"><span class="thumb"></span></span>
            </label>
          </div>
        </div>

        <div v-else class="field">
          <label>
            {{ p.label }}
            <span v-if="p.required" style="color: var(--red)">*</span>
          </label>

          <!-- 目录参数：输入框 + 浏览按钮 -->
          <div v-if="p.label.includes('目录')" class="dir-row">
            <input
              class="input"
              v-model="form[p.name]"
              :placeholder="p.placeholder"
              @paste="onPasteInput($event, p.name)"
            />
            <button class="btn btn-ghost dir-btn" @click="openBrowser(p.name)">📂 浏览</button>
          </div>

          <textarea
            v-else-if="p.kind === 'textarea'"
            class="textarea"
            v-model="form[p.name]"
            :placeholder="p.placeholder"
            @paste="onPasteInput($event, p.name)"
          ></textarea>
          <input
            v-else
            class="input"
            v-model="form[p.name]"
            :placeholder="p.placeholder"
            @paste="onPasteInput($event, p.name)"
          />
          <div v-if="p.help" class="hint">{{ p.help }}</div>
        </div>
      </template>

      <div style="display: flex; gap: 12px; margin-top: 22px">
        <button class="btn btn-primary" :disabled="submitting" @click="submit">
          {{ submitting ? '提交中…' : '🚀 开始下载' }}
        </button>
        <router-link to="/tasks" class="btn btn-ghost" style="text-decoration:none">
          查看任务队列
        </router-link>
      </div>
    </div>

    <!-- 输出实时预览 -->
    <div v-if="previewData && (previewData.images.length || previewData.videos.length)" class="card preview-card">
      <div class="preview-head">
        <span class="section-title" style="margin:0">📥 输出预览</span>
        <span class="preview-dir">{{ previewDir }}</span>
      </div>

      <!-- 图片堆叠轮播 -->
      <div v-if="previewData.images.length" class="stack-wrap">
        <button class="g-nav g-prev" @click="carouselPrev">‹</button>
        <div class="stack">
          <div
            v-for="(img, i) in previewData.images"
            :key="img"
            class="stack-card"
            :style="stackStyle(i, previewData.images.length)"
          >
            <img :src="mediaUrl(img)" loading="lazy" alt="" />
          </div>
        </div>
        <button class="g-nav g-next" @click="carouselNext">›</button>
        <div class="stack-count">{{ carouselIdx + 1 }} / {{ previewData.images.length }}</div>
      </div>

      <!-- 视频播放器（最多渲染 8 个，避免大量 video 元素拖垮页面） -->
      <div v-if="previewData.videos.length" class="video-list">
        <video
          v-for="v in previewData.videos.slice(0, 8)"
          :key="v"
          :src="mediaUrl(v)"
          controls
          preload="metadata"
        ></video>
        <div v-if="previewData.videos.length > 8" class="video-more">
          还有 {{ previewData.videos.length - 8 }} 个视频，已省略渲染
        </div>
      </div>
    </div>

    <!-- 目录选择器弹层 -->
    <transition name="fade">
      <div v-if="browsing" class="browse-mask" @click="browsing = false">
        <div class="browse-panel" @click.stop>
          <div class="browse-head">
            <button class="btn btn-ghost btn-sm" :disabled="!browsePath || browsePath === '.'" @click="browseUp">⬆ 上级</button>
            <span class="browse-path">{{ browsePath === '.' ? '根目录' : browsePath }}</span>
            <button class="btn btn-ghost btn-sm" @click="browsing = false">取消</button>
          </div>
          <div class="browse-list">
            <div v-if="!browseDirs.length" class="browse-empty">（无子目录）</div>
            <button
              v-for="d in browseDirs"
              :key="d.rel"
              class="browse-item"
              @click="loadBrowse(d.rel)"
              @dblclick="pickDir(d.rel)"
            >
              <span class="browse-icon">📁</span>
              <span class="browse-name">{{ d.name }}</span>
              <span class="browse-count">{{ d.entries }} 条</span>
              <span class="browse-pick" @click.stop="pickDir(d.rel)">选此目录</span>
            </button>
          </div>
          <div class="browse-tip">单击进入子目录 · 点「选此目录」确认</div>
        </div>
      </div>
    </transition>
  </div>
</template>

<style scoped>
.script-card {
  cursor: pointer;
  position: relative;
  padding: 20px;
  border: 2px solid transparent;
}
.script-card.picked {
  border-color: var(--blue);
  box-shadow: 0 10px 36px rgba(0, 113, 227, 0.22);
}
.script-icon { font-size: 30px; margin-bottom: 10px; }
.script-name { font-size: 15.5px; font-weight: 700; margin-bottom: 5px; }
.script-desc { font-size: 12.5px; color: var(--text-2); line-height: 1.55; }
.script-warn { font-size: 12px; color: var(--orange); margin-top: 8px; }

/* ---------- 目录选择 ---------- */
.dir-row { display: flex; gap: 10px; }
.dir-row .input { flex: 1; }
.dir-btn { flex-shrink: 0; }

.browse-mask {
  position: fixed;
  inset: 0;
  z-index: 520;
  background: rgba(10, 10, 12, 0.4);
  backdrop-filter: blur(10px);
  display: flex;
  align-items: center;
  justify-content: center;
}
.browse-panel {
  width: min(560px, 92vw);
  max-height: 74vh;
  background: rgba(250, 250, 252, 0.96);
  border-radius: 20px;
  box-shadow: 0 30px 90px rgba(0, 0, 0, 0.3);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.browse-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 18px;
  border-bottom: 1px solid rgba(0, 0, 0, 0.06);
}
.browse-path {
  flex: 1;
  font-size: 13.5px;
  font-weight: 700;
  font-family: Consolas, monospace;
  word-break: break-all;
}
.browse-list { flex: 1; overflow-y: auto; padding: 10px 12px; }
.browse-empty { text-align: center; color: var(--text-2); padding: 30px; font-size: 13px; }
.browse-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 11px 14px;
  border: none;
  border-radius: 12px;
  background: transparent;
  font-family: var(--font);
  font-size: 14px;
  cursor: pointer;
  transition: background 150ms;
  text-align: left;
}
.browse-item:hover { background: rgba(0, 0, 0, 0.05); }
.browse-icon { font-size: 17px; }
.browse-name { flex: 1; font-weight: 600; word-break: break-all; }
.browse-count { font-size: 12px; color: var(--text-2); }
.browse-pick {
  font-size: 12px;
  color: var(--blue);
  background: rgba(0, 113, 227, 0.1);
  padding: 3px 10px;
  border-radius: 8px;
  white-space: nowrap;
}
.browse-pick:hover { background: rgba(0, 113, 227, 0.2); }
.browse-tip {
  padding: 10px 16px;
  font-size: 12px;
  color: var(--text-2);
  border-top: 1px solid rgba(0, 0, 0, 0.06);
  text-align: center;
}

/* ---------- 输出预览 ---------- */
.preview-card { margin-top: 20px; }
.preview-head {
  display: flex;
  align-items: baseline;
  gap: 14px;
  margin-bottom: 18px;
  flex-wrap: wrap;
}
.preview-dir {
  font-size: 12px;
  color: var(--text-2);
  font-family: Consolas, monospace;
  word-break: break-all;
}

.stack-wrap {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 10px 50px 26px;
}
.stack {
  position: relative;
  width: 320px;
  height: 240px;
}
.stack-card {
  position: absolute;
  inset: 0;
  border-radius: 16px;
  overflow: hidden;
  background: #fff;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.18);
}
.stack-card img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.stack-count {
  position: absolute;
  bottom: 4px;
  left: 50%;
  transform: translateX(-50%);
  font-size: 12px;
  color: var(--text-2);
  font-variant-numeric: tabular-nums;
  background: rgba(255, 255, 255, 0.8);
  padding: 2px 12px;
  border-radius: 10px;
  backdrop-filter: blur(6px);
}
.g-nav {
  position: absolute;
  top: 45%;
  z-index: 40;
  width: 38px;
  height: 38px;
  border-radius: 50%;
  border: none;
  background: rgba(0, 0, 0, 0.08);
  color: var(--text);
  font-size: 22px;
  line-height: 1;
  cursor: pointer;
  transition: background 180ms;
}
.g-nav:hover { background: rgba(0, 0, 0, 0.16); }
.g-prev { left: 0; }
.g-next { right: 0; }

.video-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 14px;
}
.video-list video {
  width: 100%;
  border-radius: 14px;
  background: #000;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
}
.video-more {
  grid-column: 1 / -1;
  text-align: center;
  font-size: 12.5px;
  color: var(--text-2);
  padding: 10px;
}

.fade-enter-active { transition: opacity 200ms ease-out; }
.fade-leave-active { transition: opacity 150ms ease-in; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
