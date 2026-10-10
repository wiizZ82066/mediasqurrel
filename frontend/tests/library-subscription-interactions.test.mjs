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
globalThis.window = { matchMedia: () => ({ matches: true }), removeEventListener: () => {} }
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

function setupComponent(name, t) {
  const filename = fileURLToPath(new URL(`../src/views/${name}.vue`, import.meta.url))
  const { descriptor, errors } = parse(fs.readFileSync(filename, 'utf8'), { filename })
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: name })
  const template = compileTemplate({ id: name, filename, source: descriptor.template.content,
    compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  const api = {}, mounted = [], cleanups = [], timers = new Map()
  let timerId = 0
  const store = vue.reactive({ subs: [], libraryState: {} })
  const scope = vue.effectScope()
  const env = {
    ...vue, api, store, toast: () => {}, refreshSubs: async () => {},
    onTaskEvent: () => () => {}, onMounted: fn => mounted.push(fn),
    onBeforeUnmount: fn => cleanups.push(fn),
    useRoute: () => ({ query: {} }), useRouter: () => ({ replace: () => {} }),
    setTimeout: fn => { const id = ++timerId; timers.set(id, fn); return id },
    clearTimeout: id => timers.delete(id),
    AppIcon: {}, PlatformLogo: {}, ActivityProgress: {}, MediaThumbnail: {}, Avatar: {}, SchedulePanel: {},
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
    state, api, store, timers, unmount,
    async mount() { mounted.forEach(fn => fn()); await vue.nextTick() },
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

test('collapsed library controls preserve filters without fetching author/date choices', async t => {
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
  assert.equal(requests, 2)
  state.query.value = 'saved search'
  state.platform.value = 'weibo'
  state.dateFrom.value = '2026-01-01'
  state.filtersOpen.value = false
  await vue.nextTick()
  await state.loadAuxiliary()
  assert.equal(requests, 2)
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
