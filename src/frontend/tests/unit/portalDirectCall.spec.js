// @vitest-environment jsdom
/**
 * trinity-enterprise#1046: Workspace Direct calls in the browser.
 * The rules, the endpointer, the fold, the composable's protocol, the two call buttons,
 * and the mounted call bars.
 */
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import {
  answersAsk, callButtons, cardOutcome, classifyAnswer, directBarsState, directStatusLine, isDirectCallId,
  isEcho, looksUnfinished, optionForAnswer,
} from '../../src/components/portal/portalDirectCall'
import { groupVoiceBlocks, voiceTaskCaption } from '../../src/components/portal/portalVoiceMode'
import { createEndpointer, samplesToPcm16Base64, VAD } from '../../src/utils/voiceEndpointing'
import { useDirectVoiceSession } from '../../src/composables/useDirectVoiceSession'
import CallBars from '../../src/components/portal/CallBars.vue'
import DirectVoiceSettings from '../../src/components/settings/DirectVoiceSettings.vue'
import { useSettingsStore } from '../../src/stores/settings'

const APPROVAL = { id: 'a1', kind: 'approval', options: ['Approve', 'Reject'] }

describe('a spoken answer', () => {
  it.each(['yes', 'Yes.', 'yeah', 'go ahead', 'Do it!', 'okay'])('%s is a yes', (t) => {
    expect(classifyAnswer(t)).toBe('yes')
  })
  it.each(['no', 'Nope.', "don't", 'cancel', 'no thanks'])('%s is a no', (t) => {
    expect(classifyAnswer(t)).toBe('no')
  })
  it.each(['yes but change the subject first', 'no idea', 'okay so what about tomorrow', ''])(
    '"%s" is words for the agent, not an answer', (t) => expect(classifyAnswer(t)).toBe('other'))

  it('maps onto the offered options, and only for an approval', () => {
    expect(optionForAnswer(APPROVAL, 'yes')).toBe('Approve')
    expect(optionForAnswer(APPROVAL, 'no')).toBe('Reject')
    expect(optionForAnswer({ kind: 'question', options: ['Approve'] }, 'yes')).toBeNull()
    expect(optionForAnswer({ kind: 'approval', options: ['Monday', 'Friday'] }, 'yes')).toBeNull()
  })

  const base = { waiting: APPROVAL, text: 'yes', duringPlayback: false, startedAt: 10000, promptEndedAt: 9000 }
  it('counts after the prompt finished', () => expect(answersAsk(base)).toBe('yes'))
  it('does not count over the prompt: the mic may hear its own "yes or no"', () => {
    expect(answersAsk({ ...base, duringPlayback: true })).toBeNull()
    expect(answersAsk({ ...base, startedAt: 9299 })).toBeNull()
    expect(answersAsk({ ...base, promptEndedAt: null })).toBeNull()
  })
  it('counts at exactly the gap, and not with nothing waiting', () => {
    expect(answersAsk({ ...base, startedAt: 9300 })).toBe('yes')
    expect(answersAsk({ ...base, waiting: null })).toBeNull()
  })
  it('names how a card answer ended, and nothing for an expired ask', () => {
    expect(cardOutcome({ ...APPROVAL, response: 'Approve' })).toBe('approved')
    expect(cardOutcome({ ...APPROVAL, response: 'Reject' })).toBe('rejected')
    expect(cardOutcome({ ...APPROVAL, status: 'expired' })).toBeNull()
  })
})

describe('what was heard', () => {
  it('treats our own words as echo, and new words as speech', () => {
    expect(isEcho('the date is monday', ['The date is Monday.'])).toBe(true)
    expect(isEcho('what about tuesday then', ['The date is Monday.'])).toBe(false)
    expect(isEcho('stop', ['Do not stop.'])).toBe(false)
  })
  it('holds an unfinished sentence', () => {
    expect(looksUnfinished('I want to send it to the')).toBe(true)
    expect(looksUnfinished('Send it to the.')).toBe(true)
    expect(looksUnfinished('Send it to Alice.')).toBe(false)
  })
})

describe('the two call buttons', () => {
  const live = { available: true }
  const direct = { available: true, provider: 'gemini' }
  it('renders nothing for a portal-token client', () => {
    expect(callButtons({ isPlatform: false, realtimeVoice: live, directVoice: direct }).render).toBe(false)
  })
  it('offers each call on its own availability, with the reason when it is off', () => {
    const both = callButtons({ isPlatform: true, realtimeVoice: live, directVoice: direct })
    expect([both.live.enabled, both.live.label]).toEqual([true, 'Start a voice call'])
    expect([both.direct.enabled, both.direct.label]).toEqual([true, 'Start a Direct call'])
    const noDirect = callButtons({ isPlatform: true, realtimeVoice: live, directVoice: { available: false, reason: 'No speech key.' } })
    expect([noDirect.live.enabled, noDirect.direct.enabled, noDirect.direct.label]).toEqual([true, false, 'No speech key.'])
    expect(callButtons({ isPlatform: true, realtimeVoice: { available: false }, directVoice: direct }).live.label).toContain('not available')
  })
  it('while one call is on, its button ends it and the other says a call is on', () => {
    const onDirect = callButtons({ isPlatform: true, realtimeVoice: live, directVoice: direct, directActive: true })
    expect([onDirect.direct.enabled, onDirect.direct.label]).toEqual([true, 'End the call (Esc)'])
    expect([onDirect.live.enabled, onDirect.live.label]).toEqual([false, 'Another call is on'])
    const onLive = callButtons({ isPlatform: true, realtimeVoice: live, directVoice: direct, liveActive: true })
    expect([onLive.live.enabled, onLive.direct.enabled]).toEqual([true, false])
  })
  it('states the honest wait on a runtime with no live stream', () => {
    expect(directStatusLine({ runtime: 'gemini-cli', provider: 'gemini', earlySpeech: false })).toContain('replies take about 10 s')
    expect(directStatusLine({ runtime: 'claude-code', provider: 'gemini', earlySpeech: true })).not.toContain('10 s')
  })
})

describe('the thread after a call', () => {
  const rows = [
    { role: 'user', content: 'typed' },
    { role: 'user', content: 'said', voiceCallId: 'vd_1' },
    { role: 'assistant', content: 'answered', voiceCallId: 'vd_1' },
    { role: 'system', content: 'Direct call · 2 min', voiceCallId: 'vd_1', source: 'voice' },
  ]
  it('folds a finished Direct call into one named block', () => {
    const items = groupVoiceBlocks(rows)
    expect(items.map((i) => i.kind)).toEqual(['message', 'voice-call'])
    expect(items[1].label).toBe('Direct call · 2 min')
    expect(items[1].turns).toHaveLength(2)
  })
  it('leaves a call that is still on in the open', () => {
    expect(groupVoiceBlocks(rows, { liveCallId: 'vd_1' }).every((i) => i.kind === 'message')).toBe(true)
  })
  it('names the block when the window cut the label off', () => {
    expect(groupVoiceBlocks(rows.slice(0, 3))[1].label).toBe('Direct call · 2 spoken turns')
  })
  it('does not fold a Live call task row, and captions it as before', () => {
    const task = { role: 'user', content: 'task', voiceCallId: 'vs_1' }
    expect(groupVoiceBlocks([task])[0].kind).toBe('message')
    expect(voiceTaskCaption(task)).not.toBe('')
    expect(voiceTaskCaption({ role: 'user', voiceCallId: 'vd_1' })).toBe('')
    expect(isDirectCallId('vs_1')).toBe(false)
  })
})

describe('the endpointer', () => {
  const FRAME = 1600 // 100 ms at 16 kHz
  const loud = () => new Float32Array(FRAME).fill(0.2)
  const quiet = () => new Float32Array(FRAME).fill(0.0001)
  const feed = (ep, frame, n, opts) => { for (let i = 0; i < n; i++) ep.push(frame(), opts) }

  it('ends an utterance after the silence and keeps the preroll', () => {
    const onUtterance = vi.fn()
    const ep = createEndpointer({ onUtterance })
    feed(ep, quiet, 5); feed(ep, loud, 10); feed(ep, quiet, 9)
    expect(onUtterance).toHaveBeenCalledTimes(1)
    expect(onUtterance.mock.calls[0][0].samples.length).toBeGreaterThan(FRAME * 10)
    expect(onUtterance.mock.calls[0][0].duringPlayback).toBe(false)
  })
  it('discards a click too short to be speech', () => {
    const onUtterance = vi.fn()
    const ep = createEndpointer({ onUtterance, vad: { ...VAD, startMs: 100, minVoicedMs: 300 } })
    feed(ep, loud, 1); feed(ep, quiet, 12)
    expect(onUtterance).not.toHaveBeenCalled()
  })
  it('needs more to start over playback, and says it began during playback', () => {
    const onSpeechStart = vi.fn()
    const ep = createEndpointer({ onSpeechStart, onUtterance: vi.fn() })
    feed(ep, loud, 3, { playing: true })
    expect(onSpeechStart).not.toHaveBeenCalled()
    feed(ep, loud, 1, { playing: true })
    expect(onSpeechStart).toHaveBeenCalledWith({ duringPlayback: true })
  })
  it('cuts a monologue at the cap', () => {
    const onUtterance = vi.fn()
    const ep = createEndpointer({ onUtterance })
    feed(ep, loud, 301)
    expect(onUtterance).toHaveBeenCalledTimes(1)
  })
  it('encodes samples as little-endian PCM16', () => {
    const bytes = atob(samplesToPcm16Base64(new Float32Array([1, -1])))
    expect(bytes.charCodeAt(0)).toBe(0xff)
    expect(bytes.charCodeAt(1)).toBe(0x7f)
    expect(bytes.charCodeAt(3)).toBe(0x80)
  })
})

describe('the call protocol', () => {
  function session(overrides = {}) {
    setActivePinia(createPinia())
    const hooks = { onTranscript: vi.fn(), onBargeIn: vi.fn(), onAnswer: vi.fn(), ...overrides }
    return { hooks, call: useDirectVoiceSession(hooks) }
  }
  const hear = (call, seq, text) => call._handleFrame({ type: 'transcript_live', seq, text })

  it('hands a finished sentence to the conversation and starts nothing itself', () => {
    const { hooks, call } = session()
    hear(call, 1, 'What time is it?')
    expect(hooks.onTranscript).toHaveBeenCalledWith('What time is it?')
    expect(hooks.onAnswer).not.toHaveBeenCalled()
  })
  it('waits for the rest of an unfinished sentence', () => {
    vi.useFakeTimers()
    const { hooks, call } = session()
    hear(call, 1, 'send it to')
    expect(hooks.onTranscript).not.toHaveBeenCalled()
    hear(call, 2, 'Alice.')
    expect(hooks.onTranscript).toHaveBeenCalledWith('send it to Alice.')
    hear(call, 3, 'and then')
    vi.advanceTimersByTime(3000)
    expect(hooks.onTranscript).toHaveBeenLastCalledWith('and then')
    vi.useRealTimers()
  })
  it('stops the running turn when talked over', () => {
    const { hooks, call } = session()
    call._handleFrame({ type: 'speak_state', state: 'thinking', execution_id: 'e1' })
    hear(call, 1, 'Actually, stop.')
    expect(hooks.onBargeIn).toHaveBeenCalledTimes(1)
    expect(hooks.onTranscript).toHaveBeenCalledWith('Actually, stop.')
  })
  it('does not count a yes before the prompt has played', () => {
    const { hooks, call } = session()
    call.promptAsk(APPROVAL)
    hear(call, 1, 'Yes.')
    expect(hooks.onAnswer).not.toHaveBeenCalled()
    expect(hooks.onTranscript).toHaveBeenCalledWith('Yes.')
  })
  it('does not keep an open question waiting for a yes', () => {
    const { call } = session()
    call.promptAsk({ id: 'q', kind: 'question' })
    expect(call.waitingAsk.value).toBeNull()
  })
  it('reports an ending and the reason', () => {
    const { call } = session()
    call._handleFrame({ type: 'status', state: 'ended', reason: 'cap' })
    expect(call.endReason.value).toBe('cap')
  })
})

describe('the call bars', () => {
  it('maps every Direct status onto a bars state', () => {
    expect(directBarsState('listening')).toBe('listening')
    expect(directBarsState('speaking')).toBe('speaking')
    expect(['transcribing', 'thinking', 'awaiting_approval'].map(directBarsState)).toEqual(['working', 'working', 'working'])
    expect(directBarsState('connecting')).toBe('connecting')
    expect(directBarsState('ended')).toBe('idle')
  })

  function mountBars(props = {}) {
    const frames = []
    const raf = vi.fn((cb) => { frames.push(cb); return frames.length })
    const caf = vi.fn()
    vi.stubGlobal('requestAnimationFrame', raf)
    vi.stubGlobal('cancelAnimationFrame', caf)
    const w = mount(CallBars, { props: { state: 'listening', size: 48, ...props }, attachTo: document.body })
    // Runs the queued frames n times, as the browser would.
    const tick = (n = 1) => { for (let i = 0; i < n; i++) frames.shift()?.(performance.now() + i * 16) }
    return { w, raf, caf, tick }
  }
  const heights = (w) => w.findAll('[data-testid="call-bar"]').map((b) => parseFloat(b.element.style.height))
  const done = (w) => { w.unmount(); vi.unstubAllGlobals(); vi.restoreAllMocks() }

  it('is four bars in a disc the size it was given, hidden from assistive tech', () => {
    const { w } = mountBars()
    const root = w.get('[data-testid="call-bars"]')
    expect(root.element.style.width).toBe('48px')
    expect(root.attributes('aria-hidden')).toBe('true')
    expect(w.findAll('[data-testid="call-bar"]')).toHaveLength(4)
    done(w)
  })

  it('the bars rise with the sound while listening, and sit flat in silence', async () => {
    const { w, tick } = mountBars({ amplitude: 0 })
    tick(30)
    const quiet = Math.max(...heights(w))
    await w.setProps({ amplitude: 0.8 })
    tick(30)
    expect(Math.max(...heights(w))).toBeGreaterThan(quiet * 2)
    done(w)
  })

  it('muted is flat dots whatever the sound', () => {
    const { w, tick } = mountBars({ amplitude: 1, muted: true })
    tick(30)
    expect(heights(w).every((h) => h <= 4.5)).toBe(true)
    expect(w.get('[data-testid="call-bars"]').attributes('data-state')).toBe('muted')
    done(w)
  })

  it('while working the bars move with no sound at all', () => {
    const { w, tick } = mountBars({ state: 'working', amplitude: 0 })
    tick(10)
    const a = heights(w)
    tick(15)
    expect(heights(w)).not.toEqual(a)
    expect(Math.max(...heights(w))).toBeGreaterThan(4.5)
    done(w)
  })

  it('each state is tinted with its own token', async () => {
    const { w } = mountBars()
    const cls = () => w.get('[data-testid="call-bars"]').classes().join(' ') + ' ' + w.get('[data-testid="call-bar"]').classes().join(' ')
    expect(cls()).toContain('status-success')
    await w.setProps({ state: 'working' })
    expect(cls()).toContain('state-autonomous')
    await w.setProps({ state: 'speaking' })
    expect(cls()).toContain('status-info')
    await w.setProps({ muted: true })
    expect(cls()).not.toMatch(/status-|state-/)
    done(w)
  })

  it('stops animating when it leaves the page', () => {
    const { w, caf } = mountBars()
    w.unmount()
    expect(caf).toHaveBeenCalledTimes(1)
    vi.unstubAllGlobals()
  })

  it('under reduced motion it holds one still frame per state and never animates', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true }))
    const { w, raf } = mountBars({ state: 'speaking', amplitude: 0.9 })
    expect(Math.max(...heights(w))).toBeGreaterThan(4.5)
    await w.setProps({ muted: true })
    expect(heights(w).every((h) => h <= 4.5)).toBe(true)
    expect(raf).not.toHaveBeenCalled()
    done(w)
  })
})

describe('the settings card', () => {
  async function card({ read, write }) {
    setActivePinia(createPinia())
    const store = useSettingsStore()
    store.getDirectVoiceSettings = read
    store.setDirectVoiceSettings = write
    const w = mount(DirectVoiceSettings)
    await new Promise((r) => setTimeout(r))
    return w
  }
  const OK = { enabled: true, provider: 'auto', ready_provider: 'gemini', reason: null }

  it('shows the provider in use, and the reason when there is none', async () => {
    const w = await card({ read: vi.fn().mockResolvedValue(OK) })
    expect(w.get('[data-testid="direct-voice-state"]').text()).toBe('Using gemini.')
    const off = await card({ read: vi.fn().mockResolvedValue({ ...OK, ready_provider: null, reason: 'Add a key.' }) })
    expect(off.get('[data-testid="direct-voice-state"]').text()).toBe('Add a key.')
  })
  it('saves a toggle and shows what the server now says', async () => {
    const write = vi.fn().mockResolvedValue({ ...OK, enabled: false })
    const w = await card({ read: vi.fn().mockResolvedValue(OK), write })
    await w.get('[role="switch"]').trigger('click')
    await new Promise((r) => setTimeout(r))
    expect(write).toHaveBeenCalledWith({ enabled: false })
    expect(w.get('[role="switch"]').attributes('aria-checked')).toBe('false')
  })
  it('names a failed read or save', async () => {
    const w = await card({ read: vi.fn().mockRejectedValue(new Error('x')) })
    expect(w.get('[data-testid="direct-voice-error"]').text()).toContain('Could not read')
  })
})
