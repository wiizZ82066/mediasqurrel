import { planDraft, planPayload, addLiveTime } from '../src/schedule-ui.js'
import { liveLabels, liveState } from '../src/live-status.js'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import { test } from 'node:test'

const require = createRequire(new URL('../package.json', import.meta.url))
const { parse, compileScript, compileTemplate } = require('@vue/compiler-sfc')
const vue = require('vue')
function setup(t) {
  const filename = new URL('../src/components/LiveRecordingPanel.vue', import.meta.url)
  const { descriptor, errors } = parse(fs.readFileSync(filename, 'utf8'))
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: 'live-test' })
  assert.deepEqual(compileTemplate({ id:'live-test',source:descriptor.template.content,
    compilerOptions:{bindingMetadata:script.bindings} }).errors, [])
  const mounted=[], cleanups=[], notifications=[]
  const api={liveStatus:async()=>({monitor:{room_url:'https://live.douyin.com/123',enabled:1,interval_seconds:60},records:[]})}
  const env={...vue,planDraft,planPayload,addLiveTime,liveLabels,liveState,api,toast:(...args)=>notifications.push(args),CalendarPlanFields:{},IntervalPicker:{},AppIcon:{},ActivityProgress:{},
    onMounted:fn=>mounted.push(fn),onBeforeUnmount:fn=>cleanups.push(fn),setInterval:()=>1,clearInterval:()=>{}}
  const component=new Function(...Object.keys(env), script.content.replace(/^import .*$/gm,'').replace('export default','return'))(...Object.values(env))
  const scope=vue.effectScope()
  const state=scope.run(()=>component.setup({sub:{id:1}},{expose:()=>{},emit:()=>{}}))
  t.after(()=>{cleanups.forEach(fn=>fn());scope.stop()})
  return {state,api,mounted,cleanups,notifications}
}

test('live polling refreshes progress without replacing an edited room or interval',async t=>{
  const {state,api}=setup(t)
  await state.load(true)
  state.room.value='https://live.douyin.com/456';state.interval.value=120
  api.liveStatus=async()=>({monitor:{room_url:'https://live.douyin.com/123',enabled:1},records:[{status:'recording',bytes:500}]})
  await state.load()
  assert.equal(state.room.value,'https://live.douyin.com/456')
  assert.equal(state.interval.value,120)
  assert.equal(state.active.value,true)
})

test('stop switches off auto recording and surfaces errors without losing the saved history',async t=>{
  const {state,api,notifications}=setup(t)
  await state.load(true)
  api.stopLive=async()=>({stopping:true})
  await state.stop()
  assert.equal(state.enabled.value,false)
  api.stopLive=async()=>{throw new Error('fixture failure')}
  await state.stop()
  assert.equal(state.saving.value,false)
  assert.equal(notifications.at(-1)[0],'fixture failure')
})

test('closing the live panel discards a delayed status response',async t=>{
  const {state,api,cleanups}=setup(t)
  let resolve
  api.liveStatus=()=>new Promise(yes=>{resolve=yes})
  const pending=state.load()
  cleanups.forEach(fn=>fn())
  resolve({monitor:{status:'recording'},records:[{status:'recording'}]})
  await pending
  assert.equal(state.monitor.value,null)
  assert.deepEqual(state.records.value,[])
})

test('adding a recording time expands the unified plan and preserves existing options until save',async t=>{
  const {state,api}=setup(t)
  await state.load(true)
  state.calendarEnabled.value=true
  state.draft.value={...state.draft.value,times:'09:00',kind:'weekly',weekdays:[1,3]}
  let saves=0
  api.saveLive=async()=>{saves++}
  assert.equal(state.planOpen.value,false)
  state.addTime({started_at:'2026-10-20T13:41:40Z'})
  assert.equal(state.planOpen.value,true)
  assert.equal(state.intervalEnabled.value,true)
  assert.equal(state.calendarEnabled.value,true)
  assert.equal(state.draft.value.times,'09:00, 21:41')
  assert.equal(state.draft.value.kind,'weekly')
  assert.deepEqual(state.draft.value.weekdays,[1,3])
  assert.match(state.planNotice.value,/历史记录未保存真实开播时间/)
  assert.equal(saves,0)
})
