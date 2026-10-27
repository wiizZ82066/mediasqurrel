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
import Avatar from '../components/Avatar.vue'
import MediaDetails from '../components/MediaDetails.vue'
import { mediaProfile } from '../viewer-presentation.js'
import { createRecordingPlayback } from '../recording-playback.js'
import { buildLibraryRows, libraryBatchSize, visibleLibraryRows, mergeLibraryEntries, shouldLoadNextLibraryBatch } from '../library-layout.js'
import { applyDefaultVolume } from '../playback.js'
const route=useRoute(), router=useRouter(), saved=store.libraryState || {}
const roots=ref([]), rootId=ref(saved.rootId || ''), entries=ref(saved.entries || []), total=ref(saved.total || 0), nextCursor=ref(saved.nextCursor ?? null)
const entriesFilterKey=ref(saved.entriesFilterKey || null)
const viewMode=ref(saved.viewMode || 'timeline'), author=ref(saved.author || ''), query=ref(saved.query || ''), platform=ref(saved.platform || ''), mediaType=ref(saved.mediaType || ''), status=ref(saved.status || ''), sort=ref(saved.sort || 'desc')
const dateFrom=ref(saved.dateFrom || ''), dateTo=ref(saved.dateTo || ''), groupBy=ref(saved.groupBy || 'month'), dates=ref([])
const authors=ref([]), authorQuery=ref(''), authorPage=ref(1), authorTotal=ref(0), authorsBusy=ref(false), authorsError=ref(''), authorsOpen=ref(false)
const authorsInline=ref(false)
const gridEl=ref(null), gridWidth=ref(700), viewportTop=ref(0), viewportHeight=ref(800), compact=ref(false), focusedEntry=ref(null), revealBusy=ref(false)
let resizeObserver=null, scrollFrame=null, authorController=null, authorSequence=0
let viewportBatchCapacity=0
const hasMore=computed(()=>!!nextCursor.value)
const loading=ref(false), error=ref(''), scan=ref(null), scanBusy=ref(false), changed=ref(false)
const gallery=ref(null), galleryIndex=ref(0), detailBusy=ref(false), detailError=ref(''), livePlaying=ref(false), mediaError=ref('')
const locateBusy=ref(false), locateMessage=ref(''), detailNotice=ref('')
let locateController=null, locateSequence=0
const viewerEl=ref(null), closeButton=ref(null), liveVideo=ref(null), filtersOpen=ref(false), cardLiveId=ref(null), cardLiveVideo=ref(null)
const openMenuVisible=ref(false), openMenuEl=ref(null), openMenuButton=ref(null), openBusy=ref(false)
const segmentVideo=ref(null)
let collectionPlaybackSequence=0, continuationVolume=null
let recordingPlayer=null, recordingReady=Promise.resolve(true)
let listedRevision=saved.revision || 0
let livePlaybackSequence=0, cardLiveSequence=0
let ready=false, disposed=false, listSequence=0, detailSequence=0, auxSequence=0, listController=null, detailController=null, auxController=null, debounceTimer=null, authorTimer=null, scanTimer=null, contentEl=null, restoreFocus=null, preloads=[]

const activeScan=computed(()=>['queued','running'].includes(scan.value?.status))
const currentItem=computed(()=>gallery.value?.gallery?.[galleryIndex.value] || null)
const currentLocation=computed(()=>itemLocation(gallery.value,currentItem.value))
const socialViewer=computed(()=>gallery.value?.platform==='weibo' && !gallery.value?.collection)
const viewerProfile=computed(()=>mediaProfile(gallery.value))
const viewerMotion=computed(()=>store.preferences?.viewer_motion!==false)
const visibleThumbnails=computed(()=>{const items=gallery.value?.gallery || []; const start=Math.max(0,Math.min(galleryIndex.value-4,items.length-9));return items.slice(start,start+9).map((item,i)=>({...item,index:start+i}))})
const selectedRoot=computed(()=>roots.value.find(root=>root.id===rootId.value))
const unsubscribe=onTaskEvent(message=>{
  if(message.type==='library_updated' || message.type==='library_indexed')changed.value=true
  if(message.type==='library_cover'){const update=message.entry;for(const item of [...entries.value,...(gallery.value?[gallery.value]:[])])if(item.id===update.id && (!item.signature || !update.cover_signature || item.signature===update.cover_signature))Object.assign(item,update)}
  if(ready && route.query.task_id && !gallery.value && !locateBusy.value && !detailBusy.value && (message.type==='reconnect' || (message.type==='library_indexed' && message.task_id===String(route.query.task_id))))locateRoute()
})
const datesLabel={published:'发布时间',directory:'目录日期',downloaded:'下载时间',imported:'导入时间',recorded:'录制开始'}
const filterCount=computed(()=>[query.value.trim(),platform.value,mediaType.value,status.value,dateFrom.value || dateTo.value,sort.value==='asc'].filter(Boolean).length)
const filterSummary=computed(()=>{
  const parts=[]
  if(query.value.trim())parts.push('搜索：'+query.value.trim())
  if(author.value)parts.push('作者：'+author.value)
  if(platform.value)parts.push({weibo:'微博',douyin:'抖音'}[platform.value])
  if(mediaType.value)parts.push({image:'图片',video:'视频',live:'Live 图',text:'文字'}[mediaType.value])
  if(status.value)parts.push({complete:'已验证完整',legacy:'旧格式存档',partial:'未完成',missing:'文件缺失'}[status.value])
  if(dateFrom.value || dateTo.value)parts.push((dateFrom.value || '最早')+' 至 '+(dateTo.value || '最新'))
  if(sort.value==='asc')parts.push('从旧到新')
  return parts.join(' · ')
})
function clearFilters(){query.value='';platform.value='';mediaType.value='';status.value='';dateFrom.value='';dateTo.value='';sort.value='desc'}
function dayOf(value) { if(!value) return '日期未知'; const date=new Date(value); return Number.isNaN(date.getTime())?String(value):new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(date) }
function displayTime(value) { if(!value) return ''; const date=new Date(value);return Number.isNaN(date.getTime())?value:new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(date) }
function segmentTime(value){return value?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value)):'未知'}
function gapText(seconds){if(seconds===null || seconds===undefined)return '未知';const value=Math.round(seconds);return value>=3600?`${Math.floor(value/3600)} 小时 ${Math.floor(value%3600/60)} 分钟`:value>=60?`${Math.floor(value/60)} 分 ${value%60} 秒`:`${value} 秒`}
function itemLocation(entry,item){return item?.source_entry_id?{id:item.source_entry_id,root_id:item.root_id,rel_dir:item.rel_dir}:entry}
function segmentNumber(item){const index=gallery.value?.collection?.parts?.findIndex(part=>part.id===item.segment?.id) ?? -1;return index<0?item.index+1:index+1}
const entryGroups=computed(()=>{ const groups=new Map();for(const entry of entries.value){const day=dayOf(entry.sort_at);const key=groupBy.value==='year'?day.slice(0,4):groupBy.value==='month'?day.slice(0,7):day;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(entry)}return [...groups].map(([date,items])=>({date,items})) })
const virtualLayout=computed(()=>buildLibraryRows(entryGroups.value,gridWidth.value,compact.value))
const visibleRows=computed(()=>visibleLibraryRows(virtualLayout.value,viewportTop.value,viewportHeight.value,focusedEntry.value))
function mediaUrl(entry,rel) { return `/media/roots/${encodeURIComponent(entry.root_id)}/${[entry.rel_dir,rel].filter(Boolean).join('/').split('/').map(encodeURIComponent).join('/')}` }
function thumbUrl(entry,rel,width=480) { return '/api/thumb?'+new URLSearchParams({root_id:entry.root_id,p:[entry.rel_dir,rel].filter(Boolean).join('/'),w:width}) }
function coverStyle(entry) { return entry.cover_face?{objectPosition:`${entry.cover_face.x*100}% ${entry.cover_face.y*100}%`}:{} }
function remember() {
  const matches=entriesMatchFilters() && !changed.value
  store.libraryState={revision:listedRevision,rootId:rootId.value,entries:matches?entries.value:[],total:matches?total.value:0,nextCursor:matches?nextCursor.value:null,entriesFilterKey:matches?entriesFilterKey.value:null,viewMode:viewMode.value,author:author.value,query:query.value,platform:platform.value,mediaType:mediaType.value,status:status.value,sort:sort.value,dateFrom:dateFrom.value,dateTo:dateTo.value,groupBy:groupBy.value,scrollTop:matches?(contentEl?.scrollTop || 0):0}
}
function filters() { return {root_id:rootId.value,author:author.value,q:query.value.trim(),platform:platform.value,media_type:mediaType.value,status:status.value,sort:sort.value,date_from:dateFrom.value,date_to:dateTo.value} }
function entriesMatchFilters(){return entriesFilterKey.value===JSON.stringify(filters())}
async function loadEntries({reset=true,restore=false,append=false}={}) {
  if(!rootId.value || (append && (loading.value || !hasMore.value || !entriesMatchFilters())))return
  listController?.abort();listController=new AbortController();const sequence=++listSequence
  const cursor=append?nextCursor.value:''
  const requestedFilters=filters(), filterKey=JSON.stringify(requestedFilters)
  const requestedRevision=store.libraryRevision || 0
  const pageSize=libraryBatchSize(virtualLayout.value,viewportHeight.value)
  viewportBatchCapacity=append?Math.max(viewportBatchCapacity,pageSize):pageSize
  if(!append){stopCardLive();entries.value=[];nextCursor.value=null;entriesFilterKey.value=null;focusedEntry.value=null;if(contentEl && !restore)contentEl.scrollTop=0}
  loading.value=true;error.value=''
  try {
    if(dateFrom.value && dateTo.value && dateFrom.value>dateTo.value)throw new Error('开始日期不能晚于结束日期。')
    const result=await api.libraryEntries({...requestedFilters,cursor,page_size:pageSize,signal:listController.signal})
    if(disposed || sequence!==listSequence || filterKey!==JSON.stringify(filters()))return
    entries.value=mergeLibraryEntries(append?entries.value:[],result.items);total.value=result.total;nextCursor.value=result.next_cursor || null;entriesFilterKey.value=filterKey;listedRevision=append?listedRevision:requestedRevision;changed.value=listedRevision!==(store.libraryRevision || 0)
    await nextTick();if(restore && contentEl)contentEl.scrollTop=saved.scrollTop || 0
    updateViewport();remember()
  }catch(e){if(sequence===listSequence && e.name!=='AbortError')error.value=e.message}
  finally{if(sequence===listSequence){loading.value=false;await nextTick();maybeLoadMore()}}
}
async function loadAuthors({append=false}={}) {
  if(!rootId.value || (append && (authorsBusy.value || authors.value.length>=authorTotal.value)))return
  authorController?.abort();authorController=new AbortController();const sequence=++authorSequence
  const requestedPage=append?authorPage.value+1:1
  authorsBusy.value=true;authorsError.value=''
  if(!append){authors.value=[];authorPage.value=1}
  try{
    const result=await api.libraryAuthors({root_id:rootId.value,q:authorQuery.value,page:requestedPage,page_size:60,signal:authorController.signal})
    if(disposed || sequence!==authorSequence)return
    const items=new Map((append?authors.value:[]).map(item=>[item.name,item]));for(const item of result.items)items.set(item.name,item)
    authors.value=[...items.values()];authorTotal.value=result.total;authorPage.value=requestedPage
  }catch(e){if(sequence===authorSequence && e.name!=='AbortError')authorsError.value=e.message}
  finally{if(sequence===authorSequence)authorsBusy.value=false}
}
async function loadAuxiliary() {
  if(!rootId.value || !filtersOpen.value)return
  auxController?.abort();auxController=new AbortController();const sequence=++auxSequence
  try{const result=await api.libraryDates({root_id:rootId.value,author:author.value,q:query.value.trim(),group:groupBy.value,limit:240,signal:auxController.signal});if(!disposed && sequence===auxSequence)dates.value=result.items}catch{}
}
function selectAuthor(name){author.value=name;authorsOpen.value=false;nextTick(updateViewport)}
function refreshEntries(){loadEntries();loadAuthors();loadAuxiliary()}
function updateViewport(){
  authorsInline.value=window.innerWidth<1100
  if(!gridEl.value || !contentEl)return
  const bounds=gridEl.value.getBoundingClientRect(), contentBounds=contentEl.getBoundingClientRect()
  gridWidth.value=bounds.width;viewportHeight.value=contentEl.clientHeight || 800;viewportTop.value=Math.max(0,contentBounds.top-bounds.top);compact.value=window.innerWidth<768
}
function maybeLoadMore(){
  if(!ready || disposed || loading.value || error.value || gallery.value || !hasMore.value || !gridEl.value || !contentEl)return
  const bounds=gridEl.value.getBoundingClientRect()
  const capacity=libraryBatchSize(virtualLayout.value,viewportHeight.value)
  // A larger viewport or restored cache may need one larger batch. Reserve
  // that capacity in loadEntries so a short response cannot drain all cursors.
  const viewportBottom=Math.min(contentEl.getBoundingClientRect().bottom,window.innerHeight)
  if(capacity>viewportBatchCapacity && entries.value.length<capacity && Math.max(bounds.bottom,bounds.top+virtualLayout.value.height)<viewportBottom){loadEntries({append:true});return}
  if(shouldLoadNextLibraryBatch({scrollTop:contentEl.scrollTop,gridTop:bounds.top,gridBottom:bounds.bottom,layoutHeight:virtualLayout.value.height,viewportBottom:contentEl.getBoundingClientRect().bottom,windowHeight:window.innerHeight}))loadEntries({append:true})
}
function onScroll(){if(scrollFrame!==null)return;scrollFrame=requestAnimationFrame(()=>{scrollFrame=null;updateViewport();maybeLoadMore()})}
async function revealEntry(entry,rel=null){if(revealBusy.value)return;revealBusy.value=true;try{await api.libraryReveal(entry.id,rel);toast(rel?'已在文件资源管理器中定位文件':'已打开内容目录','success')}catch(e){toast(e.message,'error')}finally{revealBusy.value=false}}
function blurCard(event){if(!event.currentTarget.contains(event.relatedTarget))focusedEntry.value=null}
function closeOpenMenu(restore=false){openMenuVisible.value=false;if(restore)openMenuButton.value?.focus()}
async function showOpenMenu(last=false){
  if(!currentItem.value?.rel || openBusy.value)return
  openMenuVisible.value=true
  await nextTick()
  if(openMenuVisible.value){const items=openMenuEl.value?.querySelectorAll('[role="menuitem"]');(last?items?.[items.length-1]:items?.[0])?.focus()}
}
function toggleOpenMenu(){if(openMenuVisible.value)closeOpenMenu(true);else showOpenMenu()}
function onViewerPointerDown(event){if(openMenuVisible.value && !openMenuEl.value?.contains(event.target))closeOpenMenu()}
function onOpenMenuFocusOut(event){if(!event.currentTarget.contains(event.relatedTarget))closeOpenMenu()}
function viewerControls(){return [...(viewerEl.value?.querySelectorAll('button:not(:disabled),[href],video[controls],summary,[tabindex]:not([tabindex="-1"])') || [])].filter(control=>!control.getClientRects || control.getClientRects().length>0)}
function onOpenMenuKey(event){
  if(event.key==='Escape'){event.preventDefault();if(openMenuVisible.value)closeOpenMenu(true);else closeGallery();return}
  if(!openMenuVisible.value){if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();showOpenMenu(event.key==='ArrowUp')}else onKey(event);return}
  const items=[...(openMenuEl.value?.querySelectorAll('[role="menuitem"]') || [])]
  const index=items.indexOf(document.activeElement)
  if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){
    event.preventDefault()
    const next=event.key==='Home'?0:event.key==='End'?items.length-1:(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length
    items[next]?.focus()
  }else if(event.key==='Tab'){
    event.preventDefault()
    const controls=viewerControls().filter(control=>control===openMenuButton.value || !openMenuEl.value?.contains(control))
    const index=controls.indexOf(openMenuButton.value), next=(index+(event.shiftKey?-1:1)+controls.length)%controls.length
    closeOpenMenu();controls[next]?.focus()
  }
}
async function openCurrentFile(mode){
  const entry=gallery.value, item=currentItem.value
  if(openBusy.value || !entry || !item?.rel || !['default','choose'].includes(mode))return
  const target={id:item.source_entry_id || entry.id,rel:item.rel}
  closeOpenMenu();openBusy.value=true;closeButton.value?.focus()
  try{
    const result=await api.libraryOpen(target.id,target.rel,mode)
    if(mode==='default' && !result.cancelled)toast('已交给默认应用处理','success')
  }catch(error){toast(error.message,'error')}
  finally{
    openBusy.value=false
    await nextTick()
    if(!disposed && currentLocation.value?.id===target.id && currentItem.value?.rel===target.rel && document.activeElement===closeButton.value)openMenuButton.value?.focus()
  }
}
watch([rootId,author,query,platform,mediaType,status,sort,dateFrom,dateTo],()=>{if(!ready)return;clearTimeout(debounceTimer);listController?.abort();++listSequence;loading.value=true;debounceTimer=setTimeout(()=>{loadEntries({reset:true});loadAuxiliary()},300)})
watch(filtersOpen,open=>{if(open && ready)loadAuxiliary();else if(!open){auxController?.abort();++auxSequence}nextTick(updateViewport)})
watch([authorsOpen,authorsInline,()=>store.libraryAuthorsCollapsed],()=>nextTick(updateViewport))
watch(rootId,()=>{if(ready){author.value='';scan.value=null;pollScan();loadAuthors()}})
watch(groupBy,()=>{if(ready){loadAuxiliary();nextTick(updateViewport)}})
watch(authorQuery,()=>{if(!ready)return;authorController?.abort();++authorSequence;clearTimeout(authorTimer);authorTimer=setTimeout(()=>loadAuthors(),300)})
watch(visibleRows,rows=>{if(cardLiveId.value && !rows.some(row=>row.items?.some(item=>item.id===cardLiveId.value)))stopCardLive()})
function navigateDate(event) {const value=event.target.value;if(!value){dateFrom.value='';dateTo.value='';return}const [y,m,d]=value.split('-').map(Number);dateFrom.value=`${y}-${String(m||1).padStart(2,'0')}-${String(d||1).padStart(2,'0')}`;const last=new Date(Date.UTC(y,m || 12,0));dateTo.value=d?dateFrom.value:`${y}-${String(m||12).padStart(2,'0')}-${m?last.getUTCDate():31}`}
async function pollScan() {
  try {const result=await api.libraryScans();if(disposed)return;const old=scan.value;scan.value=result.items?.find(item=>item.root_id===rootId.value) || null;if(old && ['running','queued'].includes(old.status) && !activeScan.value){await loadEntries();loadAuxiliary();loadAuthors();toast(scan.value.status==='success'?'媒体索引已更新':'索引扫描已停止，请查看结果',scan.value.status==='success'?'success':'info')}}catch{ /* list remains available while polling retries */ }
}
async function startScan() {scanBusy.value=true;try{await api.libraryScan(rootId.value);await pollScan();toast('已开始建立索引，原文件保持原位','info')}catch(e){toast(e.message,'error')}finally{scanBusy.value=false}}
async function cancelScan(){try{await api.cancelLibraryScan(scan.value.id);await pollScan()}catch(e){toast(e.message,'error')}}
async function openEntry(id) {
  cancelLocate()
  closeOpenMenu()
  stopCardLive()
  detailController?.abort();detailController=new AbortController();const sequence=++detailSequence;detailBusy.value=true;detailError.value='';detailNotice.value='';restoreFocus=document.activeElement
  try{const result=await api.libraryEntry(id,detailController.signal);if(disposed || sequence!==detailSequence)return;stopLive();++collectionPlaybackSequence;continuationVolume=null;gallery.value=result;document.querySelector('.layout')?.setAttribute('inert','');galleryIndex.value=result.initial_gallery_index || 0;livePlaying.value=false;mediaError.value='';await nextTick();closeButton.value?.focus();preloadNeighbors()}
  catch(e){if(sequence===detailSequence && e.name!=='AbortError')detailError.value=e.message}
  finally{if(sequence===detailSequence)detailBusy.value=false}
}
function clearPreloads(){for(const image of preloads)image.src='';preloads=[]}
function preloadNeighbors(){clearPreloads();const entry=gallery.value;if(!entry)return;for(const index of [galleryIndex.value-1,galleryIndex.value+1]){const item=entry.gallery[index];const rel=item?.type==='image'?item.rel:item?.poster;if(rel){const image=new Image();image.src=mediaUrl(itemLocation(entry,item),rel);preloads.push(image)}}}
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
function selectMedia(index){if(index===null || index<0 || index>=(gallery.value?.gallery?.length || 0))return;++collectionPlaybackSequence;continuationVolume=null;galleryIndex.value=index}
function navigateMedia(delta){selectMedia(galleryIndex.value+delta)}
function fitMedia(event){const media=event.target,width=media.videoWidth || media.naturalWidth,height=media.videoHeight || media.naturalHeight;if(width>0 && height>0)media.style.setProperty('--media-ratio',String(width/height))}
function videoMetadata(event){fitMedia(event);if(event.target===segmentVideo.value)mediaError.value='';if(continuationVolume && event.target===segmentVideo.value)Object.assign(event.target,continuationVolume);else applyDefaultVolume(event,store.preferences?.default_volume)}
function releaseRecording(){recordingPlayer?.dispose();recordingPlayer=null;recordingReady=Promise.resolve(true)}
watch(segmentVideo,video=>{
  releaseRecording()
  if(video && currentItem.value?.playback_info_url){
    recordingPlayer=createRecordingPlayback(video,{infoUrl:currentItem.value.playback_info_url,nativeUrl:mediaUrl(currentLocation.value,currentItem.value.rel)})
    recordingReady=recordingPlayer.ready
  }
},{flush:'post'})
async function continueCollection(event){
  const entry=gallery.value, video=event.target, next=galleryIndex.value+1
  if(!entry?.collection || video!==segmentVideo.value || next>=entry.gallery.length)return
  const sequence=++collectionPlaybackSequence
  continuationVolume={volume:video.volume,muted:video.muted,playbackRate:video.playbackRate}
  galleryIndex.value=next
  await nextTick()
  const following=segmentVideo.value
  if(sequence!==collectionPlaybackSequence || gallery.value!==entry || galleryIndex.value!==next || !following)return
  Object.assign(following,continuationVolume)
  await recordingReady
  if(sequence!==collectionPlaybackSequence || gallery.value!==entry || galleryIndex.value!==next || segmentVideo.value!==following)return
  try{await following.play()}catch(error){if(sequence===collectionPlaybackSequence && error.name!=='AbortError')mediaError.value='已切换下一段，请点击播放继续。'}
}
watch(galleryIndex,()=>{closeOpenMenu();stopLive();mediaError.value='';preloadNeighbors()},{flush:'sync'})
function deactivateViewer(element){element.inert=true;element.setAttribute('aria-hidden','true')}
function closeGallery(){++collectionPlaybackSequence;continuationVolume=null;closeOpenMenu();cancelLocate();++detailSequence;detailController?.abort();detailBusy.value=false;stopLive();segmentVideo.value?.pause?.();releaseRecording();document.querySelector('.layout')?.removeAttribute('inert');gallery.value=null;clearPreloads();restoreFocus?.focus?.({preventScroll:true});if(route.query.from==='home'){if(window.history.state?.back==='/')router.back();else router.replace('/')}else if(route.query.entry_id || route.query.task_id || route.query.entry)router.replace({path:'/library',query:{}})}
function onKey(event){if(!gallery.value || event.defaultPrevented)return;if(event.key==='Escape'){event.preventDefault();if(openMenuVisible.value)closeOpenMenu(true);else closeGallery()}else if(['ArrowRight','ArrowLeft'].includes(event.key)){if(event.target?.closest?.('video,input,textarea,select,[contenteditable="true"]'))return;event.preventDefault();navigateMedia(event.key==='ArrowRight'?1:-1)}else if(event.key==='Tab'){const controls=viewerControls();const first=controls[0],last=controls.at(-1);if(event.shiftKey && document.activeElement===first){event.preventDefault();last?.focus()}else if(!event.shiftKey && document.activeElement===last){event.preventDefault();first?.focus()}}}
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
onMounted(async()=>{
  contentEl=document.querySelector('.content');window.addEventListener('keydown',onKey);contentEl?.addEventListener('scroll',onScroll,{passive:true});window.addEventListener('resize',onScroll)
  updateViewport()
  try{
    const result=await api.libraryRoots();roots.value=result.items
    const restored=roots.value.some(item=>item.id===rootId.value) && saved.entries?.length && entriesMatchFilters() && listedRevision===(store.libraryRevision || 0)
    if(!roots.value.some(item=>item.id===rootId.value))rootId.value=result.default_root_id || roots.value[0]?.id || ''
    if(restored){await nextTick();updateViewport();await nextTick();if(contentEl)contentEl.scrollTop=saved.scrollTop || 0;updateViewport()}
    else await loadEntries()
    await Promise.all([loadAuthors(),loadAuxiliary()]);ready=true
    if(typeof ResizeObserver!=='undefined'){resizeObserver=new ResizeObserver(()=>{updateViewport();maybeLoadMore()});if(gridEl.value)resizeObserver.observe(gridEl.value)}
    updateViewport();maybeLoadMore();await locateRoute();if(disposed)return;await pollScan();if(disposed)return;scanTimer=setInterval(()=>{if(activeScan.value)pollScan()},2000)
  }catch(e){error.value=e.message;ready=true}
})
onBeforeUnmount(()=>{++collectionPlaybackSequence;releaseRecording();stopCardLive();stopLive();unsubscribe();document.querySelector('.layout')?.removeAttribute('inert');remember();disposed=true;cancelLocate();++detailSequence;listController?.abort();detailController?.abort();auxController?.abort();authorController?.abort();resizeObserver?.disconnect();if(scrollFrame!==null)cancelAnimationFrame(scrollFrame);contentEl?.removeEventListener('scroll',onScroll);window.removeEventListener('resize',onScroll);clearTimeout(debounceTimer);clearTimeout(authorTimer);clearInterval(scanTimer);clearPreloads();window.removeEventListener('keydown',onKey)})

</script>
<template>
  <div class="library-view">
    <header class="library-header">
      <div class="library-heading"><h1 class="page-title">媒体库</h1><span v-if="entriesFilterKey" class="library-total" :aria-label="'筛选结果 '+total+' 条'">{{ total }} 条</span></div>
      <div class="library-actions">
        <button v-if="roots.length" class="btn btn-ghost author-toggle" :aria-expanded="authorsOpen" aria-controls="library-authors" @click="authorsOpen=!authorsOpen"><AppIcon name="person" :size="19"/><span :title="author">{{ author || '选择作者' }}</span></button>
        <button v-if="roots.length" class="btn library-filter-toggle" :class="{'is-active':filtersOpen || filterCount}" :aria-expanded="filtersOpen" aria-controls="library-filters" @click="filtersOpen=!filtersOpen"><AppIcon name="search" :size="19"/><span>搜索与筛选</span><span v-if="filterCount" class="filter-count">{{ filterCount }}</span><AppIcon name="chevron-right" :size="12" class="filter-chevron"/></button>
        <button class="btn btn-ghost library-refresh" :disabled="!rootId || loading" aria-label="刷新内容" title="刷新内容" @click="loadEntries()"><AppIcon name="refresh" :size="20"/></button>
        <button class="btn btn-ghost library-index" :disabled="!rootId || activeScan || scanBusy" :aria-label="activeScan?'正在建立索引':'更新索引'" title="扫描媒体目录并更新索引" @click="startScan"><AppIcon name="scan" :size="20"/><span>{{ activeScan?'正在索引':'更新索引' }}</span></button>
      </div>
    </header>
    <p v-if="filterSummary" class="library-filter-summary" aria-live="polite"><span>{{ filterSummary }}</span><button v-if="filterCount" class="text-button" @click="clearFilters">清除筛选</button></p>
    <section v-if="roots.length" id="library-filters" v-show="filtersOpen" class="library-filters" aria-label="搜索与筛选">
      <label class="library-search"><AppIcon name="search" :size="21"/><span class="sr-only">搜索媒体库</span><input v-model="query" class="input" type="search" placeholder="搜索作者、标题、正文或日期"></label>
      <div class="library-filter-fields">
        <label>媒体目录<select v-model="rootId" class="select"><option v-for="root in roots" :key="root.id" :value="root.id">{{ root.label }}</option></select></label>
        <label>平台<select v-model="platform" class="select"><option value="">全部平台</option><option value="weibo">微博</option><option value="douyin">抖音</option></select></label>
        <label>内容<select v-model="mediaType" class="select"><option value="">全部类型</option><option value="image">图片</option><option value="video">视频</option><option value="live">Live 图</option><option value="text">文字</option></select></label>
        <label>状态<select v-model="status" class="select"><option value="">全部可用</option><option value="complete">已验证完整</option><option value="legacy">旧格式存档</option><option value="partial">未完成</option><option value="missing">文件缺失</option></select></label>
        <label>排序<select v-model="sort" class="select"><option value="desc">从新到旧</option><option value="asc">从旧到新</option></select></label>
      </div>
      <div class="library-date-fields">
        <label>开始日期<input v-model="dateFrom" type="date" class="input"></label><label>结束日期<input v-model="dateTo" type="date" class="input"></label>
        <label>分组<select v-model="groupBy" class="select"><option value="year">年</option><option value="month">月</option><option value="day">日</option></select></label>
        <label>日期导航<select class="select" aria-label="跳转日期范围" @change="navigateDate"><option value="">全部日期</option><option v-for="item in dates" :key="item.date" :value="item.date">{{ item.date }} · {{ item.count }} 条</option></select></label>
      </div>
      <details class="library-date-help"><summary>日期规则</summary><p>按发布时间排序；缺失时依次使用目录日期、下载时间、导入时间。日期按 Asia/Shanghai 显示，导航列出最近 240 组。</p></details>
    </section>
    <div v-if="scan" class="scan-note"><div><AppIcon name="scan" :size="20"/>索引扫描：{{ {running:'进行中',queued:'排队中',success:'完成',failed:'失败',cancelled:'已取消',interrupted:'已中断'}[scan.status] || scan.status }} · 已检查 {{ scan.processed || 0 }} 个目录，更新 {{ scan.changed || 0 }} 条<span v-if="scan.error" class="error-text"> · {{ scan.error }}</span><button v-if="activeScan" class="text-button" @click="cancelScan">取消</button></div><ActivityProgress v-if="activeScan" :status="scan.status" :progress="{completed:scan.processed,total:scan.total,unit:'个目录',label:'后台建立媒体索引'}" label="媒体索引进度"/></div>
    <p v-if="locateBusy" class="list-note" role="status">{{ locateMessage }}</p><p v-if="detailNotice" class="list-note" role="status">{{ detailNotice }} <button class="text-button" @click="locateRoute">继续查看</button></p>
    <p v-if="detailBusy" class="list-note" role="status">正在读取内容详情…</p><p v-if="detailError" class="error-text" role="alert">{{ detailError }}</p>
    <div v-if="error" class="card error-text" role="alert">{{ error }} <button class="text-button" @click="loadEntries({append:entries.length>0 && hasMore})">重新加载</button></div>
    <p v-if="loading" class="list-note" role="status">正在读取媒体索引…</p>
    <p v-if="changed" class="list-note" role="status">媒体内容或录播合集已更新。<button class="text-button" :disabled="loading" @click="refreshEntries">刷新列表</button></p>

    <Teleport defer to="#library-sidebar" :disabled="authorsInline">
      <aside id="library-authors" class="author-list" :class="{'is-open':authorsOpen,'card':authorsInline}" aria-label="作者列表">
        <h2><AppIcon name="person" :size="20"/>作者 <span>{{ authorTotal }}</span></h2>
        <label><span class="sr-only">搜索作者列表</span><input v-model="authorQuery" class="input" type="search" placeholder="查找作者"></label>
        <div class="author-options"><button :class="{selected:!author}" :aria-pressed="!author" @click="selectAuthor('')">全部作者</button><button v-for="item in authors" :key="item.name" :class="{selected:author===item.name}" :aria-pressed="author===item.name" @click="selectAuthor(item.name)"><span>{{ item.name }}</span><small>{{ item.count }}</small></button></div>
        <p v-if="authorsBusy" class="list-note">正在读取作者…</p><p v-else-if="!authors.length && authorQuery" class="list-note">没有匹配的作者</p>
        <p v-if="authorsError" class="error-text">{{ authorsError }} <button class="text-button" @click="loadAuthors()">重试</button></p>
        <button v-if="authors.length<authorTotal" class="text-button" :disabled="authorsBusy" @click="loadAuthors({append:true})">显示更多作者</button>
      </aside>
    </Teleport>
      <div class="library-feed">
    <div v-if="!loading && !error && !entries.length" class="card library-empty"><AppIcon name="library" :size="72"/><h2>{{ query || author || dateFrom || dateTo || status || platform || mediaType?'没有符合条件的内容':'这里还没有媒体内容' }}</h2><p>已有媒体文件夹可在设置中只建立索引；文件不会复制或移动。</p><p v-if="selectedRoot" class="root-path">当前目录：{{ selectedRoot.path }}</p><div><router-link to="/settings" class="btn btn-ghost">媒体目录与导入</router-link><router-link to="/" class="btn btn-primary">下载内容</router-link></div></div>
        <div ref="gridEl" class="virtual-entries" :style="{height:virtualLayout.height+'px'}">
          <div v-for="row in visibleRows" :key="row.key" class="virtual-row" :style="{transform:'translateY('+row.top+'px)',height:row.height+'px'}">
            <h2 v-if="row.type==='heading'" class="group-heading">{{ row.date }}<span>{{ row.count }} 条已载入</span></h2>
            <div v-else class="entry-grid" :style="{gridTemplateColumns:'repeat('+virtualLayout.columns+',minmax(0,1fr))',gap:virtualLayout.gap+'px'}">
<article v-for="entry in row.items" :key="entry.id" class="entry-card" :style="{height:virtualLayout.cardHeight+'px'}" @focusin="focusedEntry=entry.id" @focusout="blurCard">
  <button class="entry-open" @click="openEntry(entry.id)"><div class="entry-cover" @pointerenter="hoverCardLive(entry,$event)" @pointerleave="stopCardLive(entry.id)"><MediaThumbnail v-if="entry.cover && entry.availability!=='missing'" :src="thumbUrl(entry,entry.cover)" :position="coverStyle(entry)"/><AppIcon v-else :name="entry.availability==='missing'?'folder':'library'" :size="54"/><video v-if="cardLiveId===entry.id && entry.cover_live_rel && entry.availability!=='missing'" :ref="element=>setCardVideo(element,entry.id)" class="card-live-video" :src="mediaUrl(entry,entry.cover_live_rel)" :style="coverStyle(entry)" muted loop playsinline preload="none" aria-hidden="true" @error="cardLiveError"></video><span v-if="entry.archive_status==='partial' || entry.availability==='missing'" class="archive-badge">{{ entry.availability==='missing'?'文件缺失':'未完成' }}</span></div><div class="entry-caption"><div class="entry-author"><PlatformLogo v-if="entry.platform" :platform="entry.platform" :size="19"/><strong>{{ entry.author }}</strong></div><p class="entry-title"><ContentText :text="entry.text_preview || entry.date_dir || '文字内容'" :platform="entry.platform"/></p><p class="entry-date">{{ dayOf(entry.sort_at) }} · {{ datesLabel[entry.date_source] || '日期' }}</p><p v-if="entry.collection" class="entry-count">录播合集 · {{ entry.collection.part_count }} 段 · 分段连播</p><p v-else class="entry-count">{{ entry.counts?.photos || 0 }} 图片 · {{ entry.counts?.videos || 0 }} 视频 · {{ entry.counts?.lives || 0 }} Live</p></div></button>
  <button class="entry-reveal" :disabled="revealBusy" :aria-label="'在文件资源管理器中打开 '+entry.author+' 的内容目录'" title="在文件资源管理器中打开内容目录" @click.stop="revealEntry(entry)"><AppIcon name="folder" :size="20"/></button>
</article>
            </div>
          </div>
        </div>
        <div v-if="total" class="load-more" role="status"><span>{{ loading?'正在读取更多内容…':hasMore?'向下滚动继续浏览':'已显示全部内容' }}</span><button v-if="hasMore" class="text-button" :disabled="loading" @click="loadEntries({append:true})">{{ error?'重试加载':'继续加载' }}</button><span>{{ entries.length }} / {{ total }} 条</span></div>
      </div>
    <Teleport to="body">
      <Transition name="viewer" :css="viewerMotion" @before-leave="deactivateViewer">
        <div v-if="gallery" class="gallery-mask" :class="{ 'viewer-motion-on': viewerMotion }" @pointerdown="onViewerPointerDown" @click.self="closeGallery">
          <section ref="viewerEl" class="gallery-panel" role="dialog" aria-modal="true" aria-label="媒体查看器">
            <header class="gallery-head">
              <div v-if="socialViewer" class="viewer-context">
                <PlatformLogo platform="weibo" :size="30" />
                <div><strong>微博详情</strong></div>
              </div>
              <div v-else class="gallery-head-title">
                <Avatar :src="viewerProfile.avatarUrl" :name="gallery.author" :size="38" />
                <div><strong>{{ gallery.author }}</strong><p>{{ displayTime(gallery.sort_at) }} · {{ datesLabel[gallery.date_source] || '内容时间' }}</p></div>
              </div>
              <span class="viewer-counter" aria-live="polite" aria-atomic="true">{{ gallery.gallery?.length ? galleryIndex+1 : 0 }}<span> / {{ gallery.gallery?.length || 0 }}</span></span>
              <div class="viewer-actions">
                <button class="viewer-button" title="打开内容目录" aria-label="打开内容目录" :disabled="revealBusy" @click="revealEntry(currentLocation || gallery)"><AppIcon name="folder" :size="20" /></button>
                <button v-if="currentItem" class="viewer-button" title="在文件资源管理器中定位当前文件" aria-label="在文件资源管理器中定位当前文件" :disabled="revealBusy" @click="revealEntry(currentLocation,currentItem.rel)"><AppIcon name="search" :size="20" /></button>
                <div v-if="currentItem?.rel" ref="openMenuEl" class="viewer-open" @keydown.stop="onOpenMenuKey" @focusout="onOpenMenuFocusOut">
                  <button ref="openMenuButton" class="viewer-button viewer-open-toggle" aria-haspopup="menu" :aria-expanded="openMenuVisible" aria-controls="viewer-open-options" :aria-label="currentItem.live?'打开当前 Live 视频':'打开当前文件'" :disabled="openBusy" @click="toggleOpenMenu">{{ openBusy?'打开中…':'打开' }}<AppIcon name="chevron-right" :size="14" /></button>
                  <Transition name="viewer-menu" :css="viewerMotion">
                    <div v-if="openMenuVisible" id="viewer-open-options" class="viewer-open-options" role="menu" aria-label="打开当前文件">
                      <button role="menuitem" tabindex="-1" @click="openCurrentFile('default')">{{ currentItem.live?'用默认应用打开 Live 视频':'用默认应用打开' }}</button>
                      <button role="menuitem" tabindex="-1" @click="openCurrentFile('choose')">选择打开方式…</button>
                    </div>
                  </Transition>
                </div>
              </div>
              <button ref="closeButton" class="viewer-button viewer-close" title="关闭（Esc）" aria-label="关闭查看器" @click="closeGallery"><AppIcon name="close" :size="22" /></button>
            </header>

            <div class="viewer-shell" :class="{ 'viewer-social': socialViewer, 'is-text-only': !currentItem }">
              <div class="viewer-canvas">
                <section v-if="gallery.collection" class="collection-timeline" aria-label="录播合集分段">
                  <div class="collection-summary"><strong>录播合集 · {{ gallery.collection.part_count }} 段</strong><span>分段自动连播 · 北京时间</span></div>
                  <p v-if="currentItem?.segment">本段开始 {{ segmentTime(currentItem.segment.started_at) }} · 收尾完成 {{ segmentTime(currentItem.segment.finished_at) }}</p>
                  <p v-if="currentItem?.segment?.gap_before_seconds > 0" class="collection-gap">距上一段收尾约 {{ gapText(currentItem.segment.gap_before_seconds) }}，此间未录制。</p>
                  <p v-if="currentItem?.segment?.error" class="collection-gap">{{ currentItem.segment.error }}</p>
                  <details class="collection-segment-details">
                    <summary>全部分段与时间</summary>
                    <div class="collection-segments"><button v-for="(part,index) in gallery.collection.parts" :key="part.id" :disabled="!part.playable" :aria-pressed="currentItem?.segment?.id===part.id" @click="selectMedia(part.gallery_index)">分段 {{ index+1 }} · {{ segmentTime(part.started_at) }}{{ !part.playable?' · 无可用视频':'' }}</button></div>
                  </details>
                </section>

                <div v-if="currentItem" class="gallery-stage">
                  <button v-if="gallery.gallery.length>1" class="viewer-button gallery-prev" :disabled="galleryIndex===0" aria-label="上一项" @click="navigateMedia(-1)"><AppIcon name="chevron-left" :size="22" /></button>
                  <div :key="currentItem.id || currentItem.rel" class="gallery-media" @pointerenter="hoverLive" @pointerleave="leaveLive">
                    <template v-if="currentItem.live">
                      <video v-if="livePlaying" ref="liveVideo" :src="mediaUrl(currentLocation,currentItem.rel)" :poster="currentItem.poster?mediaUrl(currentLocation,currentItem.poster):undefined" controls muted loop playsinline preload="metadata" @loadedmetadata="fitMedia" @error="liveError"></video>
                      <img v-else-if="currentItem.poster" :src="mediaUrl(currentLocation,currentItem.poster)" alt="Live 图静态画面，鼠标悬停可静音播放" @load="fitMedia" @error="mediaError='静态画面无法读取，可尝试播放 Live 图。'">
                      <button class="live-play" :aria-pressed="livePlaying" @click="toggleLive"><AppIcon name="live" :size="18" />{{ livePlaying?'停止 Live 图':'播放 Live 图' }}</button>
                    </template>
                    <video v-else-if="currentItem.type==='video'" ref="segmentVideo" :key="currentItem.id || currentItem.rel" :src="currentItem.playback_info_url?undefined:mediaUrl(currentLocation,currentItem.rel)" :poster="currentItem.poster?mediaUrl(currentLocation,currentItem.poster):undefined" controls playsinline preload="metadata" @loadedmetadata="videoMetadata" @ended="continueCollection" @error="mediaError='视频无法读取或浏览器不支持此格式，请检查原文件。'"></video>
                    <img v-else :src="mediaUrl(currentLocation,currentItem.rel)" alt="媒体原图" @load="fitMedia" @error="mediaError='图片无法读取，请检查文件是否移动或删除。'">
                    <p v-if="mediaError" class="media-error" role="alert">{{ mediaError }}</p>
                  </div>
                  <button v-if="gallery.gallery.length>1" class="viewer-button gallery-next" :disabled="galleryIndex>=(gallery.gallery.length-1)" aria-label="下一项" @click="navigateMedia(1)"><AppIcon name="chevron-right" :size="22" /></button>
                </div>

                <nav v-if="visibleThumbnails.length>1" class="gallery-thumbnails" :aria-label="gallery.collection?'选择录播分段':'选择媒体'">
                  <button v-for="item in visibleThumbnails" :key="item.id || item.rel" :class="{ selected:item.index===galleryIndex, 'collection-segment-button':!!gallery.collection }" :aria-label="gallery.collection ? '查看分段 '+segmentNumber(item) : '查看第 '+(item.index+1)+' 项'" :aria-pressed="item.index===galleryIndex" @click="selectMedia(item.index)">
                    <strong v-if="gallery.collection" class="collection-segment-label">分段 {{ segmentNumber(item) }}</strong>
                    <template v-else><img v-if="item.type==='image' || item.poster" :src="thumbUrl(itemLocation(gallery,item),item.poster || item.rel,160)" alt=""><AppIcon v-else name="play" :size="25" /><span v-if="item.live">Live</span></template>
                  </button>
                </nav>

                <details v-if="!socialViewer" class="viewer-description" :open="!currentItem">
                  <summary><div><span class="viewer-eyebrow">内容说明</span><p><ContentText :text="gallery.text_preview || (gallery.collection?'查看录播内容与存档信息':'查看正文与存档信息')" :platform="gallery.platform" /></p></div><AppIcon name="chevron-right" :size="17" /></summary>
                  <div class="viewer-notes"><MediaDetails :entry="gallery" /></div>
                </details>
              </div>

              <aside v-if="socialViewer" class="viewer-sidebar" aria-label="微博正文与作者">
                <MediaDetails :entry="gallery" :date-text="displayTime(gallery.sort_at)" :date-label="datesLabel[gallery.date_source] || '内容时间'" show-profile />
              </aside>
            </div>
          </section>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>
<style scoped>
.author-panel { margin:20px 0; }.author-panel>label { display:block; max-width:250px; margin-bottom:12px; }.author-chips { display:flex; gap:8px; flex-wrap:wrap; }.author-chips button { border:1px solid var(--border); border-radius:12px; padding:8px 12px; font-size:13px; background:#fff; cursor:pointer; }.author-chips .selected { border-color:var(--blue); color:var(--blue); }.author-chips span { color:var(--text-2); margin-left:8px; }.author-pager { display:flex; gap:12px; align-items:center; font-size:12px; margin-top:12px; }.scan-note { background:#e8f0f2; padding:12px 16px; border-radius:12px; font-size:12px; line-height:1.8; margin:18px 0; }.scan-note .app-icon { margin-right:6px; }.list-note { font-size:13px; margin:14px 0; color:var(--text-2); }.entry-section { margin-bottom:28px; }.entry-section h2 { font-size:19px; margin:22px 0 14px; display:flex; align-items:center; gap:12px; }.entry-section h2 span { font-size:12px; font-weight:400; color:var(--text-2); }.entry-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:18px; }.entry-card { min-width:0; text-align:left; border:1px solid var(--border); border-radius:var(--radius-card); overflow:hidden; background:#fff; box-shadow:var(--shadow-card); cursor:pointer; }.entry-card:hover { border-color:#a5becb; }.entry-cover { aspect-ratio:4/3; position:relative; display:flex; align-items:center; justify-content:center; background:#e8efec; overflow:hidden; }.entry-cover img { width:100%; height:100%; object-fit:cover; }.archive-badge { position:absolute; background:#182b35bb; color:#fff; border-radius:6px; padding:3px 7px; font-size:10px; }.archive-badge { right:10px; top:10px; }.entry-caption { padding:13px 15px 15px; }.entry-author { display:flex; align-items:center; gap:6px; min-width:0; }.entry-author strong { font-size:13px; text-overflow:ellipsis; white-space:nowrap; overflow:hidden; }.entry-title { font-size:12.5px; line-height:1.7; height:42px; overflow:hidden; margin:6px 0 8px; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }.entry-date,.entry-count { font-size:10.5px; color:var(--text-2); line-height:1.7; }.pagination { display:flex; align-items:center; justify-content:center; gap:18px; margin:32px 0 15px; font-size:13px; color:var(--text-2); }.library-empty { text-align:center; padding:60px 25px; }.library-empty h2 { font-size:19px; margin:20px 0 14px; }.library-empty p { font-size:13px; color:var(--text-2); line-height:1.8; }.library-empty>div { margin-top:24px; display:flex; justify-content:center; flex-wrap:wrap; gap:12px; }.root-path { overflow-wrap:anywhere; }.text-button { border:0; color:var(--blue); background:none; cursor:pointer; font-size:12px; padding:3px 5px; }.text-button:disabled { opacity:.4; }.error-text { color:#b53d30; font-size:13px; line-height:1.7; }.sr-only { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0); }
@media(min-width:1450px){.entry-grid { grid-template-columns:repeat(4,minmax(0,1fr)); }}
@media(max-width:1050px){.entry-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }}
@media(max-width:767px){.entry-grid { gap:12px; }.entry-caption { padding:10px; }.entry-title { font-size:12px; }.entry-count { font-size:9px; }.pagination { gap:10px; font-size:11px; }.pagination .btn { padding:9px 14px; }}

.author-list { height:100%; min-width:0; overflow:auto; overscroll-behavior:contain; }
.author-list h2 { display:flex; gap:8px; align-items:center; font-size:15px; margin:0 4px 14px; }
.author-list h2 span { margin-left:auto; color:var(--text-2); font-size:12px; font-weight:400; }
.author-list label { display:block; margin-bottom:12px; }.author-list input { padding:9px; font-size:12px; min-width:0; }
.author-options { display:grid; gap:5px; }.author-options button { display:flex; align-items:center; gap:8px; text-align:left; width:100%; min-width:0; padding:10px; border:1px solid transparent; border-radius:10px; color:var(--text); background:transparent; cursor:pointer; }
.author-options button>span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.author-options small { color:var(--text-2); margin-left:auto; }.author-options .selected { background:#e8f1f8; border-color:#bdcfdf; color:#176196; }
.author-toggle { display:none; }.library-feed { min-width:0; }.virtual-entries { position:relative; min-width:0; }.virtual-row { position:absolute; inset:0 0 auto; }.group-heading { display:flex; align-items:center; gap:12px; height:56px; font-size:19px; margin:0; }.group-heading span { font-size:11px; font-weight:400; color:var(--text-2); }
.entry-card { display:block; padding:0; position:relative; }.entry-caption { height:140px; box-sizing:border-box; }.entry-cover { width:100%; }.load-more { display:flex; align-items:center; justify-content:center; gap:12px; flex-wrap:wrap; min-height:70px; padding:12px; color:var(--text-2); font-size:12px; }
@media(max-width:1099px){.author-toggle { display:flex; }.author-list { display:none; height:auto; max-height:320px; padding:16px 12px; margin-bottom:20px; }.author-list.is-open { display:block; }.author-options { grid-template-columns:repeat(2,minmax(0,1fr)); }}

.entry-open { display:block; width:100%; height:100%; padding:0; border:0; background:transparent; color:inherit; font:inherit; text-align:left; cursor:pointer; }
.entry-open:focus-visible { outline:2px solid var(--blue); outline-offset:-3px; border-radius:var(--radius-card); }
.entry-reveal { position:absolute; right:9px; bottom:9px; width:32px; height:32px; padding:0; border:1px solid #d9e4e8; border-radius:9px; background:#f2f6f8; display:grid; place-items:center; cursor:pointer; }
.entry-reveal:focus-visible { outline:2px solid var(--blue); outline-offset:2px; }
.entry-reveal:hover { background:#e5eef4; }.entry-reveal:disabled { opacity:.4; cursor:default; }
.entry-count { padding-right:32px; }
</style>

<style scoped src="../styles/media-viewer.css"></style>
<style scoped src="../styles/library-toolbar.css"></style>
