// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A2 — the 2026-09-30 ruling as amended, item 2: an
 * ask raised by a BACKGROUND process (schedule, loop, gate — `raised_in_turn`
 * not true) lives in the Inbox only.
 *
 *   - no tile in Main or in any chat, and no history row there;
 *   - the Inbox's Action tab lists it while it waits, and its All tab is its
 *     history once it ended;
 *   - `chat_id` stays Main on the row — the pane's reply lands there — but no
 *     surface points at Main as the place the ask lives: an ask card offers
 *     "Open the conversation" only for an ask a chat turn raised.
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
import { askThreadLink } from '@/components/portal/portalUtils'
import { actionItems, allItems } from '@/components/portal/portalInbox'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const bg = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', priority: 'medium', title: `Nightly ${id}`, question: 'Ship?',
  options: ['Approve', 'Reject'], status: 'pending', created_at: '2026-09-30T10:03:00Z', expires_at: null,
  chat_id: 'main', raised_in_turn: false, ended_at: null, ended_by: null, sync: 'confirmed', aging: false, ...over,
})
const ENDED = bg('done', { status: 'answered', ended_by: 'you', ended_at: '2026-09-30T11:00:00Z' })

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

describe('a background ask draws in no chat', () => {
  it('Main — the chat its chat_id names — shows neither the waiting ask nor the ended one', async () => {
    store.asks = [bg('open'), ENDED]
    store.fetchHistory = vi.fn(async () => ({
      sessionId: 'main',
      messages: [{ id: 'm1', role: 'assistant', content: 'Morning.', created_at: '2026-09-30T09:00:00Z' }],
    }))
    wrapper = shallowMount(PortalConversation, {
      props: { agent: { name: 'scout', playbooks: [] }, sessionId: 'main', threads: [{ id: 'main', agent_name: 'scout', is_main: true }] },
      global: { renderStubDefaultSlot: true },
    })
    await flushPromises()
    expect(wrapper.find('[data-ask-id]').exists()).toBe(false)
    expect(wrapper.findAllComponents(PortalAsks)).toHaveLength(0)
    expect(wrapper.find('[data-testid="portal-chat-ask-ended"]').exists()).toBe(false)
  })
})

describe('the Inbox is its home', () => {
  it('Action lists it while it waits; All keeps it once it ended', () => {
    const asks = [bg('open'), ENDED]
    expect(actionItems(asks.filter((a) => a.status === 'pending')).map((i) => i.id)).toEqual(['open'])
    expect(allItems([], asks, {}).filter((i) => i.type === 'ask').map((i) => i.id).sort()).toEqual(['done', 'open'])
  })
})

describe('no card points at Main as where a background ask lives', () => {
  it('askThreadLink: only an ask a chat turn raised links to its conversation', () => {
    expect(askThreadLink(bg('x'), null)).toBeNull()
    expect(askThreadLink(bg('x', { raised_in_turn: 'true' }), null)).toBeNull()
    expect(askThreadLink(bg('x', { raised_in_turn: true, chat_id: 's1' }), null)).toBe('s1')
  })
  it('a card with the thread link on offers no "Open the conversation" for it', async () => {
    store.asks = [bg('b1')]
    wrapper = mount(PortalAsks, { props: { agentName: 'scout', currentSessionId: 'elsewhere' } })
    await flushPromises()
    expect(wrapper.find('[data-testid="portal-ask-b1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="portal-ask-open-thread-b1"]').exists()).toBe(false)
  })
})
