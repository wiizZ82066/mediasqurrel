import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mediaProfile } from '../src/viewer-presentation.js'

test('viewer uses validated identities and local avatar endpoints, including older Weibo metadata', () => {
  assert.deepEqual(mediaProfile({ platform:'weibo', meta:{'原文链接':'https://weibo.com/123456/post'} }), {
    avatarUrl:'/api/authors/weibo/123456/avatar', sourceUrl:'https://weibo.com/123456/post',
  })
  assert.equal(mediaProfile({ platform:'weibo',meta:{'作者UID':'123456'} }).avatarUrl, '/api/authors/weibo/123456/avatar')
  assert.equal(mediaProfile({ platform:'douyin',meta:{'作者sec_uid':'MS4wLjAB12345678'} }).avatarUrl, '/api/authors/douyin/MS4wLjAB12345678/avatar')
  assert.deepEqual(mediaProfile({ platform:'weibo',author:'123456' }), { avatarUrl:'',sourceUrl:'' })
  assert.deepEqual(mediaProfile(null), { avatarUrl:'',sourceUrl:'' })
})

test('imported metadata cannot create script, credential, private host or mismatched platform links', () => {
  for (const url of ['javascript:alert(1)','file:///private','https://127.0.0.1/private',
    'https://weibo.com.example.invalid/123456/post','https://user:secret@weibo.com/123456/post',
    'https://weibo.com:8443/123456/post','https://www.douyin.com/video/123456','http://weibo.com/123456/post']) {
    assert.equal(mediaProfile({ platform:'weibo',meta:{'原文链接':url} }).sourceUrl, '', url)
  }
  assert.equal(mediaProfile({ platform:'weibo',meta:{'作者UID':'../private'} }).avatarUrl, '')
  assert.equal(mediaProfile({ platform:'douyin',meta:{'作者sec_uid':'MS4wLjAB/../private'} }).avatarUrl, '')
})
