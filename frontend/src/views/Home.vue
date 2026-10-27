<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api.js'
import { store, onTaskEvent, toast } from '../store.js'
import AppIcon from '../components/AppIcon.vue'
import Avatar from '../components/Avatar.vue'
import PlatformLogo from '../components/PlatformLogo.vue'
import MediaThumbnail from '../components/MediaThumbnail.vue'
import ContentText from '../components/ContentText.vue'
import ActivityProgress from '../components/ActivityProgress.vue'
import { livePresentation, liveDuration } from '../live-status.js'
import { homeThumbnail, homeTime, nextScanLabel } from '../home-presentation.js'

const data = ref(store.homeState.data), error = ref(''), loading = ref(!data.value), revealing = ref('')
const downloads = computed(() => data.value?.downloads)
const live = computed(() => data.value?.live)
const scans = computed(() => data.value?.scans)
const currentLive = computed(() => live.value?.current ? livePresentation(live.value.current) : null)
const firstTask = computed(() => downloads.value?.items[0])
let controller, poll, debounce, unsubscribe, disposed = false, scrollElement
async function refresh() {
  if (controller || disposed || document.hidden) return
  controller = new AbortController()
  const request = controller, timeout = setTimeout(() => request.abort(), 10000)
  try {
    const result = await api.home(request.signal)
    if (disposed) return
    data.value = result; store.homeState.data = result; error.value = ''
  } catch {
    if (!disposed) error.value = '状态暂时无法更新，请检查后台服务后重试。'
  } finally { clearTimeout(timeout); controller = null; loading.value = false }
}
function queueRefresh() {
  if (debounce || disposed || document.hidden) return
  debounce = setTimeout(() => { debounce = null; refresh() }, 1200)
}
function visible() { if (!document.hidden) refresh() }
function position(entry) {
  const face = entry.cover_face
  return face ? { objectPosition: `${face.x * 100}% ${face.y * 100}%` } : undefined
}
async function reveal(entry) {
  revealing.value = entry.id
  try { await api.libraryReveal(entry.id) } catch (reason) { toast(reason.message, 'error') }
  finally { revealing.value = '' }
}
function rememberScroll() { store.homeState.scrollTop = scrollElement?.scrollTop || 0 }
onMounted(async () => {
  scrollElement = document.querySelector('.content')
  await nextTick()
  if (disposed) return
  if (scrollElement) scrollElement.scrollTop = store.homeState.scrollTop
  scrollElement?.addEventListener('scroll', rememberScroll, { passive: true })
  refresh(); poll = setInterval(refresh, 15000)
  unsubscribe = onTaskEvent(queueRefresh)
  document.addEventListener('visibilitychange', visible)
})
onBeforeUnmount(() => {
  disposed = true; controller?.abort(); clearInterval(poll); clearTimeout(debounce); unsubscribe?.()
  document.removeEventListener('visibilitychange', visible)
  scrollElement?.removeEventListener('scroll', rememberScroll)
})
</script>

<template>
  <div class="home-view">
    <header class="home-heading">
      <div><p class="eyebrow">你的本地媒体空间</p><h1 class="page-title">首页</h1><p class="page-sub">先看看正在发生什么，再回到喜欢的内容。</p></div>
      <div class="home-connection"><span class="dot" :class="{ connected: store.wsConnected }"></span>{{ store.wsConnected ? '实时连接' : '等待连接' }}<small v-if="data">更新于 {{ homeTime(data.updated_at) }}</small></div>
    </header>
    <div v-if="error" class="home-error" role="status"><span>{{ error }}{{ data ? ' 当前保留上次读取的状态。' : '' }}</span><button class="btn btn-ghost btn-sm" @click="refresh">重试</button></div>
    <section class="status-grid" aria-label="运行状态" :aria-busy="loading">
      <article class="status-card card download-status">
        <div class="widget-heading"><span class="widget-icon"><AppIcon name="download" :size="26"/></span><h2>下载任务</h2><router-link to="/tasks" class="widget-link" aria-label="查看下载任务"><AppIcon name="chevron-right" :size="16"/></router-link></div>
        <div class="widget-number"><strong>{{ downloads ? downloads.running : '—' }}</strong><span>正在下载</span></div>
        <p class="widget-description">{{ downloads ? (downloads.queued ? `${downloads.queued} 个任务排队中` : downloads.running ? '正在保存喜欢的内容' : '队列空闲，随时可以添加内容') : '正在读取任务状态…' }}</p>
        <div class="widget-detail" v-if="firstTask">
          <div class="mini-author"><Avatar :src="firstTask.avatar_url" :name="firstTask.author" :size="26"/><PlatformLogo v-if="['weibo', 'douyin'].includes(firstTask.script_id)" :platform="firstTask.script_id" :size="16"/><span>{{ firstTask.author || firstTask.script_name }}</span></div>
          <p class="widget-title"><ContentText :platform="firstTask.script_id" :text="firstTask.display_title || '正在获取内容信息'"/></p>
          <ActivityProgress :status="firstTask.status" :progress="firstTask.progress" label="当前下载进度"/>
        </div>
        <p v-else class="widget-idle">{{ downloads ? '完成的内容会出现在下方媒体卡片中。' : '状态就绪后显示当前任务。' }}</p>
        <router-link class="widget-footer" to="/tasks">查看任务记录<AppIcon name="chevron-right" :size="14"/></router-link>
      </article>
      <article class="status-card card live-status">
        <div class="widget-heading"><span class="widget-icon"><AppIcon name="video" :size="26"/></span><h2>直播录制</h2><router-link to="/subs" class="widget-link" aria-label="查看直播订阅"><AppIcon name="chevron-right" :size="16"/></router-link></div>
        <div class="widget-number"><strong>{{ live ? live.active : '—' }}</strong><span>场正在录制或保存</span></div>
        <p class="widget-description">{{ live ? `${live.enabled} 位博主已开启直播监控` : '正在读取录播状态…' }}</p>
        <div v-if="live?.current" class="widget-detail">
          <p class="widget-title">{{ live.current.nickname || '订阅博主' }}<span class="widget-state">{{ currentLive.label }}</span></p>
          <template v-if="currentLive.active && live.current.record">
            <p class="live-elapsed">{{ liveDuration(live.current.record.duration_seconds) }}<small>已保存时长</small></p>
            <p class="widget-description">{{ live.current.record.chat?.messages || 0 }} 条文字弹幕 · 弹幕详情见订阅记录</p>
          </template>
          <p v-else class="widget-description">{{ live.current.next_check_at ? `下次检查 ${homeTime(live.current.next_check_at, { date: true })}` : '检查结果和异常原因可在订阅中查看。' }}</p>
        </div>
        <p v-else class="widget-idle">{{ live ? '为博主开启录播后，在这里查看运行状态。' : '状态就绪后显示当前录播。' }}</p>
        <router-link class="widget-footer" to="/subs">管理直播监控<AppIcon name="chevron-right" :size="14"/></router-link>
      </article>
      <article class="status-card card scan-status">
        <div class="widget-heading"><span class="widget-icon"><AppIcon name="scan" :size="26"/></span><h2>订阅扫描</h2><router-link to="/subs" class="widget-link" aria-label="查看扫描计划"><AppIcon name="chevron-right" :size="16"/></router-link></div>
        <div class="widget-number"><strong>{{ scans ? scans.active : '—' }}</strong><span>正在检查更新</span></div>
        <p class="widget-description">{{ scans ? `${scans.enabled} 位博主已启用内容订阅` : '正在读取扫描状态…' }}</p>
        <div class="widget-detail" v-if="scans">
          <template v-if="scans.active"><ActivityProgress :progress="scans.progress" status="running" label="订阅扫描进度"/></template>
          <template v-else><p class="widget-kicker">下一次扫描</p><p class="widget-title">{{ scans.next?.nickname || '暂无待执行计划' }}</p></template>
          <p class="widget-description">{{ nextScanLabel(scans.next) }}</p>
        </div>
        <p v-else class="widget-idle">状态就绪后显示下一次计划。</p>
        <router-link class="widget-footer" to="/subs">查看订阅计划<AppIcon name="chevron-right" :size="14"/></router-link>
      </article>
    </section>
    <section class="recent-section" aria-labelledby="recent-title">
      <header class="recent-heading"><div><h2 id="recent-title">最近入库</h2><p>新保存的内容，都在这里。</p></div><router-link to="/library" class="btn btn-ghost btn-sm">打开媒体库<AppIcon name="chevron-right" :size="16"/></router-link></header>
      <div v-if="data?.recent.length" class="recent-grid">
        <article v-for="entry in data.recent" :key="entry.id" class="recent-card">
          <router-link :to="{ path: '/library', query: { entry_id: entry.id, from: 'home' } }" class="recent-open" :aria-label="`查看 ${entry.author} 的内容`">
            <div class="recent-cover"><MediaThumbnail :src="homeThumbnail(entry)" :position="position(entry)"/></div>
            <div class="recent-caption"><div class="recent-author"><PlatformLogo v-if="['weibo', 'douyin'].includes(entry.platform)" :platform="entry.platform" :size="17"/><strong>{{ entry.author }}</strong></div><p class="recent-text"><ContentText :platform="entry.platform" :text="entry.text_preview || '打开查看内容'"/></p><small>{{ homeTime(entry.imported_at, { date: true }) }} 入库</small></div>
          </router-link>
          <button class="recent-folder" :disabled="revealing === entry.id" :aria-label="`在文件资源管理器中打开 ${entry.author} 的内容`" title="打开文件夹" @click="reveal(entry)"><AppIcon name="folder" :size="20"/></button>
        </article>
      </div>
      <div v-else class="home-empty card"><AppIcon name="library" :size="50"/><h3>{{ loading ? '正在读取媒体索引' : error ? '暂时无法读取最近内容' : '从第一份收藏开始' }}</h3><p>{{ loading ? '只读取近期条目，无需等待整个媒体库加载。' : error ? '恢复后台连接后重试，原有媒体不受影响。' : '下载喜欢的内容，或在媒体库为已有文件夹建立索引。' }}</p><router-link v-if="!loading && !error" to="/download" class="btn btn-primary">开始下载</router-link></div>
    </section>
  </div>
</template>

<style scoped>
.home-heading,.recent-heading { display:flex; align-items:center; justify-content:space-between; gap:18px; }
.eyebrow { color:var(--text-2); font-size:12px; letter-spacing:.08em; margin-bottom:8px; }
.home-heading .page-sub { margin-bottom:0; }.home-heading { margin-bottom:28px; }
.home-connection { display:flex; align-items:center; gap:7px; flex-wrap:wrap; justify-content:flex-end; color:var(--text-2); font-size:12px; }
.home-connection .dot { color:var(--orange); }.home-connection .connected { color:var(--green); }.home-connection small { flex-basis:100%; text-align:right; font-size:11px; }
.status-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:18px; }
.status-card { display:flex; flex-direction:column; min-width:0; padding:20px; box-shadow:none; }
.widget-heading { display:flex; align-items:center; gap:10px; }.widget-heading h2 { font-size:14px; font-weight:600; }
.widget-icon { width:40px; height:40px; border-radius:10px; display:grid; place-items:center; background:var(--soft-blue); }
.live-status .widget-icon { background:var(--soft-orange); }.scan-status .widget-icon { background:var(--soft-green); }
.widget-link { margin-left:auto; color:var(--text-2); padding:6px; border-radius:6px; }.widget-link:hover { background:var(--selected); }
.widget-number { display:flex; gap:10px; align-items:baseline; margin:20px 0 5px; }.widget-number strong { font-size:38px; font-weight:650; letter-spacing:-.04em; line-height:1.1; font-variant-numeric:tabular-nums; }.widget-number span { color:var(--text-2); font-size:12px; }
.widget-description,.widget-idle { font-size:12px; color:var(--text-2); line-height:1.7; overflow-wrap:anywhere; }
.widget-detail,.widget-idle { margin-top:18px; min-height:89px; }.widget-detail { padding:14px 0 8px; border-top:1px solid var(--border); }
.widget-title { font-size:13px; line-height:1.6; overflow-wrap:anywhere; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.mini-author { display:flex; gap:6px; align-items:center; font-size:12px; margin-bottom:6px; }.mini-author span { overflow-wrap:anywhere; }
.widget-state { display:inline-block; margin-left:7px; padding:2px 6px; border-radius:5px; background:var(--soft-orange); color:#946126; font-size:11px; }
.live-elapsed { font-size:22px; font-variant-numeric:tabular-nums; margin:7px 0; }.live-elapsed small { font-size:11px; margin-left:8px; color:var(--text-2); }
.widget-kicker { font-size:11px; color:var(--text-2); margin-bottom:5px; }.widget-footer { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-top:auto; padding-top:15px; color:var(--blue); font-size:12px; text-decoration:none; }
.recent-section { margin-top:32px; }.recent-heading { margin-bottom:18px; }.recent-heading h2 { font-size:20px; font-weight:650; }.recent-heading p { font-size:12px; color:var(--text-2); margin-top:5px; }
.recent-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:18px; }.recent-card { min-width:0; position:relative; overflow:hidden; border:1px solid var(--border); border-radius:var(--radius-card); background:#fff; }.recent-card:hover { border-color:#c9d7e9; box-shadow:var(--shadow-card-hover); }
.recent-open { display:block; height:100%; color:inherit; text-decoration:none; }.recent-cover { position:relative; aspect-ratio:4/3; background:var(--sidebar-bg); overflow:hidden; }
.recent-caption { padding:14px; }.recent-author { display:flex; align-items:center; gap:7px; }.recent-author strong { font-size:13px; font-weight:600; overflow-wrap:anywhere; }.recent-text { font-size:12px; color:var(--text-2); line-height:1.65; margin:8px 0 12px; height:40px; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; overflow-wrap:anywhere; }.recent-caption small { font-size:11px; color:var(--text-2); display:block; padding-right:34px; }
.recent-folder { position:absolute; bottom:9px; right:9px; width:30px; height:30px; display:grid; place-items:center; border:1px solid var(--border); background:var(--sidebar-bg); border-radius:7px; cursor:pointer; }.recent-folder:hover { background:var(--selected); }
.home-empty { padding:42px 24px; text-align:center; box-shadow:none; }.home-empty h3 { margin:12px 0 8px; font-size:17px; }.home-empty p { color:var(--text-2); font-size:13px; margin-bottom:18px; line-height:1.7; }
.home-error { display:flex; align-items:center; justify-content:space-between; gap:15px; padding:12px 16px; margin-bottom:18px; border-radius:10px; background:var(--soft-orange); font-size:12px; line-height:1.6; }
@media(max-width:1199px) and (min-width:768px) { .status-grid { gap:12px; }.status-card { padding:16px; }.widget-number { flex-wrap:wrap; gap:4px 10px; }.widget-number span { flex-basis:100%; } }
@media(max-width:767px) { .home-heading { align-items:flex-start; }.home-connection { max-width:96px; flex-shrink:0; padding-top:8px; }.status-grid { grid-template-columns:1fr; }.widget-detail,.widget-idle { min-height:0; }.recent-grid { grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }.recent-caption { padding:12px; }.recent-heading .btn { padding:8px; }.home-heading .page-sub { max-width:240px; }.home-error { align-items:flex-start; } }
@media(max-width:359px) { .recent-grid { grid-template-columns:1fr; } }
</style>
