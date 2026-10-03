<script setup>
// 订阅页：博主搜索（本地存档 + 线上搜索前5粉丝降序）+ CRUD + 手动扫描
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api } from '../api.js'
import { toast } from '../store.js'

const subs = ref([])
const loading = ref(true)
const scanning = ref({})
const adding = ref(false)
const form = reactive({
  platform: 'douyin',
  blogger_id: '',
  nickname: '',
  homepage: '',
  interval_minutes: 30,
})

// ---------- 博主搜索 ----------
const localAuthors = ref([])      // 本地存档作者
const onlineResults = ref([])     // 线上搜索结果
const searching = ref(false)      // 线上搜索 loading
const searchOpen = ref(false)     // 下拉开合
let debounceTimer = null
let searchSeq = 0                 // 过期响应丢弃

const keyword = computed(() => form.nickname.trim())
const kwLower = computed(() => keyword.value.toLowerCase())

// 本地匹配（即时）
const localMatches = computed(() => {
  if (!kwLower.value) return localAuthors.value.slice(0, 6)
  return localAuthors.value
    .filter((a) => a.name.toLowerCase().includes(kwLower.value))
    .slice(0, 6)
})

async function refresh() {
  try {
    subs.value = await api.subs()
  } catch (e) {
    toast('订阅加载失败: ' + e.message, 'error')
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  refresh()
  try {
    localAuthors.value = await api.localAuthors()
  } catch { /* 静默 */ }
})

// 昵称输入 -> 防抖 800ms 触发线上搜索
watch(keyword, (kw) => {
  onlineResults.value = []
  clearTimeout(debounceTimer)
  if (!kw) {
    searchOpen.value = false
    return
  }
  searchOpen.value = true
  debounceTimer = setTimeout(runOnlineSearch, 800)
})

async function runOnlineSearch() {
  const kw = keyword.value
  if (!kw) return
  const seq = ++searchSeq
  searching.value = true
  try {
    const r = await api.searchBlogger(form.platform, kw)
    if (seq === searchSeq) onlineResults.value = r.results || []
  } catch {
    if (seq === searchSeq) onlineResults.value = []
  } finally {
    if (seq === searchSeq) searching.value = false
  }
}

// 平台切换时，若有关键词则重搜
watch(() => form.platform, () => {
  if (keyword.value) runOnlineSearch()
})

function pickLocal(a) {
  // 本地作者可能有多平台身份：优先当前选中平台
  const platforms = a.platforms || {}
  const pid = platforms[form.platform]
  if (pid) {
    form.blogger_id = pid
    form.nickname = a.name
  } else {
    const [plat, id] = Object.entries(platforms)[0] || []
    if (plat && id) {
      form.platform = plat
      form.blogger_id = id
      form.nickname = a.name
    } else {
      form.nickname = a.name
      toast('该作者暂无可订阅的平台 ID（旧存档未记录）', 'info')
    }
  }
  searchOpen.value = false
}

function pickOnline(u) {
  form.blogger_id = u.blogger_id
  form.nickname = u.nickname
  searchOpen.value = false
}

function closeDropdown() {
  setTimeout(() => { searchOpen.value = false }, 150)
}

onBeforeUnmount(() => clearTimeout(debounceTimer))

// ---------- CRUD ----------
async function add() {
  if (!form.blogger_id.trim()) {
    toast('博主 ID 不能为空（可从搜索结果选择）', 'error')
    return
  }
  adding.value = true
  try {
    await api.addSub({ ...form })
    toast('订阅已添加', 'success')
    form.blogger_id = ''
    form.nickname = ''
    form.homepage = ''
    refresh()
  } catch (e) {
    toast(e.message, 'error')
  } finally {
    adding.value = false
  }
}

async function toggle(s) {
  await api.updateSub(s.id, { enabled: s.enabled ? 0 : 1 })
  refresh()
}

async function remove(s) {
  await api.removeSub(s.id)
  toast(`已删除订阅：${s.nickname || s.blogger_id}`, 'info')
  refresh()
}

async function scanNow(s) {
  scanning.value[s.id] = true
  try {
    const r = await api.scanSub(s.id)
    if (r.error) toast(`扫描失败: ${r.error}`, 'error')
    else if (r.new_items?.length) toast(`发现 ${r.new_items.length} 条新内容，已入队`, 'success')
    else toast('暂无新内容', 'info')
  } catch (e) {
    toast(e.message, 'error')
  } finally {
    scanning.value[s.id] = false
    refresh()
  }
}

const platformName = { douyin: '抖音', weibo: '微博' }
</script>

<template>
  <div class="view-root">
    <h1 class="page-title">博主订阅</h1>
    <p class="page-sub">搜索博主（本地存档 / 线上），订阅后自动扫描新内容并下载</p>

    <!-- 新增订阅 -->
    <div class="card" style="margin-bottom: 22px">
      <div class="add-grid">
        <div class="field">
          <label>平台</label>
          <select class="select" v-model="form.platform">
            <option value="douyin">抖音</option>
            <option value="weibo">微博</option>
          </select>
        </div>

        <!-- 博主搜索输入 -->
        <div class="field search-field">
          <label>搜索博主 <span style="font-weight:400;color:var(--text-2)">（输入昵称，选择结果自动填充）</span></label>
          <div class="search-box">
            <input
              class="input"
              v-model="form.nickname"
              placeholder="输入博主昵称…"
              @focus="searchOpen = true"
              @blur="closeDropdown"
            />
            <span v-if="searching" class="search-spin loading-breathe">⏳</span>
            <transition name="fade">
              <div v-if="searchOpen && (localMatches.length || onlineResults.length || searching)" class="search-drop">
                <!-- 本地存档作者 -->
                <template v-if="localMatches.length">
                  <div class="drop-sec">📁 本地已有博主</div>
                  <button
                    v-for="a in localMatches"
                    :key="a.name"
                    class="drop-item"
                    @mousedown.prevent="pickLocal(a)"
                  >
                    <span class="drop-avatar">{{ a.name.slice(0, 1) }}</span>
                    <span class="drop-name">{{ a.name }}</span>
                    <span class="drop-meta">
                      <span v-if="a.platforms.weibo" class="mini-tag">微博</span>
                      <span v-if="a.platforms.douyin" class="mini-tag">抖音</span>
                      {{ a.entries }} 条
                    </span>
                  </button>
                </template>

                <!-- 线上搜索 -->
                <div class="drop-sec">
                  🌐 线上搜索{{ searching ? '中…' : `（${platformName[form.platform]}，按粉丝数）` }}
                </div>
                <div v-if="searching && !onlineResults.length" class="drop-loading loading-breathe">
                  正在搜索，约需数秒…
                </div>
                <button
                  v-for="u in onlineResults"
                  :key="u.blogger_id"
                  class="drop-item"
                  @mousedown.prevent="pickOnline(u)"
                >
                  <img v-if="u.avatar" class="drop-avatar img" :src="u.avatar" referrerpolicy="no-referrer" alt="" />
                  <span v-else class="drop-avatar">{{ (u.nickname || '?').slice(0, 1) }}</span>
                  <span class="drop-name">
                    {{ u.nickname }}
                    <span v-if="u.verified" class="v-badge" title="认证">✓</span>
                  </span>
                  <span class="drop-meta">{{ u.followers_text || '粉丝数未知' }}</span>
                </button>
                <div v-if="!searching && !onlineResults.length && keyword" class="drop-empty">
                  <template v-if="form.platform === 'douyin'">
                    抖音线上搜索受平台风控限制<br/>
                    可从上方「本地已有博主」选择，或手动粘贴 sec_uid
                  </template>
                  <template v-else>未搜到结果，可直接手动填写 ID</template>
                </div>
              </div>
            </transition>
          </div>
        </div>

        <div class="field">
          <label>博主 ID <span style="color: var(--red)">*</span></label>
          <input
            class="input"
            v-model="form.blogger_id"
            :placeholder="form.platform === 'weibo'
              ? '微博数字 UID，如 1234567890'
              : '抖音 sec_uid（从搜索结果选择自动填充）'"
          />
          <div class="hint" style="margin-top:6px">
            {{ form.platform === 'weibo'
              ? '可从微博主页链接 weibo.com/u/数字 中提取'
              : '以 MS4wLjAB 开头的长串，新下载的抖音存档 context.md 已自动记录' }}
          </div>
        </div>
        <div class="field">
          <label>扫描间隔（分钟）</label>
          <input class="input" type="number" v-model.number="form.interval_minutes" min="5" />
        </div>
      </div>
      <button class="btn btn-primary" :disabled="adding" @click="add">
        {{ adding ? '添加中…' : '➕ 添加订阅' }}
      </button>
    </div>

    <!-- 订阅列表 -->
    <div v-if="loading" class="card empty loading-breathe">
      <div class="empty-icon">🔔</div><p>加载中…</p>
    </div>
    <div v-else-if="!subs.length" class="card empty">
      <div class="empty-icon">🌱</div>
      <p>还没有订阅任何博主</p>
    </div>

    <div v-else class="sub-list">
      <div v-for="(s, i) in subs" :key="s.id" class="card sub-card stagger-item" :style="{ animationDelay: i * 40 + 'ms' }">
        <div class="sub-head">
          <span class="sub-avatar">{{ (s.nickname || s.blogger_id).slice(0, 1) }}</span>
          <div class="sub-info">
            <div class="sub-name">
              {{ s.nickname || s.blogger_id }}
              <span class="tag" style="background: rgba(0,113,227,.1); color: var(--blue)">
                {{ platformName[s.platform] || s.platform }}
              </span>
            </div>
            <div class="sub-meta">
              每 {{ s.interval_minutes }} 分钟扫描
              <template v-if="s.last_scan_at"> · 上次 {{ s.last_scan_at.replace('T', ' ') }}</template>
              <template v-if="s.last_status === 'error'"> · <span style="color:var(--red)">上次出错</span></template>
            </div>
          </div>
          <label class="switch" @click.stop>
            <input type="checkbox" :checked="!!s.enabled" @change="toggle(s)" />
            <span class="track"><span class="thumb"></span></span>
          </label>
        </div>
        <div class="sub-actions">
          <button class="btn btn-ghost btn-sm" :disabled="scanning[s.id]" @click="scanNow(s)">
            {{ scanning[s.id] ? '扫描中…' : '⚡ 立即扫描' }}
          </button>
          <button class="btn btn-danger-ghost btn-sm" @click="remove(s)">删除</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.add-grid {
  display: grid;
  grid-template-columns: 130px 1.4fr 1fr 160px;
  gap: 14px;
  margin-bottom: 4px;
}
@media (max-width: 900px) { .add-grid { grid-template-columns: 1fr 1fr; } }

/* ---------- 搜索下拉 ---------- */
.search-field { position: relative; z-index: 30; }
.search-box { position: relative; }
.search-spin {
  position: absolute;
  right: 12px;
  top: 50%;
  transform: translateY(-50%);
  font-size: 14px;
}
.search-drop {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  right: 0;
  max-height: 340px;
  overflow-y: auto;
  background: rgba(252, 252, 253, 0.98);
  backdrop-filter: blur(20px);
  border: 1px solid rgba(0, 0, 0, 0.08);
  border-radius: 14px;
  box-shadow: 0 16px 50px rgba(0, 0, 0, 0.16);
  padding: 6px;
  z-index: 40;
}
.drop-sec {
  font-size: 11.5px;
  font-weight: 700;
  color: var(--text-2);
  padding: 8px 10px 4px;
}
.drop-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 10px;
  border: none;
  border-radius: 10px;
  background: transparent;
  font-family: var(--font);
  font-size: 13.5px;
  cursor: pointer;
  text-align: left;
  transition: background 120ms;
}
.drop-item:hover { background: rgba(0, 0, 0, 0.05); }
.drop-avatar {
  width: 30px;
  height: 30px;
  border-radius: 50%;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 700;
  color: #fff;
  background: linear-gradient(135deg, #0071e3, #34c759);
}
.drop-avatar.img {
  object-fit: cover;
  background: #eee;
}
.drop-name {
  flex: 1;
  font-weight: 600;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: flex;
  align-items: center;
  gap: 5px;
}
.v-badge {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #ff9500;
  color: #fff;
  font-size: 10px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.drop-meta {
  font-size: 12px;
  color: var(--text-2);
  display: flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
}
.mini-tag {
  padding: 1px 6px;
  border-radius: 6px;
  background: rgba(0, 113, 227, 0.1);
  color: var(--blue);
  font-size: 10.5px;
  font-weight: 600;
}
.drop-loading, .drop-empty {
  padding: 14px 10px;
  font-size: 12.5px;
  color: var(--text-2);
  text-align: center;
}

.fade-enter-active { transition: opacity 180ms ease-out; }
.fade-leave-active { transition: opacity 120ms ease-in; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* ---------- 订阅列表 ---------- */
.sub-list { display: flex; flex-direction: column; gap: 13px; }
.sub-card { padding: 18px 22px; }
.sub-head { display: flex; align-items: center; gap: 14px; }
.sub-avatar {
  width: 42px;
  height: 42px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 17px;
  font-weight: 700;
  color: #fff;
  background: linear-gradient(135deg, #0071e3, #34c759);
  flex-shrink: 0;
}
.sub-info { flex: 1; min-width: 0; }
.sub-name { font-size: 15px; font-weight: 700; display: flex; align-items: center; gap: 9px; }
.sub-meta { font-size: 12.5px; color: var(--text-2); margin-top: 3px; }
.sub-actions { margin-top: 13px; display: flex; gap: 9px; }
</style>
