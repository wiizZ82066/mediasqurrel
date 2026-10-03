<script setup>
// 媒体库页：搜索 + 作者/时间线双视图 + 排序筛选 + 摘要卡片 + 画廊（预加载）
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api.js'
import { toast } from '../store.js'

const route = useRoute()
const authors = ref([])
const loading = ref(true)
const currentAuthor = ref(null)

// 视图与筛选状态
const viewMode = ref('author')          // author | timeline
const sortKey = ref('date')             // date | size
const typeFilter = ref('all')           // all | photo | live | video
const query = ref('')

// 画廊状态
const gallery = ref(null) // { list, index, author, dateDir }
const livePlaying = ref(false) // Live 图当前是否悬停播放中

// 卡片 Live 悬停状态：{ [卡片key]: true }
const hoverLive = ref({})

const currentItem = computed(
  () => gallery.value ? gallery.value.list[gallery.value.index] || {} : {},
)

watch(
  () => gallery.value && gallery.value.index,
  () => { livePlaying.value = false },
)

function cardKey(e) {
  return e.author + '/' + e.date_dir
}

// 卡片封面悬停：进入加载 mov 循环静音播放，移出恢复封面
function cardEnter(e) {
  if (e.cover_type === 'live' && e.live_map?.[e.cover]) hoverLive.value[cardKey(e)] = true
}
function cardLeave(e) {
  hoverLive.value[cardKey(e)] = false
}

onMounted(async () => {
  try {
    authors.value = await api.library()
    if (authors.value.length) currentAuthor.value = authors.value[0].name
    // 支持外部跳转定位（任务页「媒体库」按钮）: ?author=xx&entry=yy
    const qAuthor = route.query.author ? decodeURIComponent(route.query.author) : ''
    const qEntry = route.query.entry ? decodeURIComponent(route.query.entry) : ''
    if (qAuthor && authors.value.some((a) => a.name === qAuthor)) {
      currentAuthor.value = qAuthor
      if (qEntry) {
        const entry = authors.value
          .find((a) => a.name === qAuthor)?.entries
          .find((e) => e.date_dir === qEntry)
        if (entry) setTimeout(() => openGallery(qAuthor, entry), 300)
      }
    }
  } catch (e) {
    toast('媒体库加载失败: ' + e.message, 'error')
  } finally {
    loading.value = false
  }
})

// 日期目录 -> 可比较键（'26-09-29' / '2026-09-23-16-17' 统一）
function dateKey(dateDir) {
  const parts = dateDir.split('-')
  const y = parts[0].length === 2 ? '20' + parts[0] : parts[0]
  return [y, ...parts.slice(1)].map((p) => String(p).padStart(2, '0')).join('')
}

// 条目是否通过类型筛选
function passTypeFilter(e) {
  if (typeFilter.value === 'all') return true
  if (typeFilter.value === 'photo') return e.photos.length > 0
  if (typeFilter.value === 'live') return e.lives.length > 0
  if (typeFilter.value === 'video') return e.videos.length > 0
  return true
}

// 搜索：匹配作者 / 日期 / 标题 / 正文摘要
function passSearch(e) {
  const kw = query.value.trim().toLowerCase()
  if (!kw) return true
  return [e.author, e.date_dir, e.text_preview || '', e.meta?.['视频标题'] || '']
    .some((h) => (h || '').toLowerCase().includes(kw))
}

// 重复检测：同作者内视频文件名出现多次的条目
const dupSet = computed(() => {
  const seen = new Map()
  const dup = new Set()
  for (const a of authors.value) {
    for (const e of a.entries) {
      for (const v of e.videos) {
        if (seen.has(v)) {
          dup.add(`${e.author}/${e.date_dir}`)
          dup.add(seen.get(v))
        } else {
          seen.set(v, `${e.author}/${e.date_dir}`)
        }
      }
    }
  }
  return dup
})

// 当前展示的条目（两种视图共用筛选逻辑）
const shownEntries = computed(() => {
  let list
  if (query.value.trim()) {
    // 搜索模式：全作者混排
    list = authors.value.flatMap((a) => a.entries)
  } else if (viewMode.value === 'timeline') {
    list = authors.value.flatMap((a) => a.entries)
  } else {
    const a = authors.value.find((x) => x.name === currentAuthor.value)
    list = a ? a.entries : []
  }
  list = list.filter((e) => passTypeFilter(e) && passSearch(e))
  list = [...list].sort((x, y) =>
    sortKey.value === 'size'
      ? y.size - x.size
      : dateKey(y.date_dir).localeCompare(dateKey(x.date_dir)),
  )
  return list
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

// 条目标题：视频标题 / 正文摘要 / 目录名
function entryTitle(e) {
  return (e.text_preview || e.meta?.['视频标题'] || e.date_dir || '').trim()
}

// 封面裁剪定位：检测到人脸时对准人脸（避免裁剪切脸），否则居中
function coverPos(e) {
  if (e.cover_face) {
    return {
      objectPosition: `${(e.cover_face.x * 100).toFixed(1)}% ${(e.cover_face.y * 100).toFixed(1)}%`,
    }
  }
  return {}
}

// 唯一时间行：优先精确发布时间，其次日期目录（不重复显示两个时间）
function entryTime(e) {
  return e.meta?.['发布时间'] || e.date_dir
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

// 画廊预加载：当前项 ±1 的原图
watch(
  () => gallery.value && gallery.value.index,
  () => {
    const g = gallery.value
    if (!g) return
    for (const idx of [g.index + 1, g.index - 1]) {
      const item = g.list[idx]
      if (item && item.type === 'image') {
        const img = new Image()
        img.src = mediaUrl(g.author, g.dateDir, item.rel)
      }
    }
  },
)

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
      <!-- 工具栏：搜索 + 视图切换 + 排序 + 筛选 -->
      <div class="toolbar card">
        <div class="tb-search">
          <span class="tb-search-icon">🔍</span>
          <input
            v-model="query"
            class="input tb-input"
            placeholder="搜索作者 / 日期 / 标题 / 正文…"
          />
          <button v-if="query" class="tb-clear" @click="query = ''">✕</button>
        </div>

        <div class="tb-group" role="group" aria-label="视图">
          <button class="tb-btn" :class="{ on: viewMode === 'author' && !query }" @click="viewMode = 'author'; query = ''">👤 按作者</button>
          <button class="tb-btn" :class="{ on: viewMode === 'timeline' && !query }" @click="viewMode = 'timeline'; query = ''">📅 时间线</button>
        </div>

        <div class="tb-group" role="group" aria-label="排序">
          <button class="tb-btn" :class="{ on: sortKey === 'date' }" @click="sortKey = 'date'">最新</button>
          <button class="tb-btn" :class="{ on: sortKey === 'size' }" @click="sortKey = 'size'">最大</button>
        </div>

        <div class="tb-group" role="group" aria-label="类型">
          <button class="tb-btn" :class="{ on: typeFilter === 'all' }" @click="typeFilter = 'all'">全部</button>
          <button class="tb-btn" :class="{ on: typeFilter === 'photo' }" @click="typeFilter = 'photo'">🖼️</button>
          <button class="tb-btn" :class="{ on: typeFilter === 'live' }" @click="typeFilter = 'live'">✨</button>
          <button class="tb-btn" :class="{ on: typeFilter === 'video' }" @click="typeFilter = 'video'">🎬</button>
        </div>
      </div>

      <!-- 作者条（仅按作者视图且非搜索时显示） -->
      <div v-if="!query && viewMode === 'author'" class="author-bar">
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

      <!-- 结果计数 -->
      <div class="result-count">
        {{ query ? `搜索 “${query}” 命中` : viewMode === 'timeline' ? '时间线' : currentAuthor }}
        · {{ shownEntries.length }} 条
      </div>

      <!-- 条目网格 -->
      <div v-if="shownEntries.length" class="grid grid-4">
        <div
          v-for="(e, i) in shownEntries"
          :key="cardKey(e)"
          class="card hoverable entry-card stagger-item"
          :style="{ animationDelay: Math.min(i, 12) * 40 + 'ms' }"
          @click="openGallery(e.author, e)"
          @mouseenter="cardEnter(e)"
          @mouseleave="cardLeave(e)"
        >
          <div class="entry-cover">
            <!-- Live 悬停播放（静音循环），默认显示封面 jpg -->
            <video
              v-if="hoverLive[cardKey(e)]"
              class="cover-live-video"
              :src="mediaUrl(e.author, e.date_dir, e.live_map[e.cover])"
              muted
              loop
              autoplay
              playsinline
            ></video>
            <img
              v-else-if="e.cover"
              :src="thumbUrl(e.author, e.date_dir, e.cover)"
              :style="coverPos(e)"
              loading="lazy"
              alt=""
              @error="$event.target.style.display = 'none'"
            />
            <div v-else class="cover-fallback">🖼️</div>
            <div class="entry-badges">
              <span v-if="dupSet.has(`${e.author}/${e.date_dir}`)" class="badge-s badge-dup">♻️ 重复</span>
              <span v-if="e.videos.length" class="badge-s">🎬 {{ e.videos.length }}</span>
              <span v-if="e.lives.length" class="badge-s">✨ {{ e.lives.length }}</span>
              <span v-if="e.photos.length" class="badge-s">🖼️ {{ e.photos.length }}</span>
            </div>
            <div v-if="e.cover_type === 'video'" class="play-overlay">
              <span class="play-btn">▶</span>
            </div>
            <div v-else-if="e.cover_type === 'live' && !hoverLive[cardKey(e)]" class="live-overlay">
              <span class="live-badge">LIVE</span>
            </div>
          </div>
          <div class="entry-info">
            <div class="entry-head">
              <span class="entry-title">{{ entryTitle(e) }}</span>
              <span v-if="query || viewMode === 'timeline'" class="entry-author">{{ e.author }}</span>
            </div>
            <div class="entry-meta">
              {{ entryTime(e) }} · {{ fmtSize(e.size) }}
            </div>
          </div>
        </div>
      </div>

      <div v-else class="card empty">
        <div class="empty-icon">🔍</div>
        <p>没有匹配的内容</p>
      </div>
    </template>

    <!-- 画廊弹层 -->
    <transition name="fade">
      <div v-if="gallery" class="g-mask" @click="closeGallery">
        <div class="g-stage" @click.stop>
          <div class="g-topbar">
            <span class="g-title">
              {{ gallery.author }} · {{ gallery.dateDir }}
            </span>
            <span class="g-counter">
              {{ gallery.index + 1 }} / {{ gallery.list.length }}
            </span>
            <button class="btn btn-ghost btn-sm g-close" @click="closeGallery">关闭 ✕</button>
          </div>

          <div class="g-main">
            <button class="g-nav g-prev" :disabled="gallery.index === 0" @click="galleryPrev">‹</button>

            <div class="g-item" :key="gallery.index">
              <!-- Live 图：默认封面 jpg，悬停自动加载播放 mov，移出恢复封面 -->
              <div
                v-if="currentItem.live && currentItem.poster"
                class="g-live"
                @mouseenter="livePlaying = true"
                @mouseleave="livePlaying = false"
              >
                <img
                  v-if="!livePlaying"
                  :src="mediaUrl(gallery.author, gallery.dateDir, currentItem.poster)"
                />
                <video
                  v-else
                  :src="mediaUrl(gallery.author, gallery.dateDir, currentItem.rel)"
                  loop
                  autoplay
                  playsinline
                  controls
                ></video>
                <span v-if="!livePlaying" class="live-badge">LIVE</span>
              </div>
              <img
                v-else-if="currentItem.type === 'image'"
                :src="mediaUrl(gallery.author, gallery.dateDir, currentItem.rel)"
              />
              <video
                v-else
                :src="mediaUrl(gallery.author, gallery.dateDir, currentItem.rel)"
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

          <div class="g-strip">
            <div
              v-for="(item, idx) in gallery.list"
              :key="item.rel"
              class="g-thumb"
              :class="{ active: idx === gallery.index, video: item.type === 'video' }"
              @click="gallery.index = idx"
            >
              <img
                :src="thumbUrl(gallery.author, gallery.dateDir, item.poster || item.rel)"
                loading="lazy"
                alt=""
              />
              <span v-if="item.type === 'video' && !item.live" class="g-thumb-play">▶</span>
              <span v-else-if="item.live" class="g-thumb-live">LIVE</span>
            </div>
          </div>
        </div>
      </div>
    </transition>
  </div>
</template>

<style scoped>
/* ---------- 工具栏 ---------- */
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  padding: 14px 18px;
  margin-bottom: 18px;
}
.tb-search {
  position: relative;
  flex: 1;
  min-width: 220px;
}
.tb-search-icon {
  position: absolute;
  left: 12px;
  top: 50%;
  transform: translateY(-50%);
  font-size: 14px;
  opacity: 0.5;
}
.tb-input { padding-left: 36px; padding-right: 34px; }
.tb-clear {
  position: absolute;
  right: 10px;
  top: 50%;
  transform: translateY(-50%);
  border: none;
  background: rgba(0, 0, 0, 0.08);
  color: var(--text-2);
  width: 20px;
  height: 20px;
  border-radius: 50%;
  cursor: pointer;
  font-size: 11px;
  line-height: 1;
}
.tb-clear:hover { background: rgba(0, 0, 0, 0.16); }

.tb-group {
  display: flex;
  background: rgba(0, 0, 0, 0.05);
  border-radius: 10px;
  padding: 3px;
  gap: 2px;
}
.tb-btn {
  border: none;
  background: transparent;
  font-family: var(--font);
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-2);
  padding: 6px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 180ms var(--ease);
  white-space: nowrap;
}
.tb-btn:hover { color: var(--text); }
.tb-btn.on {
  background: #fff;
  color: var(--text);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
}

.author-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 16px;
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

.result-count {
  font-size: 12.5px;
  color: var(--text-2);
  margin-bottom: 14px;
}

/* ---------- 条目卡片 ---------- */
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
  flex-wrap: wrap;
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
.badge-dup { background: rgba(255, 149, 0, 0.85); }
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
.live-overlay {
  position: absolute;
  left: 8px;
  top: 8px;
  pointer-events: none;
}
.live-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 7px;
  background: rgba(255, 59, 48, 0.9);
  color: #fff;
  font-size: 10.5px;
  font-weight: 800;
  letter-spacing: 0.5px;
}
.live-badge::before {
  content: '';
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #fff;
}
.entry-info { padding: 10px 6px 4px; }
.entry-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.entry-title {
  font-size: 13.5px;
  font-weight: 700;
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
  min-width: 0;
}
.entry-author {
  font-size: 11.5px;
  color: var(--blue);
  font-weight: 600;
  background: rgba(0, 113, 227, 0.1);
  padding: 1px 8px;
  border-radius: 7px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 90px;
  flex-shrink: 0;
}
.entry-meta { font-size: 11.5px; color: var(--text-2); margin-top: 4px; }

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

/* Live 图：悬停播放（默认封面态） */
.g-live { position: relative; }
.g-live img,
.g-live video {
  max-width: 100%;
  max-height: 66vh;
  border-radius: 14px;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.5);
  display: block;
}
.g-live .live-badge {
  position: absolute;
  left: 14px;
  top: 14px;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.3);
  pointer-events: none;
}

/* 卡片 Live 悬停播放 */
.cover-live-video {
  width: 100%;
  height: 100%;
  object-fit: cover;
  background: #000;
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
.g-thumb-live {
  position: absolute;
  left: 4px;
  top: 4px;
  padding: 1px 5px;
  border-radius: 5px;
  background: rgba(255, 59, 48, 0.9);
  color: #fff;
  font-size: 8.5px;
  font-weight: 800;
  letter-spacing: 0.5px;
}

.fade-enter-active { transition: opacity 250ms ease-out; }
.fade-leave-active { transition: opacity 180ms ease-in; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
