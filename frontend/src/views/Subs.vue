<script setup>
// 订阅页：博主订阅 CRUD + 手动扫描（扫描引擎待接入平台实现）
import { onMounted, reactive, ref } from 'vue'
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

async function refresh() {
  try {
    subs.value = await api.subs()
  } catch (e) {
    toast('订阅加载失败: ' + e.message, 'error')
  } finally {
    loading.value = false
  }
}
onMounted(refresh)

async function add() {
  if (!form.blogger_id.trim()) {
    toast('博主 ID 不能为空', 'error')
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
    else if (r.new_items.length) toast(`发现 ${r.new_items.length} 条新内容，已入队`, 'success')
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
  <h1 class="page-title">博主订阅</h1>
  <p class="page-sub">订阅博主后，系统按间隔自动扫描新内容并下载（扫描引擎陆续接入中）</p>

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
      <div class="field">
        <label>博主 ID <span style="color: var(--red)">*</span></label>
        <input class="input" v-model="form.blogger_id" placeholder="数字 UID，如 1234567890" />
      </div>
      <div class="field">
        <label>昵称（备注）</label>
        <input class="input" v-model="form.nickname" placeholder="显示名，如 某某" />
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
</template>

<style scoped>
.add-grid {
  display: grid;
  grid-template-columns: 130px 1fr 1fr 160px;
  gap: 14px;
  margin-bottom: 4px;
}
@media (max-width: 900px) { .add-grid { grid-template-columns: 1fr 1fr; } }
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
