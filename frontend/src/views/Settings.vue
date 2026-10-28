<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
import { toast, store } from '../store.js'
import AppIcon from '../components/AppIcon.vue'
const settings=ref(null), loading=ref(true), busy=ref(''), error=ref(''), checks=ref([])
const startup=ref(null), startupLoading=ref(false), startupSaving=ref(false), startupError=ref(''), folderOpening=ref('')
const labels={passed:'通过',unverified:'未验证',not_run:'未检测',pass:'通过',ok:'通过',success:'通过',fail:'失败',failed:'失败',error:'失败',warning:'需处理',warn:'需处理',unknown:'未确认',unavailable:'不可用',skipped:'未检测',not_tested:'未检测'}
function formatBytes(value){if(value===undefined || value===null)return '暂不可用';const bytes=Math.max(0,Number(value)||0);if(bytes<1024)return `${bytes} B`;const units=['KiB','MiB','GiB','TiB'];const index=Math.min(Math.floor(Math.log(bytes)/Math.log(1024))-1,units.length-1);return `${(bytes/1024**(index+1)).toFixed(1)} ${units[index]}`}
async function load(){loading.value=true;error.value='';try{settings.value=await api.settings();store.preferences={...settings.value.values}}catch(e){error.value=e.message}finally{loading.value=false}}
async function loadStartup(){startupLoading.value=true;startupError.value='';try{startup.value=await api.startupSettings()}catch(e){startupError.value=e.message}finally{startupLoading.value=false}}
async function changeStartup(enabled){if(startupSaving.value || startupLoading.value)return;startupSaving.value=true;startupError.value='';try{startup.value=await api.saveStartup(enabled);toast(enabled?'已核验并保存 Windows 启动快捷方式':'已关闭开机自启动','success')}catch(e){startupError.value=e.message;toast(e.message,'error')}finally{startupSaving.value=false}}
async function toggleStartup(event){const enabled=event.target.checked;event.target.checked=!!startup.value?.enabled;return changeStartup(enabled)}
async function openFolder(kind){folderOpening.value=kind;try{await api.openLocalFolder(kind)}catch(e){toast(e.message,'error')}finally{folderOpening.value=''}}
async function diagnose(){if(busy.value)return;busy.value='checks';try{const result=await api.checkSites();checks.value=result.checks || []}catch(e){toast(e.message,'error')}finally{busy.value=''}}
onMounted(()=>{const content=document.querySelector('.content');if(content)content.scrollTop=0;load();loadStartup()})
</script>
<template>
  <div class="settings-view"><h1 class="page-title">设置</h1>
    <p v-if="loading" role="status">正在读取设置…</p><div v-if="error" class="card error-text" role="alert">{{ error }} <button class="btn btn-ghost btn-sm" @click="load">重试</button></div>
    <template v-if="settings">
      <section class="card settings-card">
        <h2><AppIcon name="settings"/>启动与后台运行</h2>
        <label class="startup-toggle"><input type="checkbox" :checked="!!startup?.enabled" :disabled="startupLoading || startupSaving || !startup?.supported || (!startup?.available && !startup?.enabled)" @change="toggleStartup">开机自启动<span v-if="startupSaving" role="status">保存中…</span></label>
        <p class="hint">登录 Windows 后在托盘后台启动，不弹出浏览器。默认关闭，仅影响当前用户；休眠或未登录时不会唤醒电脑执行扫描。</p>
        <p v-if="startupLoading" class="hint" role="status">正在读取自启动状态…</p>
        <p v-if="startup?.message" class="hint">{{ startup.message }}</p>
        <div class="settings-actions"><button class="btn btn-ghost btn-sm" :disabled="startupLoading || startupSaving" @click="loadStartup">检查启动状态</button><button v-if="startup?.enabled && startup?.available" class="btn btn-ghost btn-sm" :disabled="startupLoading || startupSaving" @click="changeStartup(true)">重新登记</button></div>
        <p v-if="startupError" class="error-text" role="alert">{{ startupError }} <button class="text-button" :disabled="startupLoading || startupSaving" @click="loadStartup">重试</button></p>
        <p class="hint">双击 start.bat 隐藏控制台启动。关闭网页后服务继续运行；从右下角托盘可打开页面、查看本地目录或正常退出。排查启动问题时，可查看启动日志，或在终端运行 python run.py。</p>
        <div class="settings-actions"><button v-for="folder in [{kind:'library',label:'打开媒体目录'},{kind:'data',label:'打开数据目录'},{kind:'logs',label:'打开启动日志'}]" :key="folder.kind" class="btn btn-ghost btn-sm" :disabled="!!folderOpening" @click="openFolder(folder.kind)"><AppIcon name="folder"/>{{ folderOpening===folder.kind?'正在打开…':folder.label }}</button></div>
      </section>
      <section class="card settings-card"><h2><AppIcon name="folder"/>数据位置</h2><dl class="path-list"><dt>媒体库</dt><dd>{{ settings.paths?.library_root }}</dd><dt>默认下载目录</dt><dd>{{ settings.paths?.default_download_dir || settings.paths?.library_root }}</dd><dt>应用数据</dt><dd>{{ settings.paths?.data_dir }}</dd></dl></section>
      <section class="card settings-card"><h2><AppIcon name="tasks"/>任务与日志保留</h2><p>任务摘要长期保留；详细日志长期保存并压缩归档。隐藏任务、删除详细日志与删除媒体是独立操作。</p><p class="hint">本轮不按天数或条数自动删除任务和日志。任务页可显示隐藏记录并恢复。</p><dl v-if="settings.storage" class="version-list"><dt>任务记录（含隐藏）</dt><dd>{{ settings.storage.task_count ?? '暂不可用' }}</dd><dt>数据库（含事务日志）</dt><dd>{{ formatBytes(settings.storage.database_bytes) }}</dd><dt>已归档详细日志</dt><dd>{{ formatBytes(settings.storage.archived_log_bytes) }}</dd><dt>数据磁盘剩余空间</dt><dd>{{ formatBytes(settings.storage.free_bytes) }}</dd></dl><p v-if="settings.storage?.low_space" role="alert" class="error-text">数据磁盘剩余空间不足 512 MiB。建议将备份保存到空间充足的位置，并在确认后手动清理不再需要的日志；应用不会自动删除数据。</p><p v-if="settings.storage" class="hint">容量统计最多缓存 60 秒。缩略图容量为清理目标；最近 10 分钟使用的缓存受到保护，可能暂时超过目标。</p><router-link to="/tasks" class="btn btn-ghost btn-sm">管理任务与日志</router-link></section>
      <section class="card settings-card"><h2><AppIcon name="scan"/>环境与脚本检测</h2>
        <div class="settings-actions"><button class="btn btn-ghost" :disabled="!!busy" @click="diagnose">{{ busy==='checks'?'正在检查…':'检查登录与网页访问' }}</button></div>
        <p v-if="busy==='checks'" role="status" class="hint">正在检查抖音登录和平台网页连接…</p>
        <div v-for="check in checks" :key="check.id" class="check-row"><div><strong>{{ check.label }}</strong><p :title="check.note">{{ check.reason }}</p><p v-if="check.suggestion" class="hint">{{ check.suggestion }}</p></div><span class="check-badge" :class="'check-'+check.status">{{ labels[check.status] || check.status }}</span></div>
      </section>
      <details class="card settings-card version-card"><summary><AppIcon name="info"/><span>版本信息</span><AppIcon name="chevron-right" :size="16" class="version-chevron"/></summary><div class="version-content"><dl class="version-list"><template v-for="(value,key) in settings.versions" :key="key"><dt>{{ key }}</dt><dd>{{ value ?? '未安装或不可用' }}</dd></template></dl><p class="hint">图标素材与许可：<a href="/licenses/README.md" target="_blank" rel="noopener">查看来源记录</a></p></div></details>
    </template>
  </div>
</template>
<style scoped>
.settings-card { margin-bottom:22px; }.settings-card h2 { display:flex; align-items:center; gap:10px; font-size:18px; margin-bottom:20px; }.settings-card p { font-size:13px; line-height:1.7; }.settings-card .hint { font-size:12px; color:var(--text-2); margin:12px 0 18px; }
.path-list,.version-list { display:grid; grid-template-columns:auto minmax(0,1fr); gap:12px 20px; font-size:13px; line-height:1.6; }.path-list dt,.version-list dt { color:var(--text-2); }.path-list dd,.version-list dd { overflow-wrap:anywhere; }
.settings-actions { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:15px; }.check-row { display:flex; justify-content:space-between; gap:14px; border-top:1px solid var(--border); padding:15px 0; font-size:13px; }.check-row>div { flex:1; min-width:0; overflow-wrap:anywhere; }.check-row p { margin-top:7px; }.check-badge { height:max-content; border-radius:8px; background:#e8edf3; color:#647a8c; padding:4px 8px; font-size:11px; white-space:nowrap; }.check-passed { background:#e2f0e4; color:#427951; }.check-failed { background:#ffe8e2; color:#b43e35; }.check-warning { background:#fff2db; color:#8d6624; }
.startup-toggle { display:flex; align-items:center; gap:10px; font-size:14px; cursor:pointer; }.startup-toggle input { width:18px; height:18px; accent-color:var(--blue); }.startup-toggle span { color:var(--text-2); font-size:12px; }.text-button { border:0; background:transparent; color:var(--blue); cursor:pointer; }.error-text { color:#b23d32; }
.version-card>summary { display:flex; align-items:center; gap:10px; cursor:pointer; list-style:none; font-size:18px; font-weight:600; }.version-card>summary::-webkit-details-marker { display:none; }.version-chevron { margin-left:auto; }.version-card[open] .version-chevron { transform:rotate(90deg); }.version-content { border-top:1px solid var(--border); padding-top:20px; margin-top:20px; }.version-content .hint { margin-bottom:0; }
@media(max-width:600px){.path-list { grid-template-columns:1fr; gap:5px; }.path-list dd { margin-bottom:10px; }}
</style>
