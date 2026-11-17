<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api.js'

const props = defineProps({ entry: { type: Object, required: true } })
const emit = defineEmits(['close', 'recycled', 'prepare', 'failed'])
const dialog = ref(null)
const plan = ref(null)
const error = ref('')
const loading = ref(false)
const submitting = ref(false)
let controller
let disposed = false

const size = computed(() => {
  const bytes = plan.value?.bytes || 0
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 ** 2) return (bytes / 1024).toFixed(1) + ' KiB'
  if (bytes < 1024 ** 3) return (bytes / 1024 ** 2).toFixed(1) + ' MiB'
  return (bytes / 1024 ** 3).toFixed(2) + ' GiB'
})

async function load() {
  controller?.abort()
  controller = new AbortController()
  loading.value = true
  plan.value = null
  error.value = ''
  try {
    plan.value = await api.recyclePreview(props.entry.id, controller.signal)
  } catch (failure) {
    if (failure.name !== 'AbortError') error.value = failure.message
  } finally {
    loading.value = false
  }
}

async function submit() {
  if (submitting.value || !plan.value) return
  submitting.value = true
  emit('prepare')
  try {
    const result = await api.recycleEntry(props.entry.id, plan.value.revision)
    if (!disposed) emit('recycled', result)
  } catch (failure) {
    plan.value = null
    error.value = failure.message
    emit('failed')
  } finally {
    submitting.value = false
  }
}

onMounted(() => { dialog.value.showModal(); load() })
onBeforeUnmount(() => { disposed = true; controller?.abort(); dialog.value?.close() })
</script>

<template>
  <Teleport to="body">
    <dialog ref="dialog" class="recycle-dialog" aria-labelledby="recycle-title" @keydown.stop @cancel.prevent="!submitting && $emit('close')">
      <h2 id="recycle-title">移入回收站？</h2>
      <p class="recycle-entry"><strong>{{ entry.author }}</strong> · {{ entry.text_preview || entry.date_dir }}</p>
      <p v-if="loading" role="status">正在读取文件清单…</p>
      <p v-if="error" class="recycle-error" role="alert">{{ error }} <button type="button" class="btn btn-ghost btn-sm" @click="load">重新查看清单</button></p>
      <template v-if="plan">
        <p class="recycle-total">{{ plan.archives.length }} 个目录 · {{ plan.file_count }} 个文件 · {{ size }}</p>
        <div class="recycle-files" tabindex="0" aria-label="将移入回收站的文件">
          <section v-for="archive in plan.archives" :key="archive.entry_id">
            <h3>{{ archive.directory }}</h3><small>{{ archive.root }}</small>
            <ul><li v-for="file in archive.files" :key="file.path">{{ file.path }}</li></ul>
          </section>
        </div>
        <p>任务记录和日志会保留。从系统回收站还原后，点击媒体库的「更新索引」即可重新查看。</p>
        <p v-if="plan.other_copies">这条作品另有 {{ plan.other_copies }} 份独立存档，仍可在媒体库查看。</p>
      </template>
      <footer>
        <button class="btn btn-ghost" autofocus :disabled="submitting" @click="$emit('close')">取消</button>
        <button class="btn recycle-confirm" :disabled="!plan || loading || submitting" @click="submit">{{ submitting ? '正在移入…' : '移入回收站' }}</button>
      </footer>
    </dialog>
  </Teleport>
</template>

<style scoped>
.recycle-dialog { box-sizing:border-box; width:min(500px,calc(100vw - 32px)); max-height:calc(100dvh - 48px); padding:24px; margin:auto; border:1px solid var(--border); border-radius:18px; background:var(--surface,#fff); color:var(--text); box-shadow:0 18px 64px #17232f36; }
.recycle-dialog::backdrop { background:#16222f60; }
.recycle-dialog h2 { margin:0 0 8px; font-size:21px; }
.recycle-dialog p { font-size:13px; line-height:1.55; color:var(--text-2); margin:10px 0; }
.recycle-entry { overflow-wrap:anywhere; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.recycle-dialog .recycle-total { color:var(--text); font-weight:600; }
.recycle-files { max-height:min(36dvh,310px); overflow:auto; padding:12px 15px; border:1px solid var(--border); border-radius:10px; background:#f7f8fa; overflow-wrap:anywhere; }
.recycle-files h3 { margin:0; font-size:13px; }.recycle-files small { color:var(--text-2); }.recycle-files ul { margin:8px 0; padding-left:18px; font-size:12px; line-height:1.65; }
.recycle-dialog footer { display:flex; justify-content:flex-end; gap:10px; margin-top:20px; }
.recycle-dialog .recycle-error { color:#9c372c; }.recycle-confirm { background:#a5382c; color:#fff; }.recycle-confirm:disabled { opacity:.45; }
</style>
