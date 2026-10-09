<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { store, connectWS, refreshTasks, dismissToast } from './store.js'
import AppIcon from './components/AppIcon.vue'
import { api } from './api.js'
const route = useRoute()
const navItems = [
  { to: '/', label: '下载', icon: 'download' },
  { to: '/tasks', label: '任务', icon: 'tasks' },
  { to: '/library', label: '媒体库', icon: 'library' },
  { to: '/subs', label: '订阅', icon: 'subs' },
  { to: '/settings', label: '设置', icon: 'settings' },
]
const activeTasks = computed(() => store.tasks.filter(t => ['queued', 'running'].includes(t.status)).length)
const showTop = ref(false)
const contentEl = ref(null)
function onContentScroll() { showTop.value = contentEl.value?.scrollTop > 300 }
onMounted(() => { api.settings().then(result => { store.preferences = result.values }).catch(() => {}); connectWS(); refreshTasks(); contentEl.value?.addEventListener('scroll', onContentScroll, { passive: true }) })
onBeforeUnmount(() => contentEl.value?.removeEventListener('scroll', onContentScroll))
</script>
<template>
  <div class="layout" :class="'density-' + store.preferences.density">
    <aside class="sidebar">
      <div class="brand"><AppIcon name="brand" :size="36"/><span>Media Squirrel</span></div>
      <nav class="primary-nav" aria-label="主导航">
        <router-link v-for="item in navItems" :key="item.to" :to="item.to" class="nav-item" :class="{ active: route.path === item.to }" :aria-current="route.path === item.to ? 'page' : undefined">
          <AppIcon :name="item.icon"/><span>{{ item.label }}</span>
          <span v-if="item.to === '/tasks' && activeTasks" class="badge">{{ activeTasks > 99 ? '99+' : activeTasks }}</span>
        </router-link>
      </nav>
      <div class="sidebar-footer"><span class="dot" :style="{ color: store.wsConnected ? 'var(--green)' : 'var(--orange)' }"></span> {{ store.wsConnected ? '实时连接正常' : '正在重新连接…' }}<div>本地媒体收藏与浏览</div></div>
    </aside>
    <main ref="contentEl" class="content"><div class="content-inner"><router-view/></div></main>
  </div>
  <div class="toast-wrap" aria-live="polite" aria-relevant="additions text">
    <div v-for="item in store.toasts" :key="item.id" class="toast" :class="'toast-' + item.kind" role="status">
      <div><p>{{ item.text }}<span v-if="item.count > 1">（{{ item.count }} 次）</span></p><router-link v-if="item.to" :to="item.to" @click="dismissToast(item.id)">查看记录</router-link></div>
      <button class="toast-close" aria-label="关闭提示" @click="dismissToast(item.id)"><AppIcon name="close" :size="20"/></button>
    </div>
  </div>
  <button v-if="showTop" class="back-top" aria-label="回到顶部" @click="contentEl?.scrollTo({ top: 0, behavior: 'instant' })"><AppIcon name="up"/></button>
</template>
<style scoped>
.sidebar-footer .dot { display:inline-block; }
.toast-wrap { position:fixed; top:20px; right:20px; z-index:999; display:flex; flex-direction:column; gap:10px; width:min(390px,calc(100vw - 32px)); pointer-events:none; }
.toast { display:flex; align-items:flex-start; gap:14px; padding:14px 16px; background:#fff; color:var(--text); border:1px solid var(--border); border-left:4px solid var(--blue); border-radius:14px; box-shadow:var(--shadow-card); pointer-events:auto; font-size:13px; line-height:1.5; overflow-wrap:anywhere; }
.toast > div { flex:1; }.toast-success { border-left-color:#438f73; }.toast-error { border-left-color:var(--red); }.toast a { color:var(--blue); display:inline-block; margin-top:4px; }
.toast-close { border:0; background:transparent; cursor:pointer; padding:3px; }
.back-top { position:fixed; right:24px; bottom:25px; z-index:450; width:44px; height:44px; display:grid; place-items:center; border:1px solid var(--border); border-radius:50%; background:#fff; box-shadow:var(--shadow-card); cursor:pointer; }
@media(max-width:767px) { .back-top { bottom:90px; right:16px; }.toast-wrap { top:12px; right:16px; } }
</style>
