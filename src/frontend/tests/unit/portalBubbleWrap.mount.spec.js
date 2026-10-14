// @vitest-environment jsdom
/**
 * #3458 — a long unbroken token must wrap inside its bubble.
 *
 * A 3000-character URL / hash / base64 blob has no break opportunity, so its
 * min-content width IS its full length. The user bubble is a fit-content box
 * (an `items-end` column child), and fit-content never goes below min-content:
 * the bubble measured ~27,900px in a 424px column. `overflow-wrap: anywhere` is
 * the one value that also lowers min-content (`break-word` does not), so it is
 * the contract every Workspace bubble has to carry.
 *
 * WHAT THIS CAN AND CANNOT SEE. jsdom has no layout, so no test here measures a
 * width. What is asserted is that each MOUNTED bubble — user and agent, in the
 * chat and in the Inbox pane — carries the wrapping contract on the element
 * that holds the text. The real width is a browser observation (1440 and 768,
 * both themes) and is recorded on the PR, not claimed here.
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

import PortalConversation from '@/components/portal/PortalConversation.vue'
import PortalAgentBubble from '@/components/portal/PortalAgentBubble.vue'
import PortalInboxPane from '@/components/portal/PortalInboxPane.vue'
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const WRAP = '[overflow-wrap:anywhere]'
const TOKEN = 'a'.repeat(3000)
const AGENT_TOKEN = 'b'.repeat(3000)

// The innermost element whose own text is the token: the box that has to wrap.
function boxHolding(wrapper, text) {
  return wrapper.findAll('*').filter((w) => w.text() === text).pop()
}

let wrapper
let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

describe('a long unbroken token wraps inside the bubble (#3458)', () => {
  it('the chat: the user bubble carries the wrapping contract', async () => {
    store.fetchHistory = vi.fn(async () => ({
      sessionId: 's1',
      messages: [
        { id: 'u1', role: 'user', content: TOKEN },
        { id: 'r1', role: 'assistant', content: AGENT_TOKEN },
      ],
    }))
    wrapper = shallowMount(PortalConversation, {
      props: { agent: { name: 'scout', playbooks: [] }, sessionId: 's1' },
      global: { renderStubDefaultSlot: true, stubs: { PortalAgentBubble: false } },
      attachTo: document.body,
    })
    await flushPromises()

    const user = boxHolding(wrapper, TOKEN)
    expect(user.exists()).toBe(true)
    expect(user.classes()).toContain(WRAP)

    // …and the agent's reply in the same thread, through the real bubble.
    const agent = wrapper.findComponent(PortalAgentBubble)
    expect(agent.props('content')).toBe(AGENT_TOKEN)
    expect(agent.find('.rounded-2xl').classes()).toContain(WRAP)
  })

  it('the agent bubble: the tinted box around the markdown carries it', () => {
    wrapper = mount(PortalAgentBubble, { props: { content: AGENT_TOKEN }, attachTo: document.body })
    // The token rendered as markdown, inside the box that wraps it.
    const box = wrapper.find('.rounded-2xl')
    expect(box.text()).toContain(AGENT_TOKEN)
    expect(box.classes()).toContain(WRAP)
  })

  it('the Inbox pane: the user bubble carries it too', async () => {
    store.fetchHistory = vi.fn(async () => ({ sessionId: 't1', messages: [
      { id: 'm1', role: 'user', content: TOKEN },
      { id: 'm2', role: 'assistant', content: AGENT_TOKEN },
    ] }))
    store.fetchDeliverables = vi.fn(async () => [])
    const item = { key: 'thread:t1', type: 'thread', id: 't1', agent_name: 'scout', unread: 2, first_unread_id: 'm1' }
    wrapper = mount(PortalInboxPane, {
      props: { item, agentLabel: 'Scout' },
      global: { stubs: { 'router-link': true } },
      attachTo: document.body,
    })
    await flushPromises()

    const user = wrapper.find('[data-testid="inbox-pane-message-m1"]')
    expect(user.text()).toBe(TOKEN)
    expect(user.classes()).toContain(WRAP)
    expect(wrapper.find('[data-testid="inbox-pane-message-m2"] .rounded-2xl').classes()).toContain(WRAP)
  })
})
