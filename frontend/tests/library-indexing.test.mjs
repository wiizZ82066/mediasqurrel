import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'
import { api as realApi } from '../src/api.js'

const require = createRequire(new URL('../package.json', import.meta.url))
const { parse, compileScript, compileTemplate } = require('@vue/compiler-sfc')
const vue = require('vue')
const pending = { status: 'indexing', message: '下载已完成，正在同步媒体库索引…', retry_after: 1 }
const response = (status, data) => ({ status, ok: status < 400, statusText: String(status), json: async () => data })
const completed = id => ({ id, script_id: 'weibo', status: 'success' })
const entry = id => ({ id, gallery: [], root_id: 'root', rel_dir: 'author/date' })
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve() }

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

// Run actual SFC setup/watchers; no server, browser profile, downloads or media files.
function component(name, t) {
  const filename = fileURLToPath(new URL('../src/views/' + name + '.vue', import.meta.url))
  const { descriptor, errors } = parse(fs.readFileSync(filename, 'utf8'), { filename })
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: name })
  const template = compileTemplate({ id: name, filename, source: descriptor.template.content,
    compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  const api = {}, listeners = new Set(), cleanups = [], mounted = [], pushed = [], notices = []
  const route = vue.reactive({ query: {}, fullPath: '/library' })
  const store = vue.reactive({
    libraryState: {}, wsConnected: true,
    downloadState: { selectedId: 'weibo', drafts: {}, task: { ...completed('task-a'), status: 'running' } },
    taskListState: { items: [], previous: [], q: '', status: '', cursor: '', nextCursor: null },
  })
  const scope = vue.effectScope()
  let nextTimer = 0
  const timers = new Map()
  const timer = callback => { const id = ++nextTimer; timers.set(id, callback); return id }
  const env = {
    ...vue, api, store, toast: (...args) => notices.push(args),
    upsertTaskSummary: () => {}, removeTaskSummary: () => {},
    onTaskEvent: listener => { listeners.add(listener); return () => listeners.delete(listener) },
    onMounted: callback => mounted.push(callback), onBeforeUnmount: callback => cleanups.push(callback),
    useRoute: () => route, useRouter: () => ({ push: value => pushed.push(value), replace: () => {} }),
    document: { querySelector: () => null, activeElement: null },
    window: { addEventListener: () => {}, removeEventListener: () => {} },
    Image: class { set src(value) {} },
    setTimeout: timer, clearTimeout: id => timers.delete(id),
    setInterval: timer, clearInterval: id => timers.delete(id),
    AppIcon: {}, PlatformLogo: {}, ActivityProgress: {}, MediaThumbnail: {},
  }
  const source = script.content.replace(/^import .*$/gm, '').replace('export default', 'return')
  const setup = new Function(...Object.keys(env), source)(...Object.values(env)).setup
  const state = scope.run(() => setup({}, { expose: () => {} }))
  let disposed = false
  const unmount = () => {
    if (disposed) return
    disposed = true
    cleanups.forEach(callback => callback())
    scope.stop()
    timers.clear()
  }
  t.after(unmount)
  return { state, api, route, pushed, notices, store, unmount,
    emit: event => listeners.forEach(listener => listener(event)),
    mount: async () => { for (const callback of mounted) await callback() },
  }
}

test('library locate waits through 202 responses and returns the first ready entry without manual retry', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const urls = [], messages = []
  t.mock.method(globalThis, 'fetch', async url => {
    urls.push(url)
    return urls.length < 3 ? response(202, pending) : response(200, { entry_id: 'entry-a' })
  })
  const located = realApi.libraryLocate({ task_id: 'task-a' }, undefined, { onIndexing: message => messages.push(message) })
  await flush()
  assert.equal(urls.length, 1)
  t.mock.timers.tick(1000)
  await flush()
  assert.equal(urls.length, 2)
  t.mock.timers.tick(1000)
  assert.deepEqual(await located, { entry_id: 'entry-a' })
  assert.equal(messages.length, 2)
  assert.ok(urls.every(url => url === '/api/library/locate?task_id=task-a'))
  t.mock.timers.tick(60000)
  assert.equal(urls.length, 3)
})

test('genuine 404 and network failures do not poll or masquerade as indexing', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let calls = 0
  const fetch = t.mock.method(globalThis, 'fetch', async () => { calls++; return response(404, { detail: '未找到存档' }) })
  await assert.rejects(realApi.libraryLocate({ task_id: 'missing' }), error => error.status === 404 && error.message === '未找到存档')
  t.mock.timers.tick(60000)
  assert.equal(calls, 1)
  fetch.mock.mockImplementation(async () => { calls++; throw new TypeError('network unavailable') })
  await assert.rejects(realApi.libraryLocate({ task_id: 'offline' }), /network unavailable/)
  t.mock.timers.tick(60000)
  assert.equal(calls, 2)
})

test('pending locate has a bounded wait and keeps an indexing-specific timeout', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let calls = 0
  t.mock.method(globalThis, 'fetch', async () => { calls++; return response(202, pending) })
  const located = realApi.libraryLocate({ task_id: 'slow' })
  const rejected = assert.rejects(located, error => error.code === 'LIBRARY_INDEXING' && /仍在同步/.test(error.message))
  await flush()
  t.mock.timers.tick(30000)
  await rejected
  const stoppedAt = calls
  t.mock.timers.tick(60000)
  await flush()
  assert.equal(calls, stoppedAt)
})

test('aborting a pending locate cancels its delay and sends no later request', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let calls = 0
  t.mock.method(globalThis, 'fetch', async () => { calls++; return response(202, pending) })
  const controller = new AbortController()
  const located = realApi.libraryLocate({ task_id: 'cancelled' }, controller.signal)
  const rejected = assert.rejects(located, { name: 'AbortError' })
  await flush()
  controller.abort()
  await rejected
  t.mock.timers.tick(60000)
  await flush()
  assert.equal(calls, 1)
  await assert.rejects(realApi.libraryLocate({ task_id: 'already-cancelled' }, controller.signal), { name: 'AbortError' })
  assert.equal(calls, 1)
})

test('Download success automatically resolves pending indexing once, despite repeated events and reconnect', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const { state, api, emit } = component('Download', t)
  let locateCount = 0, detailCount = 0
  t.mock.method(globalThis, 'fetch', async url => {
    if (url.startsWith('/api/library/locate')) {
      locateCount++
      return locateCount === 1 ? response(202, pending) : response(200, { entry_id: 'entry-a' })
    }
    if (url === '/api/library/entries/entry-a') { detailCount++; return response(200, entry('entry-a')) }
    assert.fail('Unexpected request ' + url)
  })
  api.libraryLocate = realApi.libraryLocate
  api.libraryEntry = realApi.libraryEntry
  api.task = async () => completed('task-a')
  emit({ type: 'task_update', task: completed('task-a') })
  await flush()
  assert.match(state.previewMessage.value, /正在同步/)
  assert.equal(state.previewError.value, '')
  emit({ type: 'task_update', task: completed('task-a') })
  emit({ type: 'library_indexed', task_id: 'task-a' })
  emit({ type: 'reconnect' })
  await flush()
  assert.equal(locateCount, 1)
  t.mock.timers.tick(1000)
  await flush()
  assert.equal(state.entry.value.id, 'entry-a')
  assert.equal(state.previewLoading.value, false)
  assert.equal(state.previewError.value, '')
  assert.equal(detailCount, 1)
})

test('Download cancels platform/new-task/unmount requests and ignores late locate or detail responses', async t => {
  const { state, api, unmount } = component('Download', t)
  const first = deferred(), second = deferred(), third = deferred(), detail = deferred()
  const calls = [], details = []
  api.libraryLocate = (params, signal) => {
    calls.push({ params, signal })
    return [first, second, third][calls.length - 1].promise
  }
  api.libraryEntry = (id, signal) => { details.push({ id, signal }); return detail.promise }
  state.task.value = completed('task-a')
  const old = state.loadPreview()
  state.select('douyin')
  assert.equal(calls[0].signal.aborted, true)
  first.resolve({ entry_id: 'old-entry' })
  await old
  assert.equal(details.length, 0)
  state.task.value = { ...completed('task-b'), script_id: 'douyin' }
  const next = state.loadPreview()
  second.resolve({ entry_id: 'entry-b' })
  await flush()
  assert.equal(details.length, 1)
  state.task.value = { ...completed('task-c'), script_id: 'douyin' }
  const latest = state.loadPreview()
  assert.equal(details[0].signal.aborted, true)
  detail.resolve(entry('entry-b'))
  await next
  assert.equal(state.entry.value, null)
  unmount()
  assert.equal(calls[2].signal.aborted, true)
  third.resolve({ entry_id: 'entry-c' })
  await latest
  assert.equal(details.length, 1)
  assert.equal(state.entry.value, null)
})

test('Download resumes after a long indexing wait when its own ready event arrives', async t => {
  const { state, api, emit } = component('Download', t)
  let calls = 0
  api.libraryLocate = async () => {
    calls++
    if (calls === 1) throw Object.assign(new Error('媒体库索引仍在同步'), { code: 'LIBRARY_INDEXING' })
    return { entry_id: 'entry-a' }
  }
  api.libraryEntry = async () => entry('entry-a')
  state.task.value = completed('task-a')
  await state.loadPreview()
  assert.match(state.previewMessage.value, /仍在同步/)
  assert.equal(state.previewError.value, '')
  emit({ type: 'library_indexed', task_id: 'another-task' })
  assert.equal(calls, 1)
  emit({ type: 'library_indexed', task_id: 'task-a' })
  await flush()
  assert.equal(calls, 2)
  assert.equal(state.entry.value.id, 'entry-a')
})

test('a stale Download REST snapshot cannot reverse a newer success event', async t => {
  const { state, api } = component('Download', t)
  const stale = deferred()
  api.task = () => stale.promise
  api.libraryLocate = async () => ({ entry_id: 'entry-a' })
  api.libraryEntry = async () => entry('entry-a')
  const refresh = state.refreshTrackedTask()
  state.updateTask(completed('task-a'))
  stale.resolve({ ...completed('task-a'), status: 'running' })
  await refresh
  await flush()
  assert.equal(state.task.value.status, 'success')
  assert.equal(state.entry.value.id, 'entry-a')
})

test('Tasks waits visibly, deduplicates clicks, and only the newest live request can navigate', async t => {
  const { state, api, pushed, unmount } = component('Tasks', t)
  const first = deferred(), second = deferred(), third = deferred()
  const calls = []
  api.libraryLocate = (params, signal, { onIndexing }) => {
    calls.push({ params, signal })
    onIndexing(pending.message)
    return [first, second, third][calls.length - 1].promise
  }
  const old = state.gotoLibrary(completed('task-a'))
  await state.gotoLibrary(completed('task-a'))
  assert.equal(calls.length, 1)
  assert.match(state.locateNotice.value, /正在同步/)
  const next = state.gotoLibrary(completed('task-b'))
  assert.equal(calls[0].signal.aborted, true)
  first.resolve({ entry_id: 'old-entry' })
  await old
  assert.equal(pushed.length, 0)
  second.resolve({ entry_id: 'entry-b' })
  await next
  assert.deepEqual(pushed, [{ path: '/library', query: { entry_id: 'entry-b' } }])
  const last = state.gotoLibrary(completed('task-c'))
  unmount()
  assert.equal(calls[2].signal.aborted, true)
  third.resolve({ entry_id: 'entry-c' })
  await last
  assert.equal(pushed.length, 1)
})

test('Library route changes discard stale locates and closing cancels late details', async t => {
  const { state, api, route } = component('Library', t)
  const first = deferred(), second = deferred(), detail = deferred()
  const calls = [], details = []
  api.libraryLocate = (params, signal, { onIndexing }) => {
    calls.push({ params, signal })
    onIndexing(pending.message)
    return calls.length === 1 ? first.promise : second.promise
  }
  api.libraryEntry = (id, signal) => { details.push({ id, signal }); return detail.promise }
  route.query = { task_id: 'task-a' }; route.fullPath = '/library?task_id=task-a'
  const old = state.locateRoute()
  assert.match(state.locateMessage.value, /正在同步/)
  route.query = { task_id: 'task-b' }; route.fullPath = '/library?task_id=task-b'
  const next = state.locateRoute()
  assert.equal(calls[0].signal.aborted, true)
  first.resolve({ entry_id: 'old-entry' })
  await old
  assert.equal(details.length, 0)
  second.resolve({ entry_id: 'entry-b' })
  await flush()
  assert.equal(details[0].id, 'entry-b')
  state.closeGallery()
  assert.equal(details[0].signal.aborted, true)
  detail.resolve(entry('entry-b'))
  await next
  assert.equal(state.gallery.value, null)
  assert.equal(state.detailError.value, '')
})

test('Library deep link recovers automatically after indexing timeout and matching completion', async t => {
  const { state, api, route, mount, emit } = component('Library', t)
  api.libraryRoots = async () => ({ items: [], default_root_id: null })
  api.libraryScans = async () => ({ items: [] })
  await mount()
  let calls = 0
  api.libraryLocate = async () => {
    calls++
    if (calls === 1) throw Object.assign(new Error('媒体库索引仍在同步'), { code: 'LIBRARY_INDEXING' })
    return { entry_id: 'entry-a' }
  }
  api.libraryEntry = async () => entry('entry-a')
  route.query = { task_id: 'task-a' }; route.fullPath = '/library?task_id=task-a'
  await flush()
  assert.match(state.detailNotice.value, /仍在同步/)
  assert.equal(state.detailError.value, '')
  emit({ type: 'library_indexed', task_id: 'another-task' })
  assert.equal(calls, 1)
  emit({ type: 'library_indexed', task_id: 'task-a' })
  await flush()
  assert.equal(calls, 2)
  assert.equal(state.gallery.value.id, 'entry-a')
})
