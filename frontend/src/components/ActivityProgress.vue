<script setup>
import { computed } from 'vue'

const props = defineProps({
  progress: { type: Object, default: null },
  status: { type: String, default: 'running' },
  label: { type: String, default: '进度' },
})

const active = computed(() => props.status === 'running')
const percent = computed(() => {
  if (props.status === 'success') return 100
  const value = props.progress?.percent
  return typeof value === 'number' && Number.isFinite(value)
    ? Math.min(99, Math.max(0, value)) : null
})
const indeterminate = computed(() => active.value && percent.value == null)
const message = computed(() => props.progress?.label || {
  queued: '等待开始', running: '正在处理…', success: '已完成',
  failed: '处理失败', cancelled: '已取消', interrupted: '运行已中断',
}[props.status] || '等待开始')
const valueText = computed(() => {
  if (percent.value != null) return `${Math.floor(percent.value)}%`
  return active.value ? '总量未知' : { failed: '失败', cancelled: '已取消', interrupted: '已中断' }[props.status] || '等待中'
})

function formatBytes(value) {
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`
  return `${(value / 1024 ** 3).toFixed(1)} GB`
}

const detail = computed(() => {
  const progress = props.progress || {}
  const parts = []
  if (progress.total > 0 && progress.completed != null) {
    parts.push(`已完成 ${progress.completed} / ${progress.total} ${progress.unit || ''}`)
  }
  if (active.value && progress.bytes_done > 0) {
    parts.push(`当前文件 ${formatBytes(progress.bytes_done)}${progress.bytes_total > 0 ? ` / ${formatBytes(progress.bytes_total)}` : ''}`)
  }
  return parts.join(' · ')
})
</script>

<template>
  <div class="activity-progress" :class="'is-' + status">
    <div class="progress-caption">
      <span class="progress-message">{{ message }}</span>
      <span class="progress-value">{{ valueText }}</span>
    </div>
    <div
      class="progress-track"
      role="progressbar"
      :aria-label="label"
      aria-valuemin="0"
      aria-valuemax="100"
      :aria-valuenow="percent == null ? undefined : Math.floor(percent)"
      :aria-valuetext="`${message}，${valueText}${detail ? '，' + detail : ''}`"
      :aria-busy="active"
    >
      <!-- Unknown activity has no measured width and never grows into a percentage. -->
      <span v-if="indeterminate" class="progress-indeterminate" aria-hidden="true"></span>
      <span v-else class="progress-fill" :style="{ width: `${percent ?? 0}%` }" aria-hidden="true"></span>
    </div>
    <div v-if="detail" class="progress-detail">{{ detail }}</div>
  </div>
</template>

<style scoped>
.activity-progress { margin-top: 14px; }
.progress-caption { display: flex; align-items: baseline; gap: 12px; margin-bottom: 7px; font-size: 12px; color: var(--text-2); }
.progress-message { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.progress-value { flex-shrink: 0; font-variant-numeric: tabular-nums; }
.progress-track { position: relative; height: 6px; overflow: hidden; border-radius: 999px; background: rgba(0, 113, 227, .09); }
.progress-fill { display: block; height: 100%; border-radius: inherit; background: var(--blue); transition: width 250ms var(--ease), background 250ms var(--ease) !important; }
/* Explicit user preference: progress alone animates independently of OS motion settings. */
.progress-indeterminate { position: absolute; inset: 0; }
.progress-indeterminate::before { content: ''; display: block; width: 30%; height: 100%; border-radius: 999px; background: var(--blue); animation: progress-slide 1.4s linear infinite !important; }
.progress-detail { margin-top: 6px; font-size: 11.5px; line-height: 1.5; color: var(--text-2); font-variant-numeric: tabular-nums; }
.is-success .progress-fill { background: var(--green); }
.is-failed .progress-fill { background: var(--red); }
.is-failed .progress-track { background: rgba(255, 59, 48, .1); }
.is-failed .progress-message { color: #c52a20; }
.is-cancelled .progress-fill, .is-queued .progress-fill, .is-interrupted .progress-fill { background: #a1a1a6; }
@keyframes progress-slide { from { transform: translateX(-100%); } to { transform: translateX(333.334%); } }
</style>
