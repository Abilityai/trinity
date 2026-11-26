// @vitest-environment jsdom
/**
 * #4502 on the chat tile: what the reader is told after answering an ask from
 * the tile inside a Workspace chat.
 *
 * Mounted (#2918) through the REAL tile: PortalConversation places the ask in
 * its thread and renders the real PortalAsks for it (`portal-tile-ask`). The
 * tile is a card only while the ask waits: the moment the answer is recorded
 * it becomes one history row, so the card that sent the answer unmounts in the
 * same render. Whatever is said about the answer has to survive that.
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
import PortalAsks from '@/components/portal/PortalAsks.vue'
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const tileAsk = (over = {}) => ({
  id: 'q1', agent_name: 'scout', kind: 'question', title: 'Which region?', question: 'EMEA or APAC?',
  options: null, status: 'pending', created_at: '2026-11-17T10:00:00Z',
  chat_id: 's1', raised_in_turn: true, ...over,
})

let wrapper
let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.asksAvailable = true
  store.asksLoaded = true
  store.fetchHistory = vi.fn(async () => ({
    sessionId: 's1', messages: [{ id: 'm1', role: 'user', content: 'go', created_at: '2026-11-17T09:00:00Z' }],
  }))
  store.fetchChatTurnAsks = vi.fn(async () => [])
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

async function answerFromTile({ resume = true, rosterAfter = [], before = [] } = {}) {
  store.asks = [tileAsk()]
  store.agents = before
  store.answerAsk = vi.fn(async (id) => {
    const answered = { ...tileAsk(), status: 'answered', ended_by: 'you', ended_at: new Date().toISOString(), resume_requested: resume }
    store.asks = store.asks.map((a) => (a.id === id ? answered : a))
    return answered
  })
  store.refreshRoster = vi.fn(async () => { store.agents = rosterAfter })
  wrapper = shallowMount(PortalConversation, {
    props: { agent: { name: 'scout', playbooks: [] }, sessionId: 's1' },
    global: { renderStubDefaultSlot: true, stubs: { PortalAsks: false } },
    attachTo: document.body,
  })
  await flushPromises()
  expect(wrapper.findComponent(PortalAsks).exists()).toBe(true) // the real card, in the tile
  const input = wrapper.find('[data-testid="portal-tile-ask-input-q1"]')
  await input.setValue('EMEA')
  await input.element.form.dispatchEvent(new Event('submit'))
  await flushPromises()
  expect(store.answerAsk).toHaveBeenCalledTimes(1)
  return wrapper
}

describe('the chat tile after an answer (the ruling on #3101: the confirmation lives in the row)', () => {
  it.each([
    ['running', [{ name: 'scout', availability: 'ready' }]],
    ['stopped', [{ name: 'scout', availability: 'stopped' }]],
  ])('agent %s: the tile becomes its history row, and no "Sent" line is drawn', async (_state, roster) => {
    const w = await answerFromTile({ before: roster, rosterAfter: roster })
    expect(w.findComponent(PortalAsks).exists()).toBe(false) // the card left with the answer
    expect(w.find('[data-testid="portal-chat-ask-ended"]').exists()).toBe(true)
    expect(w.find('[data-testid="portal-tile-ask-confirmation"]').exists()).toBe(false)
    expect(document.body.textContent).not.toContain('is picking this up')
    // The card is gone before its confirmation would be composed: no roster read.
    expect(store.refreshRoster).not.toHaveBeenCalled()
  })
})
