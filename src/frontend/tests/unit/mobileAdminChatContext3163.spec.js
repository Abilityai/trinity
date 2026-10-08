// @vitest-environment jsdom
/**
 * trinity#3163: the `/m` chat sends the conversation BEFORE the message being
 * sent, and the message once.
 *
 * `sendChatMessage` pushed the new user message into `chatMessages` and only
 * then built the context, so every message, the first included, went out with
 * itself in the "Previous conversation:" block and again as the last line.
 * The same bug as the agent page's Chat tab (`chatContextHistory3163.spec.js`).
 *
 * MOUNTED through `sendChatMessage`, with `boundedHttp` stubbed by URL (the
 * `mobileAdminSkillGate.spec.js` harness). The payload is read off the
 * `/task` POST; the 202 answer is a held request, so nothing is polled.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: { tab: 'agents' } }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))
vi.mock('@/utils/boundedHttp', () => ({
  http: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

import { http } from '@/utils/boundedHttp'
import { useAuthStore } from '@/stores/auth'
import MobileAdmin from '@/views/MobileAdmin.vue'

const HELD = { status: 202, data: { status: 'pending_approval', message: 'Not run: waiting for approval.' } }

let wrapper

const count = (text, needle) => text.split(needle).length - 1
const payloads = () => http.post.mock.calls.filter(c => c[0].endsWith('/task')).map(c => c[1])

async function mountView() {
  http.get.mockImplementation(async (url) => {
    if (url === '/api/operator-queue') return { data: { items: [], count: 0 } }
    if (url === '/api/notifications') return { data: { items: [], count: 0 } }
    return { data: [] }
  })
  http.post.mockImplementation(async () => HELD)
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().isAuthenticated = true
  wrapper = mount(MobileAdmin, { global: { plugins: [pinia], stubs: { LoadFailed: true, InlineError: true } } })
  await flushPromises()
  wrapper.vm.chatAgent = 'fin'
  return wrapper
}

async function send(w, text) {
  w.vm.chatInput = text
  await w.vm.sendChatMessage()
  await flushPromises()
}

describe('/m chat context prompt (#3163)', () => {
  beforeEach(() => { vi.clearAllMocks(); document.body.innerHTML = '' })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  it('3163: the first message goes out as it is, with no history block', async () => {
    const w = await mountView()
    await send(w, 'remember 41')

    expect(payloads()[0].message).toBe('remember 41')
    expect(payloads()[0].user_message).toBe('remember 41')
  })

  it('3163: a later message carries the earlier turns and its own text once', async () => {
    const w = await mountView()
    w.vm.chatMessages = [
      { role: 'user', content: 'remember 41', timestamp: '2026-10-01T00:00:00Z' },
      { role: 'assistant', content: 'ok, 41', timestamp: '2026-10-01T00:00:01Z' },
    ]
    await send(w, 'what number?')

    const sent = payloads()[0].message
    expect(sent).toContain('Previous conversation:')
    expect(sent).toContain('User: remember 41')
    expect(sent).toContain('Assistant: ok, 41')
    expect(count(sent, 'what number?')).toBe(1)
    expect(sent.endsWith('\n\nUser: what number?')).toBe(true)
  })

  it('3163: the history keeps the last 20 messages before the new one', async () => {
    const w = await mountView()
    w.vm.chatMessages = Array.from({ length: 30 }, (_, i) => ({
      role: i % 2 === 0 ? 'user' : 'assistant',
      content: `m${i}`,
      timestamp: '2026-10-01T00:00:00Z',
    }))
    await send(w, 'newest')

    const sent = payloads()[0].message
    expect(sent).not.toContain('m9\n')
    expect(sent).toContain('User: m10\n')
    expect(sent).toContain('Assistant: m29\n')
    expect(sent.match(/^(User|Assistant): /gm)).toHaveLength(20 + 1)   // 20 history entries, then the current one
    expect(count(sent, 'newest')).toBe(1)
  })
})
