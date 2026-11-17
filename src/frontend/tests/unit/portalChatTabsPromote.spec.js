/**
 * A chat that gets a fresh unread reply while you are on the agent comes to the
 * front of the strip (right after Main), so a reply in a chat parked under
 * "N more" is never invisible. The strip otherwise still holds still
 * (trinity-enterprise#856), and the chat you have open keeps its exact slot —
 * the tabs are fixed-width, so the same index is the same place on screen and a
 * promotion can never push the open chat into the overflow menu.
 */
import { describe, it, expect } from 'vitest'
import { agentChatTabs, promoteChatInOrder, unreadSnapshot, newlyUnreadIds, NEW_CHAT_TAB_ID } from '../../src/components/portal/portalUtils.js'

const row = (id, extra = {}) => ({ id, agent_name: 'ada', created_at: '2026-10-01T00:00:00Z', ...extra })
const main = row('m', { is_main: true })
const [a, b, c, d, e] = ['a', 'b', 'c', 'd', 'e'].map((id) => row(id))
const all = [main, a, b, c, d, e]
const order = { agent: 'ada', ids: ['m', 'a', 'b', 'c', 'd', 'e'] }
const strip = (threads, o, opts = {}) => agentChatTabs(threads, 'ada', { order: o, ...opts }).map((t) => t.id)

describe('promoteChatInOrder', () => {
  it('moves the chat to the slot right after Main', () => {
    const next = promoteChatInOrder(order, all, 'e', null)
    expect(strip(all, next)).toEqual(['m', 'e', 'a', 'b', 'c', 'd'])
  })

  it('keeps the open chat at its exact slot (index 3 before and after)', () => {
    const next = promoteChatInOrder(order, all, 'e', 'c')
    const s = strip(all, next, { activeId: 'c' })
    expect(s.indexOf('c')).toBe(3)
    expect(s).toEqual(['m', 'e', 'a', 'c', 'b', 'd'])
  })

  it('lands right after the open chat when the open chat is next to Main', () => {
    const next = promoteChatInOrder(order, all, 'e', 'a')
    expect(strip(all, next, { activeId: 'a' })).toEqual(['m', 'a', 'e', 'b', 'c', 'd'])
  })

  it('an open chat that is already in the overflow stays where it is', () => {
    const next = promoteChatInOrder(order, all, 'd', 'e')
    expect(strip(all, next, { activeId: 'e' })).toEqual(['m', 'd', 'a', 'b', 'c', 'e'])
  })

  it('never promotes the open chat itself or Main', () => {
    expect(promoteChatInOrder(order, all, 'c', 'c')).toBe(order)
    expect(promoteChatInOrder(order, all, 'm', null)).toBe(order)
  })

  it('leaves the provisional New chat tab in its slot after Main', () => {
    const next = promoteChatInOrder(order, all, 'e', null)
    expect(strip(all, next, { activeId: null, draft: true })).toEqual(['m', NEW_CHAT_TAB_ID, 'e', 'a', 'b', 'c', 'd'])
  })

  it('keeps an archived chat at its own slot so Undo still returns it there', () => {
    const bArch = { ...b, archived_at: '2026-10-13T00:00:00Z' }
    const next = promoteChatInOrder(order, [main, a, bArch, c, d, e], 'e', null)
    expect(strip([main, a, bArch, c, d, e], next)).toEqual(['m', 'e', 'a', 'c', 'd'])
    expect(strip(all, next)).toEqual(['m', 'e', 'a', 'b', 'c', 'd'])
  })

  it('covers a chat born mid-visit that is not in the snapshot', () => {
    const born = row('n', { created_at: '2026-10-12T00:00:00Z' })
    const threads = [...all, born]
    const next = promoteChatInOrder(order, threads, 'e', null)
    expect(strip(threads, next)).toEqual(['m', 'e', 'n', 'a', 'b', 'c', 'd'])
  })

  it('is a no-op for an untaken order, another agent, or an unknown chat', () => {
    const untaken = { agent: 'ada', ids: null }
    expect(promoteChatInOrder(untaken, all, 'e', null)).toBe(untaken)
    expect(promoteChatInOrder(null, all, 'e', null)).toBe(null)
    expect(promoteChatInOrder(order, all, 'zzz', null)).toBe(order)
  })
})

describe('newlyUnreadIds — only the read → unread transition promotes', () => {
  it('reports a chat whose unread went from 0 to more, not a second reply', () => {
    const seen = unreadSnapshot([main, a, { ...b, unread: 1 }], 'ada')
    const now = [main, { ...a, unread: 2 }, { ...b, unread: 3 }]
    expect(newlyUnreadIds(seen, now, 'ada', null)).toEqual(['a'])
  })

  it('ignores the open chat, rooms, other agents, and chats it has not seen yet', () => {
    const seen = unreadSnapshot([main, a, b], 'ada')
    const now = [main, { ...a, unread: 1 }, { ...b, unread: 1 }, row('new', { unread: 1 }),
      { ...row('o'), agent_name: 'bob', unread: 1 }, row('r', { is_room: true, unread: 1 })]
    expect(newlyUnreadIds(seen, now, 'ada', 'a')).toEqual(['b'])
  })
})

describe('promoteChatInOrder — a promotion that moves nothing', () => {
  it('returns the same snapshot object when the chat is already in front', () => {
    expect(promoteChatInOrder(order, all, 'a', null)).toBe(order)
    expect(promoteChatInOrder(order, all, 'b', 'a')).toBe(order)
  })
})
