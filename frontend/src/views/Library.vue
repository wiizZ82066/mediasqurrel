<script setup>
// 媒体库页：作者 -> 条目时间线画廊 + 大图预览
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'
import { toast } from '../store.js'

const authors = ref([])
const loading = ref(true)
const currentAuthor = ref(null)
const preview = ref(null) // {type:'image'|'video', url}

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

function fmtSize(n) {
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return n.toFixed(1) + ' ' + units[i]
}

function openPreview(author, entry, rel) {
  const url = mediaUrl(author, entry.date_dir, rel)
  preview.value = /\.(mp4|mov)$/i.test(rel)
    ? { type: 'video', url }
    : { type: 'image', url }
}

function closePreview() {
  preview.value = null
}
</script>

<template>
  <h1 class="page-title">媒体库</h1>
  <p class="page-sub">已下载内容的时间线，点击卡片预览</p>

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
      >
        <div class="entry-cover" @click="openPreview(currentAuthor, e, e.cover || e.videos[0] || e.photos[0])">
          <img
            v-if="e.cover"
            :src="mediaUrl(currentAuthor, e.date_dir, e.cover)"
            loading="lazy"
            alt=""
          />
          <div v-else class="cover-video">🎬</div>
          <div class="entry-badges">
            <span v-if="e.videos.length" class="badge-s">🎬 {{ e.videos.length }}</span>
            <span v-if="e.lives.length" class="badge-s">✨ {{ e.lives.length }}</span>
            <span v-if="e.photos.filter(p => !p.startsWith('live/')).length" class="badge-s">
              🖼️ {{ e.photos.filter(p => !p.startsWith('live/')).length }}
            </span>
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

  <!-- 预览弹层 -->
  <transition name="fade">
    <div v-if="preview" class="preview-mask" @click="closePreview">
      <div class="preview-body" @click.stop>
        <img v-if="preview.type === 'image'" :src="preview.url" />
        <video v-else :src="preview.url" controls autoplay></video>
        <button class="btn btn-ghost preview-close" @click="closePreview">关闭</button>
      </div>
    </div>
  </transition>
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

.entry-card { padding: 10px; overflow: hidden; }
.entry-cover {
  position: relative;
  aspect-ratio: 4/3;
  border-radius: 14px;
  overflow: hidden;
  background: rgba(0, 0, 0, 0.04);
  cursor: zoom-in;
}
.entry-cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  transition: transform 400ms var(--ease);
}
.entry-card:hover .entry-cover img { transform: scale(1.045); }
.cover-video {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 40px;
  background: linear-gradient(135deg, rgba(0, 113, 227, 0.08), rgba(52, 199, 89, 0.08));
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
.entry-info { padding: 10px 6px 4px; }
.entry-date { font-size: 13.5px; font-weight: 700; }
.entry-meta { font-size: 12px; color: var(--text-2); margin-top: 2px; }

.preview-mask {
  position: fixed;
  inset: 0;
  z-index: 500;
  background: rgba(10, 10, 12, 0.72);
  backdrop-filter: blur(24px);
  display: flex;
  align-items: center;
  justify-content: center;
}
.preview-body {
  max-width: min(88vw, 1100px);
  max-height: 86vh;
  display: flex;
  flex-direction: column;
  gap: 14px;
  align-items: center;
}
.preview-body img, .preview-body video {
  max-width: 100%;
  max-height: 76vh;
  border-radius: 16px;
  box-shadow: 0 30px 90px rgba(0, 0, 0, 0.5);
}
.preview-close { color: #fff; background: rgba(255, 255, 255, 0.15); border: none; }

.fade-enter-active { transition: opacity 250ms ease-out; }
.fade-leave-active { transition: opacity 180ms ease-in; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
