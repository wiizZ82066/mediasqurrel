import test from 'node:test'
import assert from 'node:assert/strict'
import { livePresentation, liveDuration, liveSize } from '../src/live-status.js'

test('runtime recording overrides a failed status check and shows real progress',()=>{
  const view=livePresentation({status:'error',runtime_status:'recording',record:{duration_seconds:125,bytes:1024**3,chat:{messages:24}}})
  assert.equal(view.label,'已开播 · 录制中')
  assert.equal(view.active,true)
  assert.equal(view.detail,'0:02:05 · 1.00 GB · 弹幕 24 条')
})
test('empty start, stalled video and failure have distinct recoverable states',()=>{
  assert.equal(livePresentation({runtime_status:'connecting'}).label,'已开播 · 正在连接')
  assert.equal(livePresentation({runtime_status:'stalled'}).tone,'warning')
  assert.equal(livePresentation({status:'error',error:'直播流拒绝访问'}).detail,'直播流拒绝访问')
  assert.equal(livePresentation(null).label,'未开启录播')
  assert.equal(liveDuration(-1),'0:00:00')
  assert.equal(liveSize(0),'0.0 MB')
})
