<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
const props=defineProps({src:String,position:Object})
const url=ref(props.src), failed=ref(false)
let retries=0,timer=null
watch(()=>props.src,value=>{clearTimeout(timer);retries=0;failed.value=false;url.value=value})
function retry(){failed.value=true;if(retries>=3)return;const attempt=++retries;timer=setTimeout(()=>{url.value=props.src+(props.src.includes('?')?'&':'?')+'_retry='+attempt;failed.value=false},1800+attempt*500+Math.random()*600)}
onBeforeUnmount(()=>clearTimeout(timer))
</script>
<template><span class="thumbnail"><AppIcon v-if="failed || !src" name="library" :size="40"/><img v-if="src && !failed" :src="url" :style="position" loading="lazy" decoding="async" alt="" @error="retry"></span></template>
<style scoped>.thumbnail { display:flex; width:100%; height:100%; align-items:center; justify-content:center; }.thumbnail img { width:100%; height:100%; object-fit:cover; }</style>
