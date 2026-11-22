<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api.js'
import { store, onTaskEvent } from '../store.js'

const failed = ref(false)
const labels = [['recording', '录播'], ['running', '下载'], ['scanning', '扫描'], ['queued', '排队']]
const text = computed(() => {
  if (failed.value) return '后台状态暂不可用'
  if (!store.activity) return '正在读取后台状态…'
  return labels.filter(([key]) => store.activity[key] > 0)
    .map(([key, label]) => label + '：' + store.activity[key]).join(' · ') || '空闲'
})
let controller, timer, unsubscribe, stopped = false
async function refresh() {
  if (controller || stopped || document.hidden) return
  clearTimeout(timer)
  const request = new AbortController()
  controller = request
  const timeout = setTimeout(() => request.abort(), 4500)
  try {
    const result = await api.activity(request.signal)
    if (!stopped && !request.signal.aborted) {
      store.activity = { ...result, sampledAt: performance.now() }
      failed.value = false
    }
  } catch {
    if (!stopped && !document.hidden) { store.activity = null; failed.value = true }
  } finally {
    clearTimeout(timeout)
    controller = null
    if (!stopped && !document.hidden) {
      const busy = labels.some(([key]) => store.activity?.[key] > 0)
      timer = setTimeout(refresh, busy || failed.value ? 3000 : 10000)
    }
  }
}
function visibility() {
  clearTimeout(timer)
  if (document.hidden) controller?.abort()
  else refresh()
}
onMounted(() => {
  refresh()
  document.addEventListener('visibilitychange', visibility)
  unsubscribe = onTaskEvent(event => {
    if (['reconnect','activity_changed'].includes(event.type)) refresh()
  })
})
onBeforeUnmount(() => {
  stopped = true
  clearTimeout(timer)
  controller?.abort()
  unsubscribe?.()
  document.removeEventListener('visibilitychange', visibility)
})
</script>
<template>
  <div class="background-status" aria-label="后台状态" :title="'后台状态 · ' + text">
    <span class="dot" :class="{ uncertain: !store.activity }"></span><span>{{ text }}</span>
  </div>
</template>
<style scoped>
.background-status { display:flex; align-items:center; gap:7px; margin:16px 14px 4px; padding-top:14px; border-top:1px solid var(--border); color:var(--text-2); font-size:12px; font-variant-numeric:tabular-nums; }
.dot { flex:none; color:var(--green); }.dot.uncertain { color:var(--orange); }
</style>
