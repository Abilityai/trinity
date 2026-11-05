// @vitest-environment jsdom
/**
 * trinity-enterprise#1046: a Direct call in the real conversation, with the socket, mic
 * and speaker faked and everything between them real. The gates a Direct call rests on
 * live in the component: spoken words become an ordinary turn carrying the call id, the
 * composer stays usable, and a spoken yes answers on the person's own route.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(() => Promise.resolve({ data: {} })),
    put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

const player = vi.hoisted(() => ({ playing: false, last: null }))
vi.mock('@/utils/audio', () => ({
  startMicCapture: vi.fn(async () => ({ stop: vi.fn() })),
  createAudioPlayer: () => {
    player.last = {
      play: vi.fn(), stop: vi.fn(), pause: vi.fn(), resume: vi.fn(), getAmplitude: () => 0, getLevel: () => 0,
      isPlaying: () => player.playing,
    }
    return player.last
  },
}))

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {} unobserve() {} disconnect() {}
}
class FakeSocket {
  static OPEN = 1
  static CONNECTING = 0
  static last = null
  constructor(url) {
    this.url = url
    this.readyState = 1
    this.sent = []
    FakeSocket.last = this
    setTimeout(() => this.onopen?.())
  }
  send(raw) {
    const frame = JSON.parse(raw)
    this.sent.push(frame)
    // trinity-enterprise#595: the server acknowledges a ticket frame before any media.
    if (frame.type === 'auth') setTimeout(() => this.push({ type: 'auth_ok' }))
  }
  close() { this.readyState = 3 }
  push(frame) { this.onmessage({ data: JSON.stringify(frame) }) }
  of(type) { return this.sent.filter((f) => f.type === type) }
}
vi.stubGlobal('WebSocket', FakeSocket)
vi.stubGlobal('MediaRecorder', class {})
Object.defineProperty(globalThis.navigator, 'mediaDevices', { value: { getUserMedia: vi.fn() }, configurable: true })

const { default: PortalConversation } = await import('@/components/portal/PortalConversation.vue')
const { useClientPortalStore } = await import('@/stores/clientPortal')

const AGENT = { name: 'scout', playbooks: [], pulls_turns: false }
const SID = 's1'
const CALL = 'vd_call'
const TICKET = 'T'.repeat(43)
const APPROVAL = {
  id: 'ask-1', kind: 'approval', status: 'pending', agent_name: 'scout', raised_in_turn: true, chat_id: SID,
  title: 'Approve merge', options: ['Approve', 'Reject'], created_at: '2026-11-02T10:00:00Z',
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms))

let store
let wrapper
beforeEach(async () => {
  document.body.innerHTML = ''
  player.playing = false
  setActivePinia(createPinia())
  store = useClientPortalStore()
  vi.spyOn(store, 'isPlatformSession', 'get').mockReturnValue(true)
  store.realtimeVoice = { available: true, reason: null }
  store.directVoice = { available: true, reason: null, provider: 'gemini' }
  store.fetchHistory = vi.fn(async () => ({ sessionId: SID, messages: [], lastTurnOutcome: null }))
  store.streamPortalExecution = vi.fn(() => new Promise(() => {}))
  store.startPortalChat = vi.fn(async () => ({ execution_id: 'e1', session_id: SID }))
  store.cancelPortalTurn = vi.fn(async () => ({ status: 'cancelled' }))
  store.fetchAsks = vi.fn(async () => {})
  store.answerAsk = vi.fn(async () => ({}))
  store.startWorkspaceVoice = vi.fn(async () => ({
    voice_session_id: CALL, websocket_url: `/ws/voice/${CALL}`, portal_session_id: SID,
    websocket_ticket: TICKET, websocket_ticket_expires_in: 30,
    max_duration_seconds: 1800, mode: 'direct', provider: 'gemini', runtime: 'claude-code', early_speech: true,
  }))
  wrapper = mount(PortalConversation, {
    props: { agent: AGENT, sessionId: SID },
    global: { renderStubDefaultSlot: true },
    attachTo: document.body,
  })
  await flushPromises()
})
afterEach(() => {
  wrapper?.unmount()
  vi.restoreAllMocks()
  localStorage.clear()
})

async function startCall() {
  await wrapper.get('[data-testid="portal-direct-call"]').trigger('click')
  await flushPromises()
  await wait(0)
  await flushPromises()
  // trinity-enterprise#595: one more turn for the server's `auth_ok`, which starts the mic.
  await wait(0)
  await flushPromises()
  return FakeSocket.last
}
const say = async (socket, seq, text) => {
  socket.push({ type: 'transcript_live', seq, text })
  await flushPromises()
}
const composer = () => wrapper.get('textarea')

describe('a Direct call in the conversation', () => {
  it('opens its socket with no credential in the URL and sends the ticket as the first frame (trinity-enterprise#595)', async () => {
    const socket = await startCall()
    expect(socket.url.endsWith(`/ws/voice/${CALL}`)).toBe(true)
    expect(socket.url).not.toContain('?')
    expect(socket.sent[0]).toEqual({ type: 'auth', ticket: TICKET })
  })

  it('the Direct button starts a Direct call, shows the stage, and leaves the composer usable', async () => {
    await startCall()
    expect(store.startWorkspaceVoice).toHaveBeenCalledWith('scout', SID, null, 'direct')
    expect(wrapper.find('[data-testid="direct-call-stage"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="call-bars"]').exists()).toBe(true)
    expect(composer().attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="portal-agent-picker"]').attributes('disabled')).toBeDefined()
  })

  it('the strip is one row: the bars, a word, one detail line, a mute icon and a red hang-up', async () => {
    await startCall()
    const stage = wrapper.get('[data-testid="direct-call-stage"]')
    expect(stage.text()).not.toMatch(/Direct call ·|speech by|Esc ends|first word/)
    expect(stage.findAll('[data-testid="direct-call-heard"]')).toHaveLength(1)
    const mute = stage.get('[data-testid="direct-call-mute"]')
    expect(mute.attributes('aria-label')).toBe('Mute')
    expect(mute.find('svg').exists()).toBe(true)
    await mute.trigger('click')
    expect(mute.attributes('aria-label')).toBe('Unmute')
    expect(mute.attributes('aria-pressed')).toBe('true')
    const end = stage.get('[data-testid="direct-call-end"]')
    expect(end.attributes('aria-label')).toBe('End call')
    expect(end.classes().join(' ')).toContain('status-danger')
    expect(end.find('svg').exists()).toBe(true)
  })

  it('while the Direct call is on, its button ends it and the Live button is off', async () => {
    const socket = await startCall()
    const live = wrapper.get('[data-testid="portal-voice-call"]')
    expect(live.attributes('disabled')).toBeDefined()
    expect(live.attributes('title')).toBe('Another call is on')
    const direct = wrapper.get('[data-testid="portal-direct-call"]')
    expect(direct.attributes('aria-pressed')).toBe('true')
    expect(direct.attributes('title')).toBe('End the call (Esc when it is quiet)')
    await direct.trigger('click')
    await flushPromises()
    expect(socket.of('end')).toHaveLength(1)
  })

  it('the Live button starts a Live call, and the Direct button is off while it is on', async () => {
    wrapper.unmount()
    wrapper = mount(PortalConversation, { props: { agent: AGENT, sessionId: SID }, global: { renderStubDefaultSlot: true, stubs: { VoiceOverlay: true } } })
    await flushPromises()
    await wrapper.get('[data-testid="portal-voice-call"]').trigger('click')
    await flushPromises()
    expect(store.startWorkspaceVoice).toHaveBeenCalledWith('scout', SID)
    await wait(0)
    await flushPromises()
    expect(wrapper.get('[data-testid="portal-direct-call"]').attributes('disabled')).toBeDefined()
  })

  it('a Direct button that cannot start says why', async () => {
    store.directVoice = { available: false, reason: 'No speech key.' }
    await flushPromises()
    const direct = wrapper.get('[data-testid="portal-direct-call"]')
    expect(direct.attributes('disabled')).toBeDefined()
    expect(direct.attributes('title')).toBe('No speech key.')
  })

  it('sends spoken words as an ordinary turn with the call id, then asks for it to be spoken', async () => {
    const socket = await startCall()
    await say(socket, 1, 'What time is it?')
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    const [agent, text, sid, opts] = store.startPortalChat.mock.calls[0]
    expect([agent, text, sid, opts.voiceCallId]).toEqual(['scout', 'What time is it?', SID, CALL])
    expect(socket.of('speak')).toEqual([{ type: 'speak', execution_id: 'e1' }])
    expect(wrapper.find('[data-testid="portal-direct-spoken-chip"]').exists()).toBe(true)
  })

  it('never touches what the person was typing', async () => {
    const socket = await startCall()
    await composer().setValue('half a thought')
    await say(socket, 1, 'What time is it?')
    expect(composer().element.value).toBe('half a thought')
  })

  it('a turn typed during the call belongs to it too', async () => {
    const socket = await startCall()
    await composer().setValue('typed now')
    await composer().trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(store.startPortalChat.mock.calls[0][3].voiceCallId).toBe(CALL)
    expect(socket.of('speak')).toHaveLength(1)
  })

  it('a turn after the call carries no id', async () => {
    const socket = await startCall()
    socket.push({ type: 'saved', rows: 0 })
    await flushPromises()
    await composer().setValue('after')
    await composer().trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(store.startPortalChat.mock.calls[0][3].voiceCallId).toBeNull()
  })

  it('talking over a running turn stops it, sends the new words, and leaves the composer empty', async () => {
    const socket = await startCall()
    await say(socket, 1, 'Start the long job.')
    socket.push({ type: 'speak_state', state: 'thinking', execution_id: 'e1' })
    await say(socket, 2, 'Actually, stop.')
    expect(store.cancelPortalTurn).toHaveBeenCalledWith('scout', 'e1')
    expect(socket.of('interrupt')).toHaveLength(1)
    expect(composer().element.value).toBe('')
  })

  it('Escape ends the call and sends the end frame', async () => {
    const socket = await startCall()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await flushPromises()
    expect(socket.of('end')).toHaveLength(1)
  })

  it('an ask already waiting when the call began stays on its card, unspoken', async () => {
    store.asks = [APPROVAL]
    await flushPromises()
    const socket = await startCall()
    expect(socket.of('speak_ask')).toEqual([])
    expect(wrapper.get('[data-testid="direct-call-state"]').text()).toBe('Listening')
  })

  it('an ask raised while the call is connecting is spoken once the socket is open', async () => {
    let answerStart
    const started = store.startWorkspaceVoice()
    store.startWorkspaceVoice = vi.fn(() => new Promise((resolve) => { answerStart = () => resolve(started) }))
    await wrapper.get('[data-testid="portal-direct-call"]').trigger('click')
    await flushPromises()
    store.asks = [APPROVAL]
    await flushPromises()
    answerStart()
    await flushPromises()
    await wait(0)
    await flushPromises()
    expect(FakeSocket.last.of('speak_ask')).toEqual([{ type: 'speak_ask', ask_id: 'ask-1' }])
  })

  it('the reply finishing before the prompt plays does not open the answer window', async () => {
    const socket = await startCall()
    await raiseAsk(socket)
    player.playing = true
    await wait(70)
    player.playing = false
    await wait(400)
    await say(socket, 5, 'Yes.')
    expect(store.answerAsk).not.toHaveBeenCalled()
  })

  async function raiseAsk(socket) {
    socket.sent.length = 0
    store.asks = [APPROVAL]
    await flushPromises()
    expect(socket.of('speak_ask')).toEqual([{ type: 'speak_ask', ask_id: 'ask-1' }])
  }
  async function promptPlays(socket) {
    socket.push({ type: 'spoken', text: 'Approve merge. Yes or no?', ask: true })
    player.playing = true
    await wait(70)
    player.playing = false
    await wait(70)
  }

  it('a yes after the prompt answers on the person\'s route, and only then follows up', async () => {
    const socket = await startCall()
    await raiseAsk(socket)
    await promptPlays(socket)
    await wait(320)
    await say(socket, 5, 'Yes.')
    expect(store.answerAsk).toHaveBeenCalledWith('ask-1', { response: 'Approve' })
    expect(socket.of('ask_followup')).toEqual([{ type: 'ask_followup', ask_id: 'ask-1', outcome: 'approved' }])
    expect(store.startPortalChat).not.toHaveBeenCalled()
  })

  it('a yes before the prompt finished answers nothing', async () => {
    const socket = await startCall()
    await raiseAsk(socket)
    await say(socket, 5, 'Yes.')
    expect(store.answerAsk).not.toHaveBeenCalled()
  })

  it('a longer sentence during an approval goes to the agent, and the ask stays open', async () => {
    const socket = await startCall()
    await raiseAsk(socket)
    await promptPlays(socket)
    await wait(320)
    await say(socket, 5, 'Yes, but change the subject first.')
    expect(store.answerAsk).not.toHaveBeenCalled()
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
  })

  it('a refused voice answer says so and points at the card', async () => {
    store.answerAsk = vi.fn(async () => { throw new Error('no') })
    const socket = await startCall()
    await raiseAsk(socket)
    await promptPlays(socket)
    await wait(320)
    await say(socket, 5, 'No.')
    expect(wrapper.get('[data-testid="direct-call-heard"]').text()).toContain('Use the card')
    expect(socket.of('ask_followup')).toEqual([])
  })
})

// trinity-enterprise#1087: stopping the agent mid-reply, in the real conversation.
describe('stopping the agent on a Direct call', () => {
  const stopButton = () => wrapper.get('[data-testid="direct-call-stop"]')
  const escape = () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))

  async function turnRunning(socket) {
    await say(socket, 1, 'Summarise the report.')
    socket.push({ type: 'speak_state', state: 'thinking', execution_id: 'e1' })
    await flushPromises()
  }

  it('over quiet playback a normal voice pauses the agent: the bar follows what is playing', async () => {
    // The mic hands each frame to the endpointer with the player's own output level. Quiet
    // playback (getLevel 0) puts the bar at PLAY_MIN, so a normal voice (0.03) is heard;
    // without that hand-off the endpointer falls back to the old 0.045 bar and stays deaf.
    const { startMicCapture } = await import('@/utils/audio')
    await startCall()
    const onMic = startMicCapture.mock.calls.at(-1)[0]
    player.playing = true
    for (let i = 0; i < 20; i++) onMic(null, new Float32Array(320).fill(0.03))
    await flushPromises()
    expect(player.last.pause).toHaveBeenCalled()
  })

  it('the Stop button is there with its reason when there is nothing to stop', async () => {
    await startCall()
    expect(stopButton().attributes('disabled')).toBeDefined()
    expect(stopButton().attributes('aria-label')).toBe('Nothing to stop')
  })

  it('Stop silences the agent, cancels its turn as the person, and the call stays on', async () => {
    const socket = await startCall()
    await turnRunning(socket)
    expect(stopButton().attributes('disabled')).toBeUndefined()
    await stopButton().trigger('click')
    await flushPromises()
    expect(socket.of('interrupt')).toHaveLength(1)
    expect(store.cancelPortalTurn).toHaveBeenCalledWith('scout', 'e1')
    expect(socket.of('end')).toHaveLength(0)
    expect(composer().element.value).toBe('')
    expect(wrapper.get('[data-testid="direct-call-heard"]').text()).toBe('Stopped.')
  })

  it('a spoken "stop" stops the turn and is never sent as one', async () => {
    const socket = await startCall()
    await turnRunning(socket)
    await say(socket, 2, 'Stop.')
    expect(store.cancelPortalTurn).toHaveBeenCalledWith('scout', 'e1')
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    expect(socket.of('interrupt')).toHaveLength(1)
  })

  it('audio already on the wire when Stop was pressed does not play', async () => {
    const socket = await startCall()
    await turnRunning(socket)
    await stopButton().trigger('click')
    socket.push({ type: 'audio', data: 'AAE=' })
    expect(player.last.play).not.toHaveBeenCalled()
    socket.push({ type: 'audio_flush' })
    socket.push({ type: 'audio', data: 'AAE=' })
    expect(player.last.play).toHaveBeenCalledTimes(1)
  })

  it('the composer Stop on a Direct call stops the voice too, and puts no spoken words in the composer', async () => {
    const socket = await startCall()
    await turnRunning(socket)
    await wrapper.get('[aria-label="Stop this turn"]').trigger('click')
    await flushPromises()
    expect(socket.of('interrupt')).toHaveLength(1)
    expect(store.cancelPortalTurn).toHaveBeenCalledTimes(1)
    expect(composer().element.value).toBe('')
  })

  it('Escape while the agent speaks silences it and keeps both the turn and the call', async () => {
    const socket = await startCall()
    await turnRunning(socket)
    player.playing = true
    escape()
    await flushPromises()
    expect(socket.of('interrupt')).toHaveLength(1)
    expect(store.cancelPortalTurn).not.toHaveBeenCalled()
    expect(socket.of('end')).toHaveLength(0)
  })

  it('Escape with nothing playing ends the call, and leaves a running turn running', async () => {
    const socket = await startCall()
    await turnRunning(socket)
    player.playing = false
    escape()
    await flushPromises()
    expect(socket.of('end')).toHaveLength(1)
    expect(store.cancelPortalTurn).not.toHaveBeenCalled()
  })

  it('words queued behind the turn are not sent once Stop has ended it', async () => {
    let finish
    store.streamPortalExecution = vi.fn(() => new Promise((resolve) => { finish = resolve }))
    const socket = await startCall()
    await turnRunning(socket)
    // Said while the turn was still running: queued, to go once it settles.
    await say(socket, 2, 'And the appendix too.')
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    await stopButton().trigger('click')
    await flushPromises()
    store.fetchHistory = vi.fn(async () => ({ sessionId: SID, messages: [], lastTurnOutcome: { category: 'cancelled', execution_id: 'e1' } }))
    finish()
    for (let i = 0; i < 20; i++) { await flushPromises(); await wait(5) }
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
  })

  it('a stop said while the turn is still being sent cancels it as soon as it starts, and it is never spoken', async () => {
    let started
    store.startPortalChat = vi.fn(() => new Promise((resolve) => { started = () => resolve({ execution_id: 'e1', session_id: SID }) }))
    const socket = await startCall()
    await say(socket, 1, 'Summarise the report.')
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    await say(socket, 2, 'Never mind.')
    expect(store.cancelPortalTurn).not.toHaveBeenCalled()
    started()
    await flushPromises()
    expect(store.cancelPortalTurn).toHaveBeenCalledWith('scout', 'e1')
    expect(socket.of('speak')).toEqual([])
  })

  it('a stop on a run the composer does not hold (an approved run) cancels that run', async () => {
    const socket = await startCall()
    socket.push({ type: 'speak_state', state: 'thinking', execution_id: 'approved-run' })
    await flushPromises()
    await stopButton().trigger('click')
    await flushPromises()
    expect(store.cancelPortalTurn).toHaveBeenCalledWith('scout', 'approved-run')
  })
})
