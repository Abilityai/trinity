// @vitest-environment jsdom
/**
 * trinity#3163: the Chat tab's context block carries the conversation BEFORE
 * the message being sent, and the message once.
 *
 * `sendMessage` pushed the new user message into `messages` and only then built
 * the context, so the `### Previous conversation:` block ended with the message
 * that `### Current message:` repeats: sent twice, and a first message got a
 * history block holding nothing but itself.
 *
 * MOUNTED: each turn goes through `sendMessage` and the payload is read off the
 * `/task` POST, so the assertions cover what the agent receives.
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

const AGENT = 'fin'
const HISTORY = '### Previous conversation:'
const CURRENT = '### Current message:'

let wrapper
let reply = 0

const count = (text, needle) => text.split(needle).length - 1
const payloads = () => axios.post.mock.calls.filter(c => c[0].endsWith('/task')).map(c => c[1])

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

// The reply arrives on the first 5 s poll; only setTimeout is faked so
// nextTick / flushPromises keep working.
async function turn(w, text) {
  const sent = w.vm.sendMessage(text)
  await flush()
  await vi.advanceTimersByTimeAsync(5000)
  await sent
  await flush()
}

function serve(sessionMessages = {}) {
  routes.get = vi.fn(async (url) => {
    const m = url.match(/\/chat\/sessions\/(\w+)$/)
    if (m) return { data: { messages: sessionMessages[m[1]] || [] } }
    if (/\/executions\//.test(url)) return { data: { status: 'success', response: `reply ${++reply}` } }
    return { data: [] }
  })
  routes.post = vi.fn(async () => ({ data: { execution_id: `e${reply}` } }))
}

async function chatPanel() {
  wrapper = mount(ChatPanel, {
    props: { agentName: AGENT, agentStatus: 'running' },
    global: { stubs: { ModelSelector: true } },
  })
  await flush()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  reply = 0
  setActivePinia(createPinia())
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  globalThis.fetch = vi.fn(async () => { throw new Error('no SSE in this spec') })
})
afterEach(() => { wrapper?.unmount(); wrapper = null; vi.useRealTimers() })

describe('ChatPanel context prompt (#3163)', () => {
  it('3163: the first message goes out as it is, with no history block', async () => {
    serve()
    const w = await chatPanel()
    await turn(w, 'remember 41')

    expect(payloads()[0].message).toBe('remember 41')
    expect(payloads()[0].user_message).toBe('remember 41')
  })

  it('3163: a later message carries the earlier turns and its own text once', async () => {
    serve()
    const w = await chatPanel()
    await turn(w, 'remember 41')
    await turn(w, 'what number?')

    const second = payloads()[1].message
    expect(second).toContain(HISTORY)
    expect(second).toContain('User: remember 41')
    expect(second).toContain('Assistant: reply 1')
    expect(count(second, 'what number?')).toBe(1)
    expect(second.endsWith(`${CURRENT}\n\nUser: what number?`)).toBe(true)
    expect(second.indexOf('what number?')).toBeGreaterThan(second.indexOf(CURRENT))
  })

  it('3163: the history keeps the last 20 messages before the new one', async () => {
    const earlier = Array.from({ length: 30 }, (_, i) => ({
      role: i % 2 === 0 ? 'user' : 'assistant',
      content: `m${i}`,
      timestamp: '2026-10-01T00:00:00Z',
    }))
    serve({ s1: earlier })
    const w = await chatPanel()
    await w.vm.selectSession({ id: 's1' })
    await flush()
    await turn(w, 'newest')

    const sent = payloads()[0].message
    expect(sent).not.toContain('m9\n')
    expect(sent).toContain('User: m10\n')
    expect(sent).toContain('Assistant: m29\n')
    expect(sent.match(/^(User|Assistant): /gm)).toHaveLength(20 + 1)   // 20 history entries, then the current one
    expect(count(sent, 'newest')).toBe(1)
  })

  it('3163: after a held first request the next message has no empty history block', async () => {
    serve()
    routes.post = vi.fn(async () => ({
      status: 202, data: { status: 'pending_approval', message: 'Not run: waiting for approval.' },
    }))
    const w = await chatPanel()
    await w.vm.sendMessage('/pay-invoice 100 EUR')
    await flush()
    await w.vm.sendMessage('what is on today?')
    await flush()

    expect(payloads()[1].message).toBe('what is on today?')
  })
})
