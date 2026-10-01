// @vitest-environment jsdom
/**
 * #3115 stopgap (the ent#610 ruling): the chat's ask strip shows only asks that
 * are still waiting. Since #3023 the Workspace asks list also carries asks that
 * ended in the last 7 days, and the strip above the composer rendered them all,
 * so answered tiles piled up in every chat with the agent.
 *
 * Mounted (#2918), both halves:
 *   - the conversation mounts its strip's PortalAsks with `pending-only`;
 *   - PortalAsks with `pending-only` renders a pending ask, never an ended one,
 *     and renders nothing at all when only ended asks exist;
 *   - answering the LAST pending ask still shows the ent#468 "sent"
 *     confirmation — the strip must not unmount under it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, shallowMount, flushPromises } from '@vue/test-utils'
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

import PortalAsks from '@/components/portal/PortalAsks.vue'
import PortalConversation from '@/components/portal/PortalConversation.vue'
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', title: `Ask ${id}`, question: 'Go?',
  options: ['Approve', 'Reject'], status: 'pending', created_at: '2026-09-30T10:00:00Z', ...over,
})
const ANSWERED = ask('done', { status: 'answered', ended_at: '2026-09-30T11:00:00Z', ended_by: 'you' })

let wrapper
let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.asksAvailable = true
  store.asksLoaded = true
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

describe('the chat (the strip is gone — ent#610, the 09-30 ruling)', () => {
  // #3115's point survives: an ended ask never piles up as a CARD. The strip
  // above the composer is gone; a chat-turn ask is a tile in its thread, and an
  // ended one there is one muted history row, not a card.
  it("mounts a card only for this chat's waiting chat-turn ask; the answered one is a row", async () => {
    store.asks = [ask('p1', { chat_id: 's1', raised_in_turn: true }), { ...ANSWERED, chat_id: 's1', raised_in_turn: true }]
    store.fetchHistory = vi.fn(async () => ({ sessionId: 's1', messages: [{ id: 'm1', role: 'user', content: 'go', created_at: '2026-09-30T09:00:00Z' }] }))
    wrapper = shallowMount(PortalConversation, {
      props: { agent: { name: 'scout', playbooks: [] }, sessionId: 's1' },
      global: { renderStubDefaultSlot: true },
    })
    await flushPromises()
    const cards = wrapper.findAllComponents(PortalAsks)
    expect(cards.map((c) => c.props('askIds'))).toEqual([['p1']])
    expect(wrapper.findAll('[data-testid="portal-chat-ask-ended"]')).toHaveLength(1)
  })
})

describe('PortalAsks pending-only', () => {
  it('shows a waiting ask and never an answered one', async () => {
    store.asks = [ask('open'), ANSWERED]
    wrapper = mount(PortalAsks, { props: { agentName: 'scout', pendingOnly: true } })
    await flushPromises()
    expect(wrapper.text()).toContain('Ask open')
    expect(wrapper.text()).not.toContain('Ask done')
    expect(wrapper.find('[data-testid="portal-ask-ending"]').exists()).toBe(false)
  })

  it('renders nothing when every ask has ended — no empty strip', async () => {
    store.asks = [ANSWERED]
    wrapper = mount(PortalAsks, { props: { agentName: 'scout', pendingOnly: true } })
    await flushPromises()
    expect(wrapper.find('[data-testid="portal-asks"]').exists()).toBe(false)
  })

  it('answering the last pending ask still shows the sent confirmation', async () => {
    store.asks = [ask('last')]
    store.answerAsk = vi.fn(async (id) => {
      // The server's answered projection replaces the row (#3023).
      store.asks = [ask(id, { status: 'answered', ended_at: '2026-09-30T12:00:00Z', ended_by: 'you' })]
      return store.asks[0]
    })
    wrapper = mount(PortalAsks, { props: { agentName: 'scout', pendingOnly: true }, attachTo: document.body })
    await flushPromises()
    await wrapper.findAll('[data-testid="portal-ask-option-last"]')[0].trigger('click')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(store.answerAsk).toHaveBeenCalled()
    // The answered tile is gone; the component stays mounted for the confirmation.
    expect(wrapper.text()).not.toContain('Ask last')
    expect(wrapper.find('[data-testid="portal-asks"]').exists()).toBe(true)
  })

  it('without pending-only (the Inbox and history), the answered ask is still listed', async () => {
    store.asks = [ask('open'), ANSWERED]
    wrapper = mount(PortalAsks, { props: { agentName: 'scout' } })
    await flushPromises()
    expect(wrapper.text()).toContain('Ask done')
  })
})
