<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api.js'
import { store, toast, upsertTaskSummary, onTaskEvent } from '../store.js'
import AppIcon from '../components/AppIcon.vue'
import PlatformLogo from '../components/PlatformLogo.vue'
import ActivityProgress from '../components/ActivityProgress.vue'
const router = useRouter()
const saved = store.downloadState
const scripts = ref([])
const selectedId = ref(saved.selectedId)
const form = ref({})
const selected = computed(() => scripts.value.find(s => s.id === selectedId.value))
const mainParam = computed(() => selected.value?.params.find(p => ['input', 'url'].includes(p.name)))
const submitting = ref(false)
const clipboardBusy = ref(false)
const clipboardMessage = ref('')
const loadError = ref('')
const rootPath = ref('')
const defaultOutput = ref('')
const task = ref(saved.task)
const entry = ref(null)
const previewError = ref('')
const previewLoading = ref(false)
let previewSequence = 0
let inputRevision = 0
let pollTimer = null
let disposed = false
const browsing = ref(false)
const browsePath = ref('')
const browseDirs = ref([])
const browseTarget = ref('')
const browseBusy = ref(false)
let browseSequence = 0
const statusLabels = { queued:'排队中', running:'下载中', success:'下载完成', failed:'下载失败', cancelled:'已取消', interrupted:'运行中断' }
function defaults(script) { return Object.fromEntries((script?.params || []).map(p => [p.name, p.default ?? (p.kind === 'switch' ? false : '')])) }
function select(id) {
  if (selectedId.value) saved.drafts[selectedId.value] = { ...form.value }
  selectedId.value = id; saved.selectedId = id
  form.value = { ...defaults(scripts.value.find(s => s.id === id)), ...(saved.drafts[id] || {}) }
  inputRevision++; clipboardMessage.value = ''
}
watch(form, value => { if (selectedId.value) saved.drafts[selectedId.value] = { ...value } }, { deep:true })
function detectPlatforms(text) {
  const found = new Set()
  for (const match of text.matchAll(/https?:\/\/[^\s<>"，。！？]+/gi)) {
    try {
      const host = new URL(match[0]).hostname.toLowerCase()
      for (const script of scripts.value) if ((script.link_hints || []).some(h => host === h || host.endsWith('.' + h))) found.add(script.id)
    } catch { /* invalid URL is reported by validation */ }
  }
  return [...found]
}
const linkWarning = computed(() => {
  const value = String(form.value[mainParam.value?.name] || '').trim()
  if (!value) return ''
  const detected = detectPlatforms(value)
  if (detected.length > 1) return '包含多个平台链接，请每次提交一个平台的内容。'
  if (!detected.includes(selectedId.value)) return detected.length ? '链接属于另一平台；请切换平台后提交。' : '未识别到支持的链接，请检查完整分享文案或网址。'
  return ''
})
function acceptPaste(text) {
  const platforms = detectPlatforms(text)
  if (platforms.length !== 1) { clipboardMessage.value = platforms.length ? '剪贴板中有多个平台链接，请手动选择内容。' : '剪贴板中没有受支持的链接，可继续手动输入或按 Ctrl+V。'; return }
  const id = platforms[0]
  if (id !== selectedId.value) {
    const target = saved.drafts[id]
    const param = scripts.value.find(s => s.id === id)?.params.find(p => ['input','url'].includes(p.name))
    if (target?.[param?.name]) {
      // Preserve another platform's existing draft; never replace it silently.
      clipboardMessage.value = '另一平台已有未提交内容，请先处理该草稿，再粘贴。'; return
    }
    select(id)
    toast(`已识别链接并切换到${selected.value.name}`, 'info')
  }
  form.value[mainParam.value.name] = text; inputRevision++
  clipboardMessage.value = '已粘贴。检查内容后开始下载。'
}
async function readClipboard() {
  if (!mainParam.value || clipboardBusy.value) return
  if (String(form.value[mainParam.value.name] || '').trim()) { clipboardMessage.value = '已保留输入内容。请先清空，或选中文本后按 Ctrl+V 替换。'; return }
  const revision = inputRevision; const platform = selectedId.value
  clipboardBusy.value = true
  try {
    if (!navigator.clipboard?.readText) throw new Error('unsupported')
    const text = await navigator.clipboard.readText()
    if (disposed || revision !== inputRevision || platform !== selectedId.value || String(form.value[mainParam.value?.name] || '').trim()) return
    if (!text.trim()) clipboardMessage.value = '剪贴板为空。复制链接后重试，或直接输入。'
    else acceptPaste(text)
  } catch { clipboardMessage.value = '无法读取剪贴板。请允许浏览器权限后重试，或按 Ctrl+V 粘贴。' }
  finally { clipboardBusy.value = false }
}
function onPaste(event) {
  // Native paste into existing text is an explicit editing action, so leave it native.
  if (String(form.value[mainParam.value?.name] || '').trim()) return
  const text = event.clipboardData?.getData('text') || ''
  if (detectPlatforms(text).length === 1) { event.preventDefault(); acceptPaste(text) }
}
function editInput() { inputRevision++; clipboardMessage.value = '' }
async function submit() {
  if (linkWarning.value) { toast(linkWarning.value, 'error'); return }
  submitting.value = true
  try {
    const params = { ...form.value }
    if (selectedId.value === 'weibo') params.url = String(params.url || '').match(/https?:\/\/[^\s<>"，。！？）)]+/i)?.[0] || params.url
    const created = await api.createTask(selectedId.value, params)
    task.value = created; saved.task = created; upsertTaskSummary(created)
    previewSequence++; entry.value = null; previewError.value = ''; previewLoading.value = false
    toast('已创建下载任务', 'success')
    if (created.status === 'success') loadPreview()
  } catch(e) { toast(e.message, 'error') }
  finally { submitting.value = false }
}
async function loadPreview() {
  const current = task.value
  if (current?.status !== 'success') return
  const sequence = ++previewSequence
  previewLoading.value = true; previewError.value = ''
  try {
    const located = await api.libraryLocate({ task_id: current.id })
    const detail = await api.libraryEntry(located.entry_id)
    if (disposed || sequence !== previewSequence || task.value?.id !== current.id) return
    entry.value = detail
  } catch(e) { if (sequence === previewSequence) previewError.value = '媒体索引尚不可用，可查看任务记录或稍后重试。' }
  finally { if (sequence === previewSequence) previewLoading.value = false }
}
function updateTask(summary) {
  if (summary?.id !== task.value?.id) return
  const becameSuccess = summary.status === 'success' && task.value?.status !== 'success'
  task.value = { ...task.value, ...summary }; saved.task = task.value
  if (becameSuccess) loadPreview()
}
const unsubscribe = onTaskEvent(msg => {
  if (msg.type === 'task_update') updateTask(msg.task)
  if (msg.type === 'library_cover' && entry.value?.id === msg.entry.id) Object.assign(entry.value, msg.entry)
  if (msg.type === 'task_progress' && msg.task_id === task.value?.id) updateTask({ id: msg.task_id, progress: msg.progress })
  if (msg.type === 'reconnect') refreshTrackedTask()
})
async function refreshTrackedTask() {
  if (!task.value?.id) return
  try { const summary = await api.task(task.value.id); if (!disposed) updateTask(summary) } catch { /* Task page remains available. */ }
}
const coverUrl = computed(() => entry.value?.cover ? '/api/thumb?' + new URLSearchParams({ p: `${entry.value.rel_dir}/${entry.value.cover}`, root_id:entry.value.root_id, w:720 }) : '')
function openLibrary() { if (entry.value?.id) router.push({ path:'/library', query:{ entry_id:entry.value.id } }) }
function openTask() { if (task.value) router.push({ path:'/tasks', query:{ task:task.value.id } }) }
async function loadBrowse(path) {
  const sequence = ++browseSequence; browseBusy.value = true
  try { const result = await api.browse(path); if(sequence === browseSequence) { browsePath.value = result.current === '.' ? '' : result.current; browseDirs.value = result.dirs } }
  catch(e) { toast(e.message,'error') }
  finally { if(sequence === browseSequence) browseBusy.value = false }
}
function openBrowser(name) { browseTarget.value=name; browsing.value=true; loadBrowse('') }
function pickDir(path) { form.value[browseTarget.value]=path; browsing.value=false }
function browseUp() { const parts=browsePath.value.split('/').filter(Boolean); parts.pop(); loadBrowse(parts.join('/')) }
function onKey(event) { if(event.key === 'Escape') browsing.value=false }
onMounted(async () => {
  const content = document.querySelector('.content'); if (content) content.scrollTop = 0
  window.addEventListener('keydown',onKey)
  try { scripts.value=await api.scripts(); const id=scripts.value.some(s=>s.id===selectedId.value)?selectedId.value:scripts.value[0]?.id; selectedId.value=''; if(id) select(id) }
  catch(e) { loadError.value=e.message }
  api.libraryRoots().then(r=>{rootPath.value=r.items?.find(i=>i.id===r.default_root_id)?.path || ''}).catch(()=>{})
  api.settings().then(result=>{defaultOutput.value=result.paths?.default_download_dir || result.paths?.library_root || ''}).catch(()=>{})
  await refreshTrackedTask(); if(task.value?.status==='success') loadPreview()
  pollTimer=setInterval(()=>{if(!store.wsConnected || ['queued','running'].includes(task.value?.status)) refreshTrackedTask()},5000)
})
onBeforeUnmount(()=>{disposed=true; previewSequence++; clearInterval(pollTimer); unsubscribe(); window.removeEventListener('keydown',onKey)})
</script>
<template>
  <div class="download-view">
    <h1 class="page-title">下载内容</h1><p class="page-sub">保存喜欢的内容，在本地相册中慢慢浏览。</p>
    <div class="download-columns">
      <section class="card download-form">
        <div class="platforms" aria-label="下载平台">
          <button v-for="script in scripts" :key="script.id" class="platform-choice" :class="{selected:script.id===selectedId}" :aria-pressed="script.id===selectedId" @click="select(script.id)"><PlatformLogo :platform="script.id" :size="34"/><span>{{ script.name }}</span></button>
        </div>
        <p v-if="loadError" role="alert" class="error-text">脚本清单加载失败：{{ loadError }}</p>
        <p v-else-if="!selected" class="muted">正在加载脚本清单…</p>
        <form v-if="selected" @submit.prevent="submit">
          <p class="form-description">{{ selected.description }}</p>
          <template v-for="param in selected.params" :key="selectedId+param.name">
            <div v-if="param.kind==='switch'" class="field switch-row"><div><label :for="'param-'+param.name">{{ param.label }}</label><p class="hint">{{ param.help }}</p></div><label class="switch"><input :id="'param-'+param.name" v-model="form[param.name]" type="checkbox"><span class="track"><span class="thumb"></span></span></label></div>
            <div v-else class="field">
              <div class="field-label"><label :for="'param-'+param.name">{{ param.label }}<span v-if="param.required" class="required"> *</span></label><button v-if="param.name===mainParam?.name" type="button" class="btn btn-ghost btn-sm" :disabled="clipboardBusy" @click="readClipboard"><AppIcon name="paste" :size="18"/>{{ clipboardBusy?'读取中…':'粘贴' }}</button></div>
              <div v-if="param.name===mainParam?.name" class="main-input">
                <textarea :id="'param-'+param.name" v-model="form[param.name]" class="textarea" :required="param.required" :placeholder="param.placeholder" @click="!String(form[param.name] || '').trim() && readClipboard()" @input="editInput" @paste="onPaste"></textarea>
                <button v-if="form[param.name]" type="button" class="clear-input" @click="form[param.name]=''; editInput()">清空</button>
              </div>
              <div v-else-if="param.name==='out' || param.label.includes('目录')" class="directory-input"><input :id="'param-'+param.name" v-model="form[param.name]" class="input" placeholder="留空使用默认媒体目录"><button type="button" class="btn btn-ghost btn-sm" @click="openBrowser(param.name)"><AppIcon name="folder" :size="20"/>浏览</button></div>
              <input v-else :id="'param-'+param.name" v-model="form[param.name]" class="input" :required="param.required" :placeholder="param.placeholder">
              <p v-if="param.name==='out'" class="hint path-hint">默认下载：{{ defaultOutput || rootPath || '设置中的媒体目录' }}。相对路径以媒体库根目录为起点。</p>
              <p v-else-if="param.name!==mainParam?.name && param.help" class="hint">{{ param.help }}</p>
              <template v-if="param.name===mainParam?.name"><p class="hint">点击空输入框可读取剪贴板；已有内容时请用 Ctrl+V 编辑。</p><p v-if="clipboardMessage" class="clipboard-message" role="status">{{ clipboardMessage }}</p><p v-if="linkWarning" class="error-text" role="status">{{ linkWarning }}</p></template>
            </div>
          </template>
          <div class="form-actions"><button class="btn btn-primary" :disabled="submitting || !selected.available">{{ submitting?'提交中…':'开始下载' }}</button><router-link to="/tasks" class="btn btn-ghost">任务队列</router-link></div>
          <p v-if="!selected.available" class="error-text">脚本文件不可用，请在设置中检测环境。</p>
        </form>
      </section>
      <section class="card result-panel" aria-label="下载结果预览">
        <h2>本次下载</h2>
        <div v-if="!task" class="result-empty"><AppIcon name="library" :size="72"/><h3>内容会在这里等你</h3><p>提交下载后显示对应任务的进度，完成后可进入媒体库查看。</p></div>
        <template v-else>
          <div class="result-task"><PlatformLogo :platform="task.script_id"/><div><strong>{{ task.script_name }}</strong><p class="task-id">任务 {{ task.id }}</p></div><span :class="'tag tag-'+task.status">{{ statusLabels[task.status] || task.status }}</span></div>
          <ActivityProgress :status="task.status" :progress="task.progress" label="本次下载进度"/>
          <div v-if="task.status==='success'" class="completed-preview">
            <button v-if="entry" class="cover-button" aria-label="在媒体库打开本次下载" @click="openLibrary"><img v-if="coverUrl" :src="coverUrl" alt="本次下载的内容封面" :style="entry.cover_face?{objectPosition:`${entry.cover_face.x*100}% ${entry.cover_face.y*100}%`}:{}"><span v-else class="text-cover"><AppIcon name="library" :size="60"/>文字内容已保存</span></button>
            <p v-if="entry" class="result-caption">{{ entry.author }} · {{ entry.sort_at || entry.date_dir }}<span>{{ entry.text_preview || entry.text?.slice(0,140) }}</span></p>
            <p v-if="previewLoading" class="muted">正在读取媒体索引…</p><p v-if="previewError" class="muted">{{ previewError }} <button class="text-button" @click="loadPreview">重试</button></p>
          </div>
          <div v-else class="result-empty pending"><AppIcon :name="task.status==='running'?'download':'tasks'" :size="58"/><p>{{ ['running','queued'].includes(task.status)?'完成后在这里显示本次内容的封面。':'此次任务尚未完成，可在任务记录中查看原因或重试。' }}</p></div>
          <div class="form-actions"><button class="btn btn-ghost btn-sm" @click="openTask">定位任务</button><button v-if="entry" class="btn btn-primary btn-sm" @click="openLibrary">打开媒体库</button><button v-if="['queued','running'].includes(task.status)" class="btn btn-danger-ghost btn-sm" @click="api.cancelTask(task.id).then(updateTask).catch(e=>toast(e.message,'error'))">取消下载</button></div>
        </template>
      </section>
    </div>
    <div v-if="browsing" class="modal-mask" @click.self="browsing=false"><section class="browse-panel card" role="dialog" aria-modal="true" aria-label="选择输出目录"><div class="browse-head"><button class="btn btn-ghost btn-sm" :disabled="!browsePath || browseBusy" @click="browseUp">上级</button><strong>{{ browsePath || '默认媒体目录' }}</strong><button class="btn btn-ghost btn-sm" @click="browsing=false">关闭</button></div><p class="hint path-hint">{{ rootPath }}</p><div class="browse-list"><p v-if="browseBusy">正在读取目录…</p><p v-else-if="!browseDirs.length" class="muted">此目录没有子目录。</p><div v-for="dir in browseDirs" :key="dir.rel" class="browse-row"><button @click="loadBrowse(dir.rel)"><AppIcon name="folder" :size="22"/>{{ dir.name }}</button><button class="text-button" @click="pickDir(dir.rel)">选择</button></div></div><button class="btn btn-primary btn-sm" :disabled="browseBusy" @click="pickDir(browsePath)">使用当前目录</button></section></div>
  </div>
</template>
<style scoped>
.download-columns { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:24px; align-items:start; }.download-form,.result-panel { min-width:0; }.platforms { display:flex; gap:10px; margin-bottom:24px; }.platform-choice { flex:1; display:flex; align-items:center; gap:10px; border:1px solid var(--border); border-radius:14px; padding:12px; background:#fff; cursor:pointer; font-size:13px; text-align:left; }.platform-choice.selected { border-color:var(--blue); background:#f1f7fc; }.form-description { color:var(--text-2); font-size:13px; line-height:1.6; margin-bottom:22px; }.field-label { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:8px; }.field-label label { margin:0; }.required,.error-text { color:#bb392d; }.error-text,.clipboard-message { font-size:12px; margin-top:8px; line-height:1.6; }.clipboard-message { color:#47766a; }.main-input { position:relative; }.main-input textarea { min-height:150px; padding-bottom:36px; }.clear-input { position:absolute; right:12px; bottom:12px; border:0; background:#eef0f3; padding:4px 10px; border-radius:8px; font-size:12px; cursor:pointer; }.directory-input { display:flex; gap:8px; }.directory-input .input { min-width:0; }.directory-input .btn { flex-shrink:0; }.path-hint { overflow-wrap:anywhere; }.form-actions { display:flex; flex-wrap:wrap; gap:10px; margin-top:22px; }.result-panel h2 { font-size:18px; margin-bottom:22px; }.result-empty { min-height:335px; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:20px; text-align:center; padding:25px 10px; }.result-empty h3 { font-size:17px; }.result-empty p,.muted { color:var(--text-2); font-size:13px; line-height:1.7; }.result-empty.pending { min-height:260px; }.result-task { display:flex; gap:10px; align-items:center; }.result-task>div { min-width:0; flex:1; }.result-task strong { font-size:14px; }.task-id { font:11px Consolas,monospace; color:var(--text-2); overflow-wrap:anywhere; margin-top:4px; }.completed-preview { margin-top:20px; }.cover-button { border:0; width:100%; border-radius:14px; overflow:hidden; cursor:pointer; background:#e9eeec; aspect-ratio:4/3; }.cover-button img { width:100%; height:100%; object-fit:cover; display:block; }.text-cover { height:100%; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:15px; color:var(--text-2); }.result-caption { margin-top:14px; font-size:13px; line-height:1.6; overflow-wrap:anywhere; }.result-caption span { display:block; color:var(--text-2); margin-top:5px; }.text-button { border:0; background:transparent; color:var(--blue); cursor:pointer; font-size:13px; padding:4px; }.modal-mask { position:fixed; inset:0; z-index:600; background:#19242f55; display:grid; place-items:center; padding:20px; }.browse-panel { width:min(560px,100%); background:#f8f9fb; }.browse-head { display:flex; align-items:center; gap:12px; margin-bottom:12px; }.browse-head strong { flex:1; font-size:13px; overflow-wrap:anywhere; }.browse-list { max-height:48vh; overflow:auto; margin:20px 0; }.browse-row { display:flex; align-items:center; gap:8px; border-bottom:1px solid var(--border); }.browse-row>button:first-child { flex:1; border:0; background:transparent; text-align:left; padding:12px 0; display:flex; align-items:center; gap:10px; cursor:pointer; overflow-wrap:anywhere; }.hint { color:var(--text-2); font-size:12px; line-height:1.6; }
@media(max-width:1050px) { .download-columns { grid-template-columns:minmax(0,1fr); }.result-empty { min-height:230px; }.cover-button { max-height:420px; } }
@media(max-width:450px) { .platform-choice { padding:10px 8px; gap:6px; }.platform-choice span { font-size:12px; }.result-task { flex-wrap:wrap; } }
</style>
