<script setup>
import AppIcon from './AppIcon.vue'
import ActivityProgress from './ActivityProgress.vue'
defineProps({ completed: Number, stage: String, error: String })
defineEmits(['retry', 'continue'])
</script>
<template>
  <div class="startup-screen" aria-labelledby="startup-title" :aria-busy="!error">
    <div class="startup-content"><AppIcon name="brand" :size="74"/><h1 id="startup-title">Media Squirrel</h1><p>你的本地媒体空间</p>
      <ActivityProgress :status="error ? 'failed' : completed === 2 ? 'success' : 'running'" :progress="{percent:completed * 50,label:error || stage}" label="网页加载进度"/>
      <div v-if="error" class="startup-actions"><button class="btn btn-primary btn-sm" @click="$emit('retry')">重新连接</button><button class="btn btn-ghost btn-sm" @click="$emit('continue')">先打开页面</button></div>
    </div>
  </div>
</template>
<style scoped>
.startup-screen { position:fixed; inset:0; z-index:1200; display:grid; place-items:center; background:#fff; padding:24px; }
.startup-content { width:min(100%,320px); text-align:center; }.startup-content h1 { font-size:25px; margin:14px 0 8px; font-weight:650; letter-spacing:-.03em; }.startup-content>p { font-size:13px; color:var(--text-2); margin-bottom:30px; }.startup-content :deep(.progress-caption) { text-align:left; line-height:1.6; }.startup-actions { display:flex; gap:10px; justify-content:center; margin-top:24px; }
</style>
