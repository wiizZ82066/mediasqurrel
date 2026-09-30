<script setup>
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { store, connectWS, refreshTasks } from './store.js'

const route = useRoute()
const navItems = [
  { to: '/', label: '下载', icon: '⬇️' },
  { to: '/tasks', label: '任务', icon: '📋' },
  { to: '/library', label: '媒体库', icon: '🖼️' },
  { to: '/subs', label: '订阅', icon: '🔔' },
]

const activeTasks = computed(
  () => store.tasks.filter((t) => ['queued', 'running'].includes(t.status)).length,
)

onMounted(() => {
  connectWS()
  refreshTasks()
})
</script>

<template>
  <!-- 渐变 blob 背景 -->
  <div class="app-bg">
    <div class="blob blob-1"></div>
    <div class="blob blob-2"></div>
    <div class="blob blob-3"></div>
  </div>

  <div class="layout">
    <aside class="sidebar">
      <div class="brand">
        <span class="logo">🐿️</span>
        <span>Media Squirrel</span>
      </div>

      <router-link
        v-for="item in navItems"
        :key="item.to"
        :to="item.to"
        class="nav-item"
        :class="{ active: route.path === item.to }"
      >
        <span class="icon">{{ item.icon }}</span>
        <span>{{ item.label }}</span>
        <span v-if="item.to === '/tasks' && activeTasks > 0" class="badge">
          {{ activeTasks }}
        </span>
      </router-link>

      <div class="sidebar-footer">
        <div>
          <span class="dot" :style="{ color: store.wsConnected ? 'var(--green)' : 'var(--orange)' }"></span>
          {{ store.wsConnected ? '实时连接正常' : '连接中…' }}
        </div>
        <div style="margin-top:4px">本地媒体下载管理平台</div>
      </div>
    </aside>

    <main class="content">
      <div class="content-inner">
        <router-view v-slot="{ Component }">
          <transition name="page" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </div>
    </main>
  </div>

  <!-- Toast 通知 -->
  <transition-group name="toast" tag="div" class="toast-wrap">
    <div
      v-for="t in store.toasts"
      :key="t.id"
      class="toast"
      :class="'toast-' + t.kind"
    >
      {{ t.text }}
    </div>
  </transition-group>
</template>

<style scoped>
.toast-wrap {
  position: fixed;
  top: 22px;
  right: 22px;
  z-index: 999;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.toast {
  padding: 13px 20px;
  border-radius: 14px;
  font-size: 14px;
  font-weight: 600;
  color: #fff;
  backdrop-filter: blur(16px);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.18);
}
.toast-success { background: rgba(52, 199, 89, 0.92); }
.toast-error { background: rgba(255, 59, 48, 0.92); }
.toast-info { background: rgba(29, 29, 31, 0.9); }

.toast-enter-active { transition: all 300ms cubic-bezier(0.34, 1.56, 0.64, 1); }
.toast-leave-active { transition: all 250ms ease-in; }
.toast-enter-from { opacity: 0; transform: translateX(60px) scale(0.92); }
.toast-leave-to { opacity: 0; transform: translateY(-8px); }
</style>
