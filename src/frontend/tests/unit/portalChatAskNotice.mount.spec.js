// @vitest-environment jsdom
/**
 * trinity-enterprise#1134 — what the person in the chat sees of an ask that
 * did not come to them.
 *
 * AC4: an ask raised in this chat's turn that FELL to the operators (nobody is
 * assigned as primary and the owner cannot stand in) is drawn as one muted,
 * content-free line — the kind, "went to the operators", why, and who fixes
 * it. Never the ask's title, question or options: the person is not its
 * addressee, so the line must not become a second door to its content. The
 * chat read carries it as a row marked `notice: true`.
 *
 * And an ask addressed `to: project` says on its card which seat brought it to
 * this reader: "To you as this project's steward" (or creator).
 *
 * Mounted (#2918): the conversation's thread rows, and PortalAsks' card.
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
import { askNoticeLine, chatTurnAsks, chatTurnNotices } from '@/components/portal/portalChatAsks'
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const notice = (id, over = {}) => ({
  notice: true, id, agent_name: 'scout', kind: 'approval', status: 'pending',
  created_at: '2026-11-07T10:01:00Z', chat_id: 's1',
  destination: { kind: 'operators', reason: 'owner_no_email' }, ...over,
})
const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', priority: 'medium', title: `Ask ${id}`, question: 'Go?',
  options: ['Approve', 'Reject'], status: 'pending', created_at: '2026-11-07T10:00:00Z', ...over,
})

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

async function mountChat(chatRows) {
  store.asks = []
  store.fetchHistory = vi.fn(async () => ({
    sessionId: 's1',
    messages: [
      { id: 'm1', role: 'user', content: 'can I get access?', created_at: '2026-11-07T10:00:00Z' },
      { id: 'm2', role: 'assistant', content: 'I asked.', created_at: '2026-11-07T10:02:00Z' },
    ],
  }))
  store.fetchChatTurnAsks = vi.fn(async () => chatRows)
  wrapper = shallowMount(PortalConversation, {
    props: { agent: { name: 'scout', playbooks: [] }, sessionId: 's1' },
    global: { renderStubDefaultSlot: true },
  })
  await flushPromises()
}

describe('the chat: an ask of this turn that fell to the operators (AC4)', () => {
  it('is one muted line in the thread — kind, where it went, why, who fixes it', async () => {
    await mountChat([notice('n1')])
    const lines = wrapper.findAll('[data-testid="portal-chat-ask-notice"]')
    expect(lines).toHaveLength(1)
    const text = lines[0].text()
    expect(text).toContain('Approval')
    expect(text).toContain('went to the operators')
    expect(text).toContain('owner has no email address')
    expect(text).toContain('Access tab')
    // Never an answerable card: the reader is not its addressee.
    expect(wrapper.findAllComponents(PortalAsks)).toHaveLength(0)
    expect(wrapper.findAll('[data-testid="portal-chat-ask-ended"]')).toHaveLength(0)
  })

  it('is placed by time between the message that raised it and the reply', async () => {
    await mountChat([notice('n1')])
    const html = wrapper.html()
    const at = html.indexOf('portal-chat-ask-notice')
    expect(at).toBeGreaterThan(html.indexOf('can I get access?'))
    expect(at).toBeLessThan(html.indexOf('I asked.'))
  })

  it('shows none of the ask — whatever a stray row carries', async () => {
    await mountChat([notice('n1', { title: 'Grant Ada admin', question: 'secret', options: ['Yes'] })])
    const text = wrapper.find('[data-testid="portal-chat-ask-notice"]').text()
    expect(text).not.toContain('Grant Ada admin')
    expect(text).not.toContain('secret')
  })

  it("is drawn only in its own chat", async () => {
    await mountChat([notice('n2', { chat_id: 's2' })])
    expect(wrapper.findAll('[data-testid="portal-chat-ask-notice"]')).toHaveLength(0)
  })

  it('sits beside this chat\'s own ask card without displacing it', async () => {
    await mountChat([ask('p1', { chat_id: 's1', raised_in_turn: true }), notice('n1')])
    expect(wrapper.findAllComponents(PortalAsks).map((c) => c.props('askIds'))).toEqual([['p1']])
    expect(wrapper.findAll('[data-testid="portal-chat-ask-notice"]')).toHaveLength(1)
  })
})

describe('portalChatAsks — the notice rows', () => {
  it('a notice is never an ask of the chat, even stamped like one', () => {
    const n = notice('n1', { raised_in_turn: true })
    expect(chatTurnAsks([n], 's1')).toEqual([])
    expect(chatTurnNotices([n], 's1')).toEqual([n])
  })

  it('only the platform\'s literal `true` makes a notice', () => {
    expect(chatTurnNotices([notice('n1', { notice: 'yes' })], 's1')).toEqual([])
  })

  it('names the reason it has, and the remedy either way', () => {
    expect(askNoticeLine(notice('a')).reason).toContain('owner has no email address')
    expect(askNoticeLine(notice('b', { destination: { kind: 'operators', reason: 'no_owner' } })).reason)
      .toContain('agent has no owner')
    const unknown = askNoticeLine(notice('c', { destination: { kind: 'operators' } }))
    expect(unknown.reason).toContain('nobody is assigned')
    for (const l of [askNoticeLine(notice('a')), unknown]) expect(l.fix).toContain('Access tab')
    expect(askNoticeLine(notice('q', { kind: 'question' })).kind).toBe('Question')
  })
})

describe('PortalAsks: an ask that reached this reader through a project', () => {
  it.each([['steward'], ['creator']])("says the reader's seat: %s", async (seat) => {
    store.asks = [ask('pa', { destination: { kind: 'project', seat } })]
    wrapper = mount(PortalAsks, { props: { agentName: 'scout' } })
    await flushPromises()
    expect(wrapper.find('[data-testid="portal-ask-seat-pa"]').text())
      .toBe(`To you as this project's ${seat}`)
  })

  it('says nothing for an ask with no project destination', async () => {
    store.asks = [ask('a1'), ask('a2', { destination: { kind: 'operators', reason: 'no_owner' } }),
      ask('a3', { destination: { kind: 'project', seat: 'someone' } })]
    wrapper = mount(PortalAsks, { props: { agentName: 'scout' } })
    await flushPromises()
    expect(wrapper.find('[data-testid^="portal-ask-seat-"]').exists()).toBe(false)
  })
})
