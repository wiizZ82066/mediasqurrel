<script setup>
import { computed, ref, watch } from 'vue'
import { contentSegments, loadWeiboEmoticons, weiboEmoticons } from '../content-text.js'

const props = defineProps({ text: { type: String, default: '' }, platform: String })
const ready = ref(new Set()), failed = ref(new Set())
const segments = computed(() => contentSegments(props.text, props.platform, weiboEmoticons.value))
watch(() => [props.text, props.platform], () => {
  failed.value = new Set()
  if (props.platform === 'weibo' && /\[[^\]\r\n]{1,32}\]/u.test(props.text)) loadWeiboEmoticons()
}, { immediate: true })
</script>

<template>
  <span class="content-text"><template v-for="(part,index) in segments" :key="`${part.text}:${index}`"><span v-if="part.src && !failed.has(part.src)" class="emoticon"><img :src="part.src" :alt="part.text" :title="part.text" :class="{ pending: !ready.has(part.src) }" :aria-hidden="!ready.has(part.src)" decoding="async" draggable="false" @load="ready.add(part.src)" @error="failed.add(part.src)"><span v-if="!ready.has(part.src)">{{ part.text }}</span></span><template v-else>{{ part.text }}</template></template></span>
</template>

<style scoped>
.content-text { font-family:var(--font); font-variant-emoji:emoji; overflow-wrap:anywhere; }
.emoticon { position:relative; }
.emoticon img { display:inline-block; width:1.4em; height:1.4em; object-fit:contain; vertical-align:-.3em; }
.emoticon img.pending { position:absolute; width:1px; height:1px; opacity:0; pointer-events:none; }
</style>
