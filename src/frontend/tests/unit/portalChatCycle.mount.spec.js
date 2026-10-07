// @vitest-environment jsdom
/**
 * ent#621 [B] — `cycleChat`: ⌥⇧↑ / ⌥⇧↓ walk the chat tabs.
 *
 * The walk belongs to the conversation because the STRIP's inputs do — the
 * thread list, the active id, whether the provisional "New chat" tab is listed,
 * and the drafts key set. A shell that rebuilt them would be a second opinion
 * about which tabs exist, free to disagree with the tabs on screen.
 *
 * So what has to be proven here is that the key walks the SAME list the strip
 * renders, in the same order, and ends in the same emit a tab CLICK makes —
 * `open-thread` for a real chat, `new-chat` for the provisional one. Mounted
 * (#2918): a regex over the component's source would pass a `delta` applied the
 * wrong way round byte-identically.
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
import { usePortalDraftsStore } from '@/stores/portalDrafts'
import { agentChatTabs } from '@/components/portal/portalUtils'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

// Main pinned first, then recency — the strip's own order.
const MAIN = { id: 'm', session_id: 'm', agent_name: 'scout', is_main: true, title: 'Main', last_message_at: '2026-09-20T10:00:00Z' }
const T2 = { id: 't2', session_id: 't2', agent_name: 'scout', title: 'Pricing page', last_message_at: '2026-09-27T10:00:00Z' }
const T3 = { id: 't3', session_id: 't3', agent_name: 'scout', title: 'Q3 numbers', last_message_at: '2026-09-26T10:00:00Z' }
const OTHER = { id: 'x1', session_id: 'x1', agent_name: 'sage', title: 'Not this agent', last_message_at: '2026-09-28T10:00:00Z' }

let wrapper
let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.fetchHistory = vi.fn(async () => ({ sessionId: 'm', messages: [] }))
  store.streamPortalExecution = vi.fn(async () => {})
  store.startPortalChat = vi.fn(async () => ({ execution_id: 'e1', session_id: 'm' }))
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

async function mountChat(props = {}) {
  wrapper = shallowMount(PortalConversation, {
    props: { agent: { name: 'scout', playbooks: [] }, threads: [MAIN, T2, T3, OTHER], sessionId: 'm', ...props },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

const opened = () => (wrapper.emitted('open-thread') || []).map(([t]) => t.id)
const newChats = () => (wrapper.emitted('new-chat') || []).length

describe('ent#621 — cycleChat walks the strip', () => {
  it('moves forward in the strip’s own order, and wraps at the end', async () => {
    await mountChat()
    // The order this must agree with, computed the way the strip computes it.
    expect(agentChatTabs([MAIN, T2, T3, OTHER], 'scout', { activeId: 'm' }).map((t) => t.id))
      .toEqual(['m', 't2', 't3'])

    wrapper.vm.cycleChat(1)
    expect(opened()).toEqual(['t2'])
    // The walk reads the CURRENT selection, which the shell has not changed yet
    // (a real switch remounts this component), so a second press from the same
    // state asks for the same neighbour — not a drift of its own.
    await wrapper.setProps({ sessionId: 't3' })
    wrapper.vm.cycleChat(1)
    expect(opened()).toEqual(['t2', 'm'])          // past the last tab is the first
  })

  it('moves backward, and wraps the other way', async () => {
    await mountChat()
    wrapper.vm.cycleChat(-1)
    expect(opened()).toEqual(['t3'])               // before Main is the last tab
    await wrapper.setProps({ sessionId: 't2' })
    wrapper.vm.cycleChat(-1)
    expect(opened()).toEqual(['t3', 'm'])
  })

  it('only this agent’s chats are in the walk', async () => {
    await mountChat()
    wrapper.vm.cycleChat(1)
    wrapper.vm.cycleChat(-1)
    expect(opened()).not.toContain('x1')
  })

  it('from an unsent new chat, one press opens the most recent earlier chat', async () => {
    // The provisional tab is the current selection, and `currentSessionId` is
    // null while it is — so the walk has to start from the tab, not the id.
    await mountChat({ sessionId: null, newChat: true })
    wrapper.vm.cycleChat(1)
    expect(opened()).toEqual(['t2'])
    expect(newChats()).toBe(0)
  })

  it('backwards from an unsent new chat is the tab before it in the strip', async () => {
    // Not a wrap: the provisional tab sits after pinned Main, so the tab before
    // it is Main. The walk follows the strip's positions, not a separate idea of
    // where "new" belongs.
    await mountChat({ sessionId: null, newChat: true })
    wrapper.vm.cycleChat(-1)
    expect(opened()).toEqual(['m'])
  })

  it('landing on the listed unsaved chat asks for a new chat, exactly as clicking it does', async () => {
    // trinity-enterprise#657: a `new:<agent>` draft is listed as the provisional
    // tab even while another chat is open, and that tab carries no thread row —
    // emitting it would hand the shell a null to `openThread`. A click answers
    // `new-chat`; so does the key. One door, two gestures.
    const drafts = usePortalDraftsStore()
    drafts.identity = 'client@example.com'
    drafts.set('new:scout', 'half a sentence')
    await mountChat({ sessionId: 'm', newChat: false })
    expect(wrapper.vm.cycleChat).toBeTypeOf('function')
    wrapper.vm.cycleChat(1)
    expect(opened()).toEqual([])
    expect(newChats()).toBe(1)
  })

  it('fewer than two tabs is a silent no-op', async () => {
    await mountChat({ threads: [MAIN], sessionId: 'm' })
    wrapper.vm.cycleChat(1)
    wrapper.vm.cycleChat(-1)
    expect(opened()).toEqual([])
    expect(newChats()).toBe(0)
  })

  it('is reachable from the shell — exposed, and by name', async () => {
    // `conversationRef.value?.cycleChat?.(delta)` is optional-chained twice, so
    // an unexposed function is a silent no-op rather than an error. This is the
    // assertion that notices.
    await mountChat()
    expect(typeof wrapper.vm.cycleChat).toBe('function')
    expect(typeof wrapper.vm.focusComposer).toBe('function')
  })
})
