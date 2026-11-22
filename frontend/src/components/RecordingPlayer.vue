<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api.js'
import { store } from '../store.js'
import { createGrowingPlayback, createRecordingPlayback } from '../recording-playback.js'
import { itemLocation, mediaUrl, segmentTime } from '../viewer-presentation.js'
import { applyDefaultVolume } from '../playback.js'
import AppIcon from './AppIcon.vue'

const props = defineProps({ target: { type:Object, required:true } })
const emit = defineEmits(['close'])
const dialog = ref(null), video = ref(null), entry = ref(null), current = ref(0), state = ref('loading')
const messages = {
  loading:'正在加载录播…', waiting:'正在等待第一段视频写入…', preparing:'正在读取回放索引…',
  unsupported:'浏览器暂时无法追看这段视频，录制结束后可打开回放。', error:'视频暂时无法读取。', retry:'回放索引暂时无法读取。',
}
const parts = computed(() => entry.value?.collection?.parts || [])
const controller = new AbortController()
let player, serial = 0, opener, volume = null
function release() {
  ++serial
  player?.dispose()
  player = null
}
function startPart(index) {
  const previousVolume = video.value?.readyState ? video.value.volume : volume
  release()
  volume = previousVolume
  current.value = index
  const item = entry.value?.gallery?.[index]
  if (!item?.rel) { state.value = 'error'; return }
  const element = video.value, version = serial
  state.value = 'loading'
  const nativeUrl = mediaUrl(itemLocation(entry.value, item), item.rel)
  if (item.playback_info_url) {
    player = createRecordingPlayback(element, {
      infoUrl:item.playback_info_url, nativeUrl, onIndexState:value => { if (serial === version) state.value = value },
    })
    player.ready.then(ready => { if (ready && serial === version) element.play().catch(() => {}) })
  } else {
    element.src = nativeUrl
    element.load()
    element.play().catch(() => {})
  }
}
async function open() {
  release()
  state.value = 'loading'
  const version = serial
  if (props.target.recordId) {
    player = createGrowingPlayback(video.value, props.target.recordId, {
      fromStart:props.target.fromStart, onState:value => { if (serial === version) state.value = value },
    })
    return
  }
  try {
    const detail = await api.libraryEntry(props.target.entryId, controller.signal)
    if (serial !== version) return
    entry.value = detail
    startPart(detail.initial_gallery_index ?? 0)
  } catch { if (serial === version) state.value = 'error' }
}
function jump(toStart) {
  player?.seek?.(toStart)
  video.value?.play().catch(() => {})
}
function metadata(event) {
  if (volume === null) applyDefaultVolume(event, store.preferences.default_volume)
  else event.target.volume = volume
}
function ended() {
  if (entry.value?.collection && current.value + 1 < entry.value.gallery.length) startPart(current.value + 1)
}
onMounted(() => { opener = document.activeElement; dialog.value.showModal(); open() })
onBeforeUnmount(() => {
  controller.abort()
  release()
  if (video.value) { video.value.pause(); video.value.removeAttribute('src'); video.value.load() }
  dialog.value?.close()
  opener?.focus?.({preventScroll:true})
})
</script>
<template>
  <Teleport to="body">
    <dialog ref="dialog" class="recording-player" aria-labelledby="recording-title" @cancel.prevent="emit('close')" @click.self="emit('close')">
      <div class="player-body">
        <header><div><h2 id="recording-title">{{ target.title || target.author || '录播回放' }}</h2><p v-if="target.author && target.author !== target.title">{{ target.author }}</p></div>
          <button class="player-close" aria-label="关闭播放器" autofocus @click="emit('close')"><AppIcon name="close" :size="20"/></button></header>
        <video ref="video" controls playsinline preload="metadata" @loadedmetadata="metadata" @canplay="state=''"
          @error="state='error'" @ended="ended"></video>
        <p v-if="state" class="player-message" role="status">{{ messages[state] }} <button v-if="['error','retry'].includes(state)" @click="open">重试</button></p>
        <footer v-if="target.recordId"><button @click="jump(true)" :disabled="!!state">从头回看</button><button @click="jump(false)" :disabled="!!state">跳到最新片段</button></footer>
        <nav v-if="parts.length" class="part-list" aria-label="选择录播分段（北京时间）"><button v-for="(part,i) in parts" :key="part.id" :disabled="!part.playable" :aria-pressed="part.gallery_index===current" @click="startPart(part.gallery_index)">分段 {{ i+1 }} · {{ segmentTime(part.started_at) }}</button></nav>
      </div>
    </dialog>
  </Teleport>
</template>
<style scoped>
.recording-player { position:fixed; inset:0; margin:auto; width:min(920px,94vw); max-height:90dvh; padding:0; overflow:auto; color:#f4f6fa; background:#18212af5; border:1px solid #ffffff30; border-radius:18px; box-shadow:0 20px 80px #11182750; }
.recording-player::backdrop { background:#15202a99; backdrop-filter:blur(9px); }
.player-body header,.player-body footer,.part-list { display:flex; align-items:center; gap:10px; padding:14px 18px; }
.player-body header { justify-content:space-between; }.player-body h2 { font-size:17px; margin:0; overflow-wrap:anywhere; }.player-body header p { margin:4px 0 0; color:#cbd3db; font-size:13px; }
.player-body video { display:block; width:100%; max-height:64dvh; min-height:120px; background:#10181e; }.player-message { margin:0; padding:12px 18px; }
.player-body button { padding:6px 12px; border:1px solid #ffffff40; color:inherit; border-radius:8px; background:#ffffff12; cursor:pointer; }.player-body button:hover,.part-list [aria-pressed=true] { background:#ffffff2a; }.player-body button:focus-visible { outline:2px solid #a9d4ff; outline-offset:2px; }.player-body button:disabled { opacity:.5; cursor:default; }
.player-body .player-close { display:grid; place-items:center; flex:none; width:34px; height:34px; padding:0; }.part-list { flex-wrap:wrap; }
</style>
