/**
 * trinity-enterprise#856 Part A — the chat strip holds still for a visit.
 *
 * `tabOrderFrom` takes the arrival snapshot (Main first, then recency, every
 * non-room row of the agent including archived ones); `agentChatTabs` given
 * `{ order: { agent, ids } }` draws the strip in that frozen order, so a reply
 * or a re-sorted thread list changes a badge and never a position.
 */
import { describe, it, expect } from 'vitest'
import { agentChatTabs, tabOrderFrom, NEW_CHAT_TAB_ID } from '../../src/components/portal/portalUtils.js'

const row = (id, extra = {}) => ({ id, agent_name: 'ada', created_at: '2026-10-01T00:00:00Z', ...extra })
const main = row('m', { is_main: true, last_message_at: '2026-10-01T00:00:00Z' })
const a = row('a', { last_message_at: '2026-10-05T00:00:00Z', created_at: '2026-10-02T00:00:00Z' })
const b = row('b', { last_message_at: '2026-10-04T00:00:00Z', created_at: '2026-10-03T00:00:00Z' })
const c = row('c', { last_message_at: '2026-10-03T00:00:00Z', created_at: '2026-10-04T00:00:00Z' })
const ids = (tabs) => tabs.map((t) => t.id)

describe('tabOrderFrom — the arrival snapshot', () => {
  it('is Main first, then recency, ties in list order', () => {
    expect(tabOrderFrom([c, b, main, a], 'ada')).toEqual(['m', 'a', 'b', 'c'])
    const t1 = row('x1', { last_message_at: '2026-10-09T00:00:00Z' })
    const t2 = row('x2', { last_message_at: '2026-10-09T00:00:00Z' })
    expect(tabOrderFrom([t1, t2], 'ada')).toEqual(['x1', 'x2'])
  })

  it('includes archived rows and excludes rooms and other agents', () => {
    const arch = row('z', { archived_at: '2026-10-06T00:00:00Z', last_message_at: '2026-10-06T00:00:00Z' })
    const room = row('r', { is_room: true })
    const other = { ...a, id: 'o', agent_name: 'bob' }
    expect(tabOrderFrom([main, a, arch, room, other], 'ada')).toEqual(['m', 'z', 'a'])
  })

  it('is empty without an agent', () => {
    expect(tabOrderFrom([main, a], null)).toEqual([])
  })
})

describe('agentChatTabs({ order }) — the strip holds still', () => {
  const order = { agent: 'ada', ids: ['m', 'a', 'b', 'c'] }

  it('a re-sorted list mid-visit keeps every position; only the badge changes (AC 1)', () => {
    const before = agentChatTabs([main, a, b, c], 'ada', { activeId: 'a', order })
    const cNew = { ...c, last_message_at: '2026-10-10T00:00:00Z', unread: 2 }
    const after = agentChatTabs([cNew, main, b, a], 'ada', { activeId: 'a', order })
    expect(ids(before)).toEqual(['m', 'a', 'b', 'c'])
    expect(ids(after)).toEqual(['m', 'a', 'b', 'c'])
    expect(after[3].badge).toBe('2')
    expect(before[3].badge).toBe(null)
  })

  it('a send in the open chat does not move it (AC 2)', () => {
    const bSent = { ...b, last_message_at: '2026-10-11T00:00:00Z' }
    expect(ids(agentChatTabs([main, a, bSent, c], 'ada', { activeId: 'b', order }))).toEqual(['m', 'a', 'b', 'c'])
  })

  it('without an order the strip is the arrival sort (AC 3)', () => {
    const cNew = { ...c, last_message_at: '2026-10-10T00:00:00Z' }
    expect(ids(agentChatTabs([main, a, b, cNew], 'ada'))).toEqual(['m', 'c', 'a', 'b'])
  })

  it('a chat born mid-visit sits directly after Main, where the provisional tab was (AC 4)', () => {
    const born = row('n', { created_at: '2026-10-12T00:00:00Z' })
    expect(ids(agentChatTabs([main, a, b, c, born], 'ada', { activeId: 'n', order }))).toEqual(['m', 'n', 'a', 'b', 'c'])
    const prov = agentChatTabs([main, a, b, c], 'ada', { activeId: null, draft: true, order })
    expect(ids(prov)).toEqual(['m', NEW_CHAT_TAB_ID, 'a', 'b', 'c'])
  })

  it('two born chats keep created_at order when the older one gets a reply', () => {
    const n1 = row('n1', { created_at: '2026-10-12T00:00:00Z', last_message_at: '2026-10-13T09:00:00Z' })
    const n2 = row('n2', { created_at: '2026-10-12T05:00:00Z', last_message_at: '2026-10-12T06:00:00Z' })
    expect(ids(agentChatTabs([main, a, b, c, n1, n2], 'ada', { order }))).toEqual(['m', 'n2', 'n1', 'a', 'b', 'c'])
  })

  it('an archived chat drops out and returns to its own slot on Undo (AC 4)', () => {
    const bArch = { ...b, archived_at: '2026-10-13T00:00:00Z' }
    expect(ids(agentChatTabs([main, a, bArch, c], 'ada', { activeId: 'a', order }))).toEqual(['m', 'a', 'c'])
    expect(ids(agentChatTabs([main, a, b, c], 'ada', { activeId: 'a', order }))).toEqual(['m', 'a', 'b', 'c'])
  })

  it('a deep-linked archived chat is drawn at its arrival slot (AC 6)', () => {
    const z = row('z', { archived_at: '2026-10-06T00:00:00Z', last_message_at: '2026-10-04T12:00:00Z' })
    const snap = { agent: 'ada', ids: tabOrderFrom([main, a, b, c, z], 'ada') }
    expect(ids(agentChatTabs([main, a, b, c, z], 'ada', { activeId: 'z', order: snap }))).toEqual(['m', 'a', 'z', 'b', 'c'])
  })

  it("ignores another agent's order and an untaken one", () => {
    const cNew = { ...c, last_message_at: '2026-10-10T00:00:00Z' }
    expect(ids(agentChatTabs([main, a, b, cNew], 'ada', { order: { agent: 'bob', ids: ['m', 'a', 'b', 'c'] } }))).toEqual(['m', 'c', 'a', 'b'])
    expect(ids(agentChatTabs([main, a, b, cNew], 'ada', { order: { agent: 'ada', ids: null } }))).toEqual(['m', 'c', 'a', 'b'])
  })

  it('two Mains stay in a stable order', () => {
    const m2 = row('m2', { is_main: true, created_at: '2026-10-12T00:00:00Z' })
    const first = ids(agentChatTabs([main, m2, a], 'ada', { order }))
    const second = ids(agentChatTabs([m2, a, main], 'ada', { order }))
    expect(first).toEqual(second)
    expect(first.slice(0, 2).sort()).toEqual(['m', 'm2'])
  })
})
