import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createRecordingPlayback } from '../src/recording-playback.js'

const sources = { infoUrl:'/api/library/entries/fixture/playback?rel=recording.mp4', nativeUrl:'/media/recording.mp4' }
const playlist = '/api/library/entries/fixture/playback/playlist.m3u8?rel=recording.mp4&rev=fixture'
const response = value => ({ ok:true, status:200, json:async () => value })
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return {promise, resolve} }

class Video extends EventTarget {
  src = ''; currentTime = 0; duration = 100; paused = true; volume = .09; muted = false; playbackRate = 1.5
  loads = 0; pauses = 0; plays = 0
  canPlayType() { return '' }
  load() { this.loads++ }
  pause() { this.paused = true; this.pauses++ }
  play() { this.paused = false; this.plays++; return Promise.resolve() }
  removeAttribute(name) { if (name === 'src') this.src = '' }
}

function engineDouble({ automatic = true, supported = true } = {}) {
  return class Engine {
    static Events = { ERROR:'error', MEDIA_ATTACHED:'attached' }
    static isSupported() { return supported }
    static instances = []
    constructor(config) { this.config=config; this.listeners={}; this.destroyed=0; this.constructor.instances.push(this) }
    on(name, listener) { this.listeners[name]=listener }
    emit(name, data={}) { this.listeners[name]?.(name,data) }
    loadSource(source) { this.source=source }
    attachMedia(video) { this.video=video; if (automatic) queueMicrotask(() => this.attach()) }
    attach() { this.video.src='blob:fixture'; this.emit('attached') }
    destroy() { this.destroyed++ }
  }
}

test('recordings attach MSE before ready with bounded buffers and preserve native controls/preferences', async () => {
  const video=new Video(), Hls=engineDouble()
  const player=createRecordingPlayback(video,sources,{fetch:async () => response({kind:'hls',url:playlist}),loadEngine:async () => Hls})
  assert.equal(await player.ready,true)
  const engine=Hls.instances[0]
  assert.equal(engine.source,playlist); assert.equal(video.src,'blob:fixture')
  assert.equal(engine.config.maxBufferSize,24*1024*1024); assert.equal(engine.config.maxMaxBufferLength,25)
  assert.equal(video.volume,.09); assert.equal(video.playbackRate,1.5)
  player.dispose(); player.dispose()
  assert.equal(engine.destroyed,1); assert.equal(video.src,''); assert.equal(video.pauses,1)
})

test('closing during descriptor fetch aborts it without starting any media request', async () => {
  const video=new Video(); let signal
  const player=createRecordingPlayback(video,sources,{fetch:(_url,options) => new Promise((_resolve,reject) => {
    signal=options.signal; signal.addEventListener('abort',() => reject(new Error('cancelled')),{once:true})
  })})
  player.dispose()
  assert.equal(await player.ready,false); assert.equal(signal.aborted,true); assert.equal(video.src,'')
})

test('late engine imports cannot start playback after closing or replace the next item', async () => {
  const video=new Video(), pending=deferred(), entered=deferred(), Hls=engineDouble()
  const player=createRecordingPlayback(video,sources,{fetch:async () => response({kind:'hls',url:playlist}),
    loadEngine:() => { entered.resolve(); return pending.promise }})
  await entered.promise; player.dispose(); video.src='next-item.mp4'; pending.resolve(Hls)
  assert.equal(await player.ready,false); assert.equal(Hls.instances.length,0); assert.equal(video.src,'next-item.mp4')
})

test('closing while MSE attaches resolves pending continuation and destroys the engine', async () => {
  const video=new Video(), Hls=engineDouble({automatic:false})
  const player=createRecordingPlayback(video,sources,{fetch:async () => response({kind:'hls',url:playlist}),loadEngine:async () => Hls})
  await new Promise(resolve => setImmediate(resolve))
  player.dispose()
  assert.equal(await player.ready,false); assert.equal(Hls.instances[0].destroyed,1)
  assert.equal(video.src,'')
})

test('fatal playback error falls back once while retaining position, volume and playback rate', async () => {
  const video=new Video(), Hls=engineDouble()
  const player=createRecordingPlayback(video,sources,{fetch:async () => response({kind:'hls',url:playlist}),loadEngine:async () => Hls})
  await player.ready
  video.currentTime=45; video.paused=false
  const engine=Hls.instances[0]; engine.emit('error',{fatal:true}); engine.emit('error',{fatal:true})
  assert.equal(video.src,sources.nativeUrl); assert.equal(engine.destroyed,1)
  video.currentTime=0; video.dispatchEvent(new Event('loadedmetadata'))
  assert.equal(video.currentTime,45); assert.equal(video.plays,1)
  assert.equal(video.volume,.09); assert.equal(video.playbackRate,1.5)
  player.dispose()
})

test('ordinary or unsupported recordings and invalid playlist URLs use the original file', async () => {
  for (const info of [{kind:'native'}, {kind:'hls',url:'https://example.invalid/media.m3u8'},
    {kind:'hls',url:'//example.invalid/media.m3u8'}, {kind:'hls',url:'/api/private'}]) {
    const video=new Video()
    const player=createRecordingPlayback(video,sources,{fetch:async () => response(info),
      loadEngine:async () => { throw new Error('unexpected import') }})
    assert.equal(await player.ready,true); assert.equal(video.src,sources.nativeUrl)
    player.dispose()
  }
})

test('native HLS remains available on browsers without MediaSource', async () => {
  const video=new Video(); video.canPlayType=() => 'maybe'
  const player=createRecordingPlayback(video,sources,{fetch:async () => response({kind:'hls',url:playlist}),
    loadEngine:async () => engineDouble({supported:false})})
  await player.ready; assert.equal(video.src,playlist); player.dispose()
})

test('busy index retries are bounded and do not repeatedly restart native playback', async () => {
  const video=new Video(); let requests=0
  const player=createRecordingPlayback(video,sources,{fetch:async () => { requests++; return {ok:false,status:503} }})
  await player.ready
  assert.equal(requests,3); assert.equal(video.src,sources.nativeUrl); assert.equal(video.loads,1)
  player.dispose()
})
