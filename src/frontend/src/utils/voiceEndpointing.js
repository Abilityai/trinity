/**
 * Endpointing for Workspace Direct calls (trinity-enterprise#1046).
 *
 * Decides where one spoken utterance starts and ends, in the browser, because barge-in
 * and echo rejection need to know what is playing. Pure: it takes frames and a `playing`
 * flag and calls back. It knows nothing about sockets, audio contexts or the clock.
 */

export const VAD = Object.freeze({
  startMs: 220,
  startMsPlayback: 250,
  endMs: 850,
  prerollMs: 400,
  minVoicedMs: 300,
  maxMs: 30000,
})

const FLOOR_START = 0.008
const FLOOR_RISE = 0.03

// Also the mic level the orb moves with while the person speaks.
export function rmsOf(frame) {
  let sum = 0
  for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i]
  return frame.length ? Math.sqrt(sum / frame.length) : 0
}

// trinity-enterprise#1087: over playback the bar follows what is actually playing, not a
// fixed multiple. The mic runs with echo cancellation, so what leaks back tracks the
// output level: a quiet passage or the gap between two sentences must not demand a shout,
// and a loud one must not stop itself. ECHO_K is the fraction of the output level allowed
// to come back; PLAY_MIN the least the bar ever is while playing. Both are starting values
// to be tuned from measurement on speakers and headphones (the issue's AC 1 and 2).
export const ECHO_K = 0.6
export const PLAY_MIN = 0.02
// A caller that does not pass the playback level gets the bar the endpointer had before.
export const PLAY_FALLBACK = 0.045

function idleLimit(floor) {
  return Math.max(0.013, floor * 3)
}

function threshold(playing, floor, playbackLevel) {
  if (!playing) return idleLimit(floor)
  if (typeof playbackLevel !== 'number') return Math.max(PLAY_FALLBACK, floor * 6)
  return Math.max(idleLimit(floor), PLAY_MIN, ECHO_K * playbackLevel)
}

function joinFrames(frames) {
  let total = 0
  for (const f of frames) total += f.length
  const out = new Float32Array(total)
  let at = 0
  for (const f of frames) { out.set(f, at); at += f.length }
  return out
}

/**
 * @param {object} opts
 * @param {number} [opts.sampleRate]
 * @param {(info: {duringPlayback: boolean}) => void} [opts.onSpeechStart]
 * @param {(u: {samples: Float32Array, duringPlayback: boolean, voicedMs: number}) => void} opts.onUtterance
 * @param {(info: {duringPlayback: boolean}) => void} [opts.onDiscard] noise too short to send
 */
export function createEndpointer({ sampleRate = 16000, onSpeechStart, onUtterance, onDiscard, vad = VAD } = {}) {
  const s = {
    inSpeech: false, floor: FLOOR_START, above: 0, below: 0, voiced: 0, total: 0,
    frames: [], preroll: [], prerollMs: 0, duringPlayback: false,
  }

  function finish() {
    const { frames, voiced, duringPlayback } = s
    s.inSpeech = false; s.frames = []; s.above = 0; s.below = 0; s.total = 0; s.voiced = 0
    // Over playback the onset already demanded `startMsPlayback` of voice, so a crisp
    // "stop" that got that far is speech, even if shorter than an idle utterance must be.
    const least = duringPlayback ? Math.min(vad.minVoicedMs, vad.startMsPlayback) : vad.minVoicedMs
    if (voiced < least) { onDiscard?.({ duringPlayback }); return }
    onUtterance?.({ samples: joinFrames(frames), duringPlayback, voicedMs: voiced })
  }

  return {
    push(frame, { playing = false, playbackLevel } = {}) {
      const ms = (frame.length / sampleRate) * 1000
      const rms = rmsOf(frame)
      const limit = threshold(playing, s.floor, playbackLevel)

      if (!s.inSpeech) {
        if (rms < limit) s.floor += (rms - s.floor) * FLOOR_RISE
        s.preroll.push(frame); s.prerollMs += ms
        while (s.prerollMs > vad.prerollMs) {
          s.prerollMs -= (s.preroll.shift().length / sampleRate) * 1000
        }
        s.above = rms > limit ? s.above + ms : 0
        if (s.above >= (playing ? vad.startMsPlayback : vad.startMs)) {
          s.inSpeech = true
          s.duringPlayback = playing
          s.frames = s.preroll.slice(); s.total = s.prerollMs; s.voiced = s.above; s.below = 0
          s.preroll = []; s.prerollMs = 0
          onSpeechStart?.({ duringPlayback: playing })
        }
        return
      }
      s.frames.push(frame); s.total += ms
      if (rms > limit * 0.7) { s.voiced += ms; s.below = 0 } else { s.below += ms }
      if (s.below >= vad.endMs || s.total >= vad.maxMs) finish()
    },
    reset() {
      s.inSpeech = false; s.frames = []; s.preroll = []; s.prerollMs = 0
      s.above = 0; s.below = 0; s.total = 0; s.voiced = 0
    },
    get inSpeech() { return s.inSpeech },
  }
}

export function samplesToPcm16Base64(samples) {
  const bytes = new Uint8Array(samples.length * 2)
  const view = new DataView(bytes.buffer)
  for (let i = 0; i < samples.length; i++) {
    const v = Math.max(-1, Math.min(1, samples[i]))
    view.setInt16(i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true)
  }
  let binary = ''
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i])
  return btoa(binary)
}
