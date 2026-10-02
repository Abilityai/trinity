// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A2 — the 2026-09-30 ruling and its amendment, item 1,
 * on ent#734's data (#3137): an ask raised DURING a Workspace chat turn is a
 * tile inside THAT chat's thread, placed by time among the messages and
 * answerable in place (the same PortalAsks card). Once it ended, it stays in the
 * chat's history as one muted row: kind · title · ending (with who) · when.
 *
 * What it replaces: the pinned `splitChatAsks` box above the composer ("N asks
 * waiting on you") and the "N more asks" line under it. Nothing of variable
 * height sits above the composer any more (design principle 30).
 *
 * The rules pinned here:
 *   - only `raised_in_turn === true` (the literal the platform writes) AND
 *     `chat_id` = this chat draws a tile. A background ask (schedule, loop,
 *     gate) carries Main as its `chat_id` only as the reply target: no tile.
 *   - placement is by time: a tile goes before the first thread row that is
 *     strictly newer than the ask; a row with no time of its own (a reply this
 *     client just received) is the newest row. The person's own message is
 *     stamped when it is sent, so a live turn's ask lands between the two.
 *   - an ask seen WAITING during this visit stays a card after it ends (the
 *     ent#468 confirmation is on it); an ask that had already ended is a row.
 *
 * Mounted (#2918): the real PortalConversation (shallow), store seams stubbed.
 *
 * @source-text-pin: one set guard — the conversation holds exactly ONE PortalAsks
 * and it sits inside the thread loop; a mount proves the asks it was given, not
 * that no second mount exists above the composer for asks it was not.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { dirname, join } from 'path'

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
import {
  chatTurnAsks, placeAsksInThread, askTileMode, askHistoryLine,
} from '@/components/portal/portalChatAsks'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const CONVERSATION = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src', 'components', 'portal', 'PortalConversation.vue'),
  'utf8',
)

const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', priority: 'medium', title: `Ask ${id}`, question: 'Go?',
  options: ['Approve', 'Reject'], status: 'pending', created_at: '2026-09-30T10:03:00Z',
  chat_id: 's1', raised_in_turn: true, ended_at: null, ended_by: null, ...over,
})
const msg = (id, at, role = 'assistant') => ({ kind: 'message', index: Number(id.slice(1)), message: { id, role, content: id, at } })

describe('which asks a chat draws', () => {
  const asks = [
    ask('turn'),
    ask('bg', { raised_in_turn: false }),
    ask('other', { chat_id: 's2' }),
    ask('truthy', { raised_in_turn: 'true' }),
    ask('one', { raised_in_turn: 1 }),
    ask('loose', { chat_id: null }),
  ]
  it("only a literal-true chat-turn ask of THIS chat", () => {
    expect(chatTurnAsks(asks, 's1').map((a) => a.id)).toEqual(['turn'])
  })
  it('a background ask attached to Main is not drawn in Main', () => {
    const main = [ask('bg', { chat_id: 'main', raised_in_turn: false }), ask('mturn', { chat_id: 'main' })]
    expect(chatTurnAsks(main, 'main').map((a) => a.id)).toEqual(['mturn'])
  })
  it('no chat on screen draws nothing', () => {
    expect(chatTurnAsks(asks, null)).toEqual([])
    expect(chatTurnAsks(null, 's1')).toEqual([])
  })
})

describe('where a tile sits', () => {
  const items = [msg('m0', '2026-09-30T10:00:00Z', 'user'), msg('m1', '2026-09-30T10:05:00Z'), msg('m2', '2026-09-30T10:10:00Z', 'user')]
  const keys = (rows) => rows.map((r) => (r.kind === 'ask' ? `ask:${r.ask.id}` : r.message.id))

  it('between the rows it was raised between', () => {
    expect(keys(placeAsksInThread(items, [ask('a', { created_at: '2026-09-30T10:03:00Z' })])))
      .toEqual(['m0', 'ask:a', 'm1', 'm2'])
  })
  it('after everything when it is the newest, before everything when it is the oldest', () => {
    expect(keys(placeAsksInThread(items, [ask('late', { created_at: '2026-09-30T11:00:00Z' })])))
      .toEqual(['m0', 'm1', 'm2', 'ask:late'])
    expect(keys(placeAsksInThread(items, [ask('early', { created_at: '2026-09-30T09:00:00Z' })])))
      .toEqual(['ask:early', 'm0', 'm1', 'm2'])
  })
  it('a tie goes after the row, and two asks keep their own order', () => {
    const rows = placeAsksInThread(items, [
      ask('b', { created_at: '2026-09-30T10:06:00Z' }),
      ask('a', { created_at: '2026-09-30T10:05:00Z' }),
    ])
    expect(keys(rows)).toEqual(['m0', 'm1', 'ask:a', 'ask:b', 'm2'])
  })
  it('a row with no time is a reply just received: the newest row', () => {
    const live = [...items, msg('m3', null)]
    expect(keys(placeAsksInThread(live, [ask('a', { created_at: '2026-09-30T10:12:00Z' })])))
      .toEqual(['m0', 'm1', 'm2', 'ask:a', 'm3'])
  })
  it("a voice call block is placed by its first turn's time", () => {
    const call = { kind: 'voice-call', callId: 'c1', turns: [{ id: 'v1', at: '2026-09-30T10:07:00Z' }], label: '' }
    const rows = placeAsksInThread([...items.slice(0, 2), call, items[2]], [ask('a', { created_at: '2026-09-30T10:08:00Z' })])
    expect(rows.map((r) => (r.kind === 'ask' ? 'ask' : r.kind === 'voice-call' ? 'call' : r.message.id)))
      .toEqual(['m0', 'm1', 'call', 'ask', 'm2'])
  })
  it('no asks leaves the rows untouched', () => {
    expect(placeAsksInThread(items, [])).toEqual(items)
  })
})

describe('card or history row', () => {
  it('waiting → card; ended → row, even one seen waiting on this visit (team ruling 2026-10-01: it collapses right away)', () => {
    expect(askTileMode(ask('p'))).toBe('card')
    expect(askTileMode(ask('d', { status: 'answered' }))).toBe('row')
    expect(askTileMode(ask('d', { status: 'answered' }), new Set(['d']))).toBe('row')
  })
  it('the row says kind · title · ending with who · when', () => {
    const now = Date.parse('2026-09-30T12:00:00Z')
    expect(askHistoryLine(ask('d', { status: 'answered', ended_by: 'you', ended_at: '2026-09-30T10:00:00Z' }), now))
      .toEqual({ kind: 'Approval', title: 'Ask d', plain: 'Ask d', ending: 'Answered by you', when: '2h ago', at: '2026-09-30T10:00:00Z' })
    expect(askHistoryLine(ask('c', { kind: 'question', status: 'cancelled', ended_by: 'operator', ended_at: '2026-09-30T11:59:40Z' }), now))
      .toMatchObject({ kind: 'Question', ending: 'Cancelled by the operator', when: 'just now' })
    expect(askHistoryLine(ask('x', { status: 'expired', ended_at: '2026-09-30T11:00:00Z' }), now))
      .toMatchObject({ ending: 'Expired — nobody answered in time', when: '1h ago' })
  })
  it('an ending the platform has no time for says no time', () => {
    expect(askHistoryLine(ask('d', { status: 'answered', ended_by: 'you' }))).toMatchObject({ when: '', at: null })
  })
})

// ---- mounted -------------------------------------------------------------

let wrapper
let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.asksAvailable = true
  store.asksLoaded = true
  store.fetchHistory = vi.fn(async () => ({
    sessionId: 's1',
    messages: [
      { id: 'm1', role: 'user', content: 'Deploy it', created_at: '2026-09-30T10:00:00Z' },
      { id: 'm2', role: 'assistant', content: 'Asked you.', created_at: '2026-09-30T10:05:00Z' },
      { id: 'm3', role: 'user', content: 'Thanks', created_at: '2026-09-30T10:10:00Z' },
    ],
  }))
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

async function open() {
  wrapper = shallowMount(PortalConversation, {
    props: { agent: { name: 'scout', playbooks: [] }, sessionId: 's1' },
    global: { renderStubDefaultSlot: true },
  })
  await flushPromises()
}
const sequence = () => wrapper.findAll('[data-message-id], [data-ask-id]')
  .map((n) => n.attributes('data-message-id') || `ask:${n.attributes('data-ask-id')}`)

describe('the chat thread (mounted)', () => {
  it("forwards a tile's open-thread (Discuss) to the shell (ent#747)", async () => {
    store.asks = [ask('here', { created_at: '2026-09-30T10:03:00Z' })]
    await open()
    wrapper.findComponent(PortalAsks).vm.$emit('open-thread', { id: 'chat-new', agent_name: 'scout' })
    expect(wrapper.emitted('open-thread')).toEqual([[{ id: 'chat-new', agent_name: 'scout' }]])
  })

  it("draws this chat's chat-turn ask among its messages, by time, and nothing else", async () => {
    store.asks = [
      ask('here', { created_at: '2026-09-30T10:03:00Z' }),
      ask('bg', { chat_id: 's1', raised_in_turn: false }),
      ask('elsewhere', { chat_id: 's2' }),
    ]
    await open()
    expect(sequence()).toEqual(['m1', 'ask:here', 'm2', 'm3'])
    const card = wrapper.findAllComponents(PortalAsks)
    expect(card).toHaveLength(1)
    expect(card[0].props('askIds')).toEqual(['here'])
    expect(card[0].props('threadLink')).toBe(false)
    // Its own testid namespace: `portal-chat-ask` would make the card's root
    // `portal-chat-asks`, the retired pinned box's address.
    expect(card[0].props('testidPrefix')).toBe('portal-tile-ask')
  })

  it('an ask that had already ended is one muted history row, not a card', async () => {
    store.asks = [ask('done', { status: 'answered', ended_by: 'you', ended_at: '2026-09-30T10:06:00Z' })]
    await open()
    expect(wrapper.findAllComponents(PortalAsks)).toHaveLength(0)
    const row = wrapper.find('[data-testid="portal-chat-ask-ended"]')
    expect(row.exists()).toBe(true)
    expect(row.text()).toContain('Approval')
    expect(row.text()).toContain('Answered by you')
    expect(sequence()).toEqual(['m1', 'ask:done', 'm2', 'm3'])
  })

  it('an ask answered while the chat is on screen collapses at once into the muted row, which says so aloud and takes focus', async () => {
    // The team's ruling (2026-10-01, on #3101): follow the 09-30 ruling — when
    // the answer is recorded the card collapses right away into the muted row;
    // its confirmation ("Answered by you · just now") is announced via
    // aria-live; focus moves to the row, never to <body>.
    store.asks = [ask('live')]
    wrapper = shallowMount(PortalConversation, {
      props: { agent: { name: 'scout', playbooks: [] }, sessionId: 's1' },
      global: { renderStubDefaultSlot: true },
      attachTo: document.body,
    })
    await flushPromises()
    const live = wrapper.find('[data-testid="portal-chat-ask-announce"]')
    expect(live.exists()).toBe(true)
    expect(live.attributes('aria-live')).toBe('polite')
    expect(live.text()).toBe('')
    document.activeElement?.blur?.()                           // the answered card unmounts under the focus
    store.asks = [ask('live', { status: 'answered', ended_by: 'you', ended_at: new Date().toISOString() })]
    await flushPromises()
    expect(wrapper.findAllComponents(PortalAsks)).toHaveLength(0)
    const row = wrapper.find('[data-testid="portal-chat-ask-ended"]')
    expect(row.exists()).toBe(true)
    expect(row.text()).toContain('Answered by you')
    expect(row.text()).toContain('just now')
    expect(wrapper.find('[data-testid="portal-chat-ask-announce"]').text()).toMatch(/Answered by you · just now/)
    expect(document.activeElement).not.toBe(document.body)
    expect(document.activeElement).toBe(row.element)
  })

  it('an ask that ends while you type in the composer never takes your focus', async () => {
    store.asks = [ask('live')]
    wrapper = shallowMount(PortalConversation, {
      props: { agent: { name: 'scout', playbooks: [] }, sessionId: 's1' },
      global: { renderStubDefaultSlot: true },
      attachTo: document.body,
    })
    await flushPromises()
    const field = document.createElement('textarea')
    document.body.appendChild(field)
    field.focus()
    store.asks = [ask('live', { status: 'answered', ended_by: 'you', ended_at: new Date().toISOString() })]
    await flushPromises()
    expect(document.activeElement).toBe(field)
    field.remove()
  })

  it('an ask that had already ended when the chat opened is not announced', async () => {
    store.asks = [ask('old', { status: 'answered', ended_by: 'you', ended_at: '2026-09-30T10:06:00Z' })]
    await open()
    expect(wrapper.find('[data-testid="portal-chat-ask-announce"]').text()).toBe('')
  })

  it('above the composer there is no asks box and no "more asks" line', async () => {
    store.asks = [ask('here'), ask('other', { chat_id: 's2' })]
    await open()
    expect(wrapper.find('[data-testid="portal-chat-asks"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="portal-chat-asks-toggle"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="portal-chat-asks-elsewhere"]').exists()).toBe(false)
  })

  it('the only PortalAsks in the conversation is the tile inside the thread loop', () => {
    const uses = CONVERSATION.match(/<PortalAsks\b/g) || []
    expect(uses).toHaveLength(1)
    // Anchored on structure, not a comment (round 2, review I1): the tile sits
    // after the thread loop opens and before the thread's jump-to-latest control,
    // which closes the thread region above the composer.
    const loop = CONVERSATION.indexOf('in threadRows"')
    const tile = CONVERSATION.indexOf('<PortalAsks')
    const jump = CONVERSATION.indexOf('<PortalJumpToLatest')
    expect(loop).toBeGreaterThan(-1)
    expect(tile).toBeGreaterThan(loop)
    expect(tile).toBeLessThan(jump)
    expect(CONVERSATION).not.toMatch(/splitChatAsks|chatAsksLabel|asksOpen/)
  })
})

// ---- review round 2 (2026-09-30) -------------------------------------------

describe('where a tile sits across live turns and clocks (round 2: review C2, codex C6/V1)', () => {
  const keys = (rows) => rows.map((r) => (r.kind === 'ask' ? `ask:${r.ask.id}` : r.message.id))
  // The person's own message, as the composer pushes it: no clock of its own
  // (the browser's is not the server's), only the mark that it was sent here.
  const sent = (id) => ({ kind: 'message', index: Number(id.slice(1)), message: { id, role: 'user', content: id, at: null, local: true } })

  it("a second live turn's ask lands after the first turn's reply, not above it", () => {
    const rows = [msg('m0', '2026-09-30T10:00:00Z', 'user'), msg('m1', '2026-09-30T10:01:00Z'), sent('m2')]
    expect(keys(placeAsksInThread(rows, [ask('a', { created_at: '2026-09-30T10:05:10Z' })])))
      .toEqual(['m0', 'm1', 'm2', 'ask:a'])
  })
  it('a sent message is placed by the server rows before it, so no browser clock can lift an ask above it', () => {
    const rows = [msg('m0', '2026-09-30T09:00:00Z'), sent('m1')]
    expect(keys(placeAsksInThread(rows, [ask('a', { created_at: '2026-09-30T10:00:00Z' })])))
      .toEqual(['m0', 'm1', 'ask:a'])
  })
  it('an ask of an EARLIER turn still sits before the reply it came before', () => {
    const rows = [msg('m0', '2026-09-30T10:00:00Z', 'user'), msg('m1', '2026-09-30T10:01:00Z'), sent('m2')]
    expect(keys(placeAsksInThread(rows, [ask('a', { created_at: '2026-09-30T10:00:30Z' })])))
      .toEqual(['m0', 'ask:a', 'm1', 'm2'])
  })
  it('while earlier messages are not shown, an ask older than the first shown row is not stacked on top', () => {
    const rows = [msg('m5', '2026-09-30T10:00:00Z', 'user'), msg('m6', '2026-09-30T10:01:00Z')]
    const asks = [ask('old', { created_at: '2026-09-29T10:00:00Z' }), ask('new', { created_at: '2026-09-30T10:00:30Z' })]
    expect(keys(placeAsksInThread(rows, asks, { truncated: true }))).toEqual(['m5', 'ask:new', 'm6'])
    expect(keys(placeAsksInThread(rows, asks))).toEqual(['ask:old', 'm5', 'ask:new', 'm6'])
  })
  it('the composer marks the sent message as sent here instead of stamping the browser clock', () => {
    const send = CONVERSATION.slice(CONVERSATION.indexOf('async function submitUserText('))
    const push = send.slice(0, send.indexOf(') - 1'))
    expect(push).toMatch(/local: true/)
    expect(push).not.toMatch(/at: new Date\(\)/)
  })
  it('a live reply carries the time the server stored it at', () => {
    const pushes = CONVERSATION.match(/messages\.value\.push\(\{\s*\.\.\.assistantRow\([\s\S]*?\),\s*at: data\.at \|\| null/g) || []
    expect(pushes).toHaveLength(2)
  })
})

describe('the chat thread across chat switches and reads (round 2: review C1/I3, codex C3)', () => {
  const answered = (id, over = {}) => ask(id, { status: 'answered', ended_by: 'you', ended_at: '2026-09-30T10:20:00Z', ...over })

  it('an ask answered right after switching to its chat collapses and is announced (the switch kept it as seen)', async () => {
    store.asks = [ask('there', { chat_id: 's2' })]
    // The chat's own read still in flight: nothing but the switch and the
    // answer may decide the card (a resolving read would re-add the ids).
    store.fetchChatTurnAsks = vi.fn(() => new Promise(() => {}))
    await open()
    await wrapper.setProps({ sessionId: 's2' })
    await flushPromises()
    store.asks = [answered('there', { chat_id: 's2', ended_at: new Date().toISOString() })]
    await flushPromises()
    expect(wrapper.findAllComponents(PortalAsks)).toHaveLength(0)
    expect(wrapper.find('[data-testid="portal-chat-ask-ended"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="portal-chat-ask-announce"]').text()).toMatch(/Answered by you/)
  })

  it('answered, then away and back: one muted row that survived the switch', async () => {
    store.asks = [ask('live')]
    await open()
    store.asks = [answered('live')]
    await flushPromises()
    await wrapper.setProps({ sessionId: 's2' })
    await flushPromises()
    await wrapper.setProps({ sessionId: 's1' })
    await flushPromises()
    expect(wrapper.findAllComponents(PortalAsks)).toHaveLength(0)
    expect(wrapper.find('[data-testid="portal-chat-ask-ended"]').exists()).toBe(true)
  })

  it("a chat keeps an ended ask the Inbox read no longer carries (the chat's own read)", async () => {
    store.asks = []
    store.fetchChatTurnAsks = vi.fn(async () => [answered('old', { created_at: '2026-06-01T10:03:00Z', ended_at: '2026-06-01T10:04:00Z' })])
    await open()
    expect(store.fetchChatTurnAsks).toHaveBeenCalledWith('s1')
    expect(wrapper.find('[data-testid="portal-chat-ask-ended"]').exists()).toBe(true)
  })

  it("the store's fresher row wins over the chat read for the same ask", async () => {
    store.asks = [answered('x')]                          // the poll saw it answered
    store.fetchChatTurnAsks = vi.fn(async () => [ask('x')]) // the chat read, older: still waiting
    await open()
    expect(wrapper.findAllComponents(PortalAsks)).toHaveLength(0)
    expect(wrapper.findAll('[data-testid="portal-chat-ask-ended"]')).toHaveLength(1)
  })

  it("a chat read that fails leaves the store's tiles in place", async () => {
    store.asks = [ask('here')]
    store.fetchChatTurnAsks = vi.fn(async () => { throw new Error('503') })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    await open()
    expect(wrapper.findAllComponents(PortalAsks)).toHaveLength(1)
    expect(warn.mock.calls.some((c) => String(c[0]).includes('[workspace] chat asks unavailable'))).toBe(true)
    warn.mockRestore()
  })

  it("a read for the chat you already left never lands in the one you're on", async () => {
    let release
    store.asks = []
    store.fetchChatTurnAsks = vi.fn((sid) => (sid === 's1'
      ? new Promise((r) => { release = () => r([answered('stale')]) })
      : Promise.resolve([answered('mine', { chat_id: 's2' })])))
    await open()
    await wrapper.setProps({ sessionId: 's2' })
    await flushPromises()
    expect(wrapper.findAll('[data-testid="portal-chat-ask-ended"]')).toHaveLength(1)
    release()                                             // s1's read lands late
    await flushPromises()
    const rows = wrapper.findAll('[data-testid="portal-chat-ask-ended"]')
    expect(rows).toHaveLength(1)                          // s2's own history is still there
    expect(rows[0].element.closest('[data-ask-id]').getAttribute('data-ask-id')).toBe('mine')
  })

  it('the history row is AA text in light (gray-500), and its full title is one hover away', async () => {
    store.asks = [answered('done', { title: 'A **long** title' })]
    await open()
    const row = wrapper.find('[data-testid="portal-chat-ask-ended"]')
    expect(row.classes()).toContain('text-gray-500')
    expect(row.classes()).not.toContain('text-gray-400')
    expect(row.attributes('title')).toBe('A long title')
  })
})

// @source-text-pin (declared): the reply-landing paths and the stick-to-bottom
// arrival need a live turn / a real scroll box that a shallow mount has neither of.
describe('a turn landing reads the asks; a new tile is an arrival (round 2: plan-design P1-B, QA mobile F3)', () => {
  it('both reply-landing paths read the asks, and the read is the store one the poll uses', () => {
    expect((CONVERSATION.match(/\n\s*refreshAsksAfterTurn\(\)\n/g) || [])).toHaveLength(2)
    const fn = CONVERSATION.slice(CONVERSATION.indexOf('function refreshAsksAfterTurn('))
    expect(fn.slice(0, fn.indexOf('\n}'))).toMatch(/store\.fetchAsks\(\)/)
  })
  it('a waiting tile new to the chat on screen counts toward "jump to latest"', () => {
    expect(CONVERSATION).toMatch(/if \(sameChat && fresh\.length\) onMessagesArrived\(fresh\.length\)/)
  })
})
