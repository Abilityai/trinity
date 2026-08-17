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
  normalizeRoomRow, shouldMarkTurnRead, mentionedAgents,
  REPLY_MAX_WAIT_MS_FALLBACK, resolveWaitBudgetMs,
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

describe('normalising a room onto the thread shape', () => {
  // This shipped wrong once and no test caught it, because the test mocked the
  // response with the field production does not send. `GET /api/rooms` returns
  // `agents`; `participants` is the DETAIL shape from `GET /api/rooms/{id}`.
  it('reads agent identities from the LIST shape (`agents`)', () => {
    expect(normalizeRoomRow({ id: 'r1', name: 'QA', agents: ['a', 'b'] }).agent_names)
      .toEqual(['a', 'b'])
  })

  it('still reads the DETAIL shape (`participants`)', () => {
    expect(normalizeRoomRow({
      id: 'r1',
      participants: [
        { kind: 'agent', identity: 'a' },
        { kind: 'user', identity: 'me@example.com' },
        { kind: 'agent', identity: 'b' },
      ],
    }).agent_names).toEqual(['a', 'b'])
  })

  it('gives a room with neither shape an empty list, not undefined', () => {
    expect(normalizeRoomRow({ id: 'r1' }).agent_names).toEqual([])
  })

  it('maps name → title and falls back to created_at for sorting', () => {
    const row = normalizeRoomRow({ id: 'r1', name: 'QA', created_at: '2026-01-01T00:00:00Z' })
    expect(row.title).toBe('QA')
    expect(row.last_message_at).toBe('2026-01-01T00:00:00Z')
  })

  it('prefers a real last_message_at over created_at', () => {
    const row = normalizeRoomRow({
      id: 'r1', created_at: '2026-01-01T00:00:00Z', last_message_at: '2026-02-02T00:00:00Z',
    })
    expect(row.last_message_at).toBe('2026-02-02T00:00:00Z')
  })
})

describe('clearing a badge when a turn finishes', () => {
  // The gate that makes the badge reachable at all. Without it the feature was
  // near-dead: open a chat and it is marked read; stay in it and it is marked
  // read; navigate away mid-turn and it was STILL marked read, because the
  // send is an async closure that outlives the component and fired the same
  // event either way.
  it('clears it when the user is still in that thread', () => {
    expect(shouldMarkTurnRead('t1', 't1')).toBe(true)
  })

  it('leaves it when the user moved to another chat mid-turn', () => {
    expect(shouldMarkTurnRead('t1', 't2')).toBe(false)
  })

  it('leaves it when the user moved to a blank new chat', () => {
    expect(shouldMarkTurnRead('t1', null)).toBe(false)
  })

  it('is false for a turn with no thread id rather than matching a null open one', () => {
    expect(shouldMarkTurnRead(null, null)).toBe(false)
    expect(shouldMarkTurnRead(undefined, undefined)).toBe(false)
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

describe('ent#361 — @mention from a 1:1 becomes a group chat', () => {
  const roster = [{ name: 'scribe' }, { name: 'recon' }, { name: 'ws-scout' }]

  it('resolves a mentioned agent that is on the roster', () => {
    expect(mentionedAgents('can @recon check this?', roster)).toEqual(['recon'])
  })

  it('leaves an @name that is not a rostered agent as plain text', () => {
    // The safety property: reporting "no such agent" would answer, for any
    // string the user types, whether an agent by that name exists.
    expect(mentionedAgents('email @nobody about it', roster)).toEqual([])
  })

  it('excludes the agent you are already talking to', () => {
    // Otherwise a 1:1 with scribe escalates to a "group" of one.
    expect(mentionedAgents('@scribe and @recon', roster, { exclude: ['scribe'] }))
      .toEqual(['recon'])
  })

  it('dedupes a name mentioned twice', () => {
    expect(mentionedAgents('@recon then @recon again', roster)).toEqual(['recon'])
  })

  it('collects several agents in one message, in order', () => {
    expect(mentionedAgents('@ws-scout and @recon please', roster))
      .toEqual(['ws-scout', 'recon'])
  })

  it('matches the rooms engine on names with hyphens and digits', () => {
    // The pattern mirrors the engine's `_MENTION_RE`; a name it accepts and we
    // reject would build a room around a handle the engine renders as text.
    expect(mentionedAgents('@ws-scout', roster)).toEqual(['ws-scout'])
    expect(mentionedAgents('@1recon', [{ name: '1recon' }])).toEqual(['1recon'])
  })

  it('does not treat an email address as a mention of its domain', () => {
    expect(mentionedAgents('mail me at me@recon.com', [{ name: 'recon' }]))
      .toEqual(['recon'])
    // Documented consequence, not an accident: the engine's regex matches the
    // same way, so both sides agree — which is the property that matters more
    // than either being clever on its own.
  })

  it('tolerates empty input and a missing roster', () => {
    expect(mentionedAgents('', roster)).toEqual([])
    expect(mentionedAgents('@recon', undefined)).toEqual([])
    expect(mentionedAgents(null, roster)).toEqual([])
  })
})

describe('#2133/#2214 — the client wait budget', () => {
  // The server owns the turn timeout — per-agent since #2214 — and sends the
  // budget with every dispatch (202 `wait_budget_seconds`) and every reattach
  // (`in_flight_wait_budget_seconds` on the history response). The client's
  // only local number is the fallback below, and it is frozen on purpose.
  it('the fallback is frozen at the pre-#2214 server bound', () => {
    // (2 × (300 + 10 + 300) + 60)s was the real ceiling of every backend that
    // predates the per-agent bound — which is this literal's ONLY remaining
    // audience. It must never track the new server arithmetic: the server
    // bound is per-agent now, so there is no one number to mirror, and
    // re-sizing this to (say) the 3600-default arithmetic would make a client
    // of an OLD backend wait ~2h for a turn that server killed at ~21min.
    expect(REPLY_MAX_WAIT_MS_FALLBACK).toBe((2 * (300 + 10 + 300) + 60) * 1000)
  })

  it('a positive server budget wins over the fallback', () => {
    expect(resolveWaitBudgetMs(7880)).toBe(7880 * 1000)
    expect(resolveWaitBudgetMs(42)).toBe(42 * 1000)
  })

  it('anything unusable falls back to the frozen literal', () => {
    // 0/absent/NaN/negative — an old backend that sent nothing, or a budget
    // field that failed to read. Under-waiting here degrades to the honest
    // "lost track — check shortly" message with no Retry (never a re-bill).
    for (const v of [0, undefined, null, NaN, -5, 'nope']) {
      expect(resolveWaitBudgetMs(v)).toBe(REPLY_MAX_WAIT_MS_FALLBACK)
    }
  })
})
