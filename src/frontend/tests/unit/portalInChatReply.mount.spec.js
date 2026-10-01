// @vitest-environment jsdom
/**
 * trinity-enterprise#738 — reply to one message from INSIDE the Workspace chat.
 *
 * Before this, a reply could only start in the Inbox (its pane's arrow opened
 * the chat with a "replying to" chip). Now every persisted agent message in the
 * chat carries the same action in its own row — Copy · Reply · thumbs — and
 * choosing it sets the SAME `replyTarget` the Inbox sets, so the chip, the send
 * (`reply_to_message_id`) and the server's refusal path are the ones #3054
 * already proved.
 *
 * Mounted (#2918): the real PortalConversation (shallow) with the real
 * PortalAgentBubble and PortalReplyChip; the store's network calls are stubbed
 * at the store seam; the shell's half of the round trip (`reply` → prop back,
 * `reply-done` → cleared) is played by the mount's own event handlers. The
 * shell's own wiring is proven against the real Portal.vue in
 * portalInboxShell.mount.spec.js.
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

// The voice call's flag, made controllable: the real composable, with its
// `isActive` swapped for a ref this spec owns — a live call needs a mic.
const voiceState = vi.hoisted(() => ({ active: null }))
vi.mock('@/composables/useVoiceSession', async (importOriginal) => {
  const actual = await importOriginal()
  const { ref } = await import('vue')
  return {
    ...actual,
    useVoiceSession: (...args) => {
      voiceState.active = ref(false)
      return { ...actual.useVoiceSession(...args), isActive: voiceState.active }
    },
  }
})

import PortalConversation from '@/components/portal/PortalConversation.vue'
import PortalAgentBubble from '@/components/portal/PortalAgentBubble.vue'
import PortalReplyChip from '@/components/portal/PortalReplyChip.vue'
import PortalInboxPane from '@/components/portal/PortalInboxPane.vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { COPY_MESSAGE_ARIA } from '@/utils/clipboard'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const REPLY = '[data-testid="portal-message-reply"]'
const AGENT_TEXT = '**Heads up**: the API returned 429 twice.\n\nDetails follow.'
const EXCERPT = 'Heads up: the API returned 429 twice.'
const HISTORY = [
  { id: 'u1', role: 'user', content: 'check the api' },
  { id: 'r1', role: 'assistant', content: AGENT_TEXT },
  // A reply the server could not persist: no id, so nothing to point a reply at.
  { role: 'assistant', content: 'not persisted' },
]

let wrapper
let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.fetchHistory = vi.fn(async () => ({ sessionId: 's1', messages: HISTORY }))
  store.streamPortalExecution = vi.fn(async () => {})
  store.startPortalChat = vi.fn(async () => ({ execution_id: 'e1', session_id: 's1' }))
  store.cancelPortalTurn = vi.fn(async () => ({ status: 'cancelled' }))
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

// The shell's half: `reply` hands the target back as the prop, `reply-done`
// clears it — exactly what Portal.vue does (proven there separately).
async function mountChat(props = {}) {
  wrapper = shallowMount(PortalConversation, {
    props: {
      agent: { name: 'scout', playbooks: [] }, sessionId: 's1',
      onReply: (target) => wrapper.setProps({ replyTarget: target }),
      'onReply-done': () => wrapper.setProps({ replyTarget: null }),
      ...props,
    },
    global: {
      renderStubDefaultSlot: true,
      stubs: { PortalReplyChip: false, PortalAgentBubble: false },
    },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

const composerChips = () => wrapper.findAllComponents(PortalReplyChip)
  .filter((c) => c.props('placement') === 'composer')
const textarea = () => wrapper.find('textarea')
const replyOn = (id) => wrapper.find(`[data-message-id="${id}"] ${REPLY}`)

describe('the Reply action on a message (ent#738)', () => {
  it('is offered on a persisted agent message — not on your own, not on a row with no id', async () => {
    await mountChat()
    expect(wrapper.findAll(REPLY)).toHaveLength(1)
    expect(replyOn('r1').exists()).toBe(true)
    expect(replyOn('u1').exists()).toBe(false)
    const button = replyOn('r1')
    expect(button.attributes('aria-label')).toBe('Reply to this message')
    expect(button.attributes('title')).toBe('Reply to this message')
  })

  it('hands the shell this chat, the message and its excerpt — and puts the caret in the composer', async () => {
    await mountChat()
    await replyOn('r1').trigger('click')
    expect(wrapper.emitted('reply')).toEqual([[{ sessionId: 's1', messageId: 'r1', excerpt: EXCERPT }]])
    expect(document.activeElement).toBe(textarea().element)
  })

  it('names the chat it RESOLVED, not the prop — a chat opened without a session id in the URL', async () => {
    // The bare-URL path: no session in the props, so the conversation resolves
    // the agent's chat itself (`loadThread(null)` on the agent change) and
    // `currentSessionId` is the only place that chat's id lives.
    await mountChat({ sessionId: null, agent: { name: 'sage', playbooks: [] } })
    await wrapper.setProps({ agent: { name: 'scout', playbooks: [] } })
    await flushPromises()
    expect(store.fetchHistory).toHaveBeenCalledWith('scout', null)
    expect(wrapper.props('sessionId')).toBeNull()
    await replyOn('r1').trigger('click')
    expect(wrapper.emitted('reply')[0][0].sessionId).toBe('s1')
    // …and the chip that comes back for that chat shows on its composer.
    await flushPromises()
    expect(composerChips()).toHaveLength(1)
  })

  it('shows the chip above the composer, tied to the field for a screen reader', async () => {
    await mountChat()
    expect(composerChips()).toHaveLength(0)
    expect(textarea().attributes('aria-describedby')).toBeUndefined()
    await replyOn('r1').trigger('click')
    await flushPromises()
    const chips = composerChips()
    expect(chips).toHaveLength(1)
    expect(chips[0].text()).toContain(EXCERPT)
    // What a screen reader is handed is the chip's sentence — not the chip
    // root, whose remove button would add "Don't reply to this message".
    const id = textarea().attributes('aria-describedby')
    expect(id).toBeTruthy()
    const described = document.getElementById(id)
    expect(chips[0].element.contains(described)).toBe(true)
    expect(described.textContent.replace(/\s+/g, ' ').trim()).toBe(`Replying to: ${EXCERPT}`)
    expect(described.querySelector('button')).toBeNull()
  })

  it('the send carries the id, and the sent message shows what it replied to', async () => {
    await mountChat()
    await replyOn('r1').trigger('click')
    await flushPromises()
    await textarea().setValue('yes, the second one')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    expect(store.startPortalChat.mock.calls[0][3].replyToMessageId).toBe('r1')
    expect(wrapper.emitted('reply-done')).toHaveLength(1)
    expect(composerChips()).toHaveLength(0)
    const sent = wrapper.findAllComponents(PortalReplyChip).filter((c) => c.props('placement') === 'message')
    expect(sent).toHaveLength(1)
    expect(sent[0].props('excerpt')).toBe(EXCERPT)
  })

  it("the chip's remove clears it and gives the caret back to the composer", async () => {
    await mountChat()
    await replyOn('r1').trigger('click')
    await flushPromises()
    // A real click focuses the button first; a synthetic one does not, so do it
    // by hand — otherwise the caret never left the composer and this proves nothing.
    const remove = wrapper.find('[data-testid="portal-reply-chip-remove"]')
    remove.element.focus()
    expect(document.activeElement).toBe(remove.element)
    await remove.trigger('click')
    await flushPromises()
    expect(wrapper.emitted('reply-done')).toHaveLength(1)
    expect(composerChips()).toHaveLength(0)
    expect(document.activeElement).toBe(textarea().element)
  })

  it('is disabled in place during a voice call — the composer it would focus is inert', async () => {
    await mountChat()
    expect(replyOn('r1').attributes('disabled')).toBeUndefined()
    voiceState.active.value = true
    await flushPromises()
    // Still there (no row reflow), just not live.
    expect(replyOn('r1').exists()).toBe(true)
    expect(replyOn('r1').attributes('disabled')).toBeDefined()
    await replyOn('r1').trigger('click')
    expect(wrapper.emitted('reply')).toBeUndefined()
  })
})

describe('Esc clears the chip — innermost first (ent#738 ruling)', () => {
  const esc = (el) => el.trigger('keydown', { key: 'Escape' })

  it('Esc in the composer drops the reply', async () => {
    await mountChat()
    await replyOn('r1').trigger('click')
    await flushPromises()
    await esc(textarea())
    await flushPromises()
    expect(wrapper.emitted('reply-done')).toHaveLength(1)
    expect(composerChips()).toHaveLength(0)
  })

  it('with a turn in flight, the first Esc drops the reply and the turn keeps running; the next Esc stops it', async () => {
    // A turn that is running and stays running: dispatched, stream never ends.
    store.streamPortalExecution = vi.fn(() => new Promise(() => {}))
    await mountChat()
    await textarea().setValue('look into it')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    // The chip is set AFTER the turn started (send clears a chip it carried).
    await replyOn('r1').trigger('click')
    await flushPromises()
    expect(composerChips()).toHaveLength(1)

    await esc(textarea())
    await flushPromises()
    expect(composerChips()).toHaveLength(0)
    expect(store.cancelPortalTurn).not.toHaveBeenCalled()

    // Positive control: with no chip left, the same key reaches the turn.
    await esc(textarea())
    await flushPromises()
    expect(store.cancelPortalTurn).toHaveBeenCalledTimes(1)
  })

  it("a target for ANOTHER chat (no chip here) never eats the Escape that stops this chat's turn", async () => {
    store.streamPortalExecution = vi.fn(() => new Promise(() => {}))
    await mountChat({ replyTarget: { sessionId: 's-other', messageId: 'm9', excerpt: 'elsewhere' } })
    expect(composerChips()).toHaveLength(0)
    await textarea().setValue('look into it')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    await esc(textarea())
    await flushPromises()
    expect(store.cancelPortalTurn).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('reply-done')).toBeUndefined()
  })

  it('an Escape an overlay already claimed (capture phase) leaves the chip alone', async () => {
    await mountChat()
    await replyOn('r1').trigger('click')
    await flushPromises()
    const claim = (e) => { if (e.key === 'Escape') e.preventDefault() }
    document.addEventListener('keydown', claim, true)
    try {
      await esc(textarea())
      await flushPromises()
    } finally {
      document.removeEventListener('keydown', claim, true)
    }
    expect(wrapper.emitted('reply-done')).toBeUndefined()
    expect(composerChips()).toHaveLength(1)
  })

  it('with the @ typeahead open, Esc closes the typeahead and keeps the reply', async () => {
    // The @ picker is offered only where rooms exist (#2128).
    store.multiAgentChatAvailable = true
    await mountChat({ roster: [{ name: 'scout' }, { name: 'sage' }] })
    await replyOn('r1').trigger('click')
    await flushPromises()
    const el = textarea()
    await el.setValue('@')
    el.element.setSelectionRange(1, 1)
    await el.trigger('input')
    await flushPromises()
    expect(wrapper.findComponent({ name: 'PortalTypeahead' }).exists()).toBe(true)
    await esc(el)
    await flushPromises()
    expect(wrapper.findComponent({ name: 'PortalTypeahead' }).exists()).toBe(false)
    expect(wrapper.emitted('reply-done')).toBeUndefined()
    expect(composerChips()).toHaveLength(1)
  })
})

describe('the bubble owns the row (ent#738)', () => {
  const mountBubble = (props = {}, slot = '') => mount(PortalAgentBubble, {
    props: { content: 'Done.', ...props },
    slots: slot ? { default: slot } : {},
    global: { stubs: { PortalMarkdown: true } },
  })

  it('offers no Reply unless the host names it', () => {
    expect(mountBubble().find(REPLY).exists()).toBe(false)
  })

  it('Copy · Reply · the slotted rating, in that order, one button recipe for both', () => {
    const w = mountBubble({ replyLabel: 'Reply to this message' }, '<span data-testid="slotted">👍</span>')
    const copy = w.find(`button[aria-label="${COPY_MESSAGE_ARIA}"]`).element
    const reply = w.find(REPLY).element
    const slotted = w.find('[data-testid="slotted"]').element
    expect(copy.compareDocumentPosition(reply) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(reply.compareDocumentPosition(slotted) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // One recipe: the same classes on both, at the thumbs' ink (gray-500 light,
    // gray-400 dark — 3:1 for an icon on white, which gray-400 is not).
    expect([...reply.classList].sort()).toEqual([...copy.classList].sort())
    expect(reply.classList.contains('text-gray-500')).toBe(true)
    expect(reply.classList.contains('dark:text-gray-400')).toBe(true)
  })

  it('emits reply on click, and not while disabled', async () => {
    const w = mountBubble({ replyLabel: 'Reply to this message' })
    await w.find(REPLY).trigger('click')
    expect(w.emitted('reply')).toHaveLength(1)
    await w.setProps({ replyDisabled: true })
    expect(w.find(REPLY).attributes('disabled')).toBeDefined()
    await w.find(REPLY).trigger('click')
    expect(w.emitted('reply')).toHaveLength(1)
  })
})

describe('the Inbox pane uses the same action (ent#738)', () => {
  it("its message's Reply still opens the chat at that message with the target", async () => {
    store.fetchHistory = vi.fn(async () => ({ sessionId: 't1', messages: [
      { id: 'm1', role: 'user', content: 'run the report', created_at: '2026-10-01T09:00:00Z' },
      { id: 'm2', role: 'assistant', content: AGENT_TEXT, created_at: '2026-10-01T09:01:00Z' },
    ] }))
    store.fetchDeliverables = vi.fn(async () => [])
    const item = { key: 'thread:t1', type: 'thread', id: 't1', agent_name: 'scout', unread: 1, first_unread_id: 'm2' }
    wrapper = mount(PortalInboxPane, {
      props: { item, agentLabel: 'Scout' },
      global: { stubs: { 'router-link': true, PortalMarkdown: true } },
      attachTo: document.body,
    })
    await flushPromises()
    const reply = wrapper.find(`[data-testid="inbox-pane-message-m2"] ${REPLY}`)
    expect(reply.exists()).toBe(true)
    expect(reply.attributes('aria-label')).toBe('Reply to this message in the chat')
    // The pane trades Copy for Reply (sign-off round 6).
    expect(wrapper.find(`[data-testid="inbox-pane-message-m2"] button[aria-label="${COPY_MESSAGE_ARIA}"]`).exists()).toBe(false)
    await reply.trigger('click')
    const [url, target] = wrapper.emitted('reply')[0]
    expect(url).toBe('/workspace/c/t1?anchor=m%3Am2')
    expect(target).toEqual({ sessionId: 't1', messageId: 'm2', excerpt: EXCERPT })
  })
})
