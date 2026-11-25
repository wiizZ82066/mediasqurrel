// Load only the selected recording's byte ranges. The player controls remain native.
const loadEngine = () => import('hls.js/light').then(module => module.default)

export function prepareRecordingPlayer() {
  // Pointer or keyboard intent only warms the player code. Video stays on disk until clicked.
  loadEngine().catch(() => {})
}

// The recorder owns the writer. This player only requests completed fragments from its local playlist.
export function createGrowingPlayback(video, id, { fromStart, onState }, deps = {}) {
  if (!/^[a-f0-9]{32}$/.test(id)) throw new Error('录播标识无效')
  const url = '/api/live/recordings/' + id + '/watch/playlist.m3u8'
  const controller = new AbortController()
  const { signal } = controller
  let engine, retryTimer, firstPlay = true
  function seek(toStart) {
    if (!video.seekable.length) return
    const start = video.seekable.start(0)
    const end = video.seekable.end(video.seekable.length - 1)
    video.currentTime = toStart ? start : Math.max(start, end - 1)
  }
  function canPlay() {
    if (!firstPlay) return
    firstPlay = false
    onState('')
    // HLS.js has already selected the start or the live edge before decoding its first frame.
    if (!engine) seek(fromStart)
    video.play().catch(() => {})
  }
  function fail(message) {
    if (signal.aborted) return
    clearTimeout(retryTimer)
    engine?.destroy()
    engine = null
    onState(message)
  }
  function waitForPlaylist(retryAfter, retry) {
    const seconds = retryAfter != null && retryAfter !== '' ? Number(retryAfter) : 1
    const delay = Number.isFinite(seconds) ? Math.max(100, Math.min(5000, seconds * 1000)) : 1000
    onState(delay < 1000 ? 'preparing' : 'waiting')
    clearTimeout(retryTimer)
    retryTimer = setTimeout(() => { if (!signal.aborted) retry() }, delay)
  }
  async function startNative() {
    try {
      const response = await (deps.fetch || fetch)(url, {signal, cache:'no-store'})
      await response.body?.cancel()
      if (signal.aborted) return
      if (response.status === 503) {
        waitForPlaylist(response.headers?.get('Retry-After'), startNative)
        return
      }
      if (!response.ok) return fail(response.status === 422 ? 'unsupported' : 'error')
      video.src = url
      video.load()
    } catch { fail('error') }
  }
  async function attach() {
    try {
      const Hls = await (deps.loadEngine || loadEngine)()
      if (signal.aborted) return
      if (Hls.isSupported()) {
        engine = new Hls({ autoStartLoad:false, liveSyncDuration: 3,
          manifestLoadPolicy: { default: { ...Hls.DefaultConfig.manifestLoadPolicy.default, errorRetry: null } },
          backBufferLength: 15, maxBufferLength: 15, maxMaxBufferLength: 30, maxBufferSize: 24 * 1024 * 1024 })
        engine.on(Hls.Events.MANIFEST_PARSED, (_, {levels}) => {
          // Our single media playlist already has the full timeline. Load the requested position directly.
          const end = levels[0]?.details?.edge
          const position = Number.isFinite(end) ? Math.max(0, end - 1) : -1
          engine.startLoad(fromStart ? 0 : position)
        })
        engine.on(Hls.Events.ERROR, (_, data) => {
          if (!data.fatal || signal.aborted) return
          if (data.details === Hls.ErrorDetails.MANIFEST_LOAD_ERROR && data.response?.code === 503) {
            const retryAfter = data.networkDetails?.getResponseHeader?.('Retry-After') ?? data.networkDetails?.headers?.get('Retry-After')
            waitForPlaylist(retryAfter, () => engine?.loadSource(url))
          } else fail(data.response?.code === 422 ? 'unsupported' : 'error')
        })
        engine.loadSource(url)
        engine.attachMedia(video)
      } else if (video.canPlayType('application/vnd.apple.mpegurl')) await startNative()
      else fail('unsupported')
    } catch { fail('error') }
  }
  video.addEventListener('canplay', canPlay)
  attach()
  return { seek, dispose() {
    controller.abort()
    clearTimeout(retryTimer)
    video.removeEventListener('canplay', canPlay)
    video.pause()
    engine?.destroy()
    engine = null
    video.removeAttribute('src')
    video.load()
  } }
}

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
