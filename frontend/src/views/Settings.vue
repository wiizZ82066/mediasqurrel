<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api.js'
import { toast, store, loginAccount, readAccountStatus } from '../store.js'
import AppIcon from '../components/AppIcon.vue'
import PlatformLogo from '../components/PlatformLogo.vue'

const accounts = [{ platform: 'douyin', name: '抖音' }, { platform: 'weibo', name: '微博' }]
const accountLabels = { signed_in:'已登录', signed_out:'未登录', expired:'登录已失效', pending:'正在核验', unknown:'暂时无法验证' }
const settings = ref(null), loading = ref(true), error = ref('')
const startup = ref(null), startupLoading = ref(false), startupSaving = ref(false), startupError = ref('')
const editing = ref(false), directory = ref(''), preview = ref(null), directoryError = ref(''), directoryBusy = ref('')
const pathField = ref(null), changeButton = ref(null), restarting = ref(false)
const busy = ref(''), checks = ref([]), folderOpening = ref('')
let timer, restartTimer, disposed = false
const startupChecked = computed(() => !!(startup.value?.enabled && startup.value?.matches_current))
const database = computed(() => settings.value?.database)
const statusLabels = { passed:'通过', failed:'失败', warning:'需处理', unverified:'未验证', skipped:'未检测' }

function checkTime(value) {
  if (!value) return ''
  const date = new Date(value)
  return date.toLocaleString('zh-CN', { month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit', hour12:false })
}
function formatBytes(bytes) {
  if (bytes == null) return '暂不可用'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  const index = Math.min(Math.floor(Math.log(Math.max(1, bytes)) / Math.log(1024)), units.length - 1)
  return (bytes / 1024 ** index).toFixed(index ? 1 : 0) + ' ' + units[index]
}
async function load() {
  loading.value = true
  error.value = ''
  try {
    settings.value = await api.settings()
    store.preferences = { ...settings.value.values }
  } catch (e) { error.value = e.message }
  finally { loading.value = false }
}
async function loadStartup() {
  if (startupLoading.value || startupSaving.value) return
  startupLoading.value = true
  startupError.value = ''
  try { startup.value = await api.startupSettings() }
  catch (e) { startupError.value = e.message }
  finally { startupLoading.value = false }
}
async function changeStartup(enabled) {
  if (startupSaving.value || startupLoading.value) return
  startupSaving.value = true
  startupError.value = ''
  try {
    await api.saveStartup(enabled)
    const actual = await api.startupSettings()
    startup.value = actual
    if (actual.enabled !== enabled || (enabled && !actual.matches_current)) throw new Error('Windows 登记与所选状态不一致，请再次勾选。')
    toast(enabled ? '已核验开机自启动' : '已核验自启动已关闭', 'success')
  } catch (e) { startupError.value = e.message }
  finally { startupSaving.value = false }
}
function toggleStartup(event) {
  const enabled = event.target.checked
  event.target.checked = startupChecked.value
  return changeStartup(enabled)
}
async function editDirectory() {
  editing.value = true
  directory.value = database.value?.next_directory || database.value?.directory || ''
  directoryError.value = ''
  preview.value = null
  await nextTick()
  pathField.value?.focus({ preventScroll:true })
  const editor = pathField.value?.form
  const nav = document.querySelector('.sidebar')
  if (editor && nav && window.matchMedia('(max-width:767px)').matches &&
      editor.getBoundingClientRect().bottom > nav.getBoundingClientRect().top) {
    editor.scrollIntoView({ block:'center', behavior:'instant' })
  }
}
async function closeEditor() {
  editing.value = false
  preview.value = null
  directoryError.value = ''
  await nextTick()
  changeButton.value?.focus()
}
async function pickDirectory() {
  if (directoryBusy.value) return
  directoryBusy.value = 'pick'
  directoryError.value = ''
  try {
    const result = await api.pickDatabase()
    if (result.directory) { directory.value = result.directory; preview.value = null }
  } catch (e) { directoryError.value = e.message }
  finally { directoryBusy.value = '' }
}
async function inspectDirectory() {
  if (directoryBusy.value || !directory.value.trim()) return
  directoryBusy.value = 'inspect'
  directoryError.value = ''
  preview.value = null
  try { preview.value = await api.previewDatabase(directory.value.trim()) }
  catch (e) { directoryError.value = e.message }
  finally { directoryBusy.value = '' }
}
async function saveDirectory() {
  if (directoryBusy.value || !preview.value) return
  directoryBusy.value = 'save'
  directoryError.value = ''
  try {
    const result = await api.selectDatabase(preview.value.directory, preview.value.kind)
    settings.value.database = { ...database.value, next_directory:result.directory, restart_required:result.restart_required, fallback:false }
    await closeEditor()
    toast(result.restart_required ? '已保存数据库目录' : '已选用当前数据库', 'success')
  } catch (e) { directoryError.value = e.message; preview.value = null }
  finally { directoryBusy.value = '' }
}
async function restart() {
  if (restarting.value) return
  restarting.value = true
  directoryError.value = ''
  try { await api.restartDatabase() }
  catch (e) { directoryError.value = e.message; restarting.value = false; return }
  const until = Date.now() + 120000
  async function reconnect() {
    if (disposed) return
    try {
      const value = await api.settings()
      if (!value.database.restart_required) { window.location.reload(); return }
    } catch { /* The listener closes while the process changes databases. */ }
    if (Date.now() < until) restartTimer = setTimeout(reconnect, 1800)
    else { restarting.value = false; directoryError.value = '服务尚未就绪。请刷新页面，或从启动器重新打开。' }
  }
  restartTimer = setTimeout(reconnect, 1500)
}
async function openFolder(kind) {
  folderOpening.value = kind
  try { await api.openLocalFolder(kind) } catch (e) { toast(e.message, 'error') }
  finally { folderOpening.value = '' }
}
async function diagnose() {
  if (busy.value) return
  busy.value = 'checks'
  try { checks.value = (await api.checkSites()).checks || [] } catch (e) { toast(e.message, 'error') }
  finally { busy.value = '' }
}
onMounted(() => {
  const content = document.querySelector('.content')
  if (content) content.scrollTop = 0
  load()
  loadStartup()
  window.addEventListener('focus', loadStartup)
  readAccountStatus()
  timer = setInterval(readAccountStatus, 10000)
})
onBeforeUnmount(() => {
  disposed = true
  window.removeEventListener('focus', loadStartup)
  clearInterval(timer)
  clearTimeout(restartTimer)
})
</script>

<template>
  <div class="settings-view">
    <h1 class="page-title">设置</h1>
    <section id="platform-accounts" aria-labelledby="accounts-heading" class="group">
      <h2 id="accounts-heading">平台账号</h2>
      <div class="account-list">
        <article v-for="account in accounts" :key="account.platform" class="account">
          <div class="account-line">
            <PlatformLogo :platform="account.platform" :size="38" aria-hidden="true"/>
            <h3>{{ account.name }}</h3>
            <span class="account-state" :class="store.accountLogin[account.platform].status" role="status">{{ accountLabels[store.accountLogin[account.platform].status] }}</span>
          </div>
          <div class="account-foot">
            <span v-if="store.accountLogin[account.platform].checked_at" class="muted">最近核验 {{ checkTime(store.accountLogin[account.platform].checked_at) }}</span>
            <button class="btn btn-ghost btn-sm" :disabled="!!store.accountLogin.pending" :aria-busy="store.accountLogin.pending===account.platform" :aria-label="(store.accountLogin[account.platform].saved?'重新登录':'登录')+account.name" @click="loginAccount(account.platform)">{{ store.accountLogin.pending===account.platform?'等待登录…':store.accountLogin[account.platform].saved?'重新登录':'登录' }}</button>
          </div>
          <p v-if="account.platform==='douyin' && store.accountLogin.douyin.limited" class="notice">平台最近一次仅返回部分公开作品。</p>
        </article>
      </div>
    </section>
    <p v-if="loading" role="status" class="muted">正在读取设置…</p>
    <p v-if="error" class="issue" role="alert">{{ error }} <button class="text-action" @click="load">重试</button></p>
    <template v-if="settings">
      <section class="group" aria-labelledby="preferences-heading">
        <h2 id="preferences-heading">常规</h2>
        <div class="sheet">
          <div class="setting-row">
            <div><h3>开机自启动</h3>
              <p class="muted" role="status">{{ startupSaving?'正在核验 Windows 设置…':startupLoading?'正在读取 Windows 设置…':startup?.message || '登录 Windows 后在托盘运行。' }}</p>
              <p v-if="startupError" class="issue" role="alert">{{ startupError }} <button class="text-action" @click="loadStartup">重试</button></p>
            </div>
            <input type="checkbox" aria-label="开机自启动" class="startup-checkbox" :checked="startupChecked" :disabled="startupSaving || startupLoading || !startup?.supported || (!startup?.available && !startup?.enabled)" @change="toggleStartup">
          </div>
          <div class="setting-row">
            <div><h3>数据库目录</h3><p class="path" :title="database?.directory">{{ database?.directory }}</p></div>
            <button ref="changeButton" class="btn btn-ghost btn-sm" :aria-expanded="editing" :disabled="editing || restarting" @click="editDirectory">更改目录</button>
          </div>
          <form v-if="editing" class="directory-editor" @submit.prevent="preview ? saveDirectory() : inspectDirectory()">
            <label for="database-path">新目录</label>
            <div class="input-row">
              <input id="database-path" ref="pathField" v-model="directory" type="text" class="path-input" autocomplete="off" :disabled="!!directoryBusy" @input="preview=null;directoryError=''">
              <button type="button" class="btn btn-ghost btn-sm" :disabled="!!directoryBusy" @click="pickDirectory">选择文件夹</button>
            </div>
            <p class="muted" role="status">{{ preview ? (preview.kind==='existing'?'已找到 Media Squirrel 数据库，切换后可查看其中的记录。':'这是空文件夹，确认后将在这里建立新库。') : '可选择已有数据库所在的文件夹，或一个空文件夹。媒体文件继续保存在原处。' }}</p>
            <p v-if="directoryError" class="issue" role="alert">{{ directoryError }}</p>
            <div class="actions"><button type="submit" class="btn btn-primary btn-sm" :disabled="!!directoryBusy || !directory.trim()">{{ directoryBusy==='inspect'?'检查中…':directoryBusy==='save'?'保存中…':preview?'确认目录':'检查目录' }}</button><button type="button" class="btn btn-ghost btn-sm" :disabled="!!directoryBusy" @click="closeEditor">取消</button></div>
          </form>
          <div v-if="database?.restart_required && !editing" class="pending-row">
            <div><strong>{{ database.fallback?'所选磁盘未连接，正在使用原数据库':'下次启动使用' }}</strong><p class="path">{{ database.next_directory }}</p></div>
            <button class="btn btn-ghost btn-sm" :disabled="restarting" @click="restart">{{ restarting?'正在重启…':'重启并应用' }}</button>
          </div>
          <p v-if="directoryError && !editing" class="inline-issue issue" role="alert">{{ directoryError }}</p>
          <details class="fold">
            <summary><h3>版本信息</h3><span class="version">Media Squirrel {{ settings.version }}</span><AppIcon name="chevron-right" :size="16" class="chevron"/></summary>
            <div class="fold-body"><dl><template v-for="(value,key) in settings.versions" :key="key"><dt>{{ key }}</dt><dd>{{ value ?? '未安装' }}</dd></template></dl><a href="/licenses/README.md" target="_blank" rel="noopener">图标来源与许可</a></div>
          </details>
        </div>
      </section>
      <section class="group" aria-labelledby="more-heading">
        <h2 id="more-heading">存储与检查</h2>
        <div class="sheet">
          <details class="fold"><summary><h3>媒体与任务日志</h3><AppIcon name="chevron-right" :size="16" class="chevron"/></summary>
            <div class="fold-body"><dl><dt>媒体库</dt><dd>{{ settings.paths?.library_root }}</dd><dt>默认下载目录</dt><dd>{{ settings.paths?.default_download_dir }}</dd><dt>任务记录</dt><dd>{{ settings.storage?.task_count ?? '暂不可用' }}</dd><dt>数据库大小</dt><dd>{{ formatBytes(settings.storage?.database_bytes) }}</dd><dt>归档日志</dt><dd>{{ formatBytes(settings.storage?.archived_log_bytes) }}</dd><dt>磁盘可用空间</dt><dd>{{ formatBytes(settings.storage?.free_bytes) }}</dd></dl>
              <p class="muted">任务历史长期保存，详细日志压缩归档。</p><p v-if="settings.storage?.low_space" class="issue" role="alert">可用空间不足 512 MiB。</p>
              <div class="actions"><router-link to="/tasks">查看任务记录</router-link><button v-for="item in [{kind:'library',label:'媒体目录'},{kind:'data',label:'数据目录'},{kind:'logs',label:'日志目录'}]" :key="item.kind" class="text-action" :disabled="!!folderOpening" @click="openFolder(item.kind)">{{ item.label }}</button></div>
            </div>
          </details>
          <details class="fold"><summary><h3>环境与脚本检测</h3><AppIcon name="chevron-right" :size="16" class="chevron"/></summary>
            <div class="fold-body"><button class="btn btn-ghost btn-sm" :disabled="!!busy" @click="diagnose">{{ busy?'检查中…':'检查平台访问' }}</button>
              <div v-for="check in checks" :key="check.id" class="check"><div><strong>{{ check.label }}</strong><p class="muted">{{ check.reason }}</p><p v-if="check.suggestion" class="muted">{{ check.suggestion }}</p></div><span>{{ statusLabels[check.status] || check.status }}</span></div>
            </div>
          </details>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.settings-view {max-width:910px;margin-inline:auto;color:var(--text)}
.group {margin:0 0 30px}
.group h2 {font-size:16px;line-height:1.5;margin:0 0 12px;font-weight:650}
.group h3 {font-size:14px;line-height:1.5;font-weight:650}
.account-list {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.account,.sheet {border:1px solid var(--border);border-radius:14px;background:var(--surface);box-shadow:var(--shadow-card)}
.account {padding:18px 20px}
.account-line,.account-foot,.setting-row,.pending-row,.fold summary,.input-row,.actions,.check {display:flex;align-items:center;gap:12px}
.account-line h3 {font-size:16px}
.account-state {margin-inline-start:auto;display:inline-flex;align-items:center;gap:6px;border-radius:20px;padding:4px 9px;background:#f1f3f6;color:#566274;font-size:12px;font-weight:550;white-space:nowrap}
.account-state::before {content:'';width:6px;height:6px;background:currentColor;border-radius:100%}
.signed_in {color:#316b4d;background:var(--soft-green)}
.expired,.unknown {color:#885b1c;background:var(--soft-orange)}
.pending {background:var(--soft-blue);color:var(--blue)}
.account-foot {justify-content:space-between;margin-top:18px}
.account-foot .btn {margin-left:auto}
.muted,.path,.notice,.issue {font-size:13px;line-height:1.55}
.muted,.path {color:var(--text-2)}
.path,dd,.issue {overflow-wrap:anywhere}
.notice {margin-top:12px;color:var(--orange)}
.setting-row,.fold summary,.pending-row {padding:16px 20px;min-height:56px;justify-content:space-between}
.sheet>*+* {border-top:1px solid var(--border)}
.setting-row>div,.pending-row>div {min-width:0}
.setting-row p,.pending-row p {margin-top:3px}
.setting-row>.btn,.pending-row>.btn,.input-row>.btn {flex-shrink:0}
.startup-checkbox {width:19px;height:19px;accent-color:var(--blue);margin:4px;cursor:pointer}
.directory-editor,.pending-row {background:var(--sidebar-bg)}
.directory-editor {padding:16px 20px;display:grid;gap:10px}
.directory-editor label,.pending-row strong {font-size:13px}
.path-input {width:100%;min-width:0;padding:9px 10px;border:1px solid #c8d2df;border-radius:8px;color:var(--text);background:var(--surface);font:inherit;font-size:13px}
.fold summary {cursor:pointer;list-style:none}
.fold summary::-webkit-details-marker {display:none}
.version {margin-left:auto;color:var(--text-2);font-size:13px}
.chevron {color:var(--text-2);flex:none}
.fold[open] .chevron {transform:rotate(90deg)}
.fold-body {padding:0 20px 17px;display:grid;gap:12px;font-size:13px}
.fold-body>.btn {justify-self:start}
.fold-body dl {display:grid;grid-template-columns:minmax(90px,max-content) minmax(0,1fr);gap:8px 20px;line-height:1.55}
.fold-body dt {color:var(--text-2)}
.actions {flex-wrap:wrap}
.check {align-items:flex-start;justify-content:space-between;border-top:1px solid var(--border);padding-top:12px}
.check span {flex:none}
.text-action,.fold-body a {color:var(--blue);font:inherit;font-size:13px}
.text-action {border:0;background:transparent;cursor:pointer}
.issue {color:var(--red)}
.inline-issue {padding:10px 20px}
@media (max-width:700px) {
  .account-list {grid-template-columns:1fr;gap:8px}
  .account {padding:15px}
  .account-foot {margin-top:12px}
  .group {margin-bottom:22px}
  .setting-row,.fold summary,.pending-row,.directory-editor {padding:14px}
  .fold-body {padding:0 14px 14px}
}
</style>
