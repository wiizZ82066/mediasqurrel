<script setup>
import { store, loginWeibo } from '../store.js'
import PlatformLogo from './PlatformLogo.vue'

defineProps({ disabled: Boolean })
const emit = defineEmits(['logged-in'])

async function openLogin() {
  if (await loginWeibo()) emit('logged-in')
}
</script>

<template>
  <button type="button" class="btn btn-ghost btn-sm" :disabled="disabled || store.weiboLogin.pending"
    :aria-busy="store.weiboLogin.pending" title="打开微博官方登录窗口" @mousedown.prevent @click="openLogin">
    <PlatformLogo platform="weibo" :size="18" aria-hidden="true" />
    {{ store.weiboLogin.pending ? '等待微博登录…' : store.weiboLogin.saved ? '重新登录微博' : '登录微博' }}
  </button>
</template>
