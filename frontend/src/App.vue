<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { store, connectWS, refreshTasks, markNotificationsRead } from './store.js'

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

const notifOpen = ref(false)
function toggleNotif() {
  notifOpen.value = !notifOpen.value
  if (notifOpen.value) markNotificationsRead()
}

// 回到顶部：内容区滚动超过阈值时显示
const showTop = ref(false)
let _contentEl = null

function onContentScroll() {
  if (_contentEl) showTop.value = _contentEl.scrollTop > 300
}

function scrollToTop() {
  _contentEl?.scrollTo({ top: 0, behavior: 'smooth' })
}

onMounted(() => {
  connectWS()
  refreshTasks()
  _contentEl = document.querySelector('.content')
  _contentEl?.addEventListener('scroll', onContentScroll, { passive: true })
})

onBeforeUnmount(() => {
  _contentEl?.removeEventListener('scroll', onContentScroll)
})
</script>

<template>
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

      <!-- 通知中心入口 -->
      <button class="nav-item notif-btn" @click="toggleNotif">
        <span class="icon">📬</span>
        <span>通知</span>
        <span v-if="store.unread > 0" class="badge">{{ store.unread > 99 ? '99+' : store.unread }}</span>
      </button>

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

  <!-- 通知面板 -->
  <transition name="notif">
    <div v-if="notifOpen" class="notif-mask" @click="toggleNotif">
      <div class="notif-panel" @click.stop>
        <div class="notif-head">
          <span class="notif-title">通知中心</span>
          <button class="btn btn-ghost btn-sm" @click="toggleNotif">关闭</button>
        </div>
        <div class="notif-list">
          <div v-if="!store.notifications.length" class="notif-empty">
            🌙 暂无通知<br/><span>订阅扫描和下载动态会在这里提醒</span>
          </div>
          <div
            v-for="(n, i) in store.notifications"
            :key="i"
            class="notif-item"
            :class="'notif-' + (n.level || 'info')"
          >
            <div class="notif-item-head">
              <span class="notif-item-title">{{ n.title }}</span>
              <span class="notif-item-time">{{ n.time }}</span>
            </div>
            <div class="notif-item-text">{{ n.text }}</div>
          </div>
        </div>
      </div>
    </div>
  </transition>

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

  <!-- 回到顶部 -->
  <transition name="topbtn">
    <button
      v-if="showTop"
      class="back-top"
      title="回到顶部"
      @click="scrollToTop"
    ><span class="tri"></span></button>
  </transition>
</template>

<style scoped>
.notif-btn { font-family: var(--font); }

.notif-mask {
  position: fixed;
  inset: 0;
  z-index: 600;
  background: rgba(10, 10, 12, 0.25);
}
.notif-panel {
  position: absolute;
  top: 0;
  right: 0;
  width: min(380px, 92vw);
  height: 100%;
  background: rgba(250, 250, 252, 0.92);
  backdrop-filter: blur(28px) saturate(180%);
  border-left: 1px solid rgba(255, 255, 255, 0.6);
  box-shadow: -18px 0 60px rgba(0, 0, 0, 0.18);
  display: flex;
  flex-direction: column;
}
.notif-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px 22px 14px;
  border-bottom: 1px solid rgba(0, 0, 0, 0.06);
}
.notif-title { font-size: 17px; font-weight: 700; }
.notif-list {
  flex: 1;
  overflow-y: auto;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.notif-empty {
  text-align: center;
  color: var(--text-2);
  padding: 60px 10px;
  font-size: 15px;
  line-height: 2;
}
.notif-empty span { font-size: 12.5px; }
.notif-item {
  background: rgba(255, 255, 255, 0.85);
  border: 1px solid rgba(0, 0, 0, 0.05);
  border-radius: 14px;
  padding: 12px 14px;
  animation: fadeUp 300ms var(--ease) both;
}
.notif-item.notif-success { border-left: 3px solid var(--green); }
.notif-item.notif-error { border-left: 3px solid var(--red); }
.notif-item.notif-info { border-left: 3px solid var(--blue); }
.notif-item-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 4px;
}
.notif-item-title { font-size: 13.5px; font-weight: 700; }
.notif-item-time { font-size: 11.5px; color: var(--text-2); font-variant-numeric: tabular-nums; }
.notif-item-text { font-size: 12.5px; color: var(--text-2); line-height: 1.55; word-break: break-all; }

.notif-enter-active { transition: all 320ms cubic-bezier(0.4, 0, 0.2, 1); }
.notif-leave-active { transition: all 200ms ease-in; }
.notif-enter-from, .notif-leave-to { opacity: 0; }
.notif-enter-from .notif-panel, .notif-leave-to .notif-panel { transform: translateX(80px); }
.notif-panel { transition: transform 320ms cubic-bezier(0.4, 0, 0.2, 1); }

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

/* 回到顶部按钮 */
.back-top {
  position: fixed;
  right: 28px;
  bottom: 32px;
  z-index: 450;
  width: 46px;
  height: 46px;
  border-radius: 50%;
  border: 1px solid rgba(255, 255, 255, 0.6);
  background: rgba(255, 255, 255, 0.72);
  backdrop-filter: blur(20px) saturate(180%);
  -webkit-backdrop-filter: blur(20px) saturate(180%);
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.14);
  color: var(--text);
  font-size: 20px;
  font-weight: 700;
  cursor: pointer;
  transition: transform 200ms var(--ease), box-shadow 200ms, background 200ms;
}
.back-top:hover {
  transform: translateY(-3px);
  background: rgba(255, 255, 255, 0.92);
  box-shadow: 0 12px 34px rgba(0, 0, 0, 0.2);
}
.back-top:active { transform: scale(0.94); }
/* 实心大向上三角（CSS 绘制） */
.back-top .tri {
  display: block;
  width: 0;
  height: 0;
  border-left: 10px solid transparent;
  border-right: 10px solid transparent;
  border-bottom: 14px solid var(--text);
  margin-top: -4px; /* 视觉居中补偿 */
}

.topbtn-enter-active { transition: all 280ms cubic-bezier(0.34, 1.56, 0.64, 1); }
.topbtn-leave-active { transition: all 200ms ease-in; }
.topbtn-enter-from,
.topbtn-leave-to { opacity: 0; transform: translateY(16px) scale(0.85); }
</style>
