<script setup>
import weibo from '../assets/weibo.svg'
import douyin from '../assets/douyin.svg'
defineProps({ platform: String, size: { type: [Number, String], default: 32 } })
</script>
<template><span class="platform-logo" :class="platform" :style="{width: size + 'px', height: size + 'px'}"><img :src="platform === 'weibo' ? weibo : douyin" :alt="platform === 'weibo' ? '微博' : '抖音'" /></span></template>
<style scoped>.platform-logo { display:inline-grid; place-items:center; border-radius:28%; background:#eef0f3; flex-shrink:0; }.platform-logo img { width:65%; height:65%; }.weibo { background:#ffe8df; }.weibo img { filter:invert(29%) sepia(71%) saturate(2088%) hue-rotate(343deg) brightness(95%) contrast(92%); }.douyin { background:#e4f2f3; }</style>
