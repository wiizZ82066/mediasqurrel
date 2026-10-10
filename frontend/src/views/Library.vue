<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api.js'
import { store, toast, onTaskEvent } from '../store.js'
import AppIcon from '../components/AppIcon.vue'
import PlatformLogo from '../components/PlatformLogo.vue'
import ActivityProgress from '../components/ActivityProgress.vue'
import MediaThumbnail from '../components/MediaThumbnail.vue'
import ContentText from '../components/ContentText.vue'
const route=useRoute(), router=useRouter(), saved=store.libraryState || {}
const roots=ref([]), rootId=ref(saved.rootId || ''), entries=ref(saved.entries || []), total=ref(saved.total || 0), page=ref(saved.page || 1)
const viewMode=ref(saved.viewMode || 'timeline'), author=ref(saved.author || ''), query=ref(saved.query || ''), platform=ref(saved.platform || ''), mediaType=ref(saved.mediaType || ''), status=ref(saved.status || ''), sort=ref(saved.sort || 'desc')
const dateFrom=ref(saved.dateFrom || ''), dateTo=ref(saved.dateTo || ''), groupBy=ref(saved.groupBy || 'month'), dates=ref([])
const authors=ref([]), authorQuery=ref(''), authorPage=ref(1), authorTotal=ref(0)
const loading=ref(false), error=ref(''), scan=ref(null), scanBusy=ref(false), changed=ref(false)
const gallery=ref(null), galleryIndex=ref(0), detailBusy=ref(false), detailError=ref(''), livePlaying=ref(false), mediaError=ref('')
const locateBusy=ref(false), locateMessage=ref(''), detailNotice=ref('')
let locateController=null, locateSequence=0
const viewerEl=ref(null), closeButton=ref(null), liveVideo=ref(null), filtersOpen=ref(false), cardLiveId=ref(null), cardLiveVideo=ref(null)
let livePlaybackSequence=0, cardLiveSequence=0
const pageSize=60
let ready=false, disposed=false, listSequence=0, detailSequence=0, auxSequence=0, listController=null, detailController=null, auxController=null, debounceTimer=null, authorTimer=null, scanTimer=null, contentEl=null, restoreFocus=null, preloads=[]
const maxPage=computed(()=>Math.max(1,Math.ceil(total.value/pageSize)))
const activeScan=computed(()=>['queued','running'].includes(scan.value?.status))
const currentItem=computed(()=>gallery.value?.gallery?.[galleryIndex.value] || null)
const visibleThumbnails=computed(()=>{const items=gallery.value?.gallery || []; const start=Math.max(0,Math.min(galleryIndex.value-4,items.length-9));return items.slice(start,start+9).map((item,i)=>({...item,index:start+i}))})
const selectedRoot=computed(()=>roots.value.find(root=>root.id===rootId.value))
const unsubscribe=onTaskEvent(message=>{
  if(message.type==='library_cover'){const update=message.entry;for(const item of [...entries.value,...(gallery.value?[gallery.value]:[])])if(item.id===update.id && (!item.signature || !update.cover_signature || item.signature===update.cover_signature))Object.assign(item,update)}
  if(ready && route.query.task_id && !gallery.value && !locateBusy.value && !detailBusy.value && (message.type==='reconnect' || (message.type==='library_indexed' && message.task_id===String(route.query.task_id))))locateRoute()
})
const datesLabel={published:'发布时间',directory:'目录日期',downloaded:'下载时间',imported:'导入时间'}
const filterSummary=computed(()=>{
  const parts=[selectedRoot.value?.label || '媒体目录']
  if(query.value.trim())parts.push('搜索：'+query.value.trim())
  if(viewMode.value==='author' && author.value)parts.push('作者：'+author.value)
  if(platform.value)parts.push({weibo:'微博',douyin:'抖音'}[platform.value])
  if(mediaType.value)parts.push({image:'图片',video:'视频',live:'Live 图',text:'文字'}[mediaType.value])
  if(status.value)parts.push({complete:'已验证完整',legacy:'旧格式存档',partial:'未完成',missing:'文件缺失'}[status.value])
  if(dateFrom.value || dateTo.value)parts.push((dateFrom.value || '最早')+' 至 '+(dateTo.value || '最新'))
  if(sort.value==='asc')parts.push('从旧到新')
  return parts.join(' · ')
})
function dayOf(value) { if(!value) return '日期未知'; const date=new Date(value); return Number.isNaN(date.getTime())?String(value):new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(date) }
function displayTime(value) { if(!value) return ''; const date=new Date(value);return Number.isNaN(date.getTime())?value:new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(date) }
const entryGroups=computed(()=>{ if(viewMode.value==='author') return [{date:author.value || '全部作者',items:entries.value}];const groups=new Map();for(const entry of entries.value){const day=dayOf(entry.sort_at);const key=groupBy.value==='year'?day.slice(0,4):groupBy.value==='month'?day.slice(0,7):day;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(entry)}return [...groups].map(([date,items])=>({date,items})) })
function mediaUrl(entry,rel) { return `/media/roots/${encodeURIComponent(entry.root_id)}/${[entry.rel_dir,rel].filter(Boolean).join('/').split('/').map(encodeURIComponent).join('/')}` }
function thumbUrl(entry,rel,width=480) { return '/api/thumb?'+new URLSearchParams({root_id:entry.root_id,p:[entry.rel_dir,rel].filter(Boolean).join('/'),w:width}) }
function coverStyle(entry) { return entry.cover_face?{objectPosition:`${entry.cover_face.x*100}% ${entry.cover_face.y*100}%`}:{} }
function remember() { store.libraryState={rootId:rootId.value,entries:entries.value,total:total.value,page:page.value,viewMode:viewMode.value,author:author.value,query:query.value,platform:platform.value,mediaType:mediaType.value,status:status.value,sort:sort.value,dateFrom:dateFrom.value,dateTo:dateTo.value,groupBy:groupBy.value,scrollTop:contentEl?.scrollTop || 0} }
function filters() { return {root_id:rootId.value,author:viewMode.value==='author'?author.value:'',q:query.value.trim(),platform:platform.value,media_type:mediaType.value,status:status.value,sort:sort.value,date_from:dateFrom.value,date_to:dateTo.value} }
async function loadEntries({reset=false,restore=false}={}) {
  if(!rootId.value) return
  listController?.abort();listController=new AbortController();const sequence=++listSequence
  if(reset)page.value=1
  loading.value=true;error.value=''
  try {
    if(dateFrom.value && dateTo.value && dateFrom.value>dateTo.value)throw new Error('开始日期不能晚于结束日期。')
    const result=await api.libraryEntries({...filters(),page:page.value,page_size:pageSize,signal:listController.signal})
    if(disposed || sequence!==listSequence)return
    stopCardLive();entries.value=result.items;total.value=result.total;changed.value=false
    if(page.value>maxPage.value){page.value=maxPage.value;return loadEntries()}
    await nextTick(); if(contentEl)contentEl.scrollTop=restore?(saved.scrollTop || 0):0
    remember()
  }catch(e){if(sequence===listSequence && e.name!=='AbortError')error.value=e.message}
  finally{if(sequence===listSequence)loading.value=false}
}
async function loadAuxiliary() {
  if(!rootId.value || !filtersOpen.value)return
  auxController?.abort();auxController=new AbortController();const sequence=++auxSequence
  const shared={root_id:rootId.value,signal:auxController.signal}
  const result=await Promise.allSettled([api.libraryAuthors({...shared,q:authorQuery.value,page:authorPage.value,page_size:30}),api.libraryDates({...shared,author:viewMode.value==='author'?author.value:'',q:query.value.trim(),group:groupBy.value,limit:240})])
  if(disposed || sequence!==auxSequence)return
  if(result[0].status==='fulfilled'){authors.value=result[0].value.items;authorTotal.value=result[0].value.total}
  if(result[1].status==='fulfilled')dates.value=result[1].value.items
}
watch([rootId,viewMode,author,query,platform,mediaType,status,sort,dateFrom,dateTo],()=>{if(!ready)return;clearTimeout(debounceTimer);listController?.abort();++listSequence;loading.value=true;debounceTimer=setTimeout(()=>{loadEntries({reset:true});loadAuxiliary()},300)})
watch(filtersOpen,open=>{if(open && ready)loadAuxiliary();else if(!open){clearTimeout(authorTimer);auxController?.abort();++auxSequence}})
watch(authorQuery,()=>{authorPage.value=1})
watch(rootId,()=>{if(ready){scan.value=null;pollScan()}})
watch(groupBy,()=>{if(ready)loadAuxiliary()})
watch([authorQuery,authorPage],()=>{if(!ready)return;clearTimeout(authorTimer);authorTimer=setTimeout(loadAuxiliary,300)})
function changePage(delta) { const next=page.value+delta;if(next<1 || next>maxPage.value || loading.value)return;page.value=next;loadEntries() }
function navigateDate(event) {const value=event.target.value;if(!value){dateFrom.value='';dateTo.value='';return}const [y,m,d]=value.split('-').map(Number);dateFrom.value=`${y}-${String(m||1).padStart(2,'0')}-${String(d||1).padStart(2,'0')}`;const last=new Date(Date.UTC(y,m || 12,0));dateTo.value=d?dateFrom.value:`${y}-${String(m||12).padStart(2,'0')}-${m?last.getUTCDate():31}`}
async function pollScan() {
  try {const result=await api.libraryScans();if(disposed)return;const old=scan.value;scan.value=result.items?.find(item=>item.root_id===rootId.value) || null;if(old && ['running','queued'].includes(old.status) && !activeScan.value){await loadEntries();loadAuxiliary();toast(scan.value.status==='success'?'媒体索引已更新':'索引扫描已停止，请查看结果',scan.value.status==='success'?'success':'info')}}catch{ /* list remains available while polling retries */ }
}
async function startScan() {scanBusy.value=true;try{await api.libraryScan(rootId.value);await pollScan();toast('已开始建立索引，原文件保持原位','info')}catch(e){toast(e.message,'error')}finally{scanBusy.value=false}}
async function cancelScan(){try{await api.cancelLibraryScan(scan.value.id);await pollScan()}catch(e){toast(e.message,'error')}}
async function openEntry(id) {
  cancelLocate()
  stopCardLive()
  detailController?.abort();detailController=new AbortController();const sequence=++detailSequence;detailBusy.value=true;detailError.value='';detailNotice.value='';restoreFocus=document.activeElement
  try{const result=await api.libraryEntry(id,detailController.signal);if(disposed || sequence!==detailSequence)return;stopLive();gallery.value=result;document.querySelector('.layout')?.setAttribute('inert','');galleryIndex.value=0;livePlaying.value=false;mediaError.value='';await nextTick();closeButton.value?.focus();preloadNeighbors()}
  catch(e){if(sequence===detailSequence && e.name!=='AbortError')detailError.value=e.message}
  finally{if(sequence===detailSequence)detailBusy.value=false}
}
function clearPreloads(){for(const image of preloads)image.src='';preloads=[]}
function preloadNeighbors(){clearPreloads();const entry=gallery.value;if(!entry)return;for(const index of [galleryIndex.value-1,galleryIndex.value+1]){const item=entry.gallery[index];const rel=item?.type==='image'?item.rel:item?.poster;if(rel){const image=new Image();image.src=mediaUrl(entry,rel);preloads.push(image)}}}
function stopCardLive(id=null){
  if(id!==null && id!==cardLiveId.value)return
  ++cardLiveSequence
  const video=cardLiveVideo.value
  if(video){video.pause();try{video.currentTime=0}catch{}}
  cardLiveVideo.value=null;cardLiveId.value=null
}
async function hoverCardLive(entry,event){
  if(event.pointerType!=='mouse' || !window.matchMedia('(hover: hover)').matches || entry.availability==='missing' || !entry.cover_live_rel || cardLiveId.value===entry.id)return
  stopCardLive()
  const sequence=++cardLiveSequence
  cardLiveId.value=entry.id
  await nextTick()
  const video=cardLiveVideo.value
  if(!video || sequence!==cardLiveSequence)return
  video.muted=true
  try{await video.play()}catch{if(sequence===cardLiveSequence)stopCardLive(entry.id)}
}
function setCardVideo(element,id){if(id===cardLiveId.value)cardLiveVideo.value=element}
function cardLiveError(event){if(event.target===cardLiveVideo.value)stopCardLive()}
function stopLive(){
  ++livePlaybackSequence
  const video=liveVideo.value
  if(video){video.pause();try{video.currentTime=0}catch{}}
  livePlaying.value=false
}
async function startLive(){
  if(!currentItem.value?.live || livePlaying.value)return
  const sequence=++livePlaybackSequence
  livePlaying.value=true;mediaError.value=''
  await nextTick()
  const video=liveVideo.value
  if(!video || sequence!==livePlaybackSequence)return
  video.muted=true
  try{await video.play()}catch(error){if(sequence===livePlaybackSequence && error.name!=='AbortError'){stopLive();mediaError.value='Live 图暂时无法播放，可再次点击播放或检查浏览器格式支持。'}}
}
function hoverLive(event){if(event.pointerType==='mouse' && window.matchMedia('(hover: hover)').matches)startLive()}
function leaveLive(event){if(event.pointerType==='mouse')stopLive()}
function toggleLive(){if(livePlaying.value)stopLive();else startLive()}
function liveError(event){if(event.target!==liveVideo.value)return;stopLive();mediaError.value='Live 图无法读取或浏览器不支持此格式，请检查原文件。'}
function navigateMedia(delta){const next=galleryIndex.value+delta;if(next<0 || next>=(gallery.value?.gallery?.length || 0))return;galleryIndex.value=next}
watch(galleryIndex,()=>{stopLive();mediaError.value='';preloadNeighbors()},{flush:'sync'})
function closeGallery(){cancelLocate();++detailSequence;detailController?.abort();detailBusy.value=false;stopLive();document.querySelector('.layout')?.removeAttribute('inert');gallery.value=null;clearPreloads();restoreFocus?.focus?.({preventScroll:true});if(route.query.entry_id || route.query.task_id || route.query.entry)router.replace({path:'/library',query:{}})}
function onKey(event){if(!gallery.value)return;if(event.key==='Escape'){event.preventDefault();closeGallery()}else if(event.key==='ArrowRight'){event.preventDefault();navigateMedia(1)}else if(event.key==='ArrowLeft'){event.preventDefault();navigateMedia(-1)}else if(event.key==='Tab'){const controls=[...viewerEl.value.querySelectorAll('button:not(:disabled),[href],video[controls],summary,[tabindex]:not([tabindex="-1"])')];const first=controls[0],last=controls.at(-1);if(event.shiftKey && document.activeElement===first){event.preventDefault();last?.focus()}else if(!event.shiftKey && document.activeElement===last){event.preventDefault();first?.focus()}}}
function cancelLocate(){++locateSequence;locateController?.abort();locateController=null;locateBusy.value=false;locateMessage.value=''}
async function locateRoute(){
  cancelLocate();++detailSequence;detailController?.abort();detailBusy.value=false;detailError.value='';detailNotice.value=''
  const target={...route.query}
  if(target.entry_id)return openEntry(String(target.entry_id))
  const params=target.task_id?{task_id:String(target.task_id)}:target.author && target.entry?{rel_dir:String(target.author)+'/'+String(target.entry),root_id:rootId.value}:null
  if(!params || disposed)return
  const controller=new AbortController();locateController=controller
  const sequence=locateSequence, path=route.fullPath
  const active=()=>!disposed && sequence===locateSequence && route.fullPath===path
  locateBusy.value=true;locateMessage.value='正在定位媒体内容…'
  try{
    const located=await api.libraryLocate(params,controller.signal,{onIndexing:message=>{if(active())locateMessage.value=message}})
    if(active())await openEntry(located.entry_id)
  }catch(e){if(active() && e.name!=='AbortError'){if(e.code==='LIBRARY_INDEXING')detailNotice.value=e.message;else detailError.value='无法定位该内容：'+e.message}}
  finally{if(active()){locateBusy.value=false;locateMessage.value='';locateController=null}}
}
watch(()=>route.fullPath,()=>{if(ready)locateRoute()},{flush:'sync'})
onMounted(async()=>{contentEl=document.querySelector('.content');window.addEventListener('keydown',onKey);try{const result=await api.libraryRoots();roots.value=result.items;if(!roots.value.some(item=>item.id===rootId.value))rootId.value=result.default_root_id || roots.value[0]?.id || '';await loadEntries({restore:!!saved.entries});await loadAuxiliary();ready=true;await locateRoute();if(disposed)return;await pollScan();if(disposed)return;scanTimer=setInterval(()=>{if(activeScan.value)pollScan()},2000)}catch(e){error.value=e.message;ready=true}})
onBeforeUnmount(()=>{stopCardLive();stopLive();unsubscribe();document.querySelector('.layout')?.removeAttribute('inert');remember();disposed=true;cancelLocate();++detailSequence;listController?.abort();detailController?.abort();auxController?.abort();clearTimeout(debounceTimer);clearTimeout(authorTimer);clearInterval(scanTimer);clearPreloads();window.removeEventListener('keydown',onKey)})
</script>
<template>
  <div class="library-view">
    <div class="page-heading"><div><h1 class="page-title">媒体库</h1><p class="page-sub">按作者或日期浏览本地内容，原文件保留在媒体目录。</p></div><button class="btn btn-ghost" :disabled="!rootId || activeScan || scanBusy" @click="startScan"><AppIcon name="scan"/>{{ activeScan?'正在建立索引':'更新索引' }}</button></div>
    <div v-if="roots.length" class="card library-toolbar">
      <div class="toolbar-row compact-toolbar"><div class="view-switch" role="group" aria-label="媒体库视图"><button :class="{selected:viewMode==='timeline'}" :aria-pressed="viewMode==='timeline'" @click="viewMode='timeline'"><AppIcon name="calendar" :size="19"/>时间线</button><button :class="{selected:viewMode==='author'}" :aria-pressed="viewMode==='author'" @click="viewMode='author'"><AppIcon name="person" :size="19"/>作者</button></div><button class="btn btn-ghost btn-sm filter-toggle" :aria-expanded="filtersOpen" aria-controls="library-filters" @click="filtersOpen=!filtersOpen">{{ filtersOpen?'收起搜索与筛选':'搜索与筛选' }}</button></div><p class="active-filter-summary" aria-live="polite">{{ filterSummary }}</p>
      <div id="library-filters" v-show="filtersOpen"><label class="search-input library-search"><span class="sr-only">搜索媒体库</span><input v-model="query" class="input" type="search" placeholder="搜索作者、标题、正文或日期"></label>
      <div class="filters-row"><label>媒体目录<select v-model="rootId" class="select"><option v-for="root in roots" :key="root.id" :value="root.id">{{ root.label }}</option></select></label><label>平台<select v-model="platform" class="select"><option value="">全部平台</option><option value="weibo">微博</option><option value="douyin">抖音</option></select></label><label>内容<select v-model="mediaType" class="select"><option value="">全部类型</option><option value="image">图片</option><option value="video">视频</option><option value="live">Live 图</option><option value="text">文字</option></select></label><label>状态<select v-model="status" class="select"><option value="">全部可用</option><option value="complete">已验证完整</option><option value="legacy">旧格式存档</option><option value="partial">未完成</option><option value="missing">文件缺失</option></select></label><label>排序<select v-model="sort" class="select"><option value="desc">从新到旧</option><option value="asc">从旧到新</option></select></label></div>
      <div class="date-row"><label>从<input v-model="dateFrom" type="date" class="input"></label><label>至<input v-model="dateTo" type="date" class="input"></label><label>分组<select v-model="groupBy" class="select"><option value="year">年</option><option value="month">月</option><option value="day">日</option></select></label><label class="date-navigation">日期导航<select class="select" aria-label="跳转日期范围" @change="navigateDate"><option value="">全部日期</option><option v-for="item in dates" :key="item.date" :value="item.date">{{ item.date }} · {{ item.count }} 条</option></select></label></div>
      <p class="toolbar-hint">按发布时间排序；缺失时依次使用目录日期、下载时间、导入时间。日期按 Asia/Shanghai 显示，导航列出最近 240 组。</p></div>
    </div>
    <div v-if="viewMode==='author' && roots.length" v-show="filtersOpen" class="author-panel"><label><span class="sr-only">筛选作者</span><input v-model="authorQuery" class="input" type="search" placeholder="筛选作者昵称"></label><div class="author-chips"><button :class="{selected:!author}" @click="author=''">全部作者</button><button v-for="item in authors" :key="item.name" :class="{selected:author===item.name}" @click="author=item.name">{{ item.name }} <span>{{ item.count }}</span></button></div><div v-if="authorTotal>30" class="author-pager"><button class="text-button" :disabled="authorPage===1" @click="authorPage--">上一组作者</button><span>{{ authorPage }} / {{ Math.ceil(authorTotal/30) }}</span><button class="text-button" :disabled="authorPage*30>=authorTotal" @click="authorPage++">下一组作者</button></div></div>
    <div v-if="scan" class="scan-note"><div><AppIcon name="scan" :size="20"/>索引扫描：{{ {running:'进行中',queued:'排队中',success:'完成',failed:'失败',cancelled:'已取消',interrupted:'已中断'}[scan.status] || scan.status }} · 已检查 {{ scan.processed || 0 }} 个目录，更新 {{ scan.changed || 0 }} 条<span v-if="scan.error" class="error-text"> · {{ scan.error }}</span><button v-if="activeScan" class="text-button" @click="cancelScan">取消</button></div><ActivityProgress v-if="activeScan" :status="scan.status" :progress="{completed:scan.processed,total:scan.total,unit:'个目录',label:'后台建立媒体索引'}" label="媒体索引进度"/></div>
    <p v-if="locateBusy" class="list-note" role="status">{{ locateMessage }}</p><p v-if="detailNotice" class="list-note" role="status">{{ detailNotice }} <button class="text-button" @click="locateRoute">继续查看</button></p>
    <p v-if="detailBusy" class="list-note" role="status">正在读取内容详情…</p><p v-if="detailError" class="error-text" role="alert">{{ detailError }}</p>
    <div v-if="error" class="card error-text" role="alert">{{ error }} <button class="text-button" @click="loadEntries()">重新加载</button></div>
    <p v-if="loading" class="list-note" role="status">正在读取媒体索引…</p>
    <div v-if="!loading && !error && !entries.length" class="card library-empty"><AppIcon name="library" :size="72"/><h2>{{ query || author || dateFrom || dateTo || status || platform || mediaType?'没有符合条件的内容':'这里还没有媒体内容' }}</h2><p>已有媒体文件夹可在设置中只建立索引；文件不会复制或移动。</p><p v-if="selectedRoot" class="root-path">当前目录：{{ selectedRoot.path }}</p><div><router-link to="/settings" class="btn btn-ghost">媒体目录与导入</router-link><router-link to="/" class="btn btn-primary">下载内容</router-link></div></div>
    <template v-for="group in entryGroups" :key="group.date"><section v-if="group.items.length" class="entry-section"><h2>{{ group.date }}<span>{{ group.items.length }} 条 / 本页</span></h2><div class="entry-grid"><button v-for="entry in group.items" :key="entry.id" class="entry-card" @click="openEntry(entry.id)"><div class="entry-cover" @pointerenter="hoverCardLive(entry,$event)" @pointerleave="stopCardLive(entry.id)"><MediaThumbnail v-if="entry.cover && entry.availability!=='missing'" :src="thumbUrl(entry,entry.cover)" :position="coverStyle(entry)"/><AppIcon v-else :name="entry.availability==='missing'?'folder':'library'" :size="54"/><video v-if="cardLiveId===entry.id && entry.cover_live_rel && entry.availability!=='missing'" :ref="element=>setCardVideo(element,entry.id)" class="card-live-video" :src="mediaUrl(entry,entry.cover_live_rel)" :style="coverStyle(entry)" muted loop playsinline preload="none" aria-hidden="true" @error="cardLiveError"></video><span v-if="entry.counts?.videos || entry.counts?.lives" class="media-kind" role="img" :title="entry.counts?.lives?'Live 图：'+entry.counts.lives+' 项':'视频：'+entry.counts.videos+' 项'" :aria-label="entry.counts?.lives?'Live 图：'+entry.counts.lives+' 项':'视频：'+entry.counts.videos+' 项'"><AppIcon :name="entry.counts?.lives?'live':'video'" :size="22"/></span><span v-if="entry.archive_status==='partial' || entry.availability==='missing'" class="archive-badge">{{ entry.availability==='missing'?'文件缺失':'未完成' }}</span></div><div class="entry-caption"><div class="entry-author"><PlatformLogo v-if="entry.platform" :platform="entry.platform" :size="19"/><strong>{{ entry.author }}</strong></div><p class="entry-title"><ContentText :text="entry.text_preview || entry.date_dir || '文字内容'" :platform="entry.platform"/></p><p class="entry-date">{{ dayOf(entry.sort_at) }} · {{ datesLabel[entry.date_source] || '日期' }}</p><p class="entry-count">{{ entry.counts?.photos || 0 }} 图片 · {{ entry.counts?.videos || 0 }} 视频 · {{ entry.counts?.lives || 0 }} Live</p></div></button></div></section></template>
    <nav v-if="total" class="pagination" aria-label="媒体分页"><button class="btn btn-ghost" :disabled="loading || page===1" @click="changePage(-1)">上一页</button><span>第 {{ page }} / {{ maxPage }} 页 · 共 {{ total }} 条</span><button class="btn btn-ghost" :disabled="loading || page>=maxPage" @click="changePage(1)">下一页</button></nav>
    <Teleport to="body"><div v-if="gallery" class="gallery-mask" @click.self="closeGallery"><section ref="viewerEl" class="gallery-panel" role="dialog" aria-modal="true" aria-label="媒体查看器"><header class="gallery-head"><div><strong>{{ gallery.author }}</strong><p>{{ displayTime(gallery.sort_at) }} · {{ datesLabel[gallery.date_source] }}</p></div><span>{{ gallery.gallery?.length ? galleryIndex+1 : 0 }} / {{ gallery.gallery?.length || 0 }}</span><button ref="closeButton" class="viewer-button" aria-label="关闭查看器" @click="closeGallery"><AppIcon name="close"/></button></header><div class="gallery-stage"><button class="viewer-button gallery-prev" :disabled="galleryIndex===0" aria-label="上一项" @click="navigateMedia(-1)"><AppIcon name="chevron-left" :size="24"/></button><div v-if="currentItem" class="gallery-media" @pointerenter="hoverLive" @pointerleave="leaveLive"><template v-if="currentItem.live"><video v-if="livePlaying" ref="liveVideo" :src="mediaUrl(gallery,currentItem.rel)" :poster="currentItem.poster?mediaUrl(gallery,currentItem.poster):undefined" controls muted loop playsinline preload="metadata" @error="liveError"></video><img v-else-if="currentItem.poster" :src="mediaUrl(gallery,currentItem.poster)" alt="Live 图静态画面，鼠标悬停可静音播放" @error="mediaError='静态画面无法读取，可尝试播放 Live 图。'"><button class="live-play" :aria-pressed="livePlaying" @click="toggleLive"><AppIcon name="play"/>{{ livePlaying?'停止 Live 图':'播放 Live 图' }}</button></template><video v-else-if="currentItem.type==='video'" :src="mediaUrl(gallery,currentItem.rel)" :poster="currentItem.poster?mediaUrl(gallery,currentItem.poster):undefined" controls playsinline preload="metadata" @error="mediaError='视频无法读取或浏览器不支持此格式，请检查原文件。'"></video><img v-else :src="mediaUrl(gallery,currentItem.rel)" alt="媒体原图" @error="mediaError='图片无法读取，请检查文件是否移动或删除。'"><p v-if="mediaError" class="media-error" role="alert">{{ mediaError }}</p></div><div v-else class="text-only"><AppIcon name="library" :size="64"/><p>这是一条文字存档</p></div><button class="viewer-button gallery-next" :disabled="galleryIndex>=(gallery.gallery?.length || 0)-1" aria-label="下一项" @click="navigateMedia(1)"><AppIcon name="chevron-right" :size="24"/></button></div><div v-if="visibleThumbnails.length>1" class="gallery-thumbnails"><button v-for="item in visibleThumbnails" :key="item.id || item.rel" :class="{selected:item.index===galleryIndex}" :aria-label="`查看第 ${item.index+1} 项`" @click="galleryIndex=item.index"><img v-if="item.type==='image' || item.poster" :src="thumbUrl(gallery,item.poster || item.rel,160)" alt=""><AppIcon v-else name="play" :size="25"/><span v-if="item.live">Live</span></button></div><details class="gallery-details"><summary>内容说明与存档信息</summary><p><ContentText :text="gallery.text || gallery.text_preview || '没有正文'" :platform="gallery.platform"/></p><dl><template v-for="(value,key) in gallery.meta" :key="key"><dt>{{ key }}</dt><dd><ContentText :text="String(value)" :platform="gallery.platform"/></dd></template><dt>目录</dt><dd>{{ gallery.rel_dir }}</dd><dt>完整性</dt><dd>{{ gallery.archive_status==='complete'?'已验证完整':gallery.archive_status==='partial'?'未完成':'旧格式，尚未确认完整' }}</dd></dl></details></section></div></Teleport>
  </div>
</template>
<style scoped>
.page-heading { display:flex; justify-content:space-between; align-items:flex-start; gap:18px; }.page-heading>.btn { flex-shrink:0; }.library-toolbar { padding:20px; margin-bottom:22px; }.toolbar-row,.filters-row,.date-row { display:flex; gap:12px; flex-wrap:wrap; }.toolbar-row { align-items:center; }.search-input { flex:1; min-width:180px; }.view-switch { display:flex; padding:3px; background:#e9edf0; border-radius:12px; }.view-switch button { display:flex; align-items:center; gap:6px; border:0; background:transparent; padding:9px 12px; border-radius:9px; cursor:pointer; font-size:13px; }.view-switch .selected { background:#fff; color:var(--blue); }.filters-row,.date-row { margin-top:16px; }.filters-row label,.date-row label { flex:1; min-width:110px; font-size:12px; color:var(--text-2); }.filters-row select,.date-row select,.date-row input { margin-top:6px; padding:9px 10px; font-size:12px; min-width:0; }.date-row .date-navigation { flex:1.6; }.toolbar-hint { color:var(--text-2); font-size:11.5px; line-height:1.6; margin-top:14px; }.author-panel { margin:20px 0; }.author-panel>label { display:block; max-width:250px; margin-bottom:12px; }.author-chips { display:flex; gap:8px; flex-wrap:wrap; }.author-chips button { border:1px solid var(--border); border-radius:12px; padding:8px 12px; font-size:13px; background:#fff; cursor:pointer; }.author-chips .selected { border-color:var(--blue); color:var(--blue); }.author-chips span { color:var(--text-2); margin-left:8px; }.author-pager { display:flex; gap:12px; align-items:center; font-size:12px; margin-top:12px; }.scan-note { background:#e8f0f2; padding:12px 16px; border-radius:12px; font-size:12px; line-height:1.8; margin:18px 0; }.scan-note .app-icon { margin-right:6px; }.list-note { font-size:13px; margin:14px 0; color:var(--text-2); }.entry-section { margin-bottom:28px; }.entry-section h2 { font-size:19px; margin:22px 0 14px; display:flex; align-items:center; gap:12px; }.entry-section h2 span { font-size:12px; font-weight:400; color:var(--text-2); }.entry-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:18px; }.entry-card { min-width:0; text-align:left; border:1px solid #fff; border-radius:17px; overflow:hidden; background:#fff; box-shadow:0 3px 15px #182b3010; cursor:pointer; }.entry-card:hover { border-color:#a5becb; }.entry-cover { aspect-ratio:4/3; position:relative; display:flex; align-items:center; justify-content:center; background:#e8efec; overflow:hidden; }.entry-cover img { width:100%; height:100%; object-fit:cover; }.media-kind,.archive-badge { position:absolute; background:#182b35bb; color:#fff; border-radius:6px; padding:3px 7px; font-size:10px; }.media-kind { left:10px; bottom:10px; display:inline-flex; align-items:center; justify-content:center; min-width:28px; min-height:28px; padding:3px; line-height:1; }.media-kind .app-icon { display:block; }.archive-badge { right:10px; top:10px; }.entry-caption { padding:13px 15px 15px; }.entry-author { display:flex; align-items:center; gap:6px; min-width:0; }.entry-author strong { font-size:13px; text-overflow:ellipsis; white-space:nowrap; overflow:hidden; }.entry-title { font-size:12.5px; line-height:1.7; height:42px; overflow:hidden; margin:6px 0 8px; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }.entry-date,.entry-count { font-size:10.5px; color:var(--text-2); line-height:1.7; }.pagination { display:flex; align-items:center; justify-content:center; gap:18px; margin:32px 0 15px; font-size:13px; color:var(--text-2); }.library-empty { text-align:center; padding:60px 25px; }.library-empty h2 { font-size:19px; margin:20px 0 14px; }.library-empty p { font-size:13px; color:var(--text-2); line-height:1.8; }.library-empty>div { margin-top:24px; display:flex; justify-content:center; flex-wrap:wrap; gap:12px; }.root-path { overflow-wrap:anywhere; }.text-button { border:0; color:var(--blue); background:none; cursor:pointer; font-size:12px; padding:3px 5px; }.text-button:disabled { opacity:.4; }.error-text { color:#b53d30; font-size:13px; line-height:1.7; }.sr-only { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0); }.gallery-mask { position:fixed; inset:0; z-index:700; display:grid; place-items:center; background:#131a24ed; padding:20px; }.gallery-panel { width:min(1200px,100%); height:min(900px,100%); color:#eef1f4; display:flex; flex-direction:column; min-height:0; }.gallery-head { display:flex; align-items:center; gap:20px; padding-bottom:12px; }.gallery-head>div { flex:1; min-width:0; }.gallery-head strong { font-size:17px; }.gallery-head p { color:#acb8c2; font-size:12px; margin-top:6px; }.gallery-head>span { font-size:12px; color:#acb8c2; }.viewer-button { border:1px solid #ffffff20; border-radius:12px; background:#ffffff12; color:#fff; width:40px; height:40px; padding:0; line-height:1; display:grid; place-items:center; cursor:pointer; flex-shrink:0; }.viewer-button .app-icon { display:block; }.viewer-button:disabled { opacity:.25; cursor:default; }.gallery-stage { min-height:0; flex:1; position:relative; display:flex; align-items:center; justify-content:center; gap:10px; }.gallery-media { flex:1; height:100%; min-width:0; display:flex; justify-content:center; align-items:center; position:relative; }.gallery-media img,.gallery-media video { max-width:100%; max-height:100%; object-fit:contain; }.live-play { position:absolute; bottom:16px; border:1px solid #fff5; background:#ffffffe6; color:#245943; padding:10px 15px; border-radius:25px; cursor:pointer; display:flex; align-items:center; gap:8px; }.media-error { position:absolute; bottom:65px; padding:12px; font-size:12px; color:#ffe3db; background:#421e1bd9; border-radius:8px; }.text-only { flex:1; text-align:center; }.text-only p { margin-top:20px; color:#aebfc4; }.gallery-thumbnails { display:flex; gap:7px; align-items:center; justify-content:center; overflow:auto; flex-shrink:0; padding:15px 0 10px; }.gallery-thumbnails button { width:58px; height:54px; flex-shrink:0; border:2px solid transparent; border-radius:8px; background:#ffffff15; overflow:hidden; cursor:pointer; position:relative; }.gallery-thumbnails .selected { border-color:#8abcdf; }.gallery-thumbnails img { width:100%; height:100%; object-fit:cover; }.gallery-thumbnails span { position:absolute; bottom:2px; left:4px; font-size:9px; color:#fff; background:#0008; }.gallery-details { font-size:12px; color:#c6d1d9; max-height:25%; overflow:auto; padding:10px 5px; line-height:1.7; }.gallery-details summary { cursor:pointer; }.gallery-details p { white-space:pre-wrap; margin:12px 0 16px; font-size:14px; line-height:1.85; overflow-wrap:anywhere; }.gallery-details dl { display:grid; grid-template-columns:auto 1fr; gap:4px 15px; }.gallery-details dd { overflow-wrap:anywhere; }
.card-live-video { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; pointer-events:none; }.compact-toolbar { justify-content:space-between; }.active-filter-summary { margin-top:10px; font-size:12px; line-height:1.6; color:var(--text-2); overflow-wrap:anywhere; }.library-search { display:block; margin-top:16px; }.compact-toolbar .view-switch { margin-top:0; }.compact-toolbar .filter-toggle { white-space:normal; }.library-toolbar { padding:16px 20px; }
@media(max-width:767px){.toolbar-row.compact-toolbar { display:flex; flex-wrap:wrap; gap:10px; }.compact-toolbar .view-switch { width:auto; margin-top:0; }.compact-toolbar .filter-toggle { margin-left:auto; }}
@media(min-width:1450px){.entry-grid { grid-template-columns:repeat(4,minmax(0,1fr)); }}
@media(max-width:1050px){.entry-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }.date-row label { flex-basis:40%; }}
@media(max-width:767px){.page-heading { flex-wrap:wrap; margin-bottom:20px; }.page-heading .page-sub { margin-bottom:0; }.toolbar-row { display:block; }.view-switch { width:max-content; margin-top:12px; }.entry-grid { gap:12px; }.entry-caption { padding:10px; }.entry-title { font-size:12px; }.entry-count { font-size:9px; }.pagination { gap:10px; font-size:11px; }.pagination .btn { padding:9px 14px; }.gallery-mask { padding:12px; }.gallery-stage { gap:0; }.gallery-prev,.gallery-next { position:absolute; bottom:14px; z-index:2; background:#16283fbb; }.gallery-prev { left:5px; }.gallery-next { right:5px; }.gallery-thumbnails { justify-content:flex-start; }.gallery-head { gap:10px; }.gallery-head strong { font-size:14px; }.gallery-head p { font-size:10px; }.library-toolbar { padding:16px; } }
</style>
