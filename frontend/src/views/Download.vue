<script setup>
// 下载页：选脚本 -> 动态渲染 manifest 表单 -> 提交任务
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api.js'
import { toast } from '../store.js'

const router = useRouter()
const scripts = ref([])
const selectedId = ref('')
const form = ref({})
const submitting = ref(false)

const selected = computed(() => scripts.value.find((s) => s.id === selectedId.value))

onMounted(async () => {
  try {
    scripts.value = await api.scripts()
    if (scripts.value.length) select(scripts.value[0].id)
  } catch (e) {
    toast('加载脚本清单失败: ' + e.message, 'error')
  }
})

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

async function submit() {
  if (!selected.value) return
  submitting.value = true
  try {
    const task = await api.createTask(selectedId.value, { ...form.value })
    toast(`${selected.value.icon} 已创建任务，正在排队执行`, 'success')
    router.push('/tasks')
  } catch (e) {
    toast(e.message, 'error')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
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
        <textarea
          v-if="p.kind === 'textarea'"
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
</style>
