<script setup>
import { ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
const props=defineProps({ src:String, name:String, size:{type:Number,default:42} })
const failed=ref(false)
watch(()=>props.src,()=>{failed.value=false})
</script>
<template><span class="avatar" :style="{width:size+'px',height:size+'px'}"><img v-if="src && !failed" :src="src" :alt="name || ''" referrerpolicy="no-referrer" loading="lazy" @error="failed=true"><AppIcon v-else name="person" :size="Math.round(size*.7)"/></span></template>
<style scoped>.avatar { display:inline-flex; align-items:center; justify-content:center; background:#e9e6f2; border-radius:50%; flex-shrink:0; overflow:hidden; }.avatar img { width:100%; height:100%; object-fit:cover; }</style>
