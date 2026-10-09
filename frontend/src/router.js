import { createRouter, createWebHistory } from 'vue-router'
import Download from './views/Download.vue'
import Tasks from './views/Tasks.vue'
import Library from './views/Library.vue'
import Subs from './views/Subs.vue'
import Settings from './views/Settings.vue'

const routes = [
  { path: '/settings', name: 'settings', component: Settings, meta: { title: '设置' } },
  { path: '/', name: 'download', component: Download, meta: { title: '下载' } },
  { path: '/tasks', name: 'tasks', component: Tasks, meta: { title: '任务' } },
  { path: '/library', name: 'library', component: Library, meta: { title: '媒体库' } },
  { path: '/subs', name: 'subs', component: Subs, meta: { title: '订阅' } },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
