// Load only the selected recording's byte ranges. The player controls remain native.
const loadEngine = () => import('hls.js/light').then(module => module.default)

export function createRecordingPlayback(video, { infoUrl, nativeUrl }, deps = {}) {
  const request = deps.fetch || globalThis.fetch
  const controller = new AbortController()
  let disposed = false, engine = null, fallbackUsed = false, restore = null
  const cleanRestore = () => { if (restore) video.removeEventListener('loadedmetadata', restore); restore = null }
  function nativeFallback() {
    if (disposed || fallbackUsed) return
    fallbackUsed = true
    const resume = !video.paused, position = video.currentTime || 0
    engine?.destroy(); engine = null
    cleanRestore()
    if (position || resume) {
      restore = () => {
        if (disposed) return
        if (position && Number.isFinite(video.duration)) video.currentTime = Math.min(position, Math.max(0, video.duration - .05))
        if (resume) video.play().catch(() => {})
        cleanRestore()
      }
      video.addEventListener('loadedmetadata', restore, { once: true })
    }
    video.src = nativeUrl
    video.load()
  }
  function delay(ms) {
    return new Promise(resolve => {
      const done = () => { clearTimeout(timer); controller.signal.removeEventListener('abort', done); resolve() }
      const timer = setTimeout(done, ms)
      controller.signal.addEventListener('abort', done, { once: true })
    })
  }
  const ready = (async () => {
    const timeout = setTimeout(() => controller.abort(), 10000)
    try {
      let response
      for (let attempt = 0; attempt < 3; attempt++) {
        if (controller.signal.aborted) throw new Error('Playback cancelled')
        response = await request(infoUrl, { signal: controller.signal })
        if (response.status !== 503 || attempt === 2) break
        await delay(1000)
      }
      if (!response.ok) throw new Error('Playback index unavailable')
      const info = await response.json()
      if (disposed) return false
      if (controller.signal.aborted) throw new Error('Playback timed out')
      if (info.kind !== 'hls') { nativeFallback(); return true }
      // Only the app's local playlist route can become a media source.
      if (!/^\/api\/library\/entries\/[^/?#]+\/playback\/playlist\.m3u8\?/.test(info.url || '')) {
        throw new Error('Invalid playback playlist')
      }
      const Hls = await (deps.loadEngine || loadEngine)()
      if (disposed) return false
      if (controller.signal.aborted) throw new Error('Playback timed out')
      if (!Hls.isSupported()) {
        if (video.canPlayType('application/vnd.apple.mpegurl')) { video.src = info.url; video.load() }
        else nativeFallback()
        return true
      }
      engine = new Hls({ enableWorker: true, backBufferLength: 15,
        maxBufferLength: 15, maxMaxBufferLength: 25, maxBufferSize: 24 * 1024 * 1024 })
      return await new Promise(resolve => {
        let settled = false
        const finish = () => { if (!settled) { settled = true; controller.signal.removeEventListener('abort', abort); resolve(!disposed) } }
        const abort = () => { if (!disposed) nativeFallback(); finish() }
        controller.signal.addEventListener('abort', abort, { once: true })
        engine.on(Hls.Events.ERROR, (_event, data) => { if (data.fatal) { nativeFallback(); finish() } })
        engine.on(Hls.Events.MEDIA_ATTACHED, finish)
        engine.loadSource(info.url)
        engine.attachMedia(video)
      })
    } catch {
      if (!disposed) nativeFallback()
      return !disposed
    } finally { clearTimeout(timeout) }
  })()
  return { ready, dispose() {
    if (disposed) return
    disposed = true; controller.abort(); cleanRestore()
    video.pause(); engine?.destroy(); engine = null
    video.removeAttribute('src'); video.load()
  } }
}
