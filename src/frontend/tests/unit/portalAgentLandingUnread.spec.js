/**
 * trinity-enterprise#856 — landing arm 0 (2026-10-13 ruling): opening an agent
 * lands on its most recent chat with an unread answer; then arm 1, the chat
 * last open this session; then the fresh-session arms (ent#784). And
 * `forgetLastOpen` drops a chat that was closed or deleted.
 */
import { describe, it, expect } from 'vitest'
import { agentLanding, agentUnreadLanding } from '../../src/components/portal/portalUtils.js'
import { forgetLastOpen, recordLastOpen } from '../../src/components/portal/portalKeymap.js'

const row = (id, extra = {}) => ({ id, agent_name: 'ada', created_at: '2026-10-01T00:00:00Z', last_message_at: '2026-10-02T00:00:00Z', message_count: 2, ...extra })

describe('agentLanding — arm 0 (unread)', () => {
  const main = row('m', { is_main: true })
  const a = row('a', { unread: 1, last_message_at: '2026-10-05T00:00:00Z' })
  const b = row('b', { unread: 3, last_message_at: '2026-10-07T00:00:00Z' })
  const last = row('l')

  it('beats the last-open chat and the drafts arm', () => {
    const drafts = { 'thread:l': { text: 'hi', updatedAt: 9 } }
    expect(agentLanding({ agentName: 'ada', threads: [main, a, b, last], lastOpenSessionId: 'l', drafts }))
      .toEqual({ agentName: 'ada', sessionId: 'b' })
  })

  it('picks the most recent unread by last_message_at, Main included', () => {
    const m = { ...main, unread: 1, last_message_at: '2026-10-09T00:00:00Z' }
    expect(agentUnreadLanding([a, b, m], 'ada')).toBe('m')
  })

  it('skips archived chats, rooms and other agents', () => {
    const arch = row('z', { unread: 5, archived_at: '2026-10-08T00:00:00Z', last_message_at: '2026-10-09T00:00:00Z' })
    const room = row('r', { unread: 5, is_room: true, last_message_at: '2026-10-09T00:00:00Z' })
    const bob = row('o', { unread: 5, agent_name: 'bob', last_message_at: '2026-10-09T00:00:00Z' })
    expect(agentUnreadLanding([arch, room, bob, a], 'ada')).toBe('a')
    expect(agentUnreadLanding([arch, room, bob], 'ada')).toBe(null)
  })
})

describe('agentLanding — arm 1 and the fresh-session arms', () => {
  it('honours the chat last open this session when nothing is unread', () => {
    expect(agentLanding({ agentName: 'ada', threads: [row('m', { is_main: true }), row('l')], lastOpenSessionId: 'l' }))
      .toEqual({ agentName: 'ada', sessionId: 'l' })
  })

  it('an archived or foreign last-open id falls through to the fresh arms', () => {
    const arch = row('l', { archived_at: '2026-10-08T00:00:00Z' })
    expect(agentLanding({ agentName: 'ada', threads: [arch], lastOpenSessionId: 'l' })).toEqual({ agentName: 'ada', sessionId: null })
    expect(agentLanding({ agentName: 'ada', threads: [row('o', { agent_name: 'bob' })], lastOpenSessionId: 'o' })).toEqual({ agentName: 'ada', sessionId: null })
  })

  it('a fresh session with no unread reuses the empty chat (ent#784 unchanged)', () => {
    const empty = row('e', { last_message_at: null, message_count: 0 })
    expect(agentLanding({ agentName: 'ada', threads: [row('m', { is_main: true }), empty] })).toEqual({ agentName: 'ada', sessionId: 'e' })
  })
})

describe('forgetLastOpen (AC 8)', () => {
  it('removes only the entry naming that chat', () => {
    const map = new Map()
    recordLastOpen(map, { agentName: 'ada', sessionId: 'l' })
    recordLastOpen(map, { agentName: 'bob', sessionId: 'k' })
    expect(forgetLastOpen(map, 'l')).toBe(true)
    expect(map.has('ada')).toBe(false)
    expect(map.get('bob')).toBe('k')
    expect(forgetLastOpen(map, 'nope')).toBe(false)
    expect(map.get('bob')).toBe('k')
  })

  it('is safe on a missing map or id', () => {
    expect(forgetLastOpen(null, 'l')).toBe(false)
    expect(forgetLastOpen(new Map(), null)).toBe(false)
  })
})
