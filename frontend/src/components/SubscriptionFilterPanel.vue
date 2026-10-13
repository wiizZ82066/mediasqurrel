<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
import { toast } from '../store.js'
import { parseDownloadTags } from '../subscription-filter.js'

const props = defineProps({ sub: { type: Object, required: true } })
const emit = defineEmits(['updated'])
const tags = ref((props.sub.download_filter?.tags || []).join('\n'))
const saving = ref(false), loading = ref(false), error = ref(''), records = ref([]), offset = ref(0)
const states = { baseline: '首次基线', legacy_seen: '旧已见记录', discovered: '待入队', dispatching: '正在入队', queued: '已入队', running: '下载中', downloaded: '下载成功', failed: '下载失败，待重试', interrupted: '下载中断，待重试', cancelled: '已取消', filtered: '未命中标签，已跳过', pending_filter: '待完整正文' }
function time(value) { return value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'short', hour12: false }).format(new Date(value)) : '' }
async function load() {
  loading.value = true; error.value = ''
  try { records.value = await api.subItems(props.sub.id, offset.value) }
  catch (e) { error.value = e.message }
  finally { loading.value = false }
}
async function page(delta) { offset.value = Math.max(0, offset.value + delta * 20); await load() }
async function save() {
  saving.value = true
  try {
    await api.updateSub(props.sub.id, { download_filter: parseDownloadTags(tags.value) })
    emit('updated'); toast('下载标签已保存，下次发现新内容时生效', 'success')
  } catch (e) { toast(e.message, 'error') }
  finally { saving.value = false }
}
onMounted(load)
</script>

<template>
  <section class="subscription-filter-panel">
    <form @submit.prevent="save">
      <label :for="`download-tags-${sub.id}`">只下载带指定标签的新内容</label>
      <textarea :id="`download-tags-${sub.id}`" v-model="tags" class="textarea" rows="3" maxlength="1700" placeholder="例如：旅行、美食；每行一个或用逗号分隔。留空下载全部。"></textarea>
      <p class="filter-help">命中任一标签即可。识别 #标签、#标签# 和平台话题，按完整标签匹配，最多 20 个。匹配微博正文或抖音文案，不是只看标题前 60 字。</p>
      <p class="filter-help">修改后用于新发现内容及失败任务的自动重试；已入队任务继续执行。之前跳过的内容不会自动补下载，首次基线与手动下载不受影响。</p>
      <button class="btn btn-primary btn-sm" :disabled="saving">{{ saving ? '保存中…' : '保存下载标签' }}</button>
    </form>
    <details class="filter-records">
      <summary>内容发现与下载决策记录</summary>
      <p class="filter-help">正文不完整且未命中时保留“待完整正文”，取得完整正文后再判断；不会假装已经下载。标签规则变更后的新决定会显示在此。</p>
      <button class="text-button" :disabled="loading" @click="load">刷新记录</button>
      <p v-if="loading" class="filter-help" role="status">正在读取记录…</p>
      <p v-else-if="error" class="filter-error" role="alert">{{ error }}</p>
      <p v-else-if="!records.length" class="filter-help">暂无内容记录，首次扫描后显示。</p>
      <article v-for="item in records" :key="item.item_id" class="filter-record">
        <strong>{{ item.title || item.item_id }}</strong>
        <p>{{ states[item.status] || item.status }} · {{ time(item.updated_at) }}</p>
        <p v-if="item.filter_decision.reason">{{ item.filter_decision.reason }}<span v-if="item.filter_decision.tags?.length"> · 当时规则：{{ item.filter_decision.tags.map(tag => '#' + tag).join('、') }}</span><span v-if="item.filter_decision.matched?.length"> · 命中：{{ item.filter_decision.matched.map(tag => '#' + tag).join('、') }}</span></p>
        <p v-if="item.last_error" class="filter-error">{{ item.last_error }}</p>
      </article>
      <div class="filter-pages"><button class="text-button" :disabled="loading || offset===0" @click="page(-1)">上一页</button><span>第 {{ offset/20+1 }} 页</span><button class="text-button" :disabled="loading || records.length<20" @click="page(1)">下一页</button></div>
    </details>
  </section>
</template>

<style scoped>
.subscription-filter-panel { margin-top:18px; border-top:1px solid var(--border); padding-top:18px; }
.subscription-filter-panel label { display:block; font-size:13px; margin-bottom:8px; }
.subscription-filter-panel textarea { width:100%; resize:vertical; min-height:80px; font-size:13px; }
.filter-help,.filter-record { color:var(--text-2); font-size:12px; line-height:1.75; overflow-wrap:anywhere; }
.filter-help { margin:10px 0; }.filter-records { margin-top:18px; font-size:13px; }.filter-records summary { cursor:pointer; }
.filter-record { padding:12px 0; border-bottom:1px solid var(--border); }.filter-record strong { color:var(--text-1); font-weight:500; }
.filter-error { color:#b53930; }.text-button { border:0; background:transparent; color:var(--blue); cursor:pointer; padding:4px 0; font-size:12px; }.text-button:disabled { opacity:.4; cursor:default; }.filter-pages { display:flex; gap:12px; align-items:center; font-size:12px; margin-top:10px; }
</style>
