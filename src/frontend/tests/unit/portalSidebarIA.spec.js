/**
 * Workspace sidebar IA — starred chats, per-agent badges, participant avatars
 * (ent#359).
 *
 * These are the pure functions behind the restructured sidebar, tested without
 * mounting it. The behaviours worth pinning are the ones where the "obvious"
 * implementation is subtly wrong:
 *
 *   * starred chats are LIFTED OUT of the date groups, not copied above them
 *     (copying makes the list lie about how many conversations exist), and
 *   * a room contributes its unread to EVERY agent in it, because there is no
 *     single agent the conversation is "with".
 */
import { describe, it, expect } from 'vitest'
import {
  partitionStarred, unreadByAgent, totalUnread, rowAgents, groupThreadsByDate,
} from '../../src/components/portal/portalUtils'

const iso = (d) => new Date(d).toISOString()
const today = iso(Date.now())
const longAgo = iso(Date.now() - 30 * 86400000)

describe('starred chats', () => {
  it('lifts a starred chat out of its date group so it appears exactly once', () => {
    const threads = [
      { id: 'a', starred: true, last_message_at: today },
      { id: 'b', starred: false, last_message_at: today },
    ]
    const { starred, rest } = partitionStarred(threads)
    const grouped = groupThreadsByDate(rest)
    const idsInGroups = grouped.flatMap((g) => g.threads.map((t) => t.id))

    expect(starred.map((t) => t.id)).toEqual(['a'])
    expect(idsInGroups).toEqual(['b'])
    // The whole point: 'a' is in the starred section and NOWHERE else.
    expect(idsInGroups).not.toContain('a')
  })

  it('keeps starred chats out of every group regardless of age', () => {
    const { rest } = partitionStarred([{ id: 'old', starred: true, last_message_at: longAgo }])
    expect(groupThreadsByDate(rest)).toEqual([])
  })

  it('tolerates a missing/!array thread list rather than throwing at render', () => {
    expect(partitionStarred(undefined)).toEqual({ starred: [], rest: [] })
  })
})

describe('per-agent "waiting on you" counts', () => {
  it('sums a thread’s unread onto its agent', () => {
    expect(unreadByAgent([
      { agent_name: 'scribe', unread: 2 },
      { agent_name: 'scribe', unread: 1 },
      { agent_name: 'recon', unread: 3 },
    ])).toEqual({ scribe: 3, recon: 3 })
  })

  it('credits a room’s unread to every agent in it', () => {
    // If three agents share a room you are behind on, all three rows should say
    // so — there is no one agent the room is "with".
    expect(unreadByAgent([
      { is_room: true, agent_names: ['scribe', 'recon'], unread: 2 },
    ])).toEqual({ scribe: 2, recon: 2 })
  })

  it('ignores chats with nothing unread', () => {
    expect(unreadByAgent([
      { agent_name: 'scribe', unread: 0 },
      { agent_name: 'recon' },
    ])).toEqual({})
  })

  it('totals across every chat for the wordmark badge', () => {
    expect(totalUnread([
      { unread: 2 }, { unread: 0 }, { unread: 5 }, {},
    ])).toBe(7)
  })
})

describe('row avatars', () => {
  it('shows one avatar for a 1:1', () => {
    expect(rowAgents({ agent_name: 'scribe' })).toEqual({ shown: ['scribe'], overflow: 0 })
  })

  it('shows every participant of a small room', () => {
    expect(rowAgents({ agent_names: ['a', 'b'] })).toEqual({ shown: ['a', 'b'], overflow: 0 })
  })

  it('caps a crowded room and reports the remainder', () => {
    // A room with eight agents must not push the title out of the row.
    expect(rowAgents({ agent_names: ['a', 'b', 'c', 'd', 'e'] }))
      .toEqual({ shown: ['a', 'b', 'c'], overflow: 2 })
  })

  it('prefers agent_names when both are present (a room carries both)', () => {
    expect(rowAgents({ agent_name: 'ignored', agent_names: ['a', 'b'] }).shown)
      .toEqual(['a', 'b'])
  })

  it('returns nothing rather than [undefined] for a chat with no agent', () => {
    expect(rowAgents({})).toEqual({ shown: [], overflow: 0 })
  })
})
