// Load only the selected recording's byte ranges. The player controls remain native.
const loadEngine = () => import('hls.js/light').then(module => module.default)

export function createRecordingPlayback(video, { infoUrl, nativeUrl, onIndexState }, deps = {}) {
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
    // Allow one cold backend scan (60 s) and contention without opening a huge raw MP4.
    let timeout = setTimeout(() => controller.abort(), 75000)
    const notice = setTimeout(() => { if (!disposed) onIndexState?.('preparing') }, 600)
    let descriptorReady = false
    try {
      let response, backoff = 1000
      while (!controller.signal.aborted) {
        response = await request(infoUrl, { signal: controller.signal })
        if (response.status !== 503 || controller.signal.aborted) break
        await delay(backoff)
        backoff = Math.min(4000, backoff * 2)
      }
      if (controller.signal.aborted || !response?.ok) throw new Error('Playback index unavailable')
      const info = await response.json()
      if (disposed) return false
      if (controller.signal.aborted) throw new Error('Playback timed out')
      descriptorReady = true
      clearTimeout(notice)
      onIndexState?.('')
      clearTimeout(timeout)
      timeout = setTimeout(() => controller.abort(), 10000)
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
        let settled = false, attached = false
        const playable = () => { if (attached && video.readyState >= 3) finish() }
        const finish = () => {
          if (settled) return
          settled = true
          video.removeEventListener('canplay', playable)
          controller.signal.removeEventListener('abort', abort)
          resolve(!disposed)
        }
        const abort = () => { if (!disposed) nativeFallback(); finish() }
        controller.signal.addEventListener('abort', abort, { once: true })
        video.addEventListener('canplay', playable)
        engine.on(Hls.Events.ERROR, (_event, data) => { if (data.fatal) { nativeFallback(); finish() } })
        engine.on(Hls.Events.MEDIA_ATTACHED, () => {
          // Chromium can reload the MediaSource after attachment and interrupt an early play().
          // Let the engine handle buffering errors once attached; closing still cancels the wait.
          attached = true
          clearTimeout(timeout)
          playable()
        })
        engine.loadSource(info.url)
        engine.attachMedia(video)
      })
    } catch {
      if (disposed) return false
      if (!descriptorReady) { onIndexState?.('retry'); return false }
      nativeFallback()
      return true
    } finally { clearTimeout(notice); clearTimeout(timeout) }
  })()
  return { ready, dispose() {
    if (disposed) return
    disposed = true; controller.abort(); cleanRestore()
    video.pause(); engine?.destroy(); engine = null
    video.removeAttribute('src'); video.load()
  } }
}
