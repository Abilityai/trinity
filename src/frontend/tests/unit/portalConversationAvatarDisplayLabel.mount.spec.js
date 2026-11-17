// @vitest-environment jsdom
/**
 * #4518 — the conversation half. An agent with a display label and no avatar
 * image shows the SAME avatar tile (initials and colour) in the conversation
 * header and beside each of its messages as in the sidebar and the briefing.
 * PortalConversation used to pass the slug (`agent.name`) to PortalAvatar while
 * the text next to it showed the label.
 *
 * Mounted: the real PortalConversation (shallow) with the real PortalAvatar;
 * the store's history read is stubbed at the store seam. Setup follows
 * portalAttachmentChipHandoff.mount.spec.js.
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
import PortalAvatar from '@/components/portal/PortalAvatar.vue'
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const AGENT = { name: 'ap-abc-p105', display_label: 'Zeta Desk', avatar_url: null, playbooks: [] }

let wrapper
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  const store = useClientPortalStore()
  store.fetchHistory = vi.fn(async () => ({
    sessionId: 's1',
    messages: [
      { id: 'm1', role: 'user', content: 'hello' },
      { id: 'm2', role: 'assistant', content: 'hi there' },
    ],
  }))
  store.streamPortalExecution = vi.fn(async () => {})
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

describe('conversation avatars follow the display label (#4518)', () => {
  it('header and transcript tiles use the label for initials and tint', async () => {
    wrapper = shallowMount(PortalConversation, {
      props: { agent: AGENT, sessionId: 's1', roster: [AGENT] },
      global: { stubs: { PortalAvatar: false } },
      attachTo: document.body,
    })
    await flushPromises()

    const header = wrapper.find('[data-testid="portal-agent-picker"]').findComponent(PortalAvatar)
    expect(header.exists()).toBe(true)
    expect(header.props('name')).toBe('Zeta Desk')
    expect(header.text()).toBe('ZD')

    // Header plus the assistant message's tile at least; none may fall back to the slug.
    const tiles = wrapper.findAllComponents(PortalAvatar)
    expect(tiles.length).toBeGreaterThanOrEqual(2)
    for (const t of tiles) expect(t.props('name')).toBe('Zeta Desk')
  })
})
