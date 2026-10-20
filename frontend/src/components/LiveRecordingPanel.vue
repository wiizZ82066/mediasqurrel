<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api.js'
import { toast } from '../store.js'
import ActivityProgress from './ActivityProgress.vue'
import AppIcon from './AppIcon.vue'
import IntervalPicker from './IntervalPicker.vue'
import CalendarPlanFields from './CalendarPlanFields.vue'
import { planDraft, planPayload, addLiveTime } from '../schedule-ui.js'
import { liveLabels, liveState } from '../live-status.js'

const props = defineProps({ sub: { type: Object, required: true } })
const emit = defineEmits(['updated'])
const room = ref(''), enabled = ref(false), interval = ref(60), monitor = ref(null), records = ref([])
const source=ref('profile'), intervalEnabled=ref(true), calendarEnabled=ref(false), draft=ref(planDraft())
const quality=ref('best')
const qualities={best:'最高可用（默认）',origin:'原画',uhd:'超清',hd:'高清',sd:'标清',ld:'流畅'}
const loading = ref(true), saving = ref(false), error = ref(''), offset = ref(0)
const mergeErrors=ref([]), merging=ref(false)
const planNotice=ref(''), formElement=ref(null), planOpen=ref(false)
function addTime(item) {
  try {
    draft.value=addLiveTime(draft.value,item,calendarEnabled.value)
    calendarEnabled.value=true
    planOpen.value=true
    planNotice.value=(item.broadcast_started_at?'平台开播时间':'开始录制时间（历史记录未保存真实开播时间）')+'已填入固定时间检查，请确认重复方式和时间后保存。'
    formElement.value?.scrollIntoView({block:'nearest'})
  } catch(e) { toast(e.message,'error') }
}
let timer, disposed = false, inFlight = false
const active = computed(() => ['connecting','recording','stalled','finalizing','recovering'].includes(liveState(monitor.value)) || records.value.some(item => ['recording','finalizing'].includes(item.status)))
const labels = liveLabels
function time(value) { return value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '—' }
function size(value) { return ((Number(value)||0)/1024/1024).toFixed(1)+' MB' }
function duration(value) { const seconds=Math.max(0,Math.floor(Number(value)||0)); return `${Math.floor(seconds/3600)}:${String(Math.floor(seconds/60)%60).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}` }
async function load(initial = false) {
  if (inFlight) return
  inFlight = true
  try {
    const data = await api.liveStatus(props.sub.id, offset.value)
    if (disposed) return
    monitor.value = data.monitor; records.value = data.records; error.value = ''
    mergeErrors.value=data.merge_errors||[];merging.value=!!data.merging
    if (initial) { room.value=data.monitor?.room_url || ''; enabled.value=!!data.monitor?.enabled; interval.value=data.monitor?.interval_seconds || 60; source.value=data.monitor?.source || (data.monitor?.room_url?'room':'profile'); intervalEnabled.value=!!(data.monitor?.interval_enabled ?? true); calendarEnabled.value=!!data.monitor?.plan; draft.value=planDraft(data.monitor?.plan||{}); quality.value=data.monitor?.quality||'best' }
  } catch(e) { if(!disposed) error.value=e.message }
  finally { loading.value=false; inFlight=false }
}
async function save() {
  if(saving.value) return
  saving.value=true
  try { await api.saveLive(props.sub.id,{room_url:source.value==='room'?room.value.trim():'',source:source.value,quality:quality.value,enabled:enabled.value,interval_seconds:Number(interval.value),interval_enabled:intervalEnabled.value,plan:calendarEnabled.value?planPayload(draft.value):null}); await load(true); emit('updated'); toast('录播设置已保存','success') }
  catch(e) { toast(e.message,'error') }
  finally { saving.value=false }
}
async function stop() {
  if(saving.value) return
  saving.value=true
  try { await api.stopLive(props.sub.id); enabled.value=false; await load(); emit('updated'); toast('已关闭自动录播，正在保存已有视频和弹幕','info') }
  catch(e) { toast(e.message,'error') }
  finally { saving.value=false }
}
async function check() { try { const result=await api.checkLive(props.sub.id); toast(result.already_running?'检查已安排，请稍候':'已安排直播检查','info'); await load() } catch(e) { toast(e.message,'error') } }
async function publish(item) { saving.value=true; try { const result=await api.publishLive(item.id); toast(result.published?'已重新加入媒体库':'仍未完成入库，请查看错误说明',result.published?'success':'error'); await load() } catch(e){toast(e.message,'error')} finally{saving.value=false} }
async function reveal(item) { try { await api.libraryReveal(item.entry_id) } catch(e){toast(e.message,'error')} }
async function page(delta) { offset.value=Math.max(0,offset.value+delta*20); await load() }
onMounted(async()=>{await load(true);if(!disposed)timer=setInterval(()=>load(),5000)})
onBeforeUnmount(()=>{disposed=true;clearInterval(timer)})
</script>

<template>
  <section class="live-panel" aria-label="直播录制设置与记录">
    <div class="live-heading"><h3>直播与弹幕</h3><span class="live-state">{{ labels[liveState(monitor)] }}</span></div>
    <p class="live-help">独立于作品扫描与下载标签。开启后定期检查，下播或停止后保存视频和弹幕；同一博主同一天的片段自动无损合并，原件和中断记录保留。</p>
    <p v-if="loading" class="live-help" role="status">正在读取录播设置…</p>
    <form v-else ref="formElement" @submit.prevent="save">
      <label :for="`live-source-${sub.id}`">检查对象</label>
      <select :id="`live-source-${sub.id}`" v-model="source" class="select" :disabled="active"><option value="profile">跟随这位订阅博主（推荐）</option><option value="room">指定直播间链接</option></select>
      <p v-if="source==='profile'" class="live-help">自动从博主主页获取直播入口，核对身份后录制，无需填写直播间链接。</p>
      <label :for="`live-quality-${sub.id}`">录制画质</label><select :id="`live-quality-${sub.id}`" v-model="quality" class="select" :disabled="active"><option v-for="(label,key) in qualities" :key="key" :value="key">{{label}}</option></select>
      <p class="live-help">档位由直播源提供，分辨率和帧率沿用原流，不额外转码。指定档位不可用时尝试更低档位，实际使用的档位记入录播记录。</p>
      <template v-if="source==='room'"><label :for="`live-room-${sub.id}`">直播间链接</label><input :id="`live-room-${sub.id}`" v-model="room" class="input" type="url" placeholder="https://live.douyin.com/房间号" required :disabled="active" maxlength="100"></template>
      <div class="live-options">
        <label class="live-toggle"><input v-model="enabled" type="checkbox" :disabled="active">自动录制这位博主</label>
      </div>
      <details class="live-plan-menu" :open="planOpen" @toggle="planOpen=$event.target.open">
      <summary>检查计划<span>设置检查方式与执行时间</span></summary>
      <div class="live-options"><label><input v-model="intervalEnabled" type="checkbox" :disabled="active">按间隔重复</label><label><input v-model="calendarEnabled" type="checkbox" :disabled="active">在指定时间执行</label></div>
      <IntervalPicker v-if="intervalEnabled" v-model="interval" :disabled="active"/>
      <div v-if="calendarEnabled" class="live-calendar"><CalendarPlanFields v-model="draft" :disabled="active"/></div>
      <p v-if="planNotice" class="live-help" role="status">{{planNotice}}</p>
      <p class="live-help">间隔与固定时间可以同时启用；手动检查不推迟计划。固定时间仅在所选时点检查，可能错过两次检查之间的短直播。录制期间每分钟确认是否下播。</p>
      </details>
      <div class="live-actions">
        <button class="btn btn-primary btn-sm" :disabled="saving || active">{{ saving?'处理中…':'保存录播设置' }}</button>
        <button type="button" class="btn btn-ghost btn-sm" :disabled="!monitor?.enabled || saving" @click="check">立即检查</button>
        <button v-if="monitor?.enabled || active" type="button" class="btn btn-danger-ghost btn-sm" :disabled="saving" @click="stop">{{ active?'停止并保存':'关闭自动录播' }}</button>
      </div>
    </form>
    <p class="live-help">上次检查 {{ time(monitor?.checked_at) }} · 下次 {{ monitor?.enabled ? (monitor.next_check_at ? time(monitor.next_check_at) : '计划已结束') : '已关闭' }}（本地时间）</p>
    <p class="live-help">最多同时录制 2 位博主，剩余空间不足 2 GiB 时停止。关闭网页可继续；退出程序后停止检查。按北京时间的录制日期合并；编码或尺寸不兼容时保留片段并提示，不降低画质。</p>
    <p v-if="error || monitor?.error" class="live-error" role="alert">{{ error || monitor.error }}</p>
    <h4>录播记录</h4>
    <p v-if="merging" class="live-help" role="status">正在后台检查和合并每日录播，校验通过后更新媒体库与任务记录。</p>
    <p v-for="failure in mergeErrors" :key="failure.day" class="live-error">{{failure.day}}：{{failure.error}}</p>
    <p v-if="!records.length" class="live-help">尚无录播。弹幕保存为 JSONL，仅保留文字、Emoji 和时间，不保存发送者身份。以视频播放位置估算时间轴；采集缺口另存记录，不自动删除。</p>
    <article v-for="item in records" :key="item.id" class="live-record">
      <strong>{{ item.title }}</strong>
      <p>{{ labels[item.status] || item.status }} · {{ time(item.started_at) }} · {{ duration(item.duration_seconds) }} · {{ size(item.bytes) }}</p>
      <p v-if="item.quality">实际画质：{{qualities[item.quality] || item.quality}}</p>
      <ActivityProgress v-if="['recording','finalizing'].includes(item.status)" status="running" :progress="{label:item.status==='finalizing'?'正在保存视频与弹幕…':`已录制 ${duration(item.duration_seconds)} · ${size(item.bytes)}`}" label="直播录制进度"/>
      <p v-if="item.chat">弹幕 {{ item.chat.messages || 0 }} 条 · {{ item.chat.status==='captured'?'采集已结束':item.chat.status==='connected'?'采集中':item.chat.status==='captured_with_gaps'?'存在采集缺口':item.chat.status==='write_failed'?'写入失败，已有数据保留':['interrupted','unavailable'].includes(item.chat.status)?'采集未正常结束，已有数据保留':'正在连接或恢复' }}</p>
      <p v-if="item.previous_id">这是同场直播中断后的新记录，前一记录保留。</p>
      <p v-if="item.error" class="live-error">{{ item.error }}</p>
      <details v-if="item.parts?.length" class="live-parts"><summary>查看 {{item.parts.length}} 次录制与中断记录</summary><div v-for="part in item.parts" :key="part.id"><p>{{time(part.started_at)}} · {{labels[part.status]}} · {{duration(part.duration_seconds)}} · 弹幕 {{part.chat_messages}} 条</p><p v-if="part.error" class="live-error">{{part.error}}</p><RouterLink class="btn btn-ghost btn-sm" :to="{path:'/tasks',query:{task:'live-'+part.id}}">原任务记录</RouterLink></div></details>
      <div class="live-actions">
        <button class="btn btn-ghost btn-sm" :disabled="active || saving" @click="addTime(item)">{{item.broadcast_started_at?'将开播时间加入检查计划':'将录制时间加入检查计划'}}</button>
        <RouterLink v-if="item.entry_id" class="btn btn-ghost btn-sm" :to="{path:'/library',query:{entry_id:item.entry_id}}">查看媒体库</RouterLink>
        <button v-if="item.entry_id" class="btn btn-ghost btn-sm" @click="reveal(item)"><AppIcon name="folder" :size="18"/>视频与弹幕文件夹</button>
        <button v-if="item.can_publish" class="btn btn-ghost btn-sm" :disabled="saving || active" @click="publish(item)">重试入库</button>
      </div>
    </article>
    <div v-if="offset || records.length===20" class="live-actions live-help"><button class="btn btn-ghost btn-sm" :disabled="offset===0 || inFlight" @click="page(-1)">上一页</button><span>第 {{ offset/20+1 }} 页</span><button class="btn btn-ghost btn-sm" :disabled="records.length<20 || inFlight" @click="page(1)">下一页</button></div>
  </section>
</template>

<style scoped>
.live-plan-menu { margin:14px 0;padding:12px;border:1px solid var(--border);border-radius:12px; }.live-plan-menu summary { cursor:pointer;font-size:13px;line-height:1.8; }.live-plan-menu summary span { color:var(--text-2);font-size:11px;margin-left:10px; }
.live-calendar { margin:14px 0;padding:14px;background:#eef3f5;border-radius:12px; }
.live-panel { margin-top:18px;padding-top:18px;border-top:1px solid var(--border); }
.live-heading,.live-options,.live-actions { display:flex;align-items:center;gap:12px;flex-wrap:wrap; }
.live-heading { justify-content:space-between; }.live-heading h3 { font-size:15px; }.live-state { color:var(--blue);font-size:12px; }
.live-help,.live-record p { font-size:12px;line-height:1.8;color:var(--text-2);overflow-wrap:anywhere; }.live-help { margin:10px 0; }
.live-panel label { font-size:13px; }.live-panel form>label { display:block;margin:12px 0 8px; }.live-options { margin:14px 0;justify-content:space-between; }.live-options label { display:flex;align-items:center;gap:8px; }.interval { width:85px; }.live-toggle input { accent-color:var(--blue); }
.live-panel h4 { margin-top:22px;font-size:13px; }.live-record { padding:14px 0;border-bottom:1px solid var(--border); }.live-record strong { font-size:13px;overflow-wrap:anywhere; }.live-record p { margin:6px 0; }.live-error { color:#b13c30!important;font-size:12px;line-height:1.7;overflow-wrap:anywhere; }.live-actions a { text-decoration:none; }
</style>
