// Apply the saved starting volume once metadata is ready; never persist a
// player's temporary volume adjustment over the user's default preference.
export function applyDefaultVolume(event, preference) {
  const media = event?.target
  if (!media || media.muted || media.defaultMuted) return
  const volume = typeof preference === 'number' && Number.isFinite(preference)
    ? Math.min(100, Math.max(0, preference)) : 50
  media.volume = volume / 100
}
