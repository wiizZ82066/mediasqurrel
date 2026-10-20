import test from 'node:test'
import assert from 'node:assert/strict'
import {addLiveTime,planDraft,planPayload} from '../src/schedule-ui.js'

test('broadcast time uses the chosen timezone, preserves recurrence and deduplicates',()=>{
  const draft=planDraft({kind:'weekly',weekdays:[1,4],times:['09:00'],timezone:'Asia/Shanghai'})
  const record={broadcast_started_at:'2026-10-19T12:30:00Z',started_at:'2026-10-19T12:45:00Z'}
  const next=addLiveTime(draft,record)
  assert.deepEqual(planPayload(next).times,['09:00','20:30'])
  assert.equal(next.kind,'weekly')
  assert.deepEqual(next.weekdays,[1,4])
  assert.equal(addLiveTime(next,record).times,next.times)
})
test('legacy recording time is an explicit fallback and a new plan drops the placeholder time',()=>{
  const next=addLiveTime(planDraft(),{started_at:'2026-10-19T12:45:00Z'},false)
  assert.equal(next.times,'20:45')
  assert.throws(()=>addLiveTime(planDraft(),{}))
})
