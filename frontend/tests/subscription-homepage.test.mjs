import assert from 'node:assert/strict'
import test from 'node:test'
import { subscriptionHomepage, newerBatch } from '../src/subscription-homepage.js'

test('profile links derive only from known platforms and valid identities', () => {
  assert.equal(subscriptionHomepage({platform:'weibo',blogger_id:'12345',homepage:'javascript:alert(1)'}), 'https://weibo.com/u/12345')
  assert.equal(subscriptionHomepage({platform:'douyin',blogger_id:'MS4w-test'}), 'https://www.douyin.com/user/MS4w-test')
  for (const blogger_id of ['', '../escape', 'javascript:evil', 'user?next=other', '//other']) {
    assert.equal(subscriptionHomepage({platform:'douyin',blogger_id}), '')
  }
  assert.equal(subscriptionHomepage({platform:'unknown',blogger_id:'123'}), '')
})

test('a delayed batch response cannot reverse a newer terminal event', () => {
  const current={started_at:'2030-01-01',revision:5,status:'success'}
  assert.equal(newerBatch(current,{...current,revision:1,status:'running'}),current)
  assert.equal(newerBatch(current,{started_at:'2029-01-01',revision:20}),current)
})
