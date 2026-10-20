<script setup>
import { computed } from 'vue'
import { livePresentation } from '../live-status.js'
const props=defineProps({monitor:Object, stale:Boolean, expanded:Boolean})
defineEmits(['open'])
const view=computed(()=>livePresentation(props.monitor))
</script>
<template>
  <button class="live-summary" :class="view.tone" type="button" :aria-expanded="expanded" @click="$emit('open')">
    <span class="live-summary-label"><span class="live-dot" aria-hidden="true"></span>录播 · {{stale?'状态待刷新':view.label}}</span>
    <span v-if="!stale && view.detail" class="live-summary-detail">{{view.detail}}</span>
    <span class="live-summary-link">{{expanded?'收起详情':'查看详情'}}</span>
  </button>
</template>
<style scoped>
.live-summary { width:100%;display:flex;align-items:center;flex-wrap:wrap;gap:7px 14px;margin-top:14px;padding:11px 13px;border:1px solid var(--border);border-radius:12px;background:#f6f8fa;color:var(--text-2);text-align:left;cursor:pointer;font:inherit;font-size:12px;line-height:1.7; }
.live-summary-label { display:inline-flex;align-items:center;gap:7px;font-weight:600;color:var(--text); }.live-dot { width:7px;height:7px;border-radius:50%;background:#92969c;flex-shrink:0; }.recording { background:#f1f8f4; }.recording .live-dot { background:#2d9259; }.warning { background:#fff7ec; }.warning .live-dot { background:#b87823; }
.live-summary-detail { flex:1;min-width:160px;overflow-wrap:anywhere; }.live-summary-link { color:var(--blue);margin-left:auto; }.live-summary:focus-visible { outline:2px solid var(--blue);outline-offset:3px; }
</style>
