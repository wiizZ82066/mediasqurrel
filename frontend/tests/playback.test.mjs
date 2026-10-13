import test from 'node:test'
import assert from 'node:assert/strict'
import { applyDefaultVolume } from '../src/playback.js'

test('new videos use the saved volume, including zero, without changing the preference', () => {
  for (const value of [0, 25, 50, 100]) {
    const media = { muted: false, volume: 1 }
    applyDefaultVolume({ target: media }, value)
    assert.equal(media.volume, value / 100)
    media.volume = 0.7
    const next = { muted: false, volume: 1 }
    applyDefaultVolume({ target: next }, value)
    assert.equal(next.volume, value / 100)
  }
})

test('Live previews remain muted and invalid or absent settings use a safe default', () => {
  for (const media of [{ muted: true, volume: 0 }, { defaultMuted: true, volume: 0 }]) {
    applyDefaultVolume({ target: media }, 80)
    assert.equal(media.volume, 0)
  }
  for (const preference of [undefined, null, NaN, Infinity, '90']) {
    const media = { muted: false, volume: 1 }
    applyDefaultVolume({ target: media }, preference)
    assert.equal(media.volume, 0.5)
  }
  for (const [preference, expected] of [[-1, 0], [101, 1]]) {
    const media = { volume: 0.5 }
    applyDefaultVolume({ target: media }, preference)
    assert.equal(media.volume, expected)
  }
  applyDefaultVolume(null, 50)
})
