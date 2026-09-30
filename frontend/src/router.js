import { createRouter, createWebHistory } from 'vue-router'
import Download from './views/Download.vue'
import Tasks from './views/Tasks.vue'
import Library from './views/Library.vue'
import Subs from './views/Subs.vue'

const routes = [
  { path: '/', name: 'download', component: Download, meta: { title: '下载', icon: '⬇️' } },
  { path: '/tasks', name: 'tasks', component: Tasks, meta: { title: '任务', icon: '📋' } },
  { path: '/library', name: 'library', component: Library, meta: { title: '媒体库', icon: '🖼️' } },
  { path: '/subs', name: 'subs', component: Subs, meta: { title: '订阅', icon: '🔔' } },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
