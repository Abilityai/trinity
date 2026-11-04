// @vitest-environment jsdom
/**
 * #3795 — a Main that Reset just minted reads as a NEW chat.
 *
 * Reset (ent#523) writes one `role: 'system'` signpost into the fresh Main
 * ("Main was reset. The previous conversation is saved as …"). The renderer
 * gated the new-chat state on `messages.length === 0`, so that one line hid the
 * briefing and suggestions and the chat looked blank. The backend, the landing
 * rule (`agentEmptyChat`) and Reset's own no-op check already counted the row as
 * empty; only the renderer disagreed.
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
import { isNewChatTranscript } from '@/components/portal/portalUtils'
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const NOTICE = 'Main was reset. The previous conversation is saved as “Earlier”.'

let wrapper
let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

async function mountWith(messages) {
  store.fetchHistory = vi.fn(async () => ({ sessionId: 's1', messages }))
  wrapper = shallowMount(PortalConversation, {
    props: { agent: { name: 'scout', playbooks: [] }, sessionId: 's1' },
    slots: { empty: '<div data-testid="briefing">briefing</div>' },
    global: { renderStubDefaultSlot: true },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

describe('the new-chat state survives the reset signpost (#3795)', () => {
  it('a Main holding only the reset notice shows the briefing, under the notice', async () => {
    const w = await mountWith([{ id: 'n1', role: 'system', content: NOTICE }])
    const line = w.find('[data-testid="portal-system-line"]')
    const briefing = w.find('[data-testid="briefing"]')
    expect(line.text()).toBe(NOTICE)
    expect(briefing.exists()).toBe(true)
    // The signpost reads first: it says where the history went, then the
    // briefing invites the first message.
    expect(line.element.compareDocumentPosition(briefing.element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('an empty chat still shows the briefing', async () => {
    const w = await mountWith([])
    expect(w.find('[data-testid="briefing"]').exists()).toBe(true)
  })

  it('a chat with a turn shows no briefing', async () => {
    const w = await mountWith([
      { id: 'n1', role: 'system', content: NOTICE },
      { id: 'u1', role: 'user', content: 'hello' },
    ])
    expect(w.find('[data-testid="briefing"]').exists()).toBe(false)
  })
})

describe('isNewChatTranscript (#3795)', () => {
  it('counts only turns, never system lines', () => {
    expect(isNewChatTranscript([])).toBe(true)
    expect(isNewChatTranscript([{ role: 'system', content: NOTICE }])).toBe(true)
    expect(isNewChatTranscript([{ role: 'system' }, { role: 'user', content: 'hi' }])).toBe(false)
    expect(isNewChatTranscript([{ role: 'assistant', content: 'hello' }])).toBe(false)
  })

  it('tolerates a missing or malformed list', () => {
    expect(isNewChatTranscript(undefined)).toBe(true)
    expect(isNewChatTranscript([null])).toBe(true)
  })
})
