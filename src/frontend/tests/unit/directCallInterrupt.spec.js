// @vitest-environment jsdom
/**
 * trinity-enterprise#1087: interrupting the agent on a Direct call.
 * The pure rules (stop words, Escape), the endpointer over playback, the player's level,
 * and the composable's stop protocol. The mounted conversation is in
 * `portalDirectCall.mount.spec.js`.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { isStopUtterance } from '../../src/components/portal/portalDirectCall'
import { createEndpointer, VAD } from '../../src/utils/voiceEndpointing'
import { directEscapeAction } from '../../src/utils/turnCancel'
import { useDirectVoiceSession } from '../../src/composables/useDirectVoiceSession'
import { createAudioPlayer } from '../../src/utils/audio'

describe('a stop word', () => {
  it.each(['stop', 'Stop.', 'wait', 'Hold on!', 'hang on', 'ok stop', 'never mind', 'Shh', 'stop please', 'be quiet', "that's enough"])(
    '"%s" stops the agent', (t) => expect(isStopUtterance(t)).toBe(true))
  it.each(['stop the build', 'wait for the tests then merge', 'hold on to that file', 'yes', 'no', '', 'Actually, stop doing that and run the tests.'])(
    '"%s" is words for the agent', (t) => expect(isStopUtterance(t)).toBe(false))
})

describe('Escape on a Direct call', () => {
  const key = (extra = {}) => ({ key: 'Escape', defaultPrevented: false, isComposing: false, ...extra })
  it('silences the agent while it is speaking, and never cancels the turn', () => {
    expect(directEscapeAction(key(), { speaking: true })).toBe('silence')
  })
  it('ends the call when nothing is playing', () => {
    expect(directEscapeAction(key(), { speaking: false })).toBe('end')
  })
  it('yields to an overlay that owns Escape, and to a claimed key', () => {
    expect(directEscapeAction(key(), { speaking: true, overlays: [true] })).toBeNull()
    expect(directEscapeAction(key(), { speaking: false, overlays: [false, true] })).toBeNull()
    expect(directEscapeAction(key({ defaultPrevented: true }), { speaking: true })).toBeNull()
    expect(directEscapeAction({ ...key(), key: 'Enter' }, { speaking: true })).toBeNull()
  })
})

describe('the endpointer over playback', () => {
  const MS = 20
  const FRAME = (16000 * MS) / 1000
  const at = (level) => () => new Float32Array(FRAME).fill(level)
  const feed = (ep, frame, n, opts) => { for (let i = 0; i < n; i++) ep.push(frame(), opts) }
  const framesFor = (ms) => Math.ceil(ms / MS)

  it('hears a normal voice over quiet playback', () => {
    const onSpeechStart = vi.fn()
    const ep = createEndpointer({ onSpeechStart, onUtterance: vi.fn() })
    feed(ep, at(0.03), framesFor(VAD.startMsPlayback), { playing: true, playbackLevel: 0.002 })
    expect(onSpeechStart).toHaveBeenCalledWith({ duringPlayback: true })
  })
  it('does not take loud playback coming back through the mic as speech', () => {
    const onSpeechStart = vi.fn()
    const ep = createEndpointer({ onSpeechStart, onUtterance: vi.fn() })
    feed(ep, at(0.03), 50, { playing: true, playbackLevel: 0.2 })
    expect(onSpeechStart).not.toHaveBeenCalled()
  })
  it('starts within the playback onset time', () => {
    expect(VAD.startMsPlayback).toBeLessThanOrEqual(250)
    const onSpeechStart = vi.fn()
    const ep = createEndpointer({ onSpeechStart, onUtterance: vi.fn() })
    feed(ep, at(0.2), framesFor(VAD.startMsPlayback) - 1, { playing: true })
    expect(onSpeechStart).not.toHaveBeenCalled()
    feed(ep, at(0.2), 1, { playing: true })
    expect(onSpeechStart).toHaveBeenCalledTimes(1)
  })
  it('sends a crisp "stop" said over the agent, even when it is shorter than an idle utterance must be', () => {
    const onUtterance = vi.fn()
    const onDiscard = vi.fn()
    const ep = createEndpointer({ onUtterance, onDiscard })
    feed(ep, at(0.2), framesFor(VAD.startMsPlayback), { playing: true })
    feed(ep, at(0.0001), framesFor(VAD.endMs), { playing: true })
    expect(onDiscard).not.toHaveBeenCalled()
    expect(onUtterance).toHaveBeenCalledTimes(1)
    expect(onUtterance.mock.calls[0][0].duringPlayback).toBe(true)
  })
})

describe('the player level', () => {
  afterEach(() => vi.unstubAllGlobals())

  function fakeContext(level) {
    const ctx = {
      state: 'running', currentTime: 0, destination: {},
      createAnalyser: () => ({
        fftSize: 0, smoothingTimeConstant: 0, frequencyBinCount: 128, connect() {},
        getFloatTimeDomainData: (arr) => arr.fill(level.value),
        getByteFrequencyData: (arr) => arr.fill(255),
      }),
      createBuffer: (_c, n) => ({ duration: n / 24000, getChannelData: () => new Float32Array(n) }),
      createBufferSource: () => ({ connect() {}, start() {} }),
      suspend: vi.fn(async () => { ctx.state = 'suspended' }),
      resume: vi.fn(async () => { ctx.state = 'running' }),
      close: vi.fn(async () => {}),
    }
    return ctx
  }

  it('is the linear RMS of what is playing, and nothing while paused', () => {
    const level = { value: 0.1 }
    vi.stubGlobal('AudioContext', function () { return fakeContext(level) })
    const player = createAudioPlayer()
    expect(player.getLevel()).toBe(0)
    player.play(btoa('\x00\x00\x00\x00'))
    expect(player.getLevel()).toBeCloseTo(0.1, 5)
    player.pause()
    expect(player.getLevel()).toBe(0)
    player.resume()
    expect(player.getLevel()).toBeCloseTo(0.1, 5)
  })
  it('holds a peak briefly, so the gap between two words does not drop the bar', () => {
    const level = { value: 0.2 }
    let t = 1000
    vi.stubGlobal('AudioContext', function () { return fakeContext(level) })
    const player = createAudioPlayer({ now: () => t })
    player.play(btoa('\x00\x00\x00\x00'))
    expect(player.getLevel()).toBeCloseTo(0.2, 5)
    level.value = 0.01
    t += 100
    expect(player.getLevel()).toBeCloseTo(0.2, 5)
    t += 300
    expect(player.getLevel()).toBeCloseTo(0.01, 5)
  })
})

describe('the stop protocol', () => {
  function session(overrides = {}) {
    setActivePinia(createPinia())
    const hooks = { onTranscript: vi.fn(), onBargeIn: vi.fn(), onStop: vi.fn(), onAnswer: vi.fn(), ...overrides }
    const call = useDirectVoiceSession(hooks)
    // A call that is on: idle state changes are guarded on `active`.
    call.active.value = true
    return { hooks, call }
  }
  const hear = (call, seq, text) => call._handleFrame({ type: 'transcript_live', seq, text })
  const thinking = (call, id = 'e1') => call._handleFrame({ type: 'speak_state', state: 'thinking', execution_id: id })

  it('a stop word stops the running turn and is never sent as a turn', () => {
    const { hooks, call } = session()
    thinking(call)
    hear(call, 1, 'Stop.')
    expect(hooks.onBargeIn).toHaveBeenCalledWith({ executionId: 'e1' })
    expect(hooks.onStop).toHaveBeenCalledTimes(1)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    expect(call.status.value).toBe('listening')
    expect(call.notice.value).toBe('Stopped.')
  })
  it('with nothing to stop, the same words go to the agent', () => {
    const { hooks, call } = session()
    hear(call, 1, 'Wait.')
    expect(hooks.onStop).not.toHaveBeenCalled()
    expect(hooks.onTranscript).toHaveBeenCalledWith('Wait.')
  })
  it('a spoken turn still being sent counts as something to stop', () => {
    const { hooks, call } = session({ isBusy: () => true })
    hear(call, 1, 'never mind')
    expect(hooks.onStop).toHaveBeenCalledTimes(1)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
  })
  it('a longer sentence over the agent still supersedes the turn, as before', () => {
    const { hooks, call } = session()
    thinking(call)
    hear(call, 1, 'Actually, stop doing that and run the tests.')
    expect(hooks.onBargeIn).toHaveBeenCalledTimes(1)
    expect(hooks.onStop).not.toHaveBeenCalled()
    expect(hooks.onTranscript).toHaveBeenCalledWith('Actually, stop doing that and run the tests.')
  })
  it('drops an unfinished sentence that was being held', () => {
    vi.useFakeTimers()
    const { hooks, call } = session()
    hear(call, 1, 'send the report to')
    thinking(call)
    hear(call, 2, 'never mind')
    vi.advanceTimersByTime(5000)
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    vi.useRealTimers()
  })
  it('"stop" answers a waiting approval with no, after its prompt has played', () => {
    let t = 10000
    const { hooks, call } = session({ now: () => t })
    call.promptAsk({ id: 'a1', kind: 'approval', options: ['Approve', 'Reject'] })
    call._handleFrame({ type: 'spoken', text: 'Approve merge? Yes or no.', ask: true })
    call._promptPlayed()
    t += 1000
    hear(call, 1, 'stop')
    expect(hooks.onAnswer).toHaveBeenCalledWith(expect.objectContaining({ id: 'a1' }), 'no')
    expect(hooks.onStop).not.toHaveBeenCalled()
  })
  it('a stop phrase the agent itself just said, heard during playback, is our own echo', () => {
    const { hooks, call } = session()
    thinking(call)
    call._handleFrame({ type: 'spoken', text: 'Hold on.' })
    call._handleUtterance({ samples: new Float32Array(16000), duringPlayback: true })
    hear(call, 1, 'Hold on.')
    expect(hooks.onStop).not.toHaveBeenCalled()
    expect(hooks.onBargeIn).not.toHaveBeenCalled()
  })

  it('interrupt() from the button stops the turn the bridge is speaking, an approved run included', () => {
    const { hooks, call } = session()
    thinking(call, 'approved-run')
    expect(call.canStop.value).toBe(true)
    call.interrupt()
    expect(hooks.onBargeIn).toHaveBeenCalledWith({ executionId: 'approved-run' })
    expect(hooks.onStop).toHaveBeenCalledTimes(1)
    expect(call.canStop.value).toBe(false)
    expect(call.status.value).toBe('listening')
  })
  it('interrupt({ cancelTurn: false }) silences without touching the turn', () => {
    const { hooks, call } = session()
    thinking(call)
    call.interrupt({ cancelTurn: false })
    expect(hooks.onBargeIn).not.toHaveBeenCalled()
    expect(hooks.onStop).toHaveBeenCalledTimes(1)
  })
  it('a transcript that was already on its way when Stop was pressed never becomes a turn', () => {
    const { hooks, call } = session()
    call._handleUtterance({ samples: new Float32Array(16000), duringPlayback: false })
    thinking(call)
    call.interrupt()
    hear(call, 1, 'What about the other file?')
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    call._handleUtterance({ samples: new Float32Array(16000), duringPlayback: false })
    hear(call, 2, 'What about the other file?')
    expect(hooks.onTranscript).toHaveBeenCalledTimes(1)
  })
  it('a prompt cut by Stop is not answered by a later yes: the card decides', () => {
    let t = 10000
    const { hooks, call } = session({ now: () => t })
    call.promptAsk({ id: 'a1', kind: 'approval', options: ['Approve', 'Reject'] })
    call._handleFrame({ type: 'spoken', text: 'Approve merge?', ask: true })
    call.interrupt({ cancelTurn: false })
    call._promptPlayed()
    t += 1000
    hear(call, 1, 'yes')
    expect(hooks.onAnswer).not.toHaveBeenCalled()
  })
  it('nothing to stop when idle', () => {
    const { call } = session()
    expect(call.canStop.value).toBe(false)
  })
})
