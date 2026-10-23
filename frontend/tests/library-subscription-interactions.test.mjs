import { buildLibraryRows, libraryBatchSize, visibleLibraryRows, mergeLibraryEntries, shouldLoadNextLibraryBatch } from '../src/library-layout.js'
import { subscriptionHomepage, newerBatch } from '../src/subscription-homepage.js'
import { scanCoverageNotice, scanStatusText, scanBadge } from '../src/subscription-scan.js'
import { liveLabels, liveDuration, liveSize } from '../src/live-status.js'
import { taskAuthor, taskTitle, taskPlatform, taskContentTime, mergeTaskSummary } from '../src/task-presentation.js'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { after, test } from 'node:test'

const require = createRequire(new URL('../package.json', import.meta.url))
const { parse, compileScript, compileTemplate } = require('@vue/compiler-sfc')
const vue = require('vue')

// Execute the real SFC setup and watchers with in-memory API/media doubles.
// Browser layout, pointer dispatch and video decoding remain browser QA concerns.
const originalGlobals = Object.fromEntries(['document', 'window', 'Image'].map(key => [key, globalThis[key]]))
globalThis.document = { querySelector: () => null, activeElement: null }
globalThis.window = { addEventListener: () => {}, matchMedia: () => ({ matches: true }), removeEventListener: () => {} }
globalThis.Image = class { set src(value) { this.value = value } }
after(() => {
  for (const [key, value] of Object.entries(originalGlobals)) {
    if (value === undefined) delete globalThis[key]
    else globalThis[key] = value
  }
})

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function mediaDouble() {
  const playback = deferred()
  return vue.markRaw({
    playback, muted: false, currentTime: 5, plays: 0, pauses: 0,
    play() { this.plays++; return playback.promise },
    pause() { this.pauses++ },
  })
}

function setupComponent(name, t, savedState = {}, sharedStore = null) {
  const filename = fileURLToPath(new URL(name === 'App' ? '../src/App.vue' : `../src/views/${name}.vue`, import.meta.url))
  const { descriptor, errors } = parse(fs.readFileSync(filename, 'utf8'), { filename })
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: name })
  const template = compileTemplate({ id: name, filename, source: descriptor.template.content,
    compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  const api = { subBatch: async () => ({status:'idle'}) }, mounted = [], cleanups = [], timers = new Map()
  let timerId = 0
  const store = sharedStore || vue.reactive({ tasks: [], subs: [], libraryState: savedState, libraryAuthorsCollapsed: false,
    taskListState: {items:[],previous:[],q:'',status:'',cursor:''} })
  let taskListener
  const scope = vue.effectScope()
  const env = {
    ...vue, api, store, scanBadge, liveLabels, liveDuration, liveSize, taskAuthor, taskTitle, taskPlatform, taskContentTime, mergeTaskSummary, removeTaskSummary: () => {}, liveState: monitor => monitor?.runtime_status || monitor?.status, LiveStatus: {},
    setInterval: () => 0, clearInterval: () => {},
    toast: () => {}, upsertTaskSummary: () => {}, MaintenancePanel: {}, refreshSubs: async () => {}, connectWS: () => {}, refreshTasks: async () => {}, dismissToast: () => {},
    onTaskEvent: fn => { taskListener=fn; return () => {} }, onMounted: fn => mounted.push(fn),
    onBeforeUnmount: fn => cleanups.push(fn),
    useRoute: () => ({ query: {} }), useRouter: () => ({ replace: () => {} }),
    setTimeout: fn => { const id = ++timerId; timers.set(id, fn); return id },
    clearTimeout: id => timers.delete(id),
    buildLibraryRows, libraryBatchSize, visibleLibraryRows, mergeLibraryEntries, shouldLoadNextLibraryBatch, subscriptionHomepage, newerBatch, scanCoverageNotice, scanStatusText, applyDefaultVolume: () => {}, ContentText: {}, CalendarPlanFields: {}, IntervalPicker: {}, AppIcon: {}, PlatformLogo: {}, ActivityProgress: {}, MediaThumbnail: {}, Avatar: {}, SchedulePanel: {}, SubscriptionFilterPanel: {}, LiveRecordingPanel: {}, parseDownloadTags: value => ({tags: []}), downloadFilterLabel: () => '全部新内容',
  }
  const source = script.content.replace(/^import .*$/gm, '').replace('export default', 'return')
  const component = new Function(...Object.keys(env), source)(...Object.values(env))
  const state = scope.run(() => component.setup({}, { expose: () => {} }))
  let disposed = false
  function unmount() {
    if (disposed) return
    disposed = true
    cleanups.forEach(fn => fn())
    scope.stop()
    timers.clear()
  }
  t.after(unmount)
  return {
    state, api, store, timers, unmount, event: value => taskListener?.(value),
    async mount() { await Promise.all(mounted.map(fn => fn())); await vue.nextTick() },
    async runTimers() {
      const pending = [...timers.values()]
      timers.clear()
      pending.forEach(fn => fn())
      await vue.nextTick()
    },
  }
}

function gallery() {
  return { root_id: 'test-root', rel_dir: 'author/date', gallery: [
    { live: true, type: 'video', rel: 'one.mov', poster: 'one.jpg' },
    { live: true, type: 'video', rel: 'two.mov', poster: 'two.jpg' },
  ] }
}

function collectionGallery() {
  return { id:'collection-anchor',root_id:'test-root',rel_dir:'author/first',collection:{part_count:2},gallery:[
    {id:'first',type:'video',rel:'recording.mp4',source_entry_id:'first-entry',root_id:'test-root',rel_dir:'author/first'},
    {id:'second',type:'video',rel:'recording.mp4',source_entry_id:'second-entry',root_id:'test-root',rel_dir:'author/second'},
  ] }
}

test('a collection advances only on ended, uses the next source and preserves player preferences',async t=>{
  const {state}=setupComponent('Library',t)
  state.gallery.value=collectionGallery()
  const first=mediaDouble(),second=mediaDouble()
  first.volume=.24;first.muted=false;first.playbackRate=1.25
  state.segmentVideo.value=first
  const advancing=state.continueCollection({target:first})
  state.segmentVideo.value=second
  second.playback.resolve()
  await advancing
  assert.equal(state.galleryIndex.value,1)
  assert.equal(second.plays,1)
  assert.equal(second.volume,.24)
  assert.equal(second.playbackRate,1.25)
  assert.equal(state.currentLocation.value.id,'second-entry')
  assert.match(state.mediaUrl(state.currentLocation.value,state.currentItem.value.rel),/author\/second\/recording.mp4$/)
  await state.continueCollection({target:second})
  assert.equal(state.galleryIndex.value,1)
  assert.equal(second.plays,1)
})

test('collection file operations target the selected source and closing cancels delayed continuation',async t=>{
  const {state,api}=setupComponent('Library',t)
  state.gallery.value=collectionGallery();state.selectMedia(1)
  let opened
  api.libraryOpen=async (...args)=>{opened=args;return {ok:true}}
  await state.openCurrentFile('default')
  assert.deepEqual(opened,['second-entry','recording.mp4','default'])
  state.selectMedia(0)
  const first=mediaDouble(),second=mediaDouble();first.volume=.5;first.playbackRate=1
  state.segmentVideo.value=first
  const advancing=state.continueCollection({target:first})
  state.closeGallery();state.segmentVideo.value=second
  await advancing
  assert.equal(second.plays,0)
})

test('ordinary video galleries do not automatically play unrelated items',async t=>{
  const {state}=setupComponent('Library',t)
  const entry=collectionGallery();delete entry.collection
  state.gallery.value=entry
  const first=mediaDouble();state.segmentVideo.value=first
  await state.continueCollection({target:first})
  assert.equal(state.galleryIndex.value,0)
})

test('startup repair saves immediately and rereads the persisted registration', async t => {
  const { state, api } = setupComponent('Settings', t)
  let saved = { enabled: true, available: true, supported: true, method: 'legacy_run', matches_current: false }
  api.startupSettings = async () => ({ ...saved })
  api.saveStartup = async enabled => {
    saved = { ...saved, enabled, method: enabled ? 'shortcut' : null, matches_current: enabled }
    return { ...saved }
  }
  await state.loadStartup()
  assert.equal(state.startup.value.matches_current, false)
  await state.changeStartup(true)
  assert.equal(state.startup.value.method, 'shortcut')
  await state.loadStartup()
  assert.equal(state.startup.value.enabled, true)
  assert.equal(state.startup.value.matches_current, true)
  const checkbox = { checked: false }
  await state.toggleStartup({ target: checkbox })
  assert.equal(state.startup.value.enabled, false)
  await state.loadStartup()
  assert.equal(state.startup.value.enabled, false)
})

test('startup save prevents duplicate requests and retains last known state on failure', async t => {
  const { state, api } = setupComponent('Settings', t)
  state.startup.value = { enabled: false, available: true, supported: true }
  const request = deferred()
  let saves = 0
  api.saveStartup = () => { saves++; return request.promise }
  const checkbox = { checked: true }
  const saving = state.toggleStartup({ target: checkbox })
  assert.equal(checkbox.checked, false)
  assert.equal(state.startupSaving.value, true)
  await state.changeStartup(true)
  assert.equal(saves, 1)
  request.reject(new Error('startup folder is read-only'))
  await saving
  assert.equal(state.startup.value.enabled, false)
  assert.match(state.startupError.value, /read-only/)
  assert.equal(state.startupSaving.value, false)
})

test('collapsed library controls preserve filters without fetching date choices', async t => {
  const { state, api } = setupComponent('Library', t)
  let requests = 0
  api.libraryAuthors = async () => { requests++; return { items: [], total: 0 } }
  api.libraryDates = async () => { requests++; return { items: [] } }
  state.rootId.value = 'test-root'
  assert.equal(state.filtersOpen.value, false)
  await state.loadAuxiliary()
  assert.equal(requests, 0)
  state.filtersOpen.value = true
  await state.loadAuxiliary()
  assert.equal(requests, 1)
  state.query.value = 'saved search'
  state.platform.value = 'weibo'
  state.dateFrom.value = '2026-01-01'
  state.filtersOpen.value = false
  await vue.nextTick()
  await state.loadAuxiliary()
  assert.equal(requests, 1)
  assert.equal(state.query.value, 'saved search')
  assert.equal(state.platform.value, 'weibo')
  assert.equal(state.dateFrom.value, '2026-01-01')
  assert.match(state.filterSummary.value, /saved search/)
})

test('Live cards only play paired present media and stale playback failure cannot stop the next card', async t => {
  const { state, api } = setupComponent('Library', t)
  api.libraryEntry = () => assert.fail('Card hover must not fetch entry details')
  for (const [entry, pointerType] of [
    [{ id: 'missing', availability: 'missing', cover_live_rel: 'one.mov' }, 'mouse'],
    [{ id: 'unpaired', availability: 'present' }, 'mouse'],
    [{ id: 'touch', availability: 'present', cover_live_rel: 'one.mov' }, 'touch'],
  ]) {
    await state.hoverCardLive(entry, { pointerType })
    assert.equal(state.cardLiveId.value, null)
  }
  const first = mediaDouble(), second = mediaDouble()
  const firstPlay = state.hoverCardLive({ id: 'one', cover_live_rel: 'one.mov' }, { pointerType: 'mouse' })
  state.setCardVideo(first, 'one')
  await vue.nextTick()
  assert.equal(first.plays, 1)
  assert.equal(first.muted, true)
  const secondPlay = state.hoverCardLive({ id: 'two', cover_live_rel: 'two.mov' }, { pointerType: 'mouse' })
  state.setCardVideo(second, 'two')
  state.setCardVideo(null, 'one') // Old DOM ref cleanup can arrive after the new ref.
  await vue.nextTick()
  assert.equal(first.pauses, 1)
  assert.equal(first.currentTime, 0)
  assert.equal(second.plays, 1)
  state.stopCardLive('one')
  first.playback.reject(new Error('late play rejection'))
  await firstPlay
  assert.equal(state.cardLiveId.value, 'two')
  assert.equal(state.cardLiveVideo.value, second)
  assert.equal(second.pauses, 0)
  state.stopCardLive('two')
  assert.equal(second.pauses, 1)
  assert.equal(state.cardLiveVideo.value, null)
  second.playback.resolve()
  await secondPlay
  assert.equal(state.cardLiveId.value, null)
})

test('current Live card playback failure falls back to its static cover', async t => {
  const { state } = setupComponent('Library', t)
  const video = mediaDouble()
  const playback = state.hoverCardLive({ id: 'one', cover_live_rel: 'one.mov' }, { pointerType: 'mouse' })
  state.setCardVideo(video, 'one')
  await vue.nextTick()
  video.playback.reject(new Error('unsupported video'))
  await playback
  assert.equal(state.cardLiveId.value, null)
  assert.equal(state.cardLiveVideo.value, null)
  assert.equal(video.pauses, 1)
})

test('viewer hover is muted, touch requires explicit play, and leaving stops playback', async t => {
  const { state } = setupComponent('Library', t)
  state.gallery.value = gallery()
  const video = mediaDouble()
  state.liveVideo.value = video
  state.hoverLive({ pointerType: 'touch' })
  await vue.nextTick()
  assert.equal(video.plays, 0)
  state.hoverLive({ pointerType: 'mouse' })
  await vue.nextTick()
  assert.equal(video.plays, 1)
  assert.equal(video.muted, true)
  state.leaveLive({ pointerType: 'mouse' })
  assert.equal(state.livePlaying.value, false)
  assert.equal(video.pauses, 1)
  assert.equal(video.currentTime, 0)
  video.playback.resolve()
  await vue.nextTick()
  const manualVideo = mediaDouble()
  state.liveVideo.value = manualVideo
  state.toggleLive() // The shared touch/keyboard button handler.
  await vue.nextTick()
  state.leaveLive({ pointerType: 'touch' })
  assert.equal(state.livePlaying.value, true)
  assert.equal(manualVideo.plays, 1)
  state.toggleLive()
  assert.equal(state.livePlaying.value, false)
  assert.equal(manualVideo.pauses, 1)
  manualVideo.playback.resolve()
})

test('changing or destroying the viewer cancels old playback without affecting a new item', async t => {
  const { state, unmount } = setupComponent('Library', t)
  state.gallery.value = gallery()
  const first = mediaDouble(), second = mediaDouble()
  state.liveVideo.value = first
  const firstPlay = state.startLive()
  await vue.nextTick()
  state.navigateMedia(1)
  assert.equal(first.pauses, 1)
  assert.equal(state.livePlaying.value, false)
  state.liveVideo.value = second
  const secondPlay = state.startLive()
  await vue.nextTick()
  first.playback.reject(new Error('stale decoder rejection'))
  await firstPlay
  assert.equal(state.livePlaying.value, true)
  assert.equal(second.pauses, 0)
  assert.equal(state.mediaError.value, '')
  state.closeGallery()
  assert.equal(second.pauses, 1)
  assert.equal(state.gallery.value, null)
  second.playback.reject(new Error('closed decoder rejection'))
  await secondPlay
  assert.equal(state.mediaError.value, '')
  state.gallery.value = gallery()
  const last = mediaDouble()
  state.liveVideo.value = last
  const lastPlay = state.startLive()
  await vue.nextTick()
  unmount()
  assert.equal(last.pauses, 1)
  assert.equal(state.livePlaying.value, false)
  last.playback.reject(new Error('unmounted decoder rejection'))
  await lastPlay
  assert.equal(state.mediaError.value, '')
})

test('folding subscription creation cancels debounce and in-flight search while retaining drafts', async t => {
  const { state, api, timers, runTimers } = setupComponent('Subs', t)
  let searchCount = 0, localCount = 0, signal
  const response = deferred()
  api.localAuthors = async () => { localCount++; return [] }
  api.searchBlogger = async (_platform, _query, searchSignal) => {
    searchCount++; signal = searchSignal; return response.promise
  }
  assert.equal(state.addOpen.value, false)
  assert.equal(state.filtersOpen.value, false)
  state.form.nickname = 'saved draft'
  await vue.nextTick()
  await runTimers()
  assert.equal(searchCount, 0)
  assert.equal(localCount, 0)
  state.addOpen.value = true
  await vue.nextTick()
  assert.equal(localCount, 1)
  assert.equal(searchCount, 0)
  state.form.nickname = 'new draft'
  await vue.nextTick()
  assert.equal(timers.size, 1)
  state.form.blogger_id = 'saved-id'
  state.addOpen.value = false
  await vue.nextTick()
  await runTimers()
  assert.equal(searchCount, 0)
  assert.equal(state.form.blogger_id, 'saved-id')
  state.addOpen.value = true
  await vue.nextTick()
  state.form.nickname = 'searching draft'
  await vue.nextTick()
  await runTimers()
  assert.equal(searchCount, 1)
  state.addOpen.value = false
  await vue.nextTick()
  assert.equal(signal.aborted, true)
  response.resolve({ results: [{ nickname: 'late response' }] })
  await vue.nextTick()
  await vue.nextTick()
  assert.deepEqual(state.onlineResults.value, [])
  assert.equal(state.searchOpen.value, false)
  state.addOpen.value = true
  await vue.nextTick()
  await runTimers()
  assert.equal(searchCount, 1)
  assert.equal(state.form.nickname, 'searching draft')
  state.filtersOpen.value = true
  state.listQuery.value = 'saved local filter'
  state.listPlatform.value = 'weibo'
  state.filtersOpen.value = false
  assert.equal(state.listQuery.value, 'saved local filter')
  assert.equal(state.listPlatform.value, 'weibo')
})

test('Weibo login only follows explicit action and keeps its platform when selection changes', async t => {
  const { state, api, mount } = setupComponent('Subs', t)
  const loginResult = deferred()
  let weiboLogins = 0, douyinLogins = 0, statusChecks = 0
  api.localAuthors = async () => []
  api.weiboAuth = async () => { statusChecks++; return { logged_in: true } }
  api.douyinAuth = async () => ({ logged_in: false })
  api.loginWeibo = async () => { weiboLogins++; return loginResult.promise }
  api.loginDouyin = async () => { douyinLogins++; return { ok: true } }
  await mount()
  state.form.platform = 'weibo'
  await vue.nextTick()
  state.addOpen.value = true
  await vue.nextTick()
  state.addOpen.value = false
  await vue.nextTick()
  await state.loadAuth()
  assert.ok(statusChecks > 0)
  assert.equal(weiboLogins, 0)
  assert.equal(douyinLogins, 0)
  assert.match(state.authLabel.value, /联网有效性待验证/)
  const login = state.login()
  assert.equal(weiboLogins, 1)
  state.form.platform = 'douyin'
  await vue.nextTick()
  loginResult.resolve({ ok: true, logged_in: true })
  await login
  assert.equal(weiboLogins, 1)
  assert.equal(douyinLogins, 0)
  assert.equal(state.wbAuth.value.logged_in, true)
  assert.equal(state.dyAuth.value.logged_in, false)
})

test('continuous library loading appends batches, deduplicates, and preserves items for retry', async t => {
  const { state, api } = setupComponent('Library', t)
  state.rootId.value = 'test-root'
  const requests = []
  api.libraryEntries = async params => { requests.push(params); return { items: [{ id: 'a' }, { id: 'b' }], total: 4, next_cursor: 'next' } }
  await state.loadEntries()
  assert.equal(requests[0].cursor, '')
  assert.equal(requests[0].page_size, 60)
  api.libraryEntries = async params => { requests.push(params); throw new Error('offline') }
  await state.loadEntries({ append: true })
  assert.deepEqual(state.entries.value.map(item => item.id), ['a', 'b'])
  assert.equal(state.nextCursor.value, 'next')
  assert.equal(state.error.value, 'offline')
  api.libraryEntries = async params => { requests.push(params); return { items: [{ id: 'b', updated: true }, { id: 'c' }, { id: 'd' }], total: 4, next_cursor: null } }
  await state.loadEntries({ append: true })
  assert.equal(requests.at(-1).cursor, 'next')
  assert.deepEqual(state.entries.value.map(item => item.id), ['a', 'b', 'c', 'd'])
  assert.equal(state.entries.value[1].updated, true)
  assert.equal(state.hasMore.value, false)
})

test('a late previous author batch cannot overwrite a new author selection', async t => {
  const { state, api } = setupComponent('Library', t)
  state.rootId.value = 'test-root'
  const old = deferred(), latest = deferred(), requests = []
  api.libraryEntries = params => { requests.push(params); return requests.length === 1 ? old.promise : latest.promise }
  state.author.value = 'one'
  const first = state.loadEntries()
  state.author.value = 'two'
  const second = state.loadEntries()
  assert.equal(requests[0].signal.aborted, true)
  latest.resolve({ items: [{ id: 'two' }], total: 1, next_cursor: null })
  await second
  old.resolve({ items: [{ id: 'one' }], total: 1, next_cursor: 'stale' })
  await first
  assert.deepEqual(state.entries.value.map(item => item.id), ['two'])
  assert.equal(state.nextCursor.value, null)
})

test('author list remains independent of collapsed search and appends without duplicate names', async t => {
  const { state, api } = setupComponent('Library', t)
  state.rootId.value = 'test-root'
  api.libraryAuthors = async params => ({ items: params.page === 1 ? [{ name: 'A', count: 2 }] : [{ name: 'A', count: 2 }, { name: 'B', count: 1 }], total: 2 })
  await state.loadAuthors()
  await state.loadAuthors({ append: true })
  assert.deepEqual(state.authors.value.map(item => item.name), ['A', 'B'])
  state.selectAuthor('B')
  assert.equal(state.filters().author, 'B')
  assert.equal(state.filtersOpen.value, false)
})

test('the first library batch stays bounded until scrolling reaches its calculated end', async t => {
  const { state, api } = setupComponent('Library', t)
  let calls = 0
  state.rootId.value = 'test-root'
  await vue.nextTick()
  state.contentEl = { removeEventListener: () => {}, scrollTop: 0, getBoundingClientRect: () => ({ top: 0, bottom: 20000 }), clientHeight: 960 }
  state.gridEl.value = { getBoundingClientRect: () => ({ width: 834, top: 340, bottom: 340 }) }
  window.innerHeight = 960
  state.ready = true
  api.libraryEntries = async () => {
    calls++
    return { items: Array.from({ length: 60 }, (_, index) => ({ id: String(index), sort_at: '2026-10-13' })), total: 100000, next_cursor: 'next' }
  }
  await state.loadEntries()
  for (let count = 0; count < 5; count++) state.maybeLoadMore()
  assert.equal(calls, 1)
  state.contentEl.scrollTop = 100
  state.maybeLoadMore()
  assert.equal(calls, 1)
  const height = state.virtualLayout.value.height
  state.gridEl.value = { getBoundingClientRect: () => ({ width: 834, top: 1000 - height, bottom: 1000 }) }
  state.contentEl.getBoundingClientRect = () => ({ top: 0, bottom: 960 })
  api.libraryEntries = async () => { calls++; return { items: [{ id: 'last' }], total: 61, next_cursor: null } }
  state.maybeLoadMore()
  await vue.nextTick()
  assert.equal(calls, 2)
})

test('leaving during the author debounce cannot restore old entries under a new filter', async t => {
  const original = setupComponent('Library', t)
  original.state.rootId.value = 'test-root'
  original.state.author.value = 'first-author'
  await vue.nextTick()
  original.api.libraryEntries = async () => ({ items: [{ id: 'old-entry' }], total: 5, next_cursor: 'old-cursor' })
  await original.state.loadEntries()
  original.state.ready = true
  original.state.author.value = 'second-author'
  await vue.nextTick()
  assert.ok(original.timers.size > 0, 'the 300ms reload is still pending')
  original.unmount()
  assert.equal(original.store.libraryState.author, 'second-author')
  assert.deepEqual(original.store.libraryState.entries, [])
  assert.equal(original.store.libraryState.nextCursor, null)
  assert.equal(original.store.libraryState.scrollTop, 0)

  const restored = setupComponent('Library', t, original.store.libraryState)
  const requests = []
  restored.api.libraryRoots = async () => ({ items: [{ id: 'test-root' }], default_root_id: 'test-root' })
  restored.api.libraryEntries = async params => { requests.push(params); return { items: [{ id: 'new-entry' }], total: 1, next_cursor: null } }
  restored.api.libraryAuthors = async () => ({ items: [], total: 0 })
  restored.api.libraryScans = async () => ({ items: [] })
  await restored.mount()
  assert.equal(requests.length, 1)
  assert.equal(requests[0].author, 'second-author')
  assert.equal(requests[0].cursor, '')
  assert.deepEqual(restored.state.entries.value.map(item => item.id), ['new-entry'])
})

test('matching cached entries are restored without a first-page refresh', async t => {
  const original = setupComponent('Library', t)
  original.state.rootId.value = 'test-root'
  await vue.nextTick()
  original.api.libraryEntries = async () => ({ items: [{ id: 'saved-entry' }], total: 2, next_cursor: 'saved-cursor' })
  await original.state.loadEntries()
  original.unmount()
  const restored = setupComponent('Library', t, original.store.libraryState)
  restored.api.libraryRoots = async () => ({ items: [{ id: 'test-root' }] })
  restored.api.libraryEntries = () => assert.fail('valid saved entries must retain their continuous position')
  restored.api.libraryAuthors = async () => ({ items: [], total: 0 })
  restored.api.libraryScans = async () => ({ items: [] })
  await restored.mount()
  assert.deepEqual(restored.state.entries.value.map(item => item.id), ['saved-entry'])
  assert.equal(restored.state.nextCursor.value, 'saved-cursor')
})

test('collection updates invalidate cached cards without interrupting playback or accepting a stale response', async t => {
  const original=setupComponent('Library',t)
  original.state.rootId.value='test-root'
  await vue.nextTick()
  original.api.libraryEntries=async()=>({items:[{id:'old-segment'}],total:1,next_cursor:null})
  await original.state.loadEntries()
  original.state.gallery.value=collectionGallery()
  const response=deferred()
  original.api.libraryEntries=()=>response.promise
  const loading=original.state.loadEntries()
  original.store.libraryRevision=1
  original.event({type:'library_updated'})
  response.resolve({items:[{id:'old-segment'}],total:1,next_cursor:null})
  await loading
  assert.equal(original.state.changed.value,true)
  assert.equal(original.state.gallery.value.id,'collection-anchor')
  original.unmount()
  assert.deepEqual(original.store.libraryState.entries,[])
})

test('returning after a background collection update reloads the saved library snapshot', async t => {
  const original=setupComponent('Library',t)
  original.state.rootId.value='test-root'
  await vue.nextTick()
  original.api.libraryEntries=async()=>({items:[{id:'old-segment'}],total:1,next_cursor:null})
  await original.state.loadEntries()
  original.unmount()
  original.store.libraryRevision=1
  const restored=setupComponent('Library',t,{},original.store)
  let requests=0
  restored.api.libraryRoots=async()=>({items:[{id:'test-root'}]})
  restored.api.libraryEntries=async()=>{requests++;return {items:[{id:'collection-anchor'}],total:1,next_cursor:null}}
  restored.api.libraryAuthors=async()=>({items:[],total:0})
  restored.api.libraryScans=async()=>({items:[]})
  await restored.mount()
  assert.equal(requests,1)
  assert.deepEqual(restored.state.entries.value.map(item=>item.id),['collection-anchor'])
})

test('expanding a small library viewport tops up once without following an unfilled cursor chain', async t => {
  const { state, api } = setupComponent('Library', t)
  const previousSize = [window.innerWidth, window.innerHeight]
  t.after(() => { [window.innerWidth, window.innerHeight] = previousSize })
  let width = 834, height = 960
  window.innerWidth = 1366; window.innerHeight = height
  state.rootId.value = 'test-root'
  await vue.nextTick()
  state.contentEl = { removeEventListener: () => {}, scrollTop: 0,
    get clientHeight() { return height }, getBoundingClientRect: () => ({ top: 0, bottom: height }) }
  state.gridEl.value = { getBoundingClientRect: () => ({ width, top: 160, bottom: 160 + state.virtualLayout.value.height }) }
  state.updateViewport()
  const items = Array.from({ length: 60 }, (_, index) => ({ id: String(index), sort_at: '2026-10-13' }))
  const requests = [], refill = deferred()
  api.libraryEntries = params => {
    requests.push(params)
    return requests.length === 1
      ? Promise.resolve({ items, total: 100000, next_cursor: 'after-small-screen' })
      : refill.promise
  }
  await state.loadEntries()
  state.ready = true
  state.maybeLoadMore()
  assert.equal(requests.length, 1)
  assert.equal(requests[0].page_size, 60)

  width = 3260; height = 2160
  window.innerWidth = 3840; window.innerHeight = height
  state.updateViewport()
  assert.ok(state.virtualLayout.value.height + 160 < height, 'the previous 60 items no longer fill the larger viewport')
  state.maybeLoadMore()
  for (let resize = 0; resize < 5; resize++) { state.updateViewport(); state.maybeLoadMore() }
  assert.equal(requests.length, 2, 'repeated resize callbacks share one in-flight request')
  assert.equal(requests[1].cursor, 'after-small-screen')
  assert.ok(requests[1].page_size > 60 && requests[1].page_size <= 100)

  // Even a stale/overlapping response that contributes no new cards must not
  // make a stationary viewport automatically exhaust an arbitrary cursor chain.
  refill.resolve({ items: items.slice(0, 3), total: 100000, next_cursor: 'still-more' })
  for (let flush = 0; flush < 6; flush++) await vue.nextTick()
  for (let resize = 0; resize < 5; resize++) { state.updateViewport(); state.maybeLoadMore() }
  assert.equal(requests.length, 2)
  assert.equal(state.entries.value.length, 60)
  assert.equal(state.nextCursor.value, 'still-more')
  assert.equal(state.contentEl.scrollTop, 0)
})

test('a 60-item cache restored on a larger screen gets one bounded continuation instead of a first-page reload', async t => {
  const original = setupComponent('Library', t)
  original.state.rootId.value = 'test-root'
  await vue.nextTick()
  const items = Array.from({ length: 60 }, (_, index) => ({ id: String(index), sort_at: '2026-10-13' }))
  original.api.libraryEntries = async () => ({ items, total: 100000, next_cursor: 'cached-cursor' })
  await original.state.loadEntries()
  original.unmount()

  const restored = setupComponent('Library', t, original.store.libraryState)
  const previousSize = [window.innerWidth, window.innerHeight]
  const previousQuery = document.querySelector
  t.after(() => { [window.innerWidth, window.innerHeight] = previousSize; document.querySelector = previousQuery })
  window.innerWidth = 3840; window.innerHeight = 2160
  const content = { addEventListener: () => {}, removeEventListener: () => {}, scrollTop: 0,
    clientHeight: 2160, getBoundingClientRect: () => ({ top: 0, bottom: 2160 }) }
  document.querySelector = selector => selector === '.content' ? content : null
  restored.state.gridEl.value = { getBoundingClientRect: () => ({ width: 3260, top: 160, bottom: 160 + restored.state.virtualLayout.value.height }) }
  const requests = []
  restored.api.libraryRoots = async () => ({ items: [{ id: 'test-root' }] })
  restored.api.libraryAuthors = async () => ({ items: [], total: 0 })
  restored.api.libraryScans = async () => ({ items: [] })
  restored.api.libraryEntries = async params => {
    requests.push(params)
    return { items: items.slice(0, 2), total: 100000, next_cursor: 'after-cached-cursor' }
  }
  await restored.mount()
  for (let flush = 0; flush < 6; flush++) await vue.nextTick()
  for (let resize = 0; resize < 5; resize++) restored.state.maybeLoadMore()
  assert.equal(requests.length, 1)
  assert.equal(requests[0].cursor, 'cached-cursor')
  assert.ok(requests[0].page_size > 60 && requests[0].page_size <= 100)
  assert.equal(restored.state.entries.value.length, 60)
})

test('desktop author collapse survives library navigation without changing the selected author', async t => {
  const app = setupComponent('App', t)
  const library = setupComponent('Library', t, {}, app.store)
  assert.equal(app.store.libraryAuthorsCollapsed, false)
  library.state.author.value = 'Selected author'
  app.state.toggleLibraryAuthors()
  await vue.nextTick()
  assert.equal(app.store.libraryAuthorsCollapsed, true)
  assert.equal(library.state.author.value, 'Selected author')
  library.unmount()
  const returned = setupComponent('Library', t, {}, app.store)
  assert.equal(returned.store.libraryAuthorsCollapsed, true)
  assert.equal(returned.state.author.value, 'Selected author')
  app.state.toggleLibraryAuthors()
  assert.equal(app.store.libraryAuthorsCollapsed, false)
  assert.equal(returned.state.author.value, 'Selected author')
})

test('desktop author collapse does not block narrow-window author selection', async t => {
  const app = setupComponent('App', t)
  const library = setupComponent('Library', t, {}, app.store)
  const previousWidth = window.innerWidth
  t.after(() => { window.innerWidth = previousWidth })
  app.state.toggleLibraryAuthors()
  window.innerWidth = 390
  library.state.updateViewport()
  library.state.authorsOpen.value = true
  await vue.nextTick()
  assert.equal(library.state.authorsInline.value, true)
  assert.equal(library.state.authorsOpen.value, true)
  library.state.selectAuthor('Mobile author')
  assert.equal(library.state.author.value, 'Mobile author')
  assert.equal(library.state.authorsOpen.value, false)
  assert.equal(app.store.libraryAuthorsCollapsed, true)
  window.innerWidth = 1366
  library.state.updateViewport()
  assert.equal(library.state.authorsInline.value, false)
  assert.equal(app.store.libraryAuthorsCollapsed, true)
})

test('daily merge updates remove original cards but preserve explicit task lookup', async t => {
  const { state, event } = setupComponent('Tasks', t)
  const original = {id:'live-first',status:'success',metadata:{live:{merged_into:null}}}
  state.tasks.value = [original]
  const updated = {...original,metadata:{live:{merged_into:'daily-result'}}}
  event({type:'task_update',task:updated})
  assert.equal(state.tasks.value.length,0)
  assert.equal(state.changed.value,true)
  state.query.value = 'live-first'
  state.tasks.value = [original]
  event({type:'task_update',task:updated})
  assert.equal(state.tasks.value[0].metadata.live.merged_into,'daily-result')
})

test('daily merge during a pending task query cannot restore the old separate card', async t => {
  const { state, api, event } = setupComponent('Tasks', t)
  const original = {id:'live-first',status:'success',metadata:{live:{merged_into:null}}}
  const result = deferred()
  api.taskPage = () => result.promise
  const loading = state.loadPage()
  event({type:'task_update',task:{...original,metadata:{live:{merged_into:'daily-result'}}}})
  result.resolve({items:[original],next_cursor:null})
  await loading
  assert.equal(state.tasks.value.length,0)
})

test('task details survive queue updates and index completion racing an older page response', async t => {
  const { state, api, event } = setupComponent('Tasks', t)
  const original = {id:'download',status:'running',metadata:{}}
  state.tasks.value = [original]
  const result = deferred()
  api.taskPage = () => result.promise
  const loading = state.loadPage()
  const details = {author:'完整的博主名字 🐿️',author_avatar_url:'/api/subs/7/avatar',display_title:'旅行记录 ☀️',published_at:'2024-02-03T04:34:56Z'}
  event({type:'library_indexed',task_id:original.id,...details})
  event({type:'task_update',task:{...original,status:'success'}})
  result.resolve({items:[original],next_cursor:null})
  await loading
  for (const [key,value] of Object.entries(details)) assert.equal(state.tasks.value[0][key],value)
  event({type:'task_update',task:{...original,status:'success'}})
  assert.equal(taskAuthor(state.tasks.value[0]),details.author)
  assert.equal(taskTitle(state.tasks.value[0]),details.display_title)
  assert.equal(state.tasks.value[0].author_avatar_url,details.author_avatar_url)
  assert.equal(taskContentTime(state.tasks.value[0]).value,details.published_at)
  event({type:'task_update',task:{...original,status:'success',metadata:{subscription:{author:'旧昵称'}}}})
  assert.equal(taskAuthor(state.tasks.value[0]),details.author)
})

test('task content time distinguishes publication, broadcast and recording from download times', () => {
  const task={created_at:'2026-10-21T00:00:00Z',finished_at:'2026-10-21T01:00:00Z'}
  assert.deepEqual(taskContentTime(task),{label:'发布',value:null})
  assert.deepEqual(taskContentTime({...task,published_at:'2024-02-03T04:34:56Z'}),{label:'发布',value:'2024-02-03T04:34:56Z'})
  assert.equal(taskContentTime({...task,metadata:{subscription:{published_at:'2024-02-03T04:34:56Z'}}}).value,'2024-02-03T04:34:56Z')
  const live={...task,metadata:{live:{}},started_at:'2026-10-20T12:00:00Z'}
  assert.deepEqual(taskContentTime(live),{label:'录制开始',value:live.started_at})
  assert.deepEqual(taskContentTime({...live,broadcast_started_at:'2026-10-20T11:00:00Z'}),{label:'开播',value:'2026-10-20T11:00:00Z'})
})

test('tasks identify pending subscriptions and recordings without guessing names from input links', t => {
  setupComponent('Tasks', t)
  const name = '很长但需要完整显示的名字'.repeat(30)
  assert.equal(taskAuthor({metadata:{subscription:{author:name}}}),name)
  assert.equal(taskTitle({metadata:{subscription:{title:'标题 🎉'}}}),'标题 🎉')
  assert.equal(taskAuthor({params:{url:'https://example.invalid/someone'}}),'')
  assert.equal(taskPlatform({script_id:'douyin_live',metadata:{live:{}}}),'douyin')
  assert.equal(taskTitle({metadata:{live:{}}}),'直播录制')
})

test('task pagination reaches fifth page, shows the end, and returns to earlier pages', async t => {
  const {state,api}=setupComponent('Tasks',t)
  api.taskPage=async ({cursor}) => {
    const start=Number(cursor || 0)
    return {items:Array.from({length:Math.min(50,234-start)},(_,i)=>({id:String(start+i),status:'success'})),
      next_cursor:start+50<234 ? String(start+50):null,total:234}
  }
  await state.loadPage()
  for(let i=0;i<4;i++) await state.nextPage()
  assert.equal(state.previous.value.length,4)
  assert.equal(state.tasks.value.length,34)
  assert.equal(state.nextCursor.value,null)
  assert.equal(state.total.value,234)
  await state.nextPage()
  assert.equal(state.previous.value.length,4)
  await state.previousPage()
  assert.equal(state.tasks.value[0].id,'150')
  assert.equal(state.previous.value.length,3)
})
