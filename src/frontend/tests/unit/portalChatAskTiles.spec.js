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
  it('waiting → card; ended → row; ended but seen waiting on this visit → still the card', () => {
    expect(askTileMode(ask('p'), new Set())).toBe('card')
    expect(askTileMode(ask('d', { status: 'answered' }), new Set())).toBe('row')
    expect(askTileMode(ask('d', { status: 'answered' }), new Set(['d']))).toBe('card')
  })
  it('the row says kind · title · ending with who · when', () => {
    const now = Date.parse('2026-09-30T12:00:00Z')
    expect(askHistoryLine(ask('d', { status: 'answered', ended_by: 'you', ended_at: '2026-09-30T10:00:00Z' }), now))
      .toEqual({ kind: 'Approval', title: 'Ask d', ending: 'Answered by you', when: '2h ago', at: '2026-09-30T10:00:00Z' })
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

  it('an ask answered while the chat is on screen keeps its card until you leave', async () => {
    store.asks = [ask('live')]
    await open()
    store.asks = [ask('live', { status: 'answered', ended_by: 'you', ended_at: '2026-09-30T10:20:00Z' })]
    await flushPromises()
    expect(wrapper.findAllComponents(PortalAsks)).toHaveLength(1)
    expect(wrapper.find('[data-testid="portal-chat-ask-ended"]').exists()).toBe(false)
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
    expect(CONVERSATION).toMatch(/v-for="\(item, k\) in threadRows"[\s\S]*<PortalAsks\b[\s\S]*<\/template>\s*\n\s*<!-- ent#525/)
    expect(CONVERSATION).not.toMatch(/splitChatAsks|chatAsksLabel|asksOpen/)
  })
})
