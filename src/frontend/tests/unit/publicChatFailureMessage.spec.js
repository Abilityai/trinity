// @vitest-environment jsdom
/**
 * #3461 — a public link's failed turn tells the visitor it failed, in their words.
 *
 * `/chat/:token` is read by anonymous visitors. When the agent's run failed, the
 * page wrote the execution row's own `error` into its red banner: the exit code
 * and the operator's remediation ("check the provider console", "Settings →
 * …"), none of which a visitor can act on and all of which describes the
 * operator's install. The page now shows one plain line and gives the visitor
 * their words back so sending again is the retry; the row's text never reaches
 * the DOM, whatever the status route answers.
 *
 * MOUNTED, through the real `sendMessage`, with `axios` stubbed by URL — the
 * harness `skillGateSenders.spec.js` and `publicLinkSessionExpiry.spec.js` use
 * for this view.
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
  useRoute: () => ({ params: { token: 'tok' }, query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

import PublicChat from '../../src/views/PublicChat.vue'

// The shape of the string the agent server writes for a run that died with no
// output: an exit code, then what the operator should do about it.
const RAW = 'Execution failed with no output (exit code 1): the model credential ' +
  'is exhausted. Check the provider console, then update the key under Settings.'
const QUESTION = 'What is on my invoice?'
const VISITOR_LINE = "The agent couldn't respond right now."

let wrapper

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

// Sends QUESTION on an open link whose turn ends with `row`.
async function sendAndFinish(row) {
  const get = {
    '/api/public/link/': { data: { valid: true, agent_available: true, require_email: false,
      agent_name: 'fin' } },
    '/status': { data: { execution_id: 'e1', gate: null, ...row } },
    '/api/public/history/': { data: { messages: [] } },
  }
  routes.get = vi.fn(async (url) => {
    for (const [pattern, answer] of Object.entries(get)) if (url.includes(pattern)) return answer
    return { data: {} }
  })
  routes.post = vi.fn(async () => ({ data: { status: 'accepted', execution_id: 'e1', async_mode: true } }))
  wrapper = mount(PublicChat)
  await flush()
  vi.useFakeTimers()
  wrapper.vm.sendMessage(QUESTION)
  await vi.advanceTimersByTimeAsync(10_000)
  wrapper.vm.chatLoading = false          // ends a poll loop that did not stop on its own
  vi.useRealTimers()
  await flush()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  setActivePinia(createPinia())
  // No SSE in this spec: the stream is `fetch`, not axios.
  globalThis.fetch = vi.fn(async () => { throw new Error('no SSE in this spec') })
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('a failed turn on a public link', () => {
  it('never renders the execution row\'s own error text', async () => {
    const w = await sendAndFinish({ status: 'failed', response: null, error: RAW })

    expect(w.html()).not.toContain('exit code')
    expect(w.html()).not.toContain('Settings')
    expect(w.html()).not.toContain('provider console')
  })

  it('says, in plain words, that the agent could not respond and to try again', async () => {
    const w = await sendAndFinish({ status: 'failed', response: null, error: RAW })

    expect(w.text()).toContain(VISITOR_LINE)
    expect(w.text()).toMatch(/send it again/i)
  })

  it('gives the visitor their message back, so the retry is one send', async () => {
    const w = await sendAndFinish({ status: 'failed', response: null, error: RAW })

    expect(w.find('textarea').element.value).toBe(QUESTION)
  })

  it('says the same when the row carries no error text at all', async () => {
    const w = await sendAndFinish({ status: 'failed', response: null, error: null })

    expect(w.text()).toContain(VISITOR_LINE)
  })
})
