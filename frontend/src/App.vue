<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { store, connectWS, refreshTasks, dismissToast } from './store.js'
import AppIcon from './components/AppIcon.vue'
import StartupScreen from './components/StartupScreen.vue'
import { api } from './api.js'
const route = useRoute()
const navItems = [
  { to: '/', label: '首页', icon: 'home' },
  { to: '/download', label: '下载', icon: 'download' },
  { to: '/tasks', label: '任务', icon: 'tasks' },
  { to: '/library', label: '媒体库', icon: 'library' },
  { to: '/subs', label: '订阅', icon: 'subs' },
]
const activeTasks = computed(() => store.tasks.filter(t => ['queued', 'running'].includes(t.status)).length)
const showTop = ref(false)
const contentEl = ref(null)
const ready = ref(false), bootCompleted = ref(0), bootError = ref(''), bootStage = ref('正在连接本地服务')
let bootController
async function bootstrap() {
  bootController?.abort()
  const controller = new AbortController()
  bootController = controller
  bootCompleted.value = 0; bootError.value = ''; bootStage.value = '正在连接本地服务'
  const timeout = setTimeout(() => controller.abort(), 10000)
  try {
    await api.health(controller.signal)
    bootCompleted.value = 1; bootStage.value = '正在读取本地偏好设置'
    const result = await api.settings(controller.signal)
    store.preferences = result.values
    bootCompleted.value = 2
    openPage()
  } catch {
    if (bootController === controller) bootError.value = '本地服务暂时无法连接，请重试。'
  } finally { clearTimeout(timeout) }
}
async function openPage() {
  ready.value = true
  connectWS(); refreshTasks()
  await nextTick()
  contentEl.value?.addEventListener('scroll', onContentScroll, { passive: true })
}
function onContentScroll() { showTop.value = contentEl.value?.scrollTop > 300 }
function toggleLibraryAuthors() { store.libraryAuthorsCollapsed = !store.libraryAuthorsCollapsed }
watch(() => route.name, () => { showTop.value = false; document.title = `${route.meta?.title || '首页'} · Media Squirrel` }, { immediate: true })
onMounted(bootstrap)
onBeforeUnmount(() => { bootController?.abort(); contentEl.value?.removeEventListener('scroll', onContentScroll) })
</script>
<template>
  <StartupScreen v-if="!ready" :completed="bootCompleted" :stage="bootStage" :error="bootError" @retry="bootstrap" @continue="openPage"/>
  <div v-else class="layout" :class="'density-' + store.preferences.density">
    <aside class="sidebar">
      <router-link to="/" class="brand"><AppIcon name="brand" :size="36"/><span>Media Squirrel<small>本地媒体空间</small></span></router-link>
      <div class="nav-caption">工作空间</div>
      <nav class="primary-nav" aria-label="主导航">
        <router-link v-for="item in navItems" :key="item.to" :to="item.to" class="nav-item" :class="{ active: route.path === item.to }" :aria-current="route.path === item.to ? 'page' : undefined">
          <AppIcon :name="item.icon"/><span>{{ item.label }}</span>
          <span v-if="item.to === '/tasks' && activeTasks" class="badge">{{ activeTasks > 99 ? '99+' : activeTasks }}</span>
        </router-link>
      </nav>
      <div class="sidebar-footer"><router-link to="/settings" class="nav-item settings-nav" :class="{active:route.name==='settings'}" :aria-current="route.name==='settings'?'page':undefined"><AppIcon name="settings"/><span>设置</span></router-link><div class="connection-status"><span class="dot" :style="{ color: store.wsConnected ? 'var(--green)' : 'var(--orange)' }"></span> {{ store.wsConnected ? '本地服务已连接' : '正在重新连接…' }}</div></div>
    </aside>
    <main ref="contentEl" class="content"><div class="mobile-header"><router-link to="/" aria-label="Media Squirrel 首页"><AppIcon name="brand" :size="28"/><span>Media Squirrel</span></router-link><router-link to="/settings" aria-label="设置" :aria-current="route.name==='settings'?'page':undefined"><AppIcon name="settings"/></router-link></div><div class="content-inner" :class="{'library-content':route.name === 'library'}"><router-view/></div></main>
    <div v-show="route.name === 'library'" class="library-sidebar-host" :class="{'is-collapsed':store.libraryAuthorsCollapsed}">
      <button class="library-sidebar-toggle" :aria-expanded="!store.libraryAuthorsCollapsed" aria-controls="library-sidebar" :aria-label="store.libraryAuthorsCollapsed?'展开作者列表':'收起作者列表'" :title="store.libraryAuthorsCollapsed?'展开作者列表':'收起作者列表'" @click="toggleLibraryAuthors"><AppIcon :name="store.libraryAuthorsCollapsed?'chevron-left':'chevron-right'" :size="20"/><span>{{ store.libraryAuthorsCollapsed?'作者':'收起作者' }}</span></button>
      <div id="library-sidebar" v-show="!store.libraryAuthorsCollapsed" class="library-sidebar-body"></div>
    </div>
  </div>
  <div class="toast-wrap" aria-live="polite" aria-relevant="additions text">
    <div v-for="item in store.toasts" :key="item.id" class="toast" :class="'toast-' + item.kind" role="status">
      <div><p>{{ item.text }}<span v-if="item.count > 1">（{{ item.count }} 次）</span></p><router-link v-if="item.to" :to="item.to" @click="dismissToast(item.id)">查看记录</router-link></div>
      <button class="toast-close" aria-label="关闭提示" @click="dismissToast(item.id)"><AppIcon name="close" :size="20"/></button>
    </div>
  </div>
  <button v-if="showTop" class="back-top" :class="{'beside-library':route.name === 'library','beside-collapsed-library':route.name === 'library' && store.libraryAuthorsCollapsed}" aria-label="回到顶部" @click="contentEl?.scrollTo({ top: 0, behavior: 'instant' })"><AppIcon name="up"/></button>
</template>
<style scoped>
.sidebar-footer .dot { display:inline-block; }
.brand { text-decoration:none; color:inherit; }.brand small { display:block; font-size:10px; font-weight:400; color:var(--text-2); letter-spacing:.03em; margin-top:4px; }
.nav-caption { margin:18px 14px 10px; font-size:10px; color:var(--text-2); letter-spacing:.08em; }.sidebar-footer { padding:0; }.connection-status { margin:16px 14px 4px; padding-top:14px; border-top:1px solid var(--border); font-size:11px; }
.mobile-header { display:none; }
.content-inner.library-content { max-width:none; }
.library-sidebar-host { display:flex; flex-direction:column; flex:0 0 240px; min-width:0; height:100%; padding:36px 18px 24px; overflow:hidden; background:var(--sidebar-bg); border-left:1px solid var(--border); }
.library-sidebar-host.is-collapsed { flex-basis:56px; padding-right:7px; padding-left:7px; }
.library-sidebar-toggle { display:flex; align-items:center; justify-content:center; gap:7px; flex-shrink:0; min-height:36px; width:100%; margin-bottom:18px; padding:7px; border:1px solid var(--border); border-radius:10px; background:#fff; color:var(--text-2); font-size:12px; cursor:pointer; }
.library-sidebar-toggle:hover { background:#edf3f7; color:var(--blue); }
.is-collapsed .library-sidebar-toggle { flex-direction:column; gap:4px; padding:8px 2px; font-size:11px; }
.library-sidebar-body { flex:1; min-height:0; }
@media(min-width:1100px) { .back-top.beside-library { right:264px; }.back-top.beside-collapsed-library { right:80px; } }
@media(max-width:1099px) { .library-sidebar-host { display:none; } }
.toast-wrap { position:fixed; top:20px; right:20px; z-index:999; display:flex; flex-direction:column; gap:10px; width:min(390px,calc(100vw - 32px)); pointer-events:none; }
.toast { display:flex; align-items:flex-start; gap:14px; padding:14px 16px; background:#fff; color:var(--text); border:1px solid var(--border); border-left:4px solid var(--blue); border-radius:14px; box-shadow:var(--shadow-card); pointer-events:auto; font-size:13px; line-height:1.5; overflow-wrap:anywhere; }
.toast > div { flex:1; }.toast-success { border-left-color:#438f73; }.toast-error { border-left-color:var(--red); }.toast a { color:var(--blue); display:inline-block; margin-top:4px; }
.toast-close { border:0; background:transparent; cursor:pointer; padding:3px; }
.back-top { position:fixed; right:24px; bottom:25px; z-index:450; width:44px; height:44px; display:grid; place-items:center; border:1px solid var(--border); border-radius:50%; background:#fff; box-shadow:var(--shadow-card); cursor:pointer; }
@media(max-width:767px) { .back-top { bottom:90px; right:16px; }.toast-wrap { top:12px; right:16px; } }
@media(max-width:767px) { .nav-caption { display:none; }.mobile-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:28px; padding-bottom:14px; border-bottom:1px solid var(--border); }.mobile-header a { display:flex; align-items:center; gap:8px; text-decoration:none; font-size:13px; font-weight:600; color:var(--text); }.mobile-header a:last-child { padding:5px; border-radius:7px; }.mobile-header [aria-current] { background:var(--selected); } }
</style>
