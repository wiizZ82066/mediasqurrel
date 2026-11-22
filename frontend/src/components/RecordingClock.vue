<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { liveDuration, recordingSeconds } from '../live-status.js'

const props = defineProps({ record: Object, sampledAt: Number, fallback: Number })
const seconds = ref(0)
let timer
function update() {
  seconds.value = recordingSeconds(props.record, props.sampledAt, performance.now(), props.fallback)
}
function sync() {
  clearInterval(timer)
  update()
  if (props.record?.status === 'recording' && !document.hidden) timer = setInterval(update, 250)
}
watch(() => [props.record, props.sampledAt, props.fallback], sync)
onMounted(() => { sync(); document.addEventListener('visibilitychange', sync) })
onBeforeUnmount(() => { clearInterval(timer); document.removeEventListener('visibilitychange', sync) })
</script>
<template><span class="recording-clock">{{ liveDuration(seconds) }}</span></template>
<style scoped>.recording-clock { font-variant-numeric:tabular-nums; }</style>
