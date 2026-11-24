// @vitest-environment jsdom
/**
 * #4537: a reply written into the open chat outside a turn the panel waited on
 * (a wake reporting back on a delegation) appears without a reload.
 *
 * The whole path is driven: the store's `noteChatReply` (what the socket
 * handler calls on `chat_response_ready`), the panel's watcher, the session
 * re-read over the mocked transport, and the merge. MOUNTED on the real
 * ChatPanel, the harness of `chatContextHistory3163.spec.js`.
 *
 * Three ways the reply used to be lost, each with its own test:
 *   - it landed while a turn was in flight (the re-read was skipped, and a
 *     turn's end never refetches messages);
 *   - the panel held a message the server never saved (a failed turn), so
 *     "only when it grew" compared equal lengths;
 *   - it was simply never wired (no test executed any of this).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const routes = vi.hoisted(() => ({ get: null, post: null }))

vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => routes.get(...a)),
    post: vi.fn((...a) => routes.post(...a)),
    put: vi.fn(async () => ({ data: {} })),
    delete: vi.fn(async () => ({ data: {} })),
    defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: {}, query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

import axios from 'axios'
import ChatPanel from '../../src/components/ChatPanel.vue'
import { useAgentsStore } from '../../src/stores/agents'
import { mergeSessionMessages, panelMessage } from '../../src/utils/chatSessionMerge'
import { useWebSocket } from '../../src/utils/websocket'
import { useAuthStore } from '../../src/stores/auth'

const AGENT = 'fin'
const row = (id, role, content, timestamp = '2026-11-14T10:00:00') => ({ id, role, content, timestamp })
const ASKED = row('m1', 'user', 'Ask the researcher for the numbers')
const PLAN = row('m2', 'assistant', 'Delegated. I will report back.')
const WAKE = row('m3', 'assistant', 'The researcher is done: revenue is up 4%.')

let wrapper
let store
/** What the server holds for session s1; every read answers from it. */
let server
/** How each execution poll answers. */
let execution

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}
const sessionReads = () => axios.get.mock.calls.filter(([url]) => /\/chat\/sessions\/s1$/.test(url)).length
const shown = () => wrapper.vm.messages.map((m) => m.content)

async function chatPanel() {
  wrapper = mount(ChatPanel, {
    props: { agentName: AGENT, agentStatus: 'running' },
    global: { stubs: { ModelSelector: true } },
  })
  await flush()
  await wrapper.vm.selectSession({ id: 's1' })
  await flush()
  return wrapper
}

/** The socket said a reply landed in a session of this agent. */
async function replyLanded(sessionId = 's1', agent = AGENT) {
  store.noteChatReply(agent, sessionId)
  await flush()
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  store = useAgentsStore()
  server = [ASKED, PLAN]
  execution = () => ({ status: 'success', response: 'Sure.' })
  routes.get = vi.fn(async (url) => {
    if (/\/chat\/sessions\/s1$/.test(url)) return { data: { messages: server } }
    if (/\/executions\//.test(url)) return { data: execution() }
    return { data: [] }
  })
  routes.post = vi.fn(async () => ({ data: { execution_id: 'e1' } }))
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  globalThis.fetch = vi.fn(async () => { throw new Error('no SSE in this spec') })
})
afterEach(() => { wrapper?.unmount(); wrapper = null; vi.useRealTimers(); localStorage.clear() })

describe('#4537 — a wake reply reaches the open chat', () => {
  it('appears in the session on screen, with no reload and no loading state', async () => {
    const w = await chatPanel()
    expect(shown()).toEqual([ASKED.content, PLAN.content])
    const before = sessionReads()

    server = [ASKED, PLAN, WAKE]
    await replyLanded()

    expect(sessionReads()).toBe(before + 1)
    expect(shown()).toEqual([ASKED.content, PLAN.content, WAKE.content])
    expect(w.text()).toContain('revenue is up 4%')
    expect(w.vm.loading).toBe(false)
  })

  it('a reply in another session, or for another agent, reads nothing here', async () => {
    await chatPanel()
    const before = sessionReads()
    server = [ASKED, PLAN, WAKE]

    const anySessionRead = () => axios.get.mock.calls.filter(([url]) => /\/chat\/sessions\/\w+$/.test(url)).length
    const all = anySessionRead()
    await replyLanded('s2')
    await replyLanded('s1', 'someone-else')

    expect(anySessionRead()).toBe(all)
    expect(sessionReads()).toBe(before)
    expect(shown()).toEqual([ASKED.content, PLAN.content])
  })

  it('a trigger with nothing new behind it leaves the list object alone', async () => {
    const w = await chatPanel()
    const list = w.vm.messages
    await replyLanded()
    expect(w.vm.messages).toBe(list)
  })

  it('a second reply is read too: each trigger is its own', async () => {
    await chatPanel()
    server = [ASKED, PLAN, WAKE]
    await replyLanded()
    const second = row('m4', 'assistant', 'And the forecast is ready.')
    server = [ASKED, PLAN, WAKE, second]
    await replyLanded()
    expect(shown().at(-1)).toBe(second.content)
  })
})

describe('#4537 — from the socket', () => {
  class FakeSocket {
    static last = null
    constructor(url) { this.url = url; FakeSocket.last = this }
    close() {}
    send() {}
  }

  it('`chat_response_ready` on /ws is what brings the reply into the open chat', async () => {
    // The real handler in `utils/websocket.js`, end to end: frame → store → panel → read.
    routes.post = vi.fn(async (url) => (url === '/api/ws/ticket' ? { data: { ticket: 't' } } : { data: { execution_id: 'e1' } }))
    globalThis.WebSocket = FakeSocket
    localStorage.setItem('token', 'jwt')
    const auth = useAuthStore()
    auth.isAuthenticated = true
    auth.token = 'jwt'
    auth.user = { email: 'x@example.com', role: 'operator' }
    await chatPanel()
    const socket = useWebSocket()
    await socket.connect()

    server = [ASKED, PLAN, WAKE]
    FakeSocket.last.onmessage({ data: JSON.stringify({ type: 'chat_response_ready', agent_name: AGENT, chat_session_id: 's1' }) })
    await flush()
    socket.disconnect()

    expect(shown()).toEqual([ASKED.content, PLAN.content, WAKE.content])
  })

  it('a trigger that names no session reads nothing', async () => {
    await chatPanel()
    const before = sessionReads()
    store.noteChatReply(AGENT, null)
    store.noteChatReply('', 's1')
    await flush()
    expect(sessionReads()).toBe(before)
  })
})

describe('#4537 — while a turn is in flight', () => {
  it('a reply that lands mid-turn is shown once the turn ends', async () => {
    const w = await chatPanel()
    const sending = w.vm.sendMessage('And the forecast?')
    await flush()
    expect(w.vm.loading).toBe(true)

    // The wake's reply lands while the panel waits on its own turn.
    server = [ASKED, PLAN, WAKE]
    const before = sessionReads()
    await replyLanded()
    expect(sessionReads()).toBe(before)               // not read mid-turn
    expect(shown()).not.toContain(WAKE.content)

    // The turn ends; its own messages are saved a moment later, not yet.
    await vi.advanceTimersByTimeAsync(5000)
    await sending
    await flush()

    expect(sessionReads()).toBe(before + 1)
    expect(shown()).toContain(WAKE.content)
    // …and the turn's own question and answer are still there, once each.
    expect(shown().filter((c) => c === 'And the forecast?')).toHaveLength(1)
    expect(shown().filter((c) => c === 'Sure.')).toHaveLength(1)
  })

  it('the turn\'s own rows, once saved, are not shown twice', async () => {
    const w = await chatPanel()
    const sending = w.vm.sendMessage('And the forecast?')
    await flush()
    await vi.advanceTimersByTimeAsync(5000)
    await sending
    await flush()

    server = [ASKED, PLAN, row('m3', 'user', 'And the forecast?'), row('m4', 'assistant', 'Sure.'), row('m5', 'assistant', WAKE.content)]
    await replyLanded()

    expect(shown()).toEqual([ASKED.content, PLAN.content, 'And the forecast?', 'Sure.', WAKE.content])
  })
})

describe('#4537 — the panel holds a message the server never saved', () => {
  it('after a failed turn the counts are equal, and the reply is still shown', async () => {
    const w = await chatPanel()
    execution = () => ({ status: 'failed', error: 'The agent is offline' })
    const sending = w.vm.sendMessage('And the forecast?')
    await flush()
    await vi.advanceTimersByTimeAsync(5000)
    await sending
    await flush()
    // On screen: the two stored rows and the question that was never saved.
    expect(shown()).toEqual([ASKED.content, PLAN.content, 'And the forecast?'])

    server = [ASKED, PLAN, WAKE]                       // three rows here, three there
    await replyLanded()

    expect(shown()).toContain(WAKE.content)
    // What the person was reading did not move: the reply comes after it.
    expect(shown()).toEqual([ASKED.content, PLAN.content, 'And the forecast?', WAKE.content])
  })
})

describe('mergeSessionMessages', () => {
  const p = (r) => panelMessage(r)
  it('nothing new: null, so the list is left alone', () => {
    expect(mergeSessionMessages([p(ASKED), p(PLAN)], [p(ASKED), p(PLAN)])).toBeNull()
    expect(mergeSessionMessages([], [])).toBeNull()
  })
  it('a new server row is appended, and stored rows keep their identity', () => {
    const out = mergeSessionMessages([p(ASKED), p(PLAN)], [p(ASKED), p(PLAN), p(WAKE)])
    expect(out.map((m) => m.id)).toEqual(['m1', 'm2', 'm3'])
  })
  it('a row the panel pushed itself is the server row with the same role and text', () => {
    const local = { role: 'assistant', content: ' Sure. ', timestamp: '2026-11-14T10:05:00.000Z' }
    expect(mergeSessionMessages([p(ASKED), local], [p(ASKED), p(row('m9', 'assistant', 'Sure.'))])).toBeNull()
  })
  it('two identical local rows match two server rows, not one twice', () => {
    const local = () => ({ role: 'user', content: 'ok' })
    const out = mergeSessionMessages([local(), local()], [p(row('a', 'user', 'ok')), p(row('b', 'user', 'ok')), p(WAKE)])
    expect(out.map((m) => m.id)).toEqual(['a', 'b', 'm3'])
  })
  it('equal lengths are not "nothing new"', () => {
    const unsaved = { role: 'user', content: 'never saved' }
    const out = mergeSessionMessages([p(ASKED), unsaved], [p(ASKED), p(WAKE)])
    expect(out.map((m) => m.content)).toEqual([ASKED.content, 'never saved', WAKE.content])
  })
  it('order comes from position, not from clocks that disagree', () => {
    const unsaved = { role: 'user', content: 'never saved', timestamp: '2020-01-01T00:00:00Z' }
    const out = mergeSessionMessages([p(ASKED), p(PLAN), unsaved], [p(ASKED), p(PLAN), p(WAKE)])
    expect(out.map((m) => m.content)).toEqual([ASKED.content, PLAN.content, 'never saved', WAKE.content])
  })
  it('keeps the id, which is what makes a stored row itself', () => {
    expect(panelMessage({ id: 'x', role: 'user', content: 'c', timestamp: 't' })).toEqual(
      { id: 'x', role: 'user', content: 'c', timestamp: 't', source: 'text', held: undefined })
  })
})
