// @vitest-environment jsdom
/**
 * #4347 — the composer placeholder says only "Message {agent}…", and the two
 * typeahead affordances are taught ONCE: a dismissible one-line hint in the
 * composer's control row on a person's first visit, remembered per browser.
 * The "@" half appears only where a chat may hold more than one agent.
 *
 * The hint sits in the control row that already exists, so showing or
 * dismissing it moves nothing.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
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
      { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import PortalConversation from '@/components/portal/PortalConversation.vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { COMPOSER_HINT_KEY } from '@/components/portal/portalUtils'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const AGENT = { name: 'scout', playbooks: [], pulls_turns: false }
const HINT = '[data-testid="portal-composer-hint"]'
let store
let wrapper

beforeEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.fetchHistory = vi.fn(async () => ({ sessionId: 's1', messages: [], inFlightExecutionId: null, lastTurnOutcome: null }))
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

async function mountChat() {
  wrapper = mount(PortalConversation, {
    props: { agent: AGENT, sessionId: 's1' },
    global: { renderStubDefaultSlot: true },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

describe('#4347 — the composer placeholder', () => {
  it('is "Message {agent}…" and nothing else, whatever the capability', async () => {
    store.multiAgentChatAvailable = true
    const w = await mountChat()
    expect(w.get('textarea').attributes('placeholder')).toBe('Message scout…')
  })
})

describe('#4347 — the first-visit hint', () => {
  it('teaches / only, when a chat cannot hold another agent', async () => {
    store.multiAgentChatAvailable = false
    const w = await mountChat()
    const text = w.get(HINT).text()
    expect(text).toContain('Type / to pick a skill')
    expect(text).not.toContain('@')
  })

  it('teaches / and @ when it can', async () => {
    store.multiAgentChatAvailable = true
    const w = await mountChat()
    expect(w.get(HINT).text()).toContain('Type / to pick a skill, @ to bring in another agent')
  })

  it('a dismissal hides it and is remembered for this browser', async () => {
    const w = await mountChat()
    await w.get('[data-testid="portal-composer-hint-dismiss"]').trigger('click')
    expect(w.find(HINT).exists()).toBe(false)
    expect(localStorage.getItem(COMPOSER_HINT_KEY)).toBe('dismissed')
    w.unmount(); wrapper = null
    const again = await mountChat()
    expect(again.find(HINT).exists()).toBe(false)
  })

  it('a storage that refuses still shows it, and the dismissal still works for this page', async () => {
    const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied') })
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied') })
    try {
      const w = await mountChat()
      expect(w.find(HINT).exists()).toBe(true)
      await w.get('[data-testid="portal-composer-hint-dismiss"]').trigger('click')
      expect(w.find(HINT).exists()).toBe(false)
    } finally {
      get.mockRestore(); set.mockRestore()
    }
  })

  it('lives inside the composer\'s control row, so it adds no row of its own', async () => {
    const w = await mountChat()
    const hint = w.get(HINT).element
    const attach = w.get('[title="Attach a file for the agent"]').element
    expect(hint.parentElement).toBe(attach.parentElement)
  })
})
