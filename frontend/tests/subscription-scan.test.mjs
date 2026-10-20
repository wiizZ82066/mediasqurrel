import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { coverageText as scanCoverageText, scanCoverageNotice, scanStatusText, terminalScanKey, scanBadge } from '../src/subscription-scan.js'

import { subscriptionHomepage, newerBatch } from '../src/subscription-homepage.js'
import { liveState } from '../src/live-status.js'

const require = createRequire(new URL('../package.json', import.meta.url))
const { parse, compileScript, compileTemplate } = require('@vue/compiler-sfc')
const vue = require('vue')

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function setupComponent(name, t, sub = { id: 1, platform: 'weibo', interval_minutes: 30 }) {
  const filename = fileURLToPath(new URL(name === 'Subs' ? '../src/views/Subs.vue' : '../src/components/SchedulePanel.vue', import.meta.url))
  const { descriptor, errors } = parse(fs.readFileSync(filename, 'utf8'), { filename })
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: name })
  assert.deepEqual(compileTemplate({ id: name, filename, source: descriptor.template.content,
    compilerOptions: { bindingMetadata: script.bindings } }).errors, [])
  const props = vue.reactive({ sub }), scope = vue.effectScope(), cleanups = [], messages = []
  const api = {
    subSchedules: async () => [], scheduleRuns: async () => ({ items: [] }),
    subScans: async () => ({ items: [] }), scanOptions: async () => ({}),
  }
  const env = { ...vue, api, store: vue.reactive({ subs: [] }),
    useRoute: () => ({query:{}}), liveState, LiveStatus: {},
    onMounted: () => {}, onBeforeUnmount: fn => cleanups.push(fn),
    toast: (...args) => messages.push(args), refreshSubs: async () => {},
    scanCoverageText, scanCoverageNotice, scanStatusText, terminalScanKey, scanBadge, subscriptionHomepage, newerBatch,
    parseDownloadTags: () => ({ tags: [] }), downloadFilterLabel: () => '全部新内容',
    ActivityProgress: {}, CalendarPlanFields: {}, IntervalPicker: {}, AppIcon: {}, PlatformLogo: {}, Avatar: {}, SchedulePanel: {}, SubscriptionFilterPanel: {}, LiveRecordingPanel: {},
  }
  const source = script.content.replace(/^import .*$/gm, '').replace('export default', 'return')
  const component = new Function(...Object.keys(env), source)(...Object.values(env))
  const state = scope.run(() => component.setup(props, { expose: () => {}, emit: () => {} }))
  let disposed = false
  function unmount() {
    if (disposed) return
    disposed = true
    cleanups.forEach(fn => fn())
    scope.stop()
  }
  t.after(unmount)
  return { state, props, api, messages, unmount }
}

async function flush() {
  await vue.nextTick()
  await Promise.resolve()
  await vue.nextTick()
}

test('legacy successful records with incomplete coverage are explicitly partial and platform login guidance is correct', () => {
  const coverage = { complete: false, reason: 'login_required', pages: 1, items: 11 }
  for (const status of ['ok', 'success', 'partial']) assert.equal(scanStatusText(status, coverage), '覆盖不足')
  assert.equal(scanStatusText('failed', coverage), '失败')
  assert.equal(scanStatusText('success', { complete: true }), '完成')
  assert.match(scanCoverageNotice(coverage, 'weibo'), /覆盖不足.*登录微博/)
  assert.match(scanCoverageNotice(coverage, 'douyin'), /登录抖音/)
  assert.match(scanCoverageText(coverage, 'weibo'), /1 页 \/ 11 条/)
  assert.equal(scanCoverageNotice({ complete: true }, 'weibo'), '')
  assert.match(scanCoverageText({ complete: false, reason: 'navigation_failed' }), /主页未能正常打开.*检查网络/)
  assert.match(scanCoverageText({ complete: false, reason: 'network_changed' }), /网络连接发生变化/)
  assert.match(scanCoverageText({ complete: false, reason: 'network_unavailable' }), /网络暂时不可用/)
})

test('name badge waits for verified downloads and disappears on a later empty scan', async t => {
  const {state} = setupComponent('Subs', t)
  const sub = {id:1,scan:{scan_id:'first',status:'running',downloaded_count:1}}
  assert.equal(state.statusBadge(sub).text,'扫描中')
  sub.scan = {scan_id:'first',status:'success',new_count:3}
  assert.equal(state.statusBadge(sub),null)
  sub.scan.downloaded_count = 1
  assert.equal(state.statusBadge(sub).text,'有新内容')
  assert.match(state.statusBadge(sub).title,/成功下载 1 条/)
  sub.scan = {scan_id:'second',status:'success',downloaded_count:0}
  assert.equal(state.statusBadge(sub),null)
  for (const status of ['failed','cancelled','interrupted']) {
    sub.scan = {status,downloaded_count:1}
    assert.equal(state.statusBadge(sub),null)
  }
})

test('partial manual scans show the coverage/login warning in toast and main-card notice', async t => {
  const { state, api, messages } = setupComponent('Subs', t)
  const sub = { id: 1, platform: 'weibo', scan: { status: 'success', coverage: { complete: false, reason: 'login_required' } } }
  api.scanSub = async () => ({ new_items: [], coverage: sub.scan.coverage })
  await state.scanNow(sub)
  assert.match(messages[0][0], /覆盖不足.*登录微博/)
  assert.notEqual(messages[0][1], 'success')
  assert.match(state.scanNotice(sub), /覆盖不足.*登录微博/)
  sub.scan.status = 'running'
  assert.equal(state.scanNotice(sub), '')
  sub.scan.status = 'failed'
  assert.equal(state.scanNotice(sub), '')
})

test('open scan records refresh only for terminal events and keep both paging positions', async t => {
  const { state, api, props } = setupComponent('SchedulePanel', t)
  const requests = []
  api.subSchedules = async () => { requests.push('plans'); return [] }
  api.scheduleRuns = async (id, offset) => { requests.push(['runs', id, offset]); return { items: [] } }
  api.subScans = async (id, offset) => { requests.push(['scans', id, offset]); return { items: [] } }
  state.offset.value = 40
  state.scanOffset.value = 20
  props.sub.scan = { scan_id: 'current', status: 'running', revision: 1 }
  await flush()
  props.sub.scan.revision++
  await flush()
  assert.equal(requests.length, 0)
  props.sub.scan = { scan_id: 'current', status: 'success', revision: 3 }
  await flush()
  assert.deepEqual(requests, ['plans', ['runs', 1, 40], ['scans', 1, 20]])
  props.sub.scan = { scan_id: 'current', status: 'success', revision: 3 }
  await flush()
  assert.equal(requests.length, 3)
  // The final persisted schedule result may arrive as a newer terminal revision.
  props.sub.scan.revision = 4
  await flush()
  assert.equal(requests.length, 6)
  assert.equal(state.offset.value, 40)
  assert.equal(state.scanOffset.value, 20)
})

test('out-of-order record responses cannot restore the previous page', async t => {
  const { state, api } = setupComponent('SchedulePanel', t)
  const old = deferred()
  api.subScans = async (_id, offset) => offset === 0 ? old.promise : { items: [{ id: 'current-page' }] }
  const first = state.load()
  state.scanOffset.value = 20
  const second = state.load()
  await second
  assert.equal(state.scans.value[0].id, 'current-page')
  old.resolve({ items: [{ id: 'outdated-page' }] })
  await first
  assert.equal(state.scans.value[0].id, 'current-page')
  assert.equal(state.loading.value, false)
})

test('each records endpoint failure is visible, with successful sections retained and retry recovery', async t => {
  const { state, api } = setupComponent('SchedulePanel', t)
  state.runs.value = [{ id: 'retained-run' }]
  api.subSchedules = async () => { throw new Error('plans unavailable') }
  api.scheduleRuns = async () => { throw new Error('runs unavailable') }
  api.subScans = async () => { throw new Error('scans unavailable') }
  await state.load()
  assert.match(state.error.value, /扫描计划读取失败：plans unavailable/)
  assert.match(state.error.value, /计划执行记录读取失败：runs unavailable/)
  assert.match(state.error.value, /扫描结果记录读取失败：scans unavailable/)
  assert.equal(state.runs.value[0].id, 'retained-run')
  api.subSchedules = async () => []
  api.scheduleRuns = async () => ({ items: [{ id: 'recovered-run' }] })
  api.subScans = async () => ({ items: [] })
  await state.load()
  assert.equal(state.error.value, '')
  assert.equal(state.runs.value[0].id, 'recovered-run')
})

test('a late successful response cannot clear the current record-load failure', async t => {
  const { state, api } = setupComponent('SchedulePanel', t)
  const old = deferred()
  let calls = 0
  api.subSchedules = () => ++calls === 1 ? old.promise : Promise.reject(new Error('current request failed'))
  const first = state.load()
  await state.load()
  const currentError = state.error.value
  assert.match(currentError, /current request failed/)
  old.resolve([])
  await first
  assert.equal(state.error.value, currentError)
})

test('closing the records panel discards its in-flight result', async t => {
  const { state, api, unmount } = setupComponent('SchedulePanel', t)
  const response = deferred()
  api.subScans = () => response.promise
  const loading = state.load()
  unmount()
  response.resolve({ items: [{ id: 'closed-panel' }] })
  await loading
  assert.deepEqual(state.scans.value, [])
})


test('refresh all deduplicates pending clicks and retains failure feedback for retry', async t => {
  const { state, api, messages } = setupComponent('Subs', t)
  const pending = deferred()
  let calls = 0
  api.refreshAllSubs = () => { calls++; return pending.promise }
  const first = state.scanAll()
  await state.scanAll()
  assert.equal(calls, 1)
  pending.reject(new Error('fixture offline'))
  await first
  assert.equal(state.batchSubmitting.value, false)
  assert.match(messages[0][0], /fixture offline/)
  api.refreshAllSubs = async () => ({ status:'running', total:3, paused:1, queued_ids:[1,2,3], running_ids:[] })
  await state.scanAll()
  assert.equal(state.batchRunning.value, true)
  assert.equal(state.isScanning({id:2}), true)
})
