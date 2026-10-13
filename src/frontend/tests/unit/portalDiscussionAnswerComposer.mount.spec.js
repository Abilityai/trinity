// @vitest-environment jsdom
/**
 * trinity-enterprise#747 × #3168 — "Send as answer" in a discussion chat sends
 * TEXT only (`WorkspaceAskAnswer.response`). With a reply chip in the composer
 * (ent#738) the chip used to be dropped without a word; the control now stands
 * down and says why (#3181 review). Mounted: the real PortalConversation
 * (shallow) with the real PortalReplyChip; the store's network is stubbed.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
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
      { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import PortalConversation from '@/components/portal/PortalConversation.vue'
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const ASK = {
  id: 'q1', agent_name: 'scout', kind: 'question', status: 'pending',
  title: 'Pick a vendor', question: 'Which one?', options: [],
  discussion_chat_id: 's1', chat_id: 'main', created_at: '2026-10-01T09:00:00Z',
}

let wrapper
let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.asks = [ASK]
  store.fetchHistory = vi.fn(async () => ({ sessionId: 's1', messages: [{ id: 'r1', role: 'assistant', content: 'Here are the options.' }] }))
  store.fetchChatTurnAsks = vi.fn(async () => [ASK])
  store.answerAsk = vi.fn(async () => ({ ...ASK, status: 'answered' }))
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

async function mountChat(props = {}) {
  wrapper = shallowMount(PortalConversation, {
    props: { agent: { name: 'scout', playbooks: [] }, sessionId: 's1', ...props },
    global: { renderStubDefaultSlot: true, stubs: { PortalReplyChip: false } },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

const row = () => wrapper.find('[data-testid="portal-discussion-answer"]')
const sendAsAnswer = () => wrapper.find('[data-testid="portal-discussion-answer-send"]')

describe('Send as answer with a reply chip in the composer', () => {
  it('is offered for typed text when nothing else is in the composer', async () => {
    await mountChat()
    await wrapper.find('textarea').setValue('Go with the EU one')
    expect(row().exists()).toBe(true)
    expect(sendAsAnswer().attributes('disabled')).toBe('false')
    expect(row().text()).toContain('Ready to decide?')
  })

  it('stands down and says why while a reply is attached — the chip is never dropped silently', async () => {
    await mountChat({ replyTarget: { sessionId: 's1', messageId: 'r1', excerpt: 'Here are the options.' } })
    await wrapper.find('textarea').setValue('Go with the EU one')
    expect(sendAsAnswer().attributes('disabled')).toBe('true')
    expect(row().text()).toContain('An answer is text only')
    expect(store.answerAsk).not.toHaveBeenCalled()
  })
})
