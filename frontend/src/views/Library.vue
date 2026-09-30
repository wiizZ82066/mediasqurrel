<script setup>
// 媒体库页：作者 -> 条目网格（缩略图封面）+ 全媒体画廊浏览
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api.js'
import { toast } from '../store.js'

const authors = ref([])
const loading = ref(true)
const currentAuthor = ref(null)

// 画廊状态
const gallery = ref(null) // { list, index, author, dateDir }

onMounted(async () => {
  try {
    authors.value = await api.library()
    if (authors.value.length) currentAuthor.value = authors.value[0].name
  } catch (e) {
    toast('媒体库加载失败: ' + e.message, 'error')
  } finally {
    loading.value = false
  }
})

const entries = computed(() => {
  const a = authors.value.find((x) => x.name === currentAuthor.value)
  return a ? a.entries : []
})

function mediaUrl(author, dateDir, rel) {
  return `/media/${encodeURIComponent(author)}/${encodeURIComponent(dateDir)}/${rel.split('/').map(encodeURIComponent).join('/')}`
}

function relPath(author, dateDir, rel) {
  return `${author}/${dateDir}/${rel}`
}

function thumbUrl(author, dateDir, rel) {
  return `/api/thumb?p=${encodeURIComponent(relPath(author, dateDir, rel))}`
}

function fmtSize(n) {
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return n.toFixed(1) + ' ' + units[i]
}

function openGallery(author, entry, startIdx = 0) {
  gallery.value = {
    list: entry.gallery,
    index: startIdx,
    author,
    dateDir: entry.date_dir,
  }
}

function galleryNext() {
  const g = gallery.value
  if (g && g.index < g.list.length - 1) g.index++
}
function galleryPrev() {
  const g = gallery.value
  if (g && g.index > 0) g.index--
}
function closeGallery() {
  gallery.value = null
}

function onKey(evt) {
  if (!gallery.value) return
  if (evt.key === 'ArrowRight') galleryNext()
  else if (evt.key === 'ArrowLeft') galleryPrev()
  else if (evt.key === 'Escape') closeGallery()
}
window.addEventListener('keydown', onKey)
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="view-root">
    <h1 class="page-title">媒体库</h1>
    <p class="page-sub">已下载内容的时间线，点击卡片浏览全部图片与视频</p>

    <div v-if="loading" class="card empty loading-breathe">
      <div class="empty-icon">🗂️</div>
      <p>正在扫描本地存档…</p>
    </div>

    <div v-else-if="!authors.length" class="card empty">
      <div class="empty-icon">📭</div>
      <p>媒体库还是空的，先去下载一些内容吧</p>
      <router-link to="/" class="btn btn-primary" style="text-decoration:none">去下载</router-link>
    </div>

    <template v-else>
      <!-- 作者选择 -->
      <div class="author-bar">
        <button
          v-for="(a, i) in authors"
          :key="a.name"
          class="author-chip stagger-item"
          :class="{ active: a.name === currentAuthor }"
          :style="{ animationDelay: i * 40 + 'ms' }"
          @click="currentAuthor = a.name"
        >
          {{ a.name }}
          <span class="chip-count">{{ a.count }}</span>
        </button>
      </div>

      <!-- 条目网格 -->
      <div class="grid grid-4">
        <div
          v-for="(e, i) in entries"
          :key="e.date_dir"
          class="card hoverable entry-card stagger-item"
          :style="{ animationDelay: i * 40 + 'ms' }"
          @click="openGallery(currentAuthor, e)"
        >
          <div class="entry-cover">
            <img
              v-if="e.cover"
              :src="thumbUrl(currentAuthor, e.date_dir, e.cover)"
              loading="lazy"
              alt=""
              @error="$event.target.style.display = 'none'"
            />
            <div v-else class="cover-fallback">🖼️</div>
            <div class="entry-badges">
              <span v-if="e.videos.length" class="badge-s">🎬 {{ e.videos.length }}</span>
              <span v-if="e.lives.length" class="badge-s">✨ {{ e.lives.length }}</span>
              <span v-if="e.photos.length" class="badge-s">🖼️ {{ e.photos.length }}</span>
            </div>
            <div v-if="e.cover_type === 'video'" class="play-overlay">
              <span class="play-btn">▶</span>
            </div>
          </div>
          <div class="entry-info">
            <div class="entry-date">{{ e.date_dir }}</div>
            <div class="entry-meta">
              {{ e.meta['发布时间'] || '' }} · {{ fmtSize(e.size) }}
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 画廊弹层 -->
    <transition name="fade">
      <div v-if="gallery" class="g-mask" @click="closeGallery">
        <div class="g-stage" @click.stop>
          <!-- 顶部信息栏 -->
          <div class="g-topbar">
            <span class="g-title">
              {{ gallery.author }} · {{ gallery.date_dir }}
            </span>
            <span class="g-counter">
              {{ gallery.index + 1 }} / {{ gallery.list.length }}
            </span>
            <button class="btn btn-ghost btn-sm g-close" @click="closeGallery">关闭 ✕</button>
          </div>

          <!-- 主舞台 -->
          <div class="g-main">
            <button
              class="g-nav g-prev"
              :disabled="gallery.index === 0"
              @click="galleryPrev"
            >‹</button>

            <div class="g-item" :key="gallery.index">
              <img
                v-if="gallery.list[gallery.index].type === 'image'"
                :src="mediaUrl(gallery.author, gallery.dateDir, gallery.list[gallery.index].rel)"
              />
              <video
                v-else
                :src="mediaUrl(gallery.author, gallery.dateDir, gallery.list[gallery.index].rel)"
                controls
                autoplay
              ></video>
            </div>

            <button
              class="g-nav g-next"
              :disabled="gallery.index === gallery.list.length - 1"
              @click="galleryNext"
            >›</button>
          </div>

          <!-- 缩略图导航条 -->
          <div class="g-strip">
            <div
              v-for="(item, idx) in gallery.list"
              :key="item.rel"
              class="g-thumb"
              :class="{ active: idx === gallery.index, video: item.type === 'video' }"
              @click="gallery.index = idx"
            >
              <img :src="thumbUrl(gallery.author, gallery.dateDir, item.rel)" loading="lazy" alt="" />
              <span v-if="item.type === 'video'" class="g-thumb-play">▶</span>
            </div>
          </div>
        </div>
      </div>
    </transition>
  </div>
</template>

<style scoped>
.author-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 22px;
}
.author-chip {
  padding: 8px 16px;
  border-radius: 980px;
  border: 1px solid rgba(0, 0, 0, 0.08);
  background: rgba(255, 255, 255, 0.7);
  font-family: var(--font);
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
  cursor: pointer;
  transition: all 200ms var(--ease);
}
.author-chip:hover { transform: translateY(-1px); background: #fff; }
.author-chip.active {
  background: var(--text);
  color: #fff;
  border-color: var(--text);
}
.chip-count { opacity: 0.6; font-size: 12px; margin-left: 3px; }

.entry-card { padding: 10px; overflow: hidden; cursor: zoom-in; }
.entry-cover {
  position: relative;
  aspect-ratio: 4/3;
  border-radius: 14px;
  overflow: hidden;
  background: rgba(0, 0, 0, 0.04);
}
.entry-cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  transition: transform 400ms var(--ease);
}
.entry-card:hover .entry-cover img { transform: scale(1.045); }
.cover-fallback {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 40px;
}
.entry-badges {
  position: absolute;
  left: 8px;
  bottom: 8px;
  display: flex;
  gap: 5px;
}
.badge-s {
  padding: 2px 8px;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.45);
  backdrop-filter: blur(6px);
  color: #fff;
  font-size: 11px;
  font-weight: 600;
}
.play-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}
.play-btn {
  width: 46px;
  height: 46px;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(8px);
  color: #fff;
  font-size: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding-left: 3px;
}
.entry-info { padding: 10px 6px 4px; }
.entry-date { font-size: 13.5px; font-weight: 700; }
.entry-meta { font-size: 12px; color: var(--text-2); margin-top: 2px; }

/* ---------- 画廊 ---------- */
.g-mask {
  position: fixed;
  inset: 0;
  z-index: 500;
  background: rgba(10, 10, 12, 0.8);
  backdrop-filter: blur(24px);
  display: flex;
  align-items: center;
  justify-content: center;
}
.g-stage {
  width: min(94vw, 1200px);
  max-height: 92vh;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.g-topbar {
  display: flex;
  align-items: center;
  gap: 14px;
  color: #fff;
}
.g-title { font-size: 14px; font-weight: 600; opacity: 0.9; }
.g-counter {
  font-size: 13px;
  opacity: 0.65;
  font-variant-numeric: tabular-nums;
}
.g-close { margin-left: auto; color: #fff; background: rgba(255,255,255,0.14); border: none; }

.g-main {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 200px;
}
.g-item {
  display: flex;
  align-items: center;
  justify-content: center;
  max-width: 100%;
  animation: gItemIn 280ms var(--ease);
}
@keyframes gItemIn {
  from { opacity: 0; transform: scale(0.97); }
  to { opacity: 1; transform: scale(1); }
}
.g-item img, .g-item video {
  max-width: 100%;
  max-height: 66vh;
  border-radius: 14px;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.5);
}
.g-nav {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  z-index: 2;
  width: 44px;
  height: 44px;
  border-radius: 50%;
  border: none;
  background: rgba(255, 255, 255, 0.16);
  backdrop-filter: blur(10px);
  color: #fff;
  font-size: 26px;
  line-height: 1;
  cursor: pointer;
  transition: background 180ms, transform 180ms;
}
.g-nav:hover:not(:disabled) { background: rgba(255, 255, 255, 0.3); }
.g-nav:disabled { opacity: 0.25; cursor: default; }
.g-prev { left: 10px; }
.g-next { right: 10px; }

.g-strip {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding: 6px 2px 10px;
}
.g-thumb {
  position: relative;
  flex-shrink: 0;
  width: 76px;
  height: 57px;
  border-radius: 8px;
  overflow: hidden;
  cursor: pointer;
  opacity: 0.5;
  transition: opacity 180ms, transform 180ms, outline-color 180ms;
  outline: 2px solid transparent;
  outline-offset: 2px;
}
.g-thumb:hover { opacity: 0.85; }
.g-thumb.active {
  opacity: 1;
  outline-color: #fff;
  transform: translateY(-2px);
}
.g-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.g-thumb-play {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 15px;
  background: rgba(0, 0, 0, 0.3);
}

.fade-enter-active { transition: opacity 250ms ease-out; }
.fade-leave-active { transition: opacity 180ms ease-in; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
