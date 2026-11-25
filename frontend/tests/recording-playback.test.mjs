import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createGrowingPlayback, createRecordingPlayback } from '../src/recording-playback.js'
import { recordingSeconds } from '../src/live-status.js'

const sources = { infoUrl:'/api/library/entries/fixture/playback?rel=recording.mp4', nativeUrl:'/media/recording.mp4' }
const playlist = '/api/library/entries/fixture/playback/playlist.m3u8?rel=recording.mp4&rev=fixture'

test('home clock ticks between fresh recorder samples and stops on a stall or lost connection', () => {
  const snapshot = {status:'recording',duration_seconds:40.2,progress_age:.1}
  assert.deepEqual([1000,2000,3000,4000].map(now => recordingSeconds(snapshot,1000,now)), [40,41,42,43])
  assert.equal(recordingSeconds(snapshot,1000,80000),44)
  assert.equal(recordingSeconds({...snapshot,status:'stalled'},1000,6000),40)
  assert.equal(recordingSeconds(null,1000,6000,40),40)
})

test('growing player can rewind or follow the edge and closing only disposes local reads', async () => {
  const video = new Video(), Hls = engineDouble()
  video.seekable = {length:1, start:() => 0, end:() => 30}
  const id = 'a'.repeat(32)
  const player = createGrowingPlayback(video, id, {fromStart:true,onState:() => {}}, {
    fetch:() => assert.fail('HLS already requests the initial playlist'),
    loadEngine:async () => Hls,
  })
  await new Promise(resolve => setImmediate(resolve))
  Hls.instances[0].emit('parsed', {levels:[{details:{edge:30}}]})
  assert.equal(Hls.instances[0].position, 0)
  player.seek(false); assert.equal(video.currentTime,29)
  player.seek(true); assert.equal(video.currentTime,0)
  player.dispose()
  assert.deepEqual(Hls.instances[0].sources, ['/api/live/recordings/' + id + '/watch/playlist.m3u8'])
  assert.equal(Hls.instances[0].destroyed,1)
})
const response = value => ({ ok:true, status:200, json:async () => value })
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return {promise, resolve} }

class Video extends EventTarget {
  src = ''; currentTime = 0; duration = 100; paused = true; volume = .09; muted = false; playbackRate = 1.5
  loads = 0; pauses = 0; plays = 0; readyState = 0
  canPlayType() { return '' }
  load() { this.loads++ }
  pause() { this.paused = true; this.pauses++ }
  play() { this.paused = false; this.plays++; return Promise.resolve() }
  removeAttribute(name) { if (name === 'src') this.src = '' }
}

function engineDouble({ automatic = true, supported = true, buffered = true } = {}) {
  return class Engine {
    static Events = { ERROR:'error', MEDIA_ATTACHED:'attached', MANIFEST_PARSED:'parsed' }
    static ErrorDetails = { MANIFEST_LOAD_ERROR:'manifestLoadError' }
    static DefaultConfig = { manifestLoadPolicy:{ default:{maxLoadTimeMs:20000} } }
    static isSupported() { return supported }
    static instances = []
    constructor(config) { this.config=config; this.listeners={}; this.destroyed=0; this.constructor.instances.push(this) }
    on(name, listener) { this.listeners[name]=listener }
    emit(name, data={}) { this.listeners[name]?.(name,data) }
    sources = []
    loadSource(source) { this.source=source; this.sources.push(source) }
    startLoad(position) { this.position=position }
    attachMedia(video) { this.video=video; if (automatic) queueMicrotask(() => this.attach()) }
    attach() { this.video.src='blob:fixture'; this.emit('attached'); if (buffered) this.canPlay() }
    canPlay() { this.video.readyState=3; this.video.dispatchEvent(new Event('canplay')) }
    destroy() { this.destroyed++ }
  }
}

test('live manifest waiting respects the server delay and stops retrying when the viewer closes', async t => {
  t.mock.timers.enable({apis:['setTimeout']})
  const Hls = engineDouble({automatic:false}), states = [], video = new Video()
  const player = createGrowingPlayback(video, 'b'.repeat(32), {fromStart:false, onState:s => states.push(s)}, {loadEngine:async () => Hls})
  await new Promise(resolve => setImmediate(resolve))
  const engine = Hls.instances[0]
  const unavailable = {fatal:true, details:Hls.ErrorDetails.MANIFEST_LOAD_ERROR, response:{code:503},
    networkDetails:{getResponseHeader:() => '1'}}
  engine.emit('error', unavailable)
  assert.equal(states.at(-1),'waiting')
  t.mock.timers.tick(900)
  assert.equal(engine.sources.length, 1)
  t.mock.timers.tick(100)
  assert.equal(engine.sources.length, 2)
  video.seekable = {length:1,start:()=>0,end:()=>40}
  engine.emit('parsed', {levels:[{details:{edge:40}}]})
  assert.equal(engine.position, 39)
  video.currentTime = 39
  engine.canPlay()
  assert.equal(video.currentTime, 39) // Already following the live edge. Keep the first decoded frame.
  engine.emit('error', unavailable)
  player.dispose()
  t.mock.timers.tick(2000)
  assert.equal(engine.sources.length, 2)
})

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

test('collection continuation waits through a media reload until the new segment can actually play', async t => {
  t.mock.timers.enable({apis:['setTimeout']})
  const video=new Video(), Hls=engineDouble({buffered:false})
  const player=createRecordingPlayback(video,sources,{fetch:async () => response({kind:'hls',url:playlist}),loadEngine:async () => Hls})
  const continuation=player.ready.then(canPlay => { if (canPlay) return video.play() })
  await new Promise(resolve => setImmediate(resolve))
  video.readyState=1; video.dispatchEvent(new Event('loadedmetadata'))
  video.readyState=0; video.dispatchEvent(new Event('emptied'))
  t.mock.timers.tick(11000)
  assert.equal(video.plays,0); assert.equal(video.src,'blob:fixture')
  Hls.instances[0].canPlay()
  await continuation
  assert.equal(video.plays,1); assert.equal(video.src,'blob:fixture')
  player.dispose()
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

test('a cold recording after contention waits for its own index without fetching the raw file', async t => {
  t.mock.timers.enable({apis:['setTimeout']})
  const video=new Video(), Hls=engineDouble(), pending=deferred(), states=[]
  let requests=0
  const player=createRecordingPlayback(video,{...sources,onIndexState:value=>states.push(value)},{
    fetch:async () => ++requests===1 ? {ok:false,status:503} : pending.promise,loadEngine:async () => Hls})
  await new Promise(resolve => setImmediate(resolve))
  t.mock.timers.tick(1000)
  await new Promise(resolve => setImmediate(resolve))
  t.mock.timers.tick(15000)
  assert.deepEqual(states,['preparing']); assert.equal(video.loads,0)
  pending.resolve(response({kind:'hls',url:playlist}))
  assert.equal(await player.ready,true); assert.equal(requests,2)
  assert.equal(video.src,'blob:fixture'); assert.deepEqual(states,['preparing',''])
  player.dispose()
})

test('persistent index contention ends with a retry action and no native MP4 request', async t => {
  t.mock.timers.enable({apis:['setTimeout']})
  const video=new Video(), states=[]
  let requests=0
  const player=createRecordingPlayback(video,{...sources,onIndexState:value=>states.push(value)},{
    fetch:async () => { requests++; return {ok:false,status:503} }})
  await new Promise(resolve => setImmediate(resolve))
  t.mock.timers.tick(1000)
  await new Promise(resolve => setImmediate(resolve))
  t.mock.timers.tick(75000)
  assert.equal(await player.ready,false); assert.equal(states.at(-1),'retry')
  assert.equal(requests,2); assert.equal(video.src,''); assert.equal(video.loads,0)
  player.dispose()
})
