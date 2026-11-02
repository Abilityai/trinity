// @vitest-environment jsdom
/**
 * trinity-enterprise#1046: Workspace Direct calls in the browser.
 * The rules, the endpointer, the fold, the composable's protocol, and the mounted mode menu.
 */
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import {
  answersAsk, callModes, cardOutcome, classifyAnswer, directStatusLine, isDirectCallId, isEcho,
  looksUnfinished, optionForAnswer, saveCallMode, loadCallMode,
} from '../../src/components/portal/portalDirectCall'
import { groupVoiceBlocks, voiceTaskCaption } from '../../src/components/portal/portalVoiceMode'
import { createEndpointer, samplesToPcm16Base64, VAD } from '../../src/utils/voiceEndpointing'
import { useDirectVoiceSession } from '../../src/composables/useDirectVoiceSession'
import CallModeMenu from '../../src/components/portal/CallModeMenu.vue'
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

describe('the modes on offer', () => {
  const live = { available: true }
  const direct = { available: true, provider: 'gemini' }
  it('renders nothing for a portal-token client', () => {
    expect(callModes({ isPlatform: false, realtimeVoice: live, directVoice: direct }).render).toBe(false)
  })
  it('defaults to Live, and remembers a Direct choice', () => {
    expect(callModes({ isPlatform: true, realtimeVoice: live, directVoice: direct }).mode).toBe('live')
    expect(callModes({ isPlatform: true, realtimeVoice: live, directVoice: direct, remembered: 'direct' }).mode).toBe('direct')
  })
  it('falls to the mode that works, with the reason when neither does', () => {
    expect(callModes({ isPlatform: true, realtimeVoice: { available: false, reason: 'x' }, directVoice: direct }).mode).toBe('direct')
    const none = callModes({ isPlatform: true, realtimeVoice: { available: false, reason: 'no key' }, directVoice: { available: false } })
    expect(none.enabled).toBe(false)
    expect(none.reason).toBe('no key')
  })
  it('remembers across visits and survives storage that throws', () => {
    const store = new Map()
    const storage = { getItem: (k) => store.get(k), setItem: (k, v) => store.set(k, v) }
    saveCallMode('direct', storage)
    expect(loadCallMode(storage)).toBe('direct')
    expect(loadCallMode({ getItem: () => { throw new Error('private') } })).toBe('live')
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

describe('the mode menu', () => {
  const items = [
    { mode: 'live', label: 'Voice call', hint: 'fast', enabled: true, reason: '' },
    { mode: 'direct', label: 'Direct call', hint: 'own words', enabled: false, reason: 'No speech key.' },
  ]
  const open = async (props = {}) => {
    const w = mount(CallModeMenu, { props: { items, mode: 'live', ...props }, attachTo: document.body })
    await w.get('[data-testid="portal-call-mode"]').trigger('click')
    return w
  }
  it('says the provider receives the audio, and why a mode is off', async () => {
    const w = await open()
    expect(w.text()).toContain('go to the speech provider')
    expect(w.text()).toContain('No speech key.')
    w.unmount()
  })
  it('chooses an available mode and refuses a disabled one', async () => {
    const w = await open()
    await w.get('[data-testid="portal-call-mode-direct"]').trigger('click')
    expect(w.emitted('choose')).toBeUndefined()
    await w.get('[data-testid="portal-call-mode-live"]').trigger('click')
    expect(w.emitted('choose')[0]).toEqual(['live'])
    w.unmount()
  })
  it('Escape closes the menu without reaching the call beneath it', async () => {
    const w = await open()
    const reachedDocument = vi.fn()
    document.addEventListener('keydown', reachedDocument)
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await w.vm.$nextTick()
    expect(w.find('[data-testid="portal-call-mode-menu"]').exists()).toBe(false)
    expect(reachedDocument).not.toHaveBeenCalled()
    document.removeEventListener('keydown', reachedDocument)
    w.unmount()
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
