<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'

const bridge = window.mediaSquirrel?.updates
const state = ref({ status: 'idle', version: '', message: '', canInstall: false })
const busy = ref(false)
const deferred = ref(false)
let timer
let polling = false
async function refresh() {
  if (!bridge || polling) return
  polling = true
  try { state.value = await bridge.state() } catch { /* Main process may be restarting. */ }
  finally { polling = false }
}
async function run(action) {
  busy.value = true
  deferred.value = false
  try { state.value = await bridge[action]() }
  catch { state.value.message = '暂时无法操作更新，请稍后重试' }
  finally { busy.value = false }
}
onMounted(() => { if (bridge) { refresh(); timer = setInterval(refresh, 2000) } })
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <section v-if="bridge" class="desktop-update" aria-label="应用更新">
    <div>v{{ state.version }} <span v-if="state.nextVersion">→ v{{ state.nextVersion }}</span></div>
    <p v-if="state.message && !deferred" role="status">{{ state.message }}<span v-if="state.status === 'downloading'">（{{ state.percent }}%）</span></p>
    <button v-if="state.canInstall" class="btn btn-ghost btn-sm" :disabled="busy" @click="run('install')">安装并重启</button>
    <button v-else class="btn btn-ghost btn-sm" :disabled="busy || ['checking', 'downloading', 'installing', 'disabled'].includes(state.status)" @click="run('check')">检查更新</button>
    <button v-if="state.canInstall && !deferred" class="btn btn-ghost btn-sm" @click="deferred = true">稍后</button>
  </section>
</template>

<style scoped>
.desktop-update { padding-bottom: 12px; margin-bottom: 12px; border-bottom: 1px solid var(--border, #ddd); }
.desktop-update p { line-height: 1.5; margin: 8px 0; overflow-wrap: anywhere; }
</style>
