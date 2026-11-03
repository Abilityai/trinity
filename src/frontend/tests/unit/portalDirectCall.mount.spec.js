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

const player = vi.hoisted(() => ({ playing: false }))
vi.mock('@/utils/audio', () => ({
  startMicCapture: vi.fn(async () => ({ stop: vi.fn() })),
  createAudioPlayer: () => ({
    play: vi.fn(), stop: vi.fn(), pause: vi.fn(), resume: vi.fn(), getAmplitude: () => 0,
    isPlaying: () => player.playing,
  }),
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
  send(raw) { this.sent.push(JSON.parse(raw)) }
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
  return FakeSocket.last
}
const say = async (socket, seq, text) => {
  socket.push({ type: 'transcript_live', seq, text })
  await flushPromises()
}
const composer = () => wrapper.get('textarea')

describe('a Direct call in the conversation', () => {
  it('the Direct button starts a Direct call, shows the stage, and leaves the composer usable', async () => {
    await startCall()
    expect(store.startWorkspaceVoice).toHaveBeenCalledWith('scout', SID, null, 'direct')
    expect(wrapper.find('[data-testid="direct-call-stage"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="call-bars"]').exists()).toBe(true)
    expect(composer().attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="portal-agent-picker"]').attributes('disabled')).toBeDefined()
  })

  it('while the Direct call is on, its button ends it and the Live button is off', async () => {
    const socket = await startCall()
    const live = wrapper.get('[data-testid="portal-voice-call"]')
    expect(live.attributes('disabled')).toBeDefined()
    expect(live.attributes('title')).toBe('Another call is on')
    const direct = wrapper.get('[data-testid="portal-direct-call"]')
    expect(direct.attributes('aria-pressed')).toBe('true')
    expect(direct.attributes('title')).toBe('End the call (Esc)')
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
